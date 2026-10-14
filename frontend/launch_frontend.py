from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动影印书转换低代码控制台。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器，适合远程服务器。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.host.strip().lower() not in LOOPBACK_HOSTS:
        parser.error(
            "Web UI is local-only: --host must be localhost, 127.0.0.1, or ::1. "
            "Use an authenticated local tunnel for remote access."
        )
    # ``frontend_app.py`` remains an import-compatible shim; new launches use
    # the canonical st.navigation entry point.
    app = Path(__file__).resolve().with_name("streamlit_app.py")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        # A developer-wide Streamlit config must not make the installed
        # product launcher reject its explicit port.
        "--global.developmentMode=false",
        f"--server.address={args.host}",
        f"--server.port={args.port}",
        f"--server.headless={'true' if args.no_browser else 'false'}",
    ]
    process = subprocess.Popen(command)
    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
