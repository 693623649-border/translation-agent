"""Offline PP-OCRv5 CPU backend; optional Paddle imports stay out of planning/UI."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from functools import lru_cache
import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import tarfile
import tempfile
import threading
import urllib.request

MODEL_BASE = "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
MODEL_FILES = ("inference.json", "inference.pdiparams", "inference.yml")
BACKEND = "paddleocr-native"


def default_models_dir() -> Path:
    return Path.home() / ".translation-agent" / "paddleocr" / "models"


def add_native_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--paddle-native-variant", choices=("mobile", "server"), default=None)
    parser.add_argument("--paddle-native-models-dir", type=Path, default=None)
    parser.add_argument("--paddle-native-threads", type=int, default=4)
    parser.add_argument("--paddle-native-det-limit", type=int, default=960,
                        help="CPU detection long-side limit (320-960); recognition batch is always 1.")


@dataclass(frozen=True)
class NativeOptions:
    variant: str = "mobile"
    models_dir: Path | None = None
    threads: int = 4
    det_limit: int = 960
    reading_direction: str = "horizontal"

    def __post_init__(self) -> None:
        if self.variant not in {"mobile", "server"}:
            raise ValueError("native PaddleOCR variant must be mobile or server")
        if not 1 <= self.threads <= 8:
            raise ValueError("--paddle-native-threads must be between 1 and 8")
        if not 320 <= self.det_limit <= 960:
            raise ValueError("--paddle-native-det-limit must be between 320 and 960")
        if self.reading_direction not in {"horizontal", "vertical"}:
            raise ValueError("unsupported PaddleOCR reading direction")
        object.__setattr__(self, "models_dir", Path(self.models_dir or default_models_dir()).expanduser().resolve())

    @property
    def model_names(self) -> tuple[str, str]:
        return tuple(f"PP-OCRv5_{self.variant}_{role}" for role in ("det", "rec"))


def options_from_args(args, profile=None) -> NativeOptions:
    variant = getattr(args, "paddle_native_variant", None)
    if variant is None and profile is not None and profile.adapter == BACKEND:
        names = {"PP-OCRv5-mobile": "mobile", "PP-OCRv5-server": "server"}
        if profile.model not in names:
            raise ValueError("native OCR profile model must be PP-OCRv5-mobile or PP-OCRv5-server")
        variant = names[profile.model]
    return NativeOptions(
        variant=variant or "mobile",
        models_dir=getattr(args, "paddle_native_models_dir", None),
        threads=getattr(args, "paddle_native_threads", 4),
        det_limit=getattr(args, "paddle_native_det_limit", 960),
        reading_direction=(getattr(args, "ocr_reading_direction", None)
                           or getattr(profile, "reading_direction", None) or "horizontal"),
    )


def readiness(options: NativeOptions) -> tuple[bool, str]:
    missing = [name for name in ("paddle", "paddleocr") if importlib.util.find_spec(name) is None]
    if missing:
        return False, '缺少原生 OCR 依赖，请在启动工作台的 Python 环境安装：pip install ".[web,paddle]"'
    for name in options.model_names:
        for filename in MODEL_FILES:
            path = options.models_dir / name / filename
            if not path.is_file() or not path.stat().st_size:
                return False, f"缺少模型 {name}；运行 translation-agent-paddle setup --variant {options.variant} --models-dir {options.models_dir}"
    return True, f"PP-OCRv5 {options.variant} · 本机 CPU · 已就绪"


@lru_cache(maxsize=64)
def _file_hash(path: str, size: int, modified: int) -> str:
    del size, modified
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _identity_file(path: Path) -> str:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return "missing"
    return _file_hash(str(path), stat.st_size, stat.st_mtime_ns)


def model_identity(options: NativeOptions, *, dpi=200, max_side=3000, quality=90) -> str:
    versions = {}
    for name in ("paddlepaddle", "paddleocr", "paddlex"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "missing"
    payload = {
        "version": 1, "variant": options.variant, "det_limit": options.det_limit,
        "threads": options.threads, "direction": options.reading_direction,
        "rec_batch": 1, "image_limit": 2000, "device": "cpu", "mkldnn": False,
        "render": [dpi, max_side, quality], "packages": versions,
        "weights": {f"{name}/{file}": _identity_file(options.models_dir / name / file)
                    for name in options.model_names for file in MODEL_FILES},
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"{BACKEND}/PP-OCRv5-{options.variant}/{digest}"


def identity_from_args(args, profile=None) -> str:
    return model_identity(options_from_args(args, profile), dpi=args.dpi,
                          max_side=args.max_image_side, quality=args.jpeg_quality)


def ordered_text(result, direction: str) -> str:
    """Order detected lines; this is not a table/footnote layout recognizer."""
    texts = result.get("rec_texts")
    boxes = result.get("rec_polys")
    if boxes is None:
        boxes = result.get("rec_boxes")
    if texts is None or boxes is None or len(texts) != len(boxes):
        raise RuntimeError("PaddleOCR returned malformed text/box results")
    rows = []
    for text, box in zip(texts, boxes):
        if not str(text).strip():
            continue
        if hasattr(box, "tolist"):
            box = box.tolist()
        if len(box) == 4 and isinstance(box[0], (int, float)):
            x1, y1, x2, y2 = box
        else:
            x1, x2 = min(p[0] for p in box), max(p[0] for p in box)
            y1, y2 = min(p[1] for p in box), max(p[1] for p in box)
        rows.append((float(x1), float(y1), float(x2), float(y2), str(text).strip()))
    if not rows:
        return ""
    if direction == "vertical":
        band = max(8.0, statistics.median(max(1, r[2] - r[0]) for r in rows) * .55)
        rows.sort(key=lambda r: (-round(r[2] / band), r[1], -r[2]))
    else:
        band = max(8.0, statistics.median(max(1, r[3] - r[1]) for r in rows) * .55)
        rows.sort(key=lambda r: (round(r[1] / band), r[0], r[1]))
    return "\n".join(r[4] for r in rows)


class PaddleNativeOCR:
    """One lazily loaded model per OCR stage, serialized across local jobs."""

    def __init__(self, options: NativeOptions, *, model_id: str) -> None:
        ready, detail = readiness(options)
        if not ready:
            raise RuntimeError(detail)
        self.options = options
        self.ocr_model = model_id
        self._engine = None
        self._mutex = threading.Lock()
        self._closed = threading.Event()
        self._process_lock = None

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        from pipeline_graph.core import OutputDirectoryLock, OutputDirectoryLockedError

        # Different Web tasks run in different processes. Hold a single CPU
        # reservation for the entire stage to avoid loading several models.
        lock = OutputDirectoryLock(Path.home() / ".translation-agent" / "paddleocr" / "cpu.lock")
        announced = False
        while True:
            if self._closed.is_set():
                raise RuntimeError("Native OCR cancelled")
            try:
                lock.acquire()
                break
            except OutputDirectoryLockedError:
                if not announced:
                    print("[paddleocr-native] waiting for another local CPU OCR task", flush=True)
                    announced = True
                self._closed.wait(.2)
        self._process_lock = lock
        try:
            # Only local weights are allowed; setup is the explicit network step.
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            from paddleocr import PaddleOCR

            det, rec = self.options.model_names
            self._engine = PaddleOCR(
                device="cpu", cpu_threads=self.options.threads, enable_mkldnn=False,
                text_detection_model_name=det,
                text_detection_model_dir=str(self.options.models_dir / det),
                text_recognition_model_name=rec,
                text_recognition_model_dir=str(self.options.models_dir / rec),
                text_recognition_batch_size=1,
                text_det_limit_side_len=self.options.det_limit,
                text_det_limit_type="max",
                use_doc_orientation_classify=False, use_doc_unwarping=False,
                use_textline_orientation=False,
            )
            return self._engine
        except BaseException:
            self._cleanup()
            raise

    def ocr_image(self, image_path: Path) -> tuple[str, str]:
        with self._mutex:
            if self._closed.is_set():
                raise RuntimeError("Native OCR cancelled")
            try:
                engine = self._ensure_engine()
                import numpy as np
                from PIL import Image

                with Image.open(image_path) as source:
                    image = source.convert("RGB")
                    image.thumbnail((2000, 2000))
                    # Paddle image arrays use OpenCV's BGR ordering.
                    results = list(engine.predict(np.asarray(image)[:, :, ::-1].copy()))
                if len(results) != 1:
                    raise RuntimeError("PaddleOCR returned an unexpected number of pages")
                text = ordered_text(results[0], self.options.reading_direction)
                if not text:
                    from book_pipeline import is_visually_blank_page
                    if not is_visually_blank_page(image_path)[0]:
                        raise RuntimeError("PaddleOCR detected no text on a nonblank page; try server or a different OCR backend")
                if self._closed.is_set():
                    raise RuntimeError("Native OCR cancelled")
                return text, f"native-cpu/{self.options.variant}"
            finally:
                if self._closed.is_set():
                    self._cleanup()

    def _cleanup(self) -> None:
        self._engine = None
        gc.collect()
        if self._process_lock is not None:
            self._process_lock.release()
            self._process_lock = None

    def close(self) -> None:
        self._closed.set()
        # Do not free native inference buffers from another thread mid-call.
        if self._mutex.acquire(blocking=False):
            try:
                self._cleanup()
            finally:
                self._mutex.release()


def setup_models(options: NativeOptions) -> None:
    from pipeline_graph.core import OutputDirectoryLock

    options.models_dir.mkdir(parents=True, exist_ok=True)
    with OutputDirectoryLock(options.models_dir / "setup.lock"):
        for name in options.model_names:
            target = options.models_dir / name
            if all((target / file).is_file() and (target / file).stat().st_size for file in MODEL_FILES):
                print(f"[cached] {name}")
                continue
            with tempfile.TemporaryDirectory(dir=options.models_dir) as folder:
                temporary = Path(folder)
                archive = temporary / "model.tar"
                with urllib.request.urlopen(f"{MODEL_BASE}/{name}_infer.tar", timeout=120) as response, archive.open("wb") as out:
                    shutil.copyfileobj(response, out)
                extracted = temporary / "extracted"
                with tarfile.open(archive) as bundle:
                    bundle.extractall(extracted, filter="data")
                roots = list(extracted.iterdir())
                if len(roots) != 1 or not all((roots[0] / file).is_file() and (roots[0] / file).stat().st_size for file in MODEL_FILES):
                    raise RuntimeError(f"Invalid model archive: {name}")
                if target.exists():
                    raise RuntimeError(f"Incomplete model directory: {target}; move it aside and retry setup")
                roots[0].rename(target)
                print(f"[installed] {name}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install/check native CPU PaddleOCR models.")
    parser.add_argument("command", choices=("setup", "status"))
    parser.add_argument("--variant", choices=("mobile", "server"), default="mobile")
    parser.add_argument("--models-dir", type=Path)
    args = parser.parse_args(argv)
    options = NativeOptions(variant=args.variant, models_dir=args.models_dir)
    if args.command == "setup":
        setup_models(options)
    ready, detail = readiness(options)
    print(detail)
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
