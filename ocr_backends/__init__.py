from .base import OCRBackend, OCRResult
from .registry import backend_factory, register_backend, registered_backends

# Built-ins self-register without importing PaddlePaddle.  The heavyweight
# framework is imported only inside spawned inference workers.
from . import paddle_local as _paddle_local  # noqa: F401,E402

__all__ = [
    "OCRBackend",
    "OCRResult",
    "backend_factory",
    "register_backend",
    "registered_backends",
]
