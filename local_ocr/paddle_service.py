from __future__ import annotations

import argparse
import fcntl
import importlib.metadata
import json
import multiprocessing as mp
import os
import queue
import signal
import socketserver
import stat
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .config import (
    PADDLE_CONTENT_KEYS,
    PADDLEOCR_PACKAGE_VERSION,
    PADDLEPADDLE_PACKAGE_VERSION,
    PADDLEX_PACKAGE_VERSION,
    validate_paddle_settings,
)
from .protocol import receive_message, send_message
from .reading_order import TextLine, order_text_lines
from .runtime_paths import ensure_private_directory, expand_runtime_path


# Layout metadata is an audit aid, not part of the OCR text/cache identity.
# Keep the response bounded even if a detector produces pathological output.
# These caps also keep the worst-case UTF-8 response comfortably below the
# local protocol's 16 MiB message limit.
MAX_LAYOUT_METADATA_LINES = 2048
MAX_LAYOUT_METADATA_TEXT_CHARS = 512


def _acquire_runtime_lock(
    path: Path,
    *,
    metadata: Mapping[str, Any],
    busy_message: str,
) -> Any:
    """Acquire a kernel-owned lock without following a pre-created symlink."""

    ensure_private_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"unable to open local OCR lock {path}: {exc}") from exc
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError(f"local OCR lock is not a regular file: {path}")
        if hasattr(os, "geteuid") and status.st_uid != os.geteuid():
            raise RuntimeError(f"local OCR lock is not owned by the current user: {path}")
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(busy_message) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(dict(metadata), ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return handle
    except BaseException:
        handle.close()
        raise


def _release_runtime_lock(handle: Any) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def _plain_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _find_recognition_payload(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        if "rec_texts" in value:
            return value
        for item in value.values():
            found = _find_recognition_payload(item)
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _find_recognition_payload(item)
            if found is not None:
                return found
    return None


def _result_json(value: Any) -> Mapping[str, Any]:
    payload = getattr(value, "json", value)
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise RuntimeError("PaddleOCR returned an unsupported result object")
    found = _find_recognition_payload(payload)
    if found is None:
        raise RuntimeError("PaddleOCR result does not contain rec_texts")
    return found


def _polygon(value: Any, fallback_index: int) -> tuple[tuple[float, float], ...]:
    try:
        points = tuple((float(point[0]), float(point[1])) for point in value)
        if len(points) >= 2:
            return points
    except (TypeError, ValueError, IndexError):
        pass
    top = float(fallback_index * 10)
    return ((0.0, top), (1.0, top), (1.0, top + 1.0), (0.0, top + 1.0))


def _extract_page(
    prediction: Any,
    *,
    reading_direction: str,
    horizontal_columns: int,
) -> tuple[str, dict[str, Any]]:
    payload = _result_json(prediction)
    raw_texts = payload.get("rec_texts")
    raw_scores = payload.get("rec_scores")
    raw_polygons = payload.get("rec_polys")
    if raw_polygons is None or len(raw_polygons) == 0:
        raw_polygons = payload.get("dt_polys")
    texts = list(raw_texts) if raw_texts is not None else []
    scores = list(raw_scores) if raw_scores is not None else []
    polygons = list(raw_polygons) if raw_polygons is not None else []
    if not texts:
        if polygons:
            raise RuntimeError(
                "PaddleOCR detected text regions but returned no recognized text"
            )
        return "[空白页]", {
            "line_count": 0,
            "mean_score": 0.0,
            "minimum_score": 0.0,
            "reading_direction": reading_direction,
            "horizontal_columns": horizontal_columns,
            "blank_page": True,
            "layout_line_count": 0,
            "layout_lines_truncated": False,
            "layout_lines": [],
        }
    lines: list[TextLine] = []
    for index, value in enumerate(texts):
        text = str(value).strip()
        if not text:
            continue
        try:
            score = float(scores[index]) if index < len(scores) else 0.0
        except (TypeError, ValueError):
            score = 0.0
        polygon = _polygon(polygons[index] if index < len(polygons) else None, index)
        lines.append(TextLine(text=text, score=score, polygon=polygon))
    ordered = order_text_lines(
        lines,
        reading_direction=reading_direction,
        horizontal_columns=horizontal_columns,
    )
    text = "\n".join(line.text for line in ordered).strip()
    if not text:
        raise RuntimeError("PaddleOCR recognized only empty text regions")
    line_scores = [line.score for line in ordered]
    metadata = {
        "line_count": len(ordered),
        "mean_score": sum(line_scores) / len(line_scores) if line_scores else 0.0,
        "minimum_score": min(line_scores) if line_scores else 0.0,
        "reading_direction": reading_direction,
        "horizontal_columns": horizontal_columns,
        "layout_line_count": len(ordered),
        "layout_lines_truncated": len(ordered) > MAX_LAYOUT_METADATA_LINES,
        "layout_lines": [
            {
                "text": line.text[:MAX_LAYOUT_METADATA_TEXT_CHARS],
                "text_truncated": len(line.text) > MAX_LAYOUT_METADATA_TEXT_CHARS,
                "score": line.score,
                "bbox": [line.left, line.top, line.right, line.bottom],
                "polygon": [[x, y] for x, y in line.polygon],
            }
            for line in ordered[:MAX_LAYOUT_METADATA_LINES]
        ],
    }
    return text, metadata


def _pipeline_kwargs(content: Mapping[str, Any], runtime: Mapping[str, Any], device: str) -> dict[str, Any]:
    validate_paddle_settings(content, runtime)
    names = {
        "text_detection_model": "text_detection_model_name",
        "text_recognition_model": "text_recognition_model_name",
    }
    allowed = PADDLE_CONTENT_KEYS - {
        "horizontal_columns",
        "reading_order_version",
        "text_detection_model",
        "text_recognition_model",
    }
    kwargs: dict[str, Any] = {"device": device}
    for key, value in content.items():
        normalized = names.get(key, key)
        if normalized in allowed:
            kwargs[normalized] = value
    if "text_recognition_batch_size" in runtime:
        kwargs["text_recognition_batch_size"] = int(
            runtime["text_recognition_batch_size"]
        )
    return kwargs


def _worker_main(
    task_queue: Any,
    result_queue: Any,
    *,
    worker_name: str,
    device: str,
    content: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> None:
    try:
        import paddle
        from paddleocr import PaddleOCR

        actual_paddleocr = importlib.metadata.version("paddleocr")
        actual_paddlex = importlib.metadata.version("paddlex")
        if actual_paddleocr != PADDLEOCR_PACKAGE_VERSION:
            raise RuntimeError(
                f"paddleocr=={PADDLEOCR_PACKAGE_VERSION} is required, got {actual_paddleocr}"
            )
        if paddle.__version__ != PADDLEPADDLE_PACKAGE_VERSION:
            raise RuntimeError(
                f"paddlepaddle=={PADDLEPADDLE_PACKAGE_VERSION} is required, "
                f"got {paddle.__version__}"
            )
        if actual_paddlex != PADDLEX_PACKAGE_VERSION:
            raise RuntimeError(
                f"paddlex=={PADDLEX_PACKAGE_VERSION} is required, got {actual_paddlex}"
            )

        pipeline = PaddleOCR(**_pipeline_kwargs(content, runtime, device))
        result_queue.put(
            {"kind": "startup", "worker": worker_name, "ok": True, "device": device}
        )
    except BaseException as exc:
        result_queue.put(
            {
                "kind": "startup",
                "worker": worker_name,
                "ok": False,
                "device": device,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return

    while True:
        task = task_queue.get()
        if task is None:
            return
        request_id = str(task.get("request_id") or "")
        deadline = float(task.get("deadline_monotonic") or 0.0)
        if deadline and time.monotonic() >= deadline:
            result_queue.put(
                {
                    "kind": "result",
                    "request_id": request_id,
                    "ok": False,
                    "error": "TimeoutError: request expired before inference",
                }
            )
            continue
        started = time.monotonic()
        try:
            predictions = list(pipeline.predict(str(task["image_path"])))
            if len(predictions) != 1:
                raise RuntimeError(
                    f"PaddleOCR returned {len(predictions)} pages for one image"
                )
            text, metadata = _extract_page(
                predictions[0],
                reading_direction=str(task.get("reading_direction") or "horizontal"),
                horizontal_columns=max(1, int(task.get("horizontal_columns") or 1)),
            )
            metadata.update(
                {
                    "worker": worker_name,
                    "device": device,
                    "elapsed_seconds": time.monotonic() - started,
                }
            )
            result_queue.put(
                {
                    "kind": "result",
                    "request_id": request_id,
                    "ok": True,
                    "text": text,
                    "metadata": metadata,
                }
            )
        except BaseException as exc:
            result_queue.put(
                {
                    "kind": "result",
                    "request_id": request_id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=8),
                }
            )


@dataclass(frozen=True)
class ServiceConfig:
    socket_path: Path
    model_identity: str
    content: Mapping[str, Any]
    runtime: Mapping[str, Any]

    @property
    def devices(self) -> tuple[str, ...]:
        raw = self.runtime.get("devices", ("gpu:0", "gpu:1"))
        if isinstance(raw, str):
            raw = (raw,)
        devices = tuple(str(value) for value in raw)
        if not devices:
            raise ValueError("local PaddleOCR service requires at least one device")
        return devices

    @property
    def instances_per_device(self) -> int:
        return max(1, int(self.runtime.get("instances_per_device", 1)))


class PaddleWorkerPool:
    def __init__(self, config: ServiceConfig) -> None:
        context = mp.get_context("spawn")
        queue_depth = max(1, int(config.runtime.get("queue_depth", 64)))
        self.task_queue = context.Queue(maxsize=queue_depth)
        self.result_queue = context.Queue()
        self.processes: list[mp.Process] = []
        self.process_by_name: dict[str, mp.Process] = {}
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.pending_lock = threading.Lock()
        self.startup: dict[str, dict[str, Any]] = {}
        self.stopped = threading.Event()
        self.gpu_lock_handles = self._acquire_gpu_locks(config.devices)
        self.collector = threading.Thread(
            target=self._collect,
            name="PaddleOCR-result-collector",
            daemon=True,
        )
        self.collector.start()
        specs = [
            (f"{device.replace(':', '-')}-{instance}", device)
            for device in config.devices
            for instance in range(config.instances_per_device)
        ]
        startup_timeout = float(
            config.runtime.get(
                "worker_startup_timeout",
                config.runtime.get("startup_timeout", 600),
            )
        )
        startup_deadline = time.monotonic() + startup_timeout
        try:
            # Let one worker download/validate the models before the other
            # replicas open the same cache.  Remaining replicas then warm in
            # parallel from local files.
            first_name, first_device = specs[0]
            self._start_worker(context, config, first_name, first_device)
            self._wait_for_startup({first_name}, startup_deadline)
            remaining_names: set[str] = set()
            for name, device in specs[1:]:
                self._start_worker(context, config, name, device)
                remaining_names.add(name)
            self._wait_for_startup(remaining_names, startup_deadline)
        except BaseException:
            self.close()
            raise

    def _start_worker(
        self,
        context: Any,
        config: ServiceConfig,
        name: str,
        device: str,
    ) -> None:
        process = context.Process(
            target=_worker_main,
            kwargs={
                "task_queue": self.task_queue,
                "result_queue": self.result_queue,
                "worker_name": name,
                "device": device,
                "content": dict(config.content),
                "runtime": dict(config.runtime),
            },
            name=f"PaddleOCR-{name}",
            daemon=True,
        )
        process.start()
        self.processes.append(process)
        self.process_by_name[name] = process

    def _wait_for_startup(self, names: set[str], deadline: float) -> None:
        if not names:
            return
        while time.monotonic() < deadline:
            completed = {name for name in names if name in self.startup}
            failures = [
                self.startup[name]
                for name in completed
                if not self.startup[name].get("ok")
            ]
            if failures:
                raise RuntimeError(f"PaddleOCR workers failed to start: {failures}")
            if completed == names:
                return
            dead = [
                name
                for name in names - completed
                if name in self.process_by_name
                and not self.process_by_name[name].is_alive()
            ]
            if dead:
                # Give the collector one final chance to consume a startup
                # error enqueued immediately before worker exit.
                time.sleep(0.1)
                failures = [
                    self.startup[name]
                    for name in dead
                    if name in self.startup and not self.startup[name].get("ok")
                ]
                if failures:
                    raise RuntimeError(
                        f"PaddleOCR workers failed to start: {failures}"
                    )
                details = [
                    (name, self.process_by_name[name].exitcode) for name in dead
                ]
                raise RuntimeError(
                    f"PaddleOCR workers exited before startup: {details}"
                )
            time.sleep(0.1)
        missing = sorted(names - set(self.startup))
        raise TimeoutError(f"PaddleOCR worker startup timed out: {missing}")

    @staticmethod
    def _acquire_gpu_locks(devices: tuple[str, ...]) -> list[Any]:
        handles: list[Any] = []
        try:
            runtime_root = ensure_private_directory(
                "/tmp/translation-agent-paddleocr-{uid}"
            )
            lock_directory = ensure_private_directory(runtime_root / "locks")
            for device in sorted(set(devices)):
                safe = "".join(character if character.isalnum() else "-" for character in device)
                path = lock_directory / f"gpu-{safe}.lock"
                handle = _acquire_runtime_lock(
                    path,
                    metadata={"pid": os.getpid(), "device": device},
                    busy_message=f"PaddleOCR device {device} is already reserved",
                )
                handles.append(handle)
            return handles
        except BaseException:
            for handle in handles:
                _release_runtime_lock(handle)
            raise

    def _collect(self) -> None:
        while not self.stopped.is_set():
            try:
                result = self.result_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if result.get("kind") == "startup":
                self.startup[str(result.get("worker"))] = result
                continue
            request_id = str(result.get("request_id") or "")
            with self.pending_lock:
                destination = self.pending.get(request_id)
            if destination is not None:
                destination.put(result)

    def health(self) -> dict[str, Any]:
        expected = len(self.processes)
        alive = sum(process.is_alive() for process in self.processes)
        failures = [value for value in self.startup.values() if not value.get("ok")]
        return {
            "ready": len(self.startup) == expected and alive == expected and not failures,
            "worker_count": expected,
            "alive_worker_count": alive,
            "started_worker_count": len(self.startup),
            "startup_failures": failures,
        }

    def submit(self, payload: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4().hex)
        destination: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.pending_lock:
            if request_id in self.pending:
                raise ValueError(f"duplicate OCR request id {request_id!r}")
            self.pending[request_id] = destination
        try:
            task = dict(payload)
            task["request_id"] = request_id
            deadline = time.monotonic() + timeout
            task["deadline_monotonic"] = deadline
            self.task_queue.put(task, timeout=max(0.001, deadline - time.monotonic()))
            return destination.get(timeout=max(0.001, deadline - time.monotonic()))
        except (queue.Empty, queue.Full) as exc:
            raise TimeoutError(f"PaddleOCR request {request_id} timed out") from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def close(self) -> None:
        self.stopped.set()
        deadline = time.monotonic() + 15.0
        for _ in self.processes:
            try:
                self.task_queue.put(None, timeout=max(0.001, deadline - time.monotonic()))
            except queue.Full:
                break
        for process in self.processes:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
        for process in self.processes:
            if process.is_alive():
                process.terminate()
        for process in self.processes:
            process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
        self.collector.join(timeout=1)
        for handle in self.gpu_lock_handles:
            _release_runtime_lock(handle)
        self.gpu_lock_handles = []


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, config: ServiceConfig, pool: PaddleWorkerPool) -> None:
        self.config = config
        self.pool = pool
        self.stop_requested = threading.Event()
        super().__init__(str(config.socket_path), _RequestHandler)

    def service_actions(self) -> None:
        health = self.pool.health()
        if (
            not self.stop_requested.is_set()
            and health["alive_worker_count"] < health["worker_count"]
        ):
            self.stop_requested.set()
            threading.Thread(target=self.shutdown, daemon=True).start()


class _RequestHandler(socketserver.BaseRequestHandler):
    server: _ThreadingUnixServer

    def handle(self) -> None:
        try:
            payload = receive_message(self.request)
            op = str(payload.get("op") or "")
            if op == "health":
                response = {
                    "ok": True,
                    "model_identity": self.server.config.model_identity,
                    **self.server.pool.health(),
                }
            elif op == "ocr":
                image_path = Path(str(payload.get("image_path") or "")).resolve(strict=True)
                if not image_path.is_file():
                    raise FileNotFoundError(image_path)
                timeout = max(1.0, float(payload.get("timeout") or 120.0))
                response = self.server.pool.submit(
                    {
                        "request_id": payload.get("request_id"),
                        "image_path": str(image_path),
                        "reading_direction": payload.get("reading_direction"),
                        "horizontal_columns": payload.get("horizontal_columns"),
                    },
                    timeout,
                )
                response["model_identity"] = self.server.config.model_identity
            elif op == "shutdown":
                response = {"ok": True}
                self.server.stop_requested.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                raise ValueError(f"unknown local OCR operation {op!r}")
        except BaseException as exc:
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        response.setdefault("model_identity", self.server.config.model_identity)
        send_message(self.request, response)


def _safe_remove_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(f"refusing to replace non-socket path: {path}")
    path.unlink()


def serve(config: ServiceConfig) -> int:
    ensure_private_directory(config.socket_path.parent)
    socket_lock = _acquire_runtime_lock(
        config.socket_path.with_name(f".{config.socket_path.name}.service.lock"),
        metadata={"pid": os.getpid(), "socket_path": str(config.socket_path)},
        busy_message=f"PaddleOCR socket is already owned: {config.socket_path}",
    )
    pool: PaddleWorkerPool | None = None
    server: _ThreadingUnixServer | None = None
    try:
        pool = PaddleWorkerPool(config)
        # Acquire every requested GPU before touching the public socket.  A
        # competing service must never unlink the live owner's endpoint.
        _safe_remove_socket(config.socket_path)
        server = _ThreadingUnixServer(config, pool)
        os.chmod(config.socket_path, 0o600)
        server.serve_forever(poll_interval=0.25)
        return 0
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            try:
                if pool is not None:
                    pool.close()
            finally:
                try:
                    _safe_remove_socket(config.socket_path)
                finally:
                    _release_runtime_lock(socket_lock)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local multi-GPU PaddleOCR service")
    parser.add_argument("--config", required=True, help="Non-secret JSON service config")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = json.loads(Path(args.config).read_text(encoding="utf-8"))
    config = ServiceConfig(
        socket_path=expand_runtime_path(str(payload["socket_path"])),
        model_identity=str(payload["model_identity"]),
        content=_plain_mapping(payload.get("content")),
        runtime=_plain_mapping(payload.get("runtime")),
    )
    validate_paddle_settings(config.content, config.runtime)
    cache_dir = config.runtime.get("model_cache_dir")
    if cache_dir:
        resolved_cache = Path(str(cache_dir)).expanduser()
        if not resolved_cache.is_absolute():
            resolved_cache = Path.cwd() / resolved_cache
        resolved_cache = resolved_cache.resolve()
        resolved_cache.mkdir(parents=True, exist_ok=True)
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(resolved_cache)
    return serve(config)


if __name__ == "__main__":
    raise SystemExit(main())
