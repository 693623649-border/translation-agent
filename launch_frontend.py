from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


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
    args = build_parser().parse_args(argv)
    app = Path(__file__).resolve().with_name("frontend_app.py")
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
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
