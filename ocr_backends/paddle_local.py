from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from local_ocr.protocol import request
from local_ocr.config import (
    PADDLE_LOCAL_IMPLEMENTATION_VERSION,
    validate_paddle_settings,
)
from local_ocr.runtime_paths import (
    ensure_private_directory,
    expand_runtime_path,
    open_private_text_file,
)
from pipeline_profiles import ModelProfile

from .registry import register_backend


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def paddle_service_identity(
    profile: ModelProfile,
    *,
    reading_direction: str | None = None,
    horizontal_columns: int | None = None,
) -> str:
    """Return the model-content identity used by the persistent GPU service."""

    payload = {
        "implementation_version": PADDLE_LOCAL_IMPLEMENTATION_VERSION,
        "adapter": profile.adapter,
        "provider": profile.provider,
        "model": profile.model,
        "reading_direction": reading_direction or profile.reading_direction or "horizontal",
        "horizontal_columns": max(
            1,
            int(
                horizontal_columns
                if horizontal_columns is not None
                else profile.content.get("horizontal_columns", 1)
            ),
        ),
        "content": _plain(profile.content),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    model = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in profile.model
    ).strip("-") or "model"
    return f"paddleocr-local/{model}/content-v3-{digest}"


def paddle_checkpoint_identity(
    profile: ModelProfile,
    *,
    reading_direction: str | None = None,
    horizontal_columns: int | None = None,
    dpi: int,
    max_image_side: int,
    jpeg_quality: int,
) -> str:
    """Return the page-cache identity, including PDF rendering semantics.

    Rendering happens in the main pipeline rather than in the persistent GPU
    service.  It therefore must invalidate page checkpoints without forcing an
    otherwise identical set of loaded model workers to restart.
    """

    service_identity = paddle_service_identity(
        profile,
        reading_direction=reading_direction,
        horizontal_columns=horizontal_columns,
    )
    payload = {
        "service_identity": service_identity,
        "render": {
            "dpi": int(dpi),
            "max_image_side": int(max_image_side),
            "jpeg_quality": int(jpeg_quality),
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    prefix = service_identity.split("/content-v3-", 1)[0]
    return f"{prefix}/checkpoint-v1-{digest}"


# Backward-compatible name for callers that only need the service/content
# identity.  PageRecord and cache callers must use paddle_checkpoint_identity.
paddle_profile_identity = paddle_service_identity


class PaddleOCRText(str):
    """String-compatible response carrying non-public confidence metadata."""

    ocr_metadata: Mapping[str, Any]

    def __new__(cls, value: str, metadata: Mapping[str, Any]) -> "PaddleOCRText":
        instance = super().__new__(cls, value)
        instance.ocr_metadata = dict(metadata)
        return instance


def _socket_path(profile: ModelProfile) -> Path:
    runtime_value = profile.runtime.get("socket_path")
    if runtime_value:
        return expand_runtime_path(str(runtime_value))
    parsed = urllib.parse.urlsplit(profile.base_url)
    if parsed.scheme != "unix" or not parsed.path:
        raise ValueError(
            f"Profile {profile.name!r} requires base_url='unix:///absolute/path.sock' "
            "or runtime.socket_path"
        )
    return expand_runtime_path(urllib.parse.unquote(parsed.path))


class PaddleLocalOCR:
    """Thread-safe client for the isolated, persistent local PaddleOCR service."""

    jpeg_optimize = False

    def __init__(
        self,
        *,
        socket_path: Path,
        service_identity: str,
        checkpoint_identity: str,
        content: Mapping[str, Any],
        runtime: Mapping[str, Any],
        reading_direction: str,
        horizontal_columns: int | None,
        timeout: float,
    ) -> None:
        self.socket_path = socket_path
        self.service_identity = service_identity
        self.ocr_model = checkpoint_identity
        self.content = dict(_plain(content))
        self.runtime = dict(_plain(runtime))
        self.reading_direction = reading_direction
        self.timeout = max(1.0, float(timeout))
        self.horizontal_columns = max(
            1,
            int(
                horizontal_columns
                if horizontal_columns is not None
                else self.content.get("horizontal_columns", 1)
            ),
        )
        self.spool_dir = ensure_private_directory(
            str(
                self.runtime.get("spool_dir")
                or "/tmp/translation-agent-ocr-{uid}"
            )
        )
        self.auto_start = bool(self.runtime.get("auto_start", True))
        self.persistent = bool(self.runtime.get("persistent", True))
        self.startup_timeout = max(
            self.timeout,
            float(self.runtime.get("startup_timeout", 600)),
        )
        self._process: subprocess.Popen[str] | None = None
        self._started_here = False
        self._start_lock = threading.Lock()
        self._service_config_path: Path | None = None

    @classmethod
    def from_profile(
        cls,
        profile: ModelProfile,
        *,
        reading_direction: str,
        horizontal_columns: int | None = None,
        dpi: int,
        max_image_side: int,
        jpeg_quality: int,
    ) -> "PaddleLocalOCR":
        if profile.adapter != "paddleocr-local":
            raise ValueError(
                f"Profile {profile.name!r} is not a paddleocr-local profile"
            )
        validate_paddle_settings(profile.content, profile.runtime)
        service_identity = paddle_service_identity(
            profile,
            reading_direction=reading_direction,
            horizontal_columns=horizontal_columns,
        )
        return cls(
            socket_path=_socket_path(profile),
            service_identity=service_identity,
            checkpoint_identity=paddle_checkpoint_identity(
                profile,
                reading_direction=reading_direction,
                horizontal_columns=horizontal_columns,
                dpi=dpi,
                max_image_side=max_image_side,
                jpeg_quality=jpeg_quality,
            ),
            content=profile.content,
            runtime=profile.runtime,
            reading_direction=reading_direction,
            horizontal_columns=horizontal_columns,
            timeout=profile.timeout,
        )

    def _request(self, payload: Mapping[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        return request(
            self.socket_path,
            payload,
            timeout=self.timeout if timeout is None else timeout,
        )

    def health(self) -> dict[str, Any]:
        try:
            response = self._request({"op": "health"}, timeout=min(self.timeout, 10.0))
        except (OSError, ConnectionError, TimeoutError, ValueError, socket.timeout):
            return {"ok": False, "ready": False}
        if response.get("model_identity") != self.service_identity:
            return {
                "ok": False,
                "ready": False,
                "error": "model identity mismatch",
                "actual_model_identity": response.get("model_identity"),
            }
        return response

    def _service_command(self, config_path: Path) -> list[str]:
        executable = str(
            self.runtime.get("python_executable") or sys.executable
        )
        return [
            executable,
            "-m",
            "local_ocr.paddle_service",
            "--config",
            str(config_path),
        ]

    def _write_service_config(self) -> Path:
        identity_digest = self.service_identity.rsplit("-", 1)[-1][:16]
        path = self.spool_dir / (
            f"service-{identity_digest}-{os.getpid()}-{uuid.uuid4().hex[:12]}.json"
        )
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        payload = {
            "socket_path": str(self.socket_path),
            "model_identity": self.service_identity,
            "content": self.content,
            "runtime": {
                key: value
                for key, value in self.runtime.items()
                if key != "python_executable"
            },
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        self._service_config_path = path
        return path

    def _cleanup_service_config(self) -> None:
        if self._service_config_path is None:
            return
        try:
            self._service_config_path.unlink()
        except FileNotFoundError:
            pass
        self._service_config_path = None

    def _ensure_service(self) -> None:
        health = self.health()
        if health.get("ready"):
            return
        if health.get("actual_model_identity"):
            raise RuntimeError(
                "A local PaddleOCR service is already using this socket with a "
                "different content identity. Choose another socket or stop it."
            )
        if not self.auto_start:
            raise RuntimeError(
                f"Local PaddleOCR service is not ready at {self.socket_path}; "
                "start local_ocr.paddle_service or enable runtime.auto_start."
            )
        with self._start_lock:
            if self.health().get("ready"):
                return
            startup_lock_path = self.socket_path.with_name(
                f".{self.socket_path.name}.startup.lock"
            )
            ensure_private_directory(startup_lock_path.parent)
            with open_private_text_file(
                startup_lock_path,
                append=True,
                read_write=True,
            ) as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    if self.health().get("ready"):
                        return
                    config_path = self._write_service_config()
                    log_path = self.spool_dir / "paddleocr-service.log"
                    log_handle = open_private_text_file(log_path, append=True)
                    try:
                        self._process = subprocess.Popen(
                            self._service_command(config_path),
                            cwd=str(Path(__file__).resolve().parents[1]),
                            stdin=subprocess.DEVNULL,
                            stdout=log_handle,
                            stderr=subprocess.STDOUT,
                            text=True,
                            start_new_session=True,
                        )
                    finally:
                        log_handle.close()
                    self._started_here = True
                    deadline = time.monotonic() + self.startup_timeout
                    last_health: dict[str, Any] = {}
                    try:
                        while time.monotonic() < deadline:
                            last_health = self.health()
                            if last_health.get("ready"):
                                self._cleanup_service_config()
                                return
                            if self._process.poll() is not None:
                                raise RuntimeError(
                                    "Local PaddleOCR service exited during startup; see "
                                    f"{log_path}"
                                )
                            failures = last_health.get("startup_failures")
                            if failures:
                                raise RuntimeError(
                                    f"Local PaddleOCR workers failed to start: {failures}"
                                )
                            time.sleep(0.5)
                        raise TimeoutError(
                            "Timed out waiting for local PaddleOCR workers; "
                            f"last health={last_health!r}, log={log_path}"
                        )
                    except BaseException:
                        self._stop_started_service()
                        self._cleanup_service_config()
                        raise
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def _stop_started_service(self) -> None:
        if not self._started_here or self._process is None:
            return
        if self._process.poll() is None:
            try:
                self._request({"op": "shutdown"}, timeout=3.0)
            except (OSError, ConnectionError, TimeoutError, ValueError):
                try:
                    os.killpg(self._process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        try:
            self._process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._process.wait(timeout=10)
        self._cleanup_service_config()

    def ocr_image(self, image_path: Path) -> tuple[str, str]:
        self._ensure_service()
        request_id = f"paddle-{os.getpid()}-{uuid.uuid4().hex}"
        response = self._request(
            {
                "op": "ocr",
                "request_id": request_id,
                "image_path": str(image_path.resolve(strict=True)),
                "reading_direction": self.reading_direction,
                "horizontal_columns": self.horizontal_columns,
                "timeout": self.timeout,
            },
            timeout=self.timeout + 5.0,
        )
        if not response.get("ok"):
            actual_identity = response.get("model_identity")
            if actual_identity not in {None, self.service_identity}:
                raise RuntimeError(
                    "Local PaddleOCR response identity changed during a run"
                )
            raise RuntimeError(str(response.get("error") or "Local PaddleOCR failed"))
        if response.get("model_identity") != self.service_identity:
            raise RuntimeError("Local PaddleOCR response identity changed during a run")
        text = str(response.get("text") or "").strip()
        if not text:
            raise RuntimeError("Local PaddleOCR returned no text")
        return PaddleOCRText(text, response.get("metadata") or {}), request_id

    def close(self) -> None:
        if self._started_here and not self.persistent:
            self._stop_started_service()


register_backend("paddleocr-local", PaddleLocalOCR.from_profile)


__all__ = [
    "PADDLE_LOCAL_IMPLEMENTATION_VERSION",
    "PaddleLocalOCR",
    "PaddleOCRText",
    "paddle_checkpoint_identity",
    "paddle_profile_identity",
    "paddle_service_identity",
]
