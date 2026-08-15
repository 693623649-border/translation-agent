from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


PADDLEOCR_PACKAGE_VERSION = "3.7.0"
PADDLEPADDLE_PACKAGE_VERSION = "3.3.0"
PADDLEX_PACKAGE_VERSION = "3.7.0"
PADDLE_LOCAL_IMPLEMENTATION_VERSION = (
    "paddle-local-v3-paddleocr3.7.0-paddlex3.7.0-paddle3.3.0"
)

PADDLE_CONTENT_KEYS = frozenset(
    {
        "ocr_version",
        "lang",
        "doc_orientation_classify_model_name",
        "doc_unwarping_model_name",
        "text_detection_model",
        "text_detection_model_name",
        "textline_orientation_model_name",
        "text_recognition_model",
        "text_recognition_model_name",
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_textline_orientation",
        "text_det_limit_side_len",
        "text_det_limit_type",
        "text_det_thresh",
        "text_det_box_thresh",
        "text_det_unclip_ratio",
        "text_det_input_shape",
        "text_rec_score_thresh",
        "text_rec_input_shape",
        "return_word_box",
        "engine",
        "engine_config",
        "precision",
        "use_tensorrt",
        "enable_hpi",
        "enable_cinn",
        "reading_order_version",
    }
)

PADDLE_RUNTIME_KEYS = frozenset(
    {
        "devices",
        "instances_per_device",
        "text_recognition_batch_size",
        "queue_depth",
        "python_executable",
        "socket_path",
        "spool_dir",
        "model_cache_dir",
        "auto_start",
        "persistent",
        "startup_timeout",
        "worker_startup_timeout",
    }
)


def _positive_integer(settings: Mapping[str, Any], key: str) -> None:
    if key not in settings:
        return
    valid = type(settings[key]) is int and settings[key] >= 1
    if not valid:
        raise ValueError(f"paddleocr-local {key} must be a positive integer")


def validate_paddle_settings(
    content: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> None:
    unknown_content = sorted(set(content) - PADDLE_CONTENT_KEYS)
    unknown_runtime = sorted(set(runtime) - PADDLE_RUNTIME_KEYS)
    if unknown_content:
        raise ValueError(
            f"unknown paddleocr-local content settings: {unknown_content}"
        )
    if unknown_runtime:
        raise ValueError(
            f"unknown paddleocr-local runtime settings: {unknown_runtime}"
        )
    for alias, canonical in (
        ("text_detection_model", "text_detection_model_name"),
        ("text_recognition_model", "text_recognition_model_name"),
    ):
        if alias in content and canonical in content:
            raise ValueError(
                f"paddleocr-local settings {alias!r} and {canonical!r} are mutually exclusive"
            )
    if content.get("engine") not in {
        None,
        "paddle",
        "paddle_static",
        "paddle_dynamic",
        "transformers",
        "onnxruntime",
    }:
        raise ValueError(f"invalid paddleocr-local engine: {content.get('engine')!r}")
    if content.get("precision") not in {None, "fp32", "fp16"}:
        raise ValueError(
            f"invalid paddleocr-local precision: {content.get('precision')!r}"
        )
    for key in (
        "instances_per_device",
        "text_recognition_batch_size",
        "queue_depth",
        "startup_timeout",
        "worker_startup_timeout",
    ):
        _positive_integer(runtime, key)
    effective_startup_timeout = int(runtime.get("startup_timeout", 600))
    effective_worker_timeout = int(
        runtime.get("worker_startup_timeout", effective_startup_timeout)
    )
    if effective_worker_timeout > effective_startup_timeout:
        raise ValueError(
            "paddleocr-local worker_startup_timeout must not exceed startup_timeout"
        )
    devices = runtime.get("devices", ("gpu:0", "gpu:1"))
    if isinstance(devices, str):
        devices = (devices,)
    if not isinstance(devices, Sequence) or not devices or not all(
        isinstance(value, str) and value.strip() for value in devices
    ):
        raise ValueError("paddleocr-local runtime.devices must be a non-empty string array")
    normalized_devices = [value.strip().lower() for value in devices]
    if len(set(normalized_devices)) != len(normalized_devices):
        raise ValueError("paddleocr-local runtime.devices must not contain duplicates")
    if not all(re.fullmatch(r"gpu:\d+", value) for value in normalized_devices):
        raise ValueError(
            "paddleocr-local runtime.devices must use explicit GPU IDs such as gpu:0"
        )
    for key in (
        "use_doc_orientation_classify",
        "use_doc_unwarping",
        "use_textline_orientation",
        "return_word_box",
        "use_tensorrt",
        "enable_hpi",
        "enable_cinn",
    ):
        if key in content and not isinstance(content[key], bool):
            raise ValueError(f"paddleocr-local content.{key} must be a boolean")
    for key in ("auto_start", "persistent"):
        if key in runtime and not isinstance(runtime[key], bool):
            raise ValueError(f"paddleocr-local runtime.{key} must be a boolean")


__all__ = [
    "PADDLE_LOCAL_IMPLEMENTATION_VERSION",
    "PADDLEOCR_PACKAGE_VERSION",
    "PADDLEPADDLE_PACKAGE_VERSION",
    "PADDLEX_PACKAGE_VERSION",
    "PADDLE_CONTENT_KEYS",
    "PADDLE_RUNTIME_KEYS",
    "validate_paddle_settings",
]
