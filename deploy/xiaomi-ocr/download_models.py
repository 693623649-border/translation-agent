#!/usr/bin/env python3
"""Download SeerRay-Lab/Xiaomi-OCR-0 weights from Hugging Face.

Host or container usage (standard library only):
  python download_models.py                  # download all required files
  python download_models.py --list           # show the file list
  python download_models.py --force          # re-download even if present

Files land in models/SeerRay-Lab--Xiaomi-OCR-0/ (the directory the batch
script and the pipeline availability probe both expect).  The default
endpoint is https://huggingface.co (reachable on this host); set
--endpoint https://hf-mirror.com or XIAOMI_OCR_HF_ENDPOINT as fallback.
Downloads resume partial files and verify the reported size.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ID = "SeerRay-Lab/Xiaomi-OCR-0"
MODEL_DIR_NAME = REPO_ID.replace("/", "--")
DEFAULT_ENDPOINT = os.getenv("XIAOMI_OCR_HF_ENDPOINT", "https://huggingface.co")

# Minimal closure of files required to load the model locally (the repo also
# ships example pictures and eval configs that inference never touches).
REQUIRED_FILES = (
    "config.json",
    "model.safetensors",
    "processor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "chat_template.jinja",
)

MODELS_ROOT = Path(__file__).resolve().parent / "models"


def _api_url(endpoint: str) -> str:
    # blobs=true makes the hub report per-file sizes.
    return f"{endpoint.rstrip('/')}/api/models/{REPO_ID}?blobs=true"


def _resolve_url(endpoint: str, filename: str) -> str:
    return f"{endpoint.rstrip('/')}/{REPO_ID}/resolve/main/{filename}"


def _fetch_remote_sizes(endpoint: str) -> dict[str, int]:
    request = urllib.request.Request(_api_url(endpoint), headers={"User-Agent": "download-models"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    sizes: dict[str, int] = {}
    for sibling in payload.get("siblings", []):
        name = sibling.get("rfilename")
        size = sibling.get("size")
        if name and isinstance(size, int):
            sizes[name] = size
    return sizes


def _download_file(url: str, target: Path, expected_size: int | None, force: bool) -> None:
    if target.is_file() and not force:
        if expected_size is None or target.stat().st_size == expected_size:
            print(f"[skip] {target.name} ({target.stat().st_size} bytes, already present)")
            return
        print(f"[stale] {target.name}: size {target.stat().st_size} != {expected_size}, re-downloading")

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.part")
    mode = "ab" if partial.exists() else "wb"
    already = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "download-models"}
    if already:
        headers["Range"] = f"bytes={already}-"

    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=120) as response, partial.open(mode) as stream:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                stream.write(chunk)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and partial.exists():
            # Range already satisfied: the file is complete on disk.
            pass
        else:
            raise
    total = partial.stat().st_size
    if expected_size is not None and total != expected_size:
        raise RuntimeError(
            f"{target.name}: downloaded {total} bytes but hub reports {expected_size}"
        )
    os.replace(partial, target)
    elapsed = max(time.perf_counter() - started, 0.001)
    print(f"[done] {target.name}: {total} bytes in {elapsed:.1f}s ({total / 1e6 / elapsed:.1f} MB/s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download Xiaomi-OCR-0 weights from Hugging Face.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="HF endpoint (default: huggingface.co; fallback hf-mirror.com)")
    parser.add_argument("--list", action="store_true", help="List required files and exit")
    parser.add_argument("--force", action="store_true", help="Re-download even if files are present")
    args = parser.parse_args(argv)

    if args.list:
        for name in REQUIRED_FILES:
            print(name)
        return 0

    endpoint = args.endpoint.strip() or DEFAULT_ENDPOINT
    print(f"[hub] {endpoint}")
    sizes = _fetch_remote_sizes(endpoint)
    missing_hub = [name for name in REQUIRED_FILES if name not in sizes]
    if missing_hub:
        print(f"error: hub does not list required files: {missing_hub}", file=sys.stderr)
        return 2

    target_dir = MODELS_ROOT / MODEL_DIR_NAME
    for name in REQUIRED_FILES:
        _download_file(_resolve_url(endpoint, name), target_dir / name, sizes.get(name), args.force)
    print(f"[ok] model dir: {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
