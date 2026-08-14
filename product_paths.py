"""Portable locations for bundled assets and writable product state."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Iterable


SOURCE_TREE_ROOT = Path(__file__).resolve().parent
PRODUCT_DATA_DIRECTORY = Path("share") / "translation-agent"


def installed_data_root(prefix: Path | str | None = None) -> Path:
    """Return the platform prefix used by setuptools ``data-files``."""

    return Path(prefix or sys.prefix).expanduser().resolve() / PRODUCT_DATA_DIRECTORY


def resource_root(
    *,
    source_root: Path | str | None = None,
    prefix: Path | str | None = None,
) -> Path:
    """Resolve bundled assets, preferring a source checkout over wheel data."""

    source = Path(source_root or SOURCE_TREE_ROOT).expanduser().resolve()
    installed = installed_data_root(prefix)
    for candidate in (source, installed):
        if any(
            (
                (candidate / "recipes").is_dir(),
                (candidate / "schemas").is_dir(),
                (candidate / "pipeline.example.toml").is_file(),
            )
        ):
            return candidate
    # Keep the result deterministic so callers can produce a useful missing
    # file error instead of silently consulting an unrelated working tree.
    return source


def recipe_paths(
    *,
    source_root: Path | str | None = None,
    prefix: Path | str | None = None,
) -> tuple[Path, ...]:
    root = resource_root(source_root=source_root, prefix=prefix)
    return tuple(sorted((root / "recipes").glob("*.toml")))


def default_profile_path(
    *,
    cwd: Path | str | None = None,
    source_root: Path | str | None = None,
    prefix: Path | str | None = None,
) -> Path:
    """Find an operator config, then fall back to the bundled example."""

    working = Path(cwd or Path.cwd()).expanduser().resolve()
    source = Path(source_root or SOURCE_TREE_ROOT).expanduser().resolve()
    assets = resource_root(source_root=source, prefix=prefix)
    candidates: Iterable[Path] = (
        working / "pipeline.toml",
        source / "pipeline.toml",
        working / "pipeline.example.toml",
        source / "pipeline.example.toml",
        assets / "pipeline.example.toml",
    )
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            return candidate
    return assets / "pipeline.example.toml"


def default_source_placeholder(
    source_mode: str,
    *,
    cwd: Path | str | None = None,
) -> Path:
    working = Path(cwd or Path.cwd()).expanduser().resolve()
    suffix = "epub" if source_mode == "epub" else "pdf"
    return working / "book" / f"book.{suffix}"


__all__ = [
    "SOURCE_TREE_ROOT",
    "default_profile_path",
    "default_source_placeholder",
    "installed_data_root",
    "recipe_paths",
    "resource_root",
]
