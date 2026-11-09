"""SHA-256 digests shared by artifact fingerprints, manifests, and audits."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Hash a file in 1 MiB blocks so large PDFs and EPUBs are never fully loaded."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class FileDigestCache:
    """Reuse a full hash only within one graph node's decision/invocation.

    The caller clears this at every node boundary, so stat metadata never
    substitutes for the first content check. Writes/replacements within that
    boundary invalidate the entry using file identity, size and change timestamps.
    """

    def __init__(self) -> None:
        self._entries: dict[Path, tuple[tuple[int, ...], str]] = {}

    def clear(self) -> None:
        self._entries.clear()

    def sha256(self, path: Path, *, hash_file: Callable[[Path], str] = sha256_file) -> str:
        path = path.resolve()
        before = path.stat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        cached = self._entries.get(path)
        if cached is not None and cached[0] == identity:
            return cached[1]
        value = hash_file(path)
        after = path.stat()
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError(f"file changed while hashing: {path}")
        self._entries[path] = (identity, value)
        return value
