"""Translate every remaining non-Chinese reader chunk in the book corpora.

Walks the workspaces, runs the gate's own classifier to find what still needs a
Chinese rendering, and drives ``knowledge_base_cli translate-kb`` one workspace at
a time. Running per workspace (rather than ``--recursive``) keeps each result
attributable and lets a failure stop the batch before it touches later corpora —
``translate-kb`` rewrites a corpus in place, so an unattended failure mode is a
half-translated library.

The run is resumable: a workspace whose chunks are already Chinese reports zero
pending and is skipped, so re-running after an interruption costs only the
workspaces that had not finished.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb_translation import classify_row  # noqa: E402

LOG = ROOT / "work" / "translate_kb_batch.log"
MODEL = "deepseek-flash"
CONCURRENCY = "4"
DELAY_BETWEEN_WORKSPACES = 2.0


def log(message: str) -> None:
    """Append one line to the batch log and echo it."""

    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def load_env() -> None:
    """Export unset ``.env`` keys, exactly as the CLI runner does."""

    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def pending_for(corpus: Path) -> int:
    """Count the chunks the release gate would still demand in Chinese."""

    pending = 0
    for line in corpus.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if classify_row(str(row.get("title") or ""), str(row.get("content") or ""))["needs_translation"]:
            pending += 1
    return pending


def main() -> int:
    load_env()
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        log("ABORT: DEEPSEEK_API_KEY is not set")
        return 2

    targets: list[tuple[int, Path]] = []
    for corpus in sorted((ROOT / "outputs").rglob("knowledge_base.jsonl")):
        pending = pending_for(corpus)
        if pending:
            targets.append((pending, corpus))
    targets.sort()

    total = sum(pending for pending, _ in targets)
    log(f"=== batch start: {len(targets)} workspaces, {total} chunks pending, model={MODEL} ===")
    for pending, corpus in targets:
        log(f"    pending {pending:>3}  {corpus.parent.relative_to(ROOT)}")

    done_chunks = 0
    for index, (pending, corpus) in enumerate(targets, start=1):
        workspace = corpus.parent
        log(f"[{index}/{len(targets)}] translating {pending} chunk(s): {workspace.name[:70]}")
        command = [
            sys.executable,
            str(ROOT / "knowledge_base_cli.py"),
            "translate-kb",
            str(workspace),
            "--model",
            MODEL,
            "--concurrency",
            CONCURRENCY,
        ]
        started = time.time()
        proc = subprocess.run(
            command, cwd=ROOT, env=os.environ, capture_output=True, text=True, encoding="utf-8"
        )
        elapsed = time.time() - started
        payload = None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
        if proc.returncode != 0 or payload is None:
            log(f"    FAILED rc={proc.returncode} after {elapsed:.0f}s")
            log(f"    stderr: {(proc.stderr or '').strip()[-800:]}")
            log("=== batch stopped so the remaining corpora stay untouched ===")
            return 1
        written = payload.get("written", 0)
        count = payload.get("translate_total", 0)
        done_chunks += int(count or 0)
        result = (payload.get("results") or [{}])[0]
        log(
            f"    ok: translated={count} written={written} "
            f"languages={result.get('languages')} in {elapsed:.0f}s"
        )
        log(f"    progress: {done_chunks}/{total} chunks")
        if index < len(targets):
            time.sleep(DELAY_BETWEEN_WORKSPACES)

    log(f"=== batch complete: {done_chunks}/{total} chunks translated ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
