"""Run translation-agent-kb with this project's .env exported.

The CLI reads its credential straight from the environment and does not load
``.env`` itself, so an unexported shell would fail with a missing-key error.
Existing environment variables always win.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_PATH = ROOT / ".env"


def load_env() -> int:
    """Export unset ``.env`` keys; return how many were applied."""

    if not ENV_PATH.is_file():
        return 0
    applied = 0
    for line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied += 1
    return applied


def main(argv: list[str]) -> int:
    applied = load_env()
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    print(f"[env] exported {applied} key(s); DEEPSEEK_API_KEY {'set' if key else 'MISSING'}", flush=True)
    if not key:
        return 2
    stripped = [a for a in argv if a != "--load-env"]
    command = [sys.executable, str(ROOT / "knowledge_base_cli.py"), *stripped]
    print(f"[run] {' '.join(stripped)}", flush=True)
    proc = subprocess.run(command, cwd=ROOT, env=os.environ)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
