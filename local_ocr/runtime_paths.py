from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import IO


def expand_runtime_path(value: str | os.PathLike[str]) -> Path:
    """Expand the local UID placeholder without resolving symlinks."""

    uid = os.geteuid() if hasattr(os, "geteuid") else os.getuid()
    raw = str(value).replace("{uid}", str(uid))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def ensure_private_directory(value: str | os.PathLike[str]) -> Path:
    """Return an owner-only runtime directory and reject a symlink target.

    The default paths are direct children of the sticky `/tmp` directory and
    include the effective UID.  `O_NOFOLLOW` closes the remaining final-path
    race, while owner validation prevents adopting a directory pre-created by
    another local account.
    """

    path = expand_runtime_path(value)
    shared_roots = {
        Path(path.anchor),
        expand_runtime_path(tempfile.gettempdir()),
    }
    if path in shared_roots:
        raise RuntimeError(
            f"refusing to convert a shared system directory into local OCR runtime storage: {path}"
        )
    try:
        path.lstat()
        existed = True
    except FileNotFoundError:
        existed = False
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError(f"unsafe local OCR runtime directory {path}: {exc}") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise RuntimeError(f"local OCR runtime path is not a directory: {path}")
        if hasattr(os, "geteuid") and status.st_uid != os.geteuid():
            raise RuntimeError(
                f"local OCR runtime directory is not owned by the current user: {path}"
            )
        permissions = stat.S_IMODE(status.st_mode)
        if existed and permissions != 0o700:
            raise RuntimeError(
                f"local OCR runtime directory must already be mode 0700: {path} "
                f"(found {permissions:04o})"
            )
        if not existed:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


def open_private_text_file(
    value: str | os.PathLike[str],
    *,
    append: bool,
    read_write: bool = False,
) -> IO[str]:
    """Open an owner-only regular text file without following symlinks."""

    path = expand_runtime_path(value)
    ensure_private_directory(path.parent)
    flags = os.O_RDWR if read_write else os.O_WRONLY
    flags |= os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if append:
        flags |= os.O_APPEND
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"unable to open private local OCR file {path}: {exc}") from exc
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError(f"local OCR path is not a regular file: {path}")
        if hasattr(os, "geteuid") and status.st_uid != os.geteuid():
            raise RuntimeError(f"local OCR file is not owned by the current user: {path}")
        os.fchmod(descriptor, 0o600)
        if read_write:
            mode = "a+" if append else "r+"
        else:
            mode = "a" if append else "w"
        return os.fdopen(descriptor, mode, encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


__all__ = [
    "ensure_private_directory",
    "expand_runtime_path",
    "open_private_text_file",
]
