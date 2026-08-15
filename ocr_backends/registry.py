from __future__ import annotations

from collections.abc import Callable
from typing import Any


BackendFactory = Callable[..., Any]
_BACKENDS: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory) -> None:
    normalized = str(name).strip().lower()
    if not normalized:
        raise ValueError("OCR backend name cannot be empty")
    if normalized in _BACKENDS and _BACKENDS[normalized] is not factory:
        raise ValueError(f"OCR backend {normalized!r} is already registered")
    _BACKENDS[normalized] = factory


def backend_factory(name: str) -> BackendFactory:
    try:
        return _BACKENDS[str(name).strip().lower()]
    except KeyError as exc:
        available = ", ".join(sorted(_BACKENDS)) or "<none>"
        raise ValueError(
            f"Unknown registered OCR backend {name!r}; available: {available}"
        ) from exc


def registered_backends() -> tuple[str, ...]:
    return tuple(sorted(_BACKENDS))
