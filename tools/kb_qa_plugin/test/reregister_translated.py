"""Re-register the workspaces whose corpus text changed.

``normalise_corpus_file`` deliberately invalidates the stored vectors — the
text moved, so the embeddings no longer describe it — and marks the manifest
``awaiting_provider``. Left that way, every translated workspace silently falls
back to lexical-only retrieval, which is exactly the degradation a reader is
least likely to notice. This rebuilds the embedding index for each affected
workspace so the derived layer matches the new text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOG = ROOT / "work" / "reregister_kb.log"


def load_env() -> None:
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


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    load_env()
    if not os.environ.get("ZHIPU_API_KEY", "").strip():
        log("ABORT: ZHIPU_API_KEY is not set")
        return 2

    targets: list[Path] = []
    for backup in sorted((ROOT / "outputs").rglob("knowledge_base.translation-source.jsonl")):
        if backup.stat().st_size > 0:
            targets.append(backup.parent)

    log(f"=== re-register start: {len(targets)} workspaces with translated text ===")
    for workspace in targets:
        log(f"    {workspace.relative_to(ROOT)}")

    failures = 0
    for index, workspace in enumerate(targets, start=1):
        log(f"[{index}/{len(targets)}] register {workspace.name[:70]}")
        started = time.time()
        # ``--allow-foreign`` is mandatory here, not a shortcut. The Chinese gate
        # refuses to register while any chunk still trips ``detect_language``,
        # and after translation a residue remains that is Chinese text retaining
        # a Japanese proper noun (magazine and manga titles such as 《りぼん》).
        # The gate's 2%-kana rule exists to stop kanji-heavy Japanese prose being
        # misfiled as Chinese, so it cannot separate that residue from real
        # foreign prose — the bypass is recorded in the manifest rather than
        # worked around silently.
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "knowledge_base_cli.py"),
                "register",
                str(workspace),
                "--allow-foreign",
            ],
            cwd=ROOT,
            env=os.environ,
            capture_output=True,
            text=True,
            encoding="utf-8",
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
            failures += 1
            log(f"    FAILED rc={proc.returncode}: {(proc.stderr or '').strip()[-400:]}")
            continue
        embedding = (payload.get("embedding") or {})
        log(
            f"    ok: embedding={embedding.get('status')} "
            f"dims={embedding.get('dimensions')} index_exists={embedding.get('index_exists')} "
            f"in {elapsed:.0f}s"
        )

    log(f"=== re-register complete: {len(targets) - failures}/{len(targets)} ok, {failures} failed ===")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
