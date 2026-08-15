"""Detect temporary output, live stage locks, and sibling pipeline workers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility path.
    fcntl = None


_PROC_ROOT = Path("/proc")


def _issue(
    code: str,
    message: str,
    *,
    path: Path | str | None = None,
    **evidence: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {"code": code, "message": message}
    if path is not None:
        value["path"] = str(path)
    if evidence:
        value["evidence"] = evidence
    return value


def _result(
    summary: str,
    *,
    metrics: dict[str, Any] | None = None,
    issues: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "summary": summary,
        "metrics": metrics or {},
        "issues": issues or [],
        "warnings": warnings or [],
    }


def check_runtime_hygiene(output_dir: Path) -> dict[str, Any]:
    """Return the canonical ``runtime.hygiene`` check payload."""

    issues: list[dict[str, Any]] = []
    temporary_paths: list[str] = []
    if output_dir.exists():
        for path in output_dir.rglob("*"):
            name = path.name.lower()
            if (
                name in {"tmp", "temp", ".tmp"}
                or name.endswith((".tmp", ".part", ".partial"))
                or (name.startswith(".") and ".tmp" in name)
                or re.fullmatch(r"_page_images_\d+_[0-9a-f-]+", name) is not None
            ):
                temporary_paths.append(str(path))
    if temporary_paths:
        issues.append(
            _issue(
                "temporary_artifacts_present",
                "输出目录仍有临时文件或目录。",
                path=output_dir,
                values=temporary_paths[:50],
                count=len(temporary_paths),
            )
        )

    active_stage_locks: list[str] = []
    stage_lock_dir = output_dir / ".stage_locks"
    if fcntl is not None and stage_lock_dir.is_dir():
        for lock_path in sorted(stage_lock_dir.glob("*.lock")):
            try:
                with lock_path.open("a+", encoding="utf-8") as handle:
                    try:
                        fcntl.flock(
                            handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB,
                        )
                    except BlockingIOError:
                        active_stage_locks.append(str(lock_path))
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                issues.append(
                    _issue(
                        "stage_lock_unreadable",
                        f"无法检查阶段锁：{exc}",
                        path=lock_path,
                    )
                )
    if active_stage_locks:
        issues.append(
            _issue(
                "active_stage_locks",
                "仍有流水线阶段锁被占用。",
                path=stage_lock_dir,
                values=active_stage_locks,
            )
        )

    active_processes: list[dict[str, Any]] = []
    output_token = str(output_dir.resolve())
    if _PROC_ROOT.is_dir():
        # The verifier itself, its launcher shell, and a supervising pipeline
        # naturally contain the output path in their command line.  They are
        # the current execution chain, not stale sibling workers.
        ancestor_pids: set[int] = {os.getpid()}
        ancestor = os.getpid()
        while ancestor > 1:
            try:
                stat = (_PROC_ROOT / str(ancestor) / "stat").read_text()
                ancestor = int(stat.rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                break
            ancestor_pids.add(ancestor)
        for proc_dir in _PROC_ROOT.iterdir():
            if not proc_dir.name.isdigit() or int(proc_dir.name) in ancestor_pids:
                continue
            try:
                raw = (proc_dir / "cmdline").read_bytes()
                command = (
                    raw.replace(b"\x00", b" ")
                    .decode("utf-8", errors="replace")
                    .strip()
                )
            except (OSError, PermissionError):
                continue
            if output_token not in command:
                continue
            if not re.search(
                r"(?:book_pipeline(?:\.py)?|publication_verifier|regenerate|ocr|translate)",
                command,
                flags=re.I,
            ):
                continue
            # Never copy argv into the report: deprecated raw-key flags may
            # still be present in another process even though this pipeline
            # no longer recommends them.
            executable = raw.split(b"\x00", 1)[0].decode(
                "utf-8", errors="replace"
            )
            active_processes.append(
                {
                    "pid": int(proc_dir.name),
                    "executable": Path(executable).name[:200],
                }
            )
    if active_processes:
        issues.append(
            _issue(
                "active_pipeline_processes",
                "仍有指向该输出目录的流水线进程。",
                path=output_dir,
                values=active_processes,
            )
        )

    return _result(
        "未发现临时产物或残留流水线进程。"
        if not issues
        else "发现临时产物或残留流水线进程。",
        metrics={
            "temporary_path_count": len(temporary_paths),
            "active_stage_lock_count": len(active_stage_locks),
            "active_process_count": len(active_processes),
        },
        issues=issues,
    )


__all__ = ["check_runtime_hygiene"]
