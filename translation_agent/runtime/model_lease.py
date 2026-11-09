"""User-level lease for heavy local models (native OCR and local LLM).

Native PaddleOCR and the managed llama.cpp server never hold model weights at
the same time on this machine.  Both acquire the same kernel ``flock`` on
``~/.translation-agent/paddleocr/cpu.lock`` (the historical OCR path, kept so
older workers and new ones exclude each other) before loading anything, and
release it only after their model has been freed.

The lease only coordinates processes that use this project.  It is not a
memory limit for other applications on the machine.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time


class HeavyModelLeaseCancelled(RuntimeError):
    """Raised when a caller cancels while waiting for the lease."""


def heavy_model_lock_path() -> Path:
    return Path.home() / ".translation-agent" / "paddleocr" / "cpu.lock"


def lease_log(component: str, message: str) -> None:
    """Print one timestamped lifecycle line (queued/loading/unloaded...)."""

    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    print(f"[{component}] {stamp} {message}", file=sys.stderr, flush=True)


def _holder_description(path: Path) -> str:
    # The file content is diagnostic metadata written by the current owner.
    try:
        owner = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return "unknown holder"
    purpose = owner.get("purpose") or "paddleocr-native"
    return f"{purpose} pid={owner.get('pid')}"


def acquire_heavy_model_lease(
    purpose: str,
    *,
    cancelled: threading.Event,
    poll_interval: float = 0.2,
):
    """Block until the heavy-model lease is held; return the held lock.

    ``cancelled`` aborts the wait without loading anything.  The returned
    :class:`OutputDirectoryLock` must be released by the caller after its
    model has been unloaded.
    """

    from translation_agent.pipeline.graph.core import (
        OutputDirectoryLock,
        OutputDirectoryLockedError,
    )

    from translation_agent.runtime.local_llm_server import recover_orphaned_server

    path = heavy_model_lock_path()
    lock = OutputDirectoryLock(path, purpose=purpose)
    announced = False
    next_recovery = 0.0
    while True:
        if cancelled.is_set():
            raise HeavyModelLeaseCancelled(f"{purpose} cancelled while waiting for the local model lease")
        try:
            lock.acquire()
        except OutputDirectoryLockedError:
            if not announced:
                lease_log(purpose, f"queued: waiting for local model lease held by {_holder_description(path)}")
                announced = True
            if time.monotonic() >= next_recovery:
                # A llama-server whose owner and supervisor were both killed
                # keeps the inherited lock; stop it so the queue can move.
                recover_orphaned_server()
                next_recovery = time.monotonic() + 5.0
            cancelled.wait(poll_interval)
            continue
        lease_log(purpose, f"lease acquired ({path})")
        return lock


def release_heavy_model_lease(purpose: str, lock) -> None:
    lock.release()
    lease_log(purpose, "lease released")
