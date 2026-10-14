"""Fail-fast guard against committing books, caches, large files or secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


FORBIDDEN_PREFIXES = (
    ".agents/",
    ".claude/",
    ".translation-agent/",
    "book/",
    "outputs/",
    "translation_cache/",
    "work/",
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class GuardIssue:
    code: str
    path: str
    detail: str


def tracked_files(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    candidates = tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )
    # A local move appears as a deleted cached path plus a new untracked path
    # before staging.  Scan the effective working tree, not a path that no
    # longer exists.
    return tuple(path for path in candidates if (root / path).is_file())


def scan_repository(
    root: Path,
    paths: Sequence[str],
    *,
    max_bytes: int = 10 * 1024 * 1024,
) -> tuple[GuardIssue, ...]:
    issues: list[GuardIssue] = []
    for relative in paths:
        normalized = relative.replace("\\", "/")
        if normalized.startswith(FORBIDDEN_PREFIXES):
            issues.append(
                GuardIssue(
                    "forbidden_tracked_path",
                    normalized,
                    "local source/cache/config directories must not be tracked",
                )
            )
            continue
        path = root / relative
        try:
            size = path.stat().st_size
        except OSError as exc:
            issues.append(GuardIssue("unreadable_tracked_file", normalized, str(exc)))
            continue
        if size > max_bytes:
            issues.append(
                GuardIssue(
                    "tracked_file_too_large",
                    normalized,
                    f"{size} bytes exceeds limit {max_bytes}",
                )
            )
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            issues.append(GuardIssue("unreadable_tracked_file", normalized, str(exc)))
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                issues.append(
                    GuardIssue(
                        "possible_secret",
                        normalized,
                        f"matched guarded pattern {pattern.pattern!r}",
                    )
                )
                break
    return tuple(issues)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--max-mb", type=int, default=10)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve()
    issues = scan_repository(
        root,
        tracked_files(root),
        max_bytes=args.max_mb * 1024 * 1024,
    )
    for issue in issues:
        print(f"[{issue.code}] {issue.path}: {issue.detail}")
    if not issues:
        print("repository guard passed")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
