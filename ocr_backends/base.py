from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class OCRResult:
    """Structured OCR response used by local and remote adapters."""

    text: str
    request_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


class OCRBackend(Protocol):
    """Minimal backend contract retained by :mod:`book_pipeline`."""

    ocr_model: str

    def ocr_image(self, image_path: Path) -> tuple[str, str]: ...

    def close(self) -> None: ...
