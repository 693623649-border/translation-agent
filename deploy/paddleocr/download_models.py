#!/usr/bin/env python3
"""按 models.manifest.json 下载 PaddleOCR 官方推理模型权重并解压到 models/ 目录。

仅依赖 Python 标准库，宿主机（Windows/WSL）与容器内均可运行。下载完成后
每个模型位于 models/<name>/，内含 inference.json 与模型文件，可直接作为
PaddleOCR 的 *_model_dir 参数使用。

用法：
    python download_models.py --list            # 查看清单
    python download_models.py --only server     # 仅下载 server 变体（det+rec）
    python download_models.py --only mobile     # 仅下载 mobile 变体
    python download_models.py --all             # 下载全部（默认行为）
    python download_models.py --only PP-OCRv5_server_det --force  # 按名称下载并重新解压
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "models.manifest.json"
MODELS_DIR = HERE / "models"


def load_manifest() -> dict:
    with MANIFEST.open(encoding="utf-8") as f:
        return json.load(f)


def select_models(manifest: dict, only: str | None) -> list[dict]:
    models = manifest["models"]
    if not only or only == "all":
        return models
    # 先按名称精确匹配，再按变体过滤
    by_name = [m for m in models if m["name"] == only]
    if by_name:
        return by_name
    return [m for m in models if m["variant"] == only]


def is_extracted(model_dir: Path) -> bool:
    return (model_dir / "inference.json").is_file()


def download(url: str, dest: Path, retries: int = 3) -> None:
    for attempt in range(1, retries + 1):
        try:
            print(f"  下载 {url}")
            with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as f:
                shutil.copyfileobj(resp, f)
            return
        except Exception as exc:  # noqa: BLE001 - 网络错误统一重试
            print(f"  第 {attempt} 次下载失败：{exc}")
            if attempt == retries:
                raise
    raise RuntimeError("unreachable")


def extract(tar_path: Path, model_dir: Path) -> None:
    """解压 tar 包。包内通常有一层 <name>_infer/ 目录，剥掉后放入 model_dir。"""
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tar_path) as tar:
            tar.extractall(tmp)  # noqa: S202 - 来源为官方模型仓库
        extracted_root = Path(tmp)
        entries = [p for p in extracted_root.iterdir()]
        inner = entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted_root
        if model_dir.exists():
            shutil.rmtree(model_dir)
        shutil.copytree(inner, model_dir)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 PaddleOCR 官方模型权重")
    parser.add_argument("--all", action="store_true", help="下载全部模型（默认行为）")
    parser.add_argument("--only", default=None, help="server / mobile / common 或具体模型名；缺省即全部")
    parser.add_argument("--force", action="store_true", help="已解压的模型也重新下载解压")
    parser.add_argument("--list", action="store_true", help="仅列出清单")
    parser.add_argument("--models-dir", default=str(MODELS_DIR), help="权重输出目录")
    args = parser.parse_args()

    manifest = load_manifest()
    if args.list:
        for m in manifest["models"]:
            print(f"{m['name']:<28} variant={m['variant']:<8} {m['note']}")
        return 0

    models = select_models(manifest, args.only or "all")
    if not models:
        print(f"未匹配到模型（--only {args.only}）", file=sys.stderr)
        return 1

    models_dir = Path(args.models_dir)
    models_dir.mkdir(parents=True, exist_ok=True)

    failed: list[str] = []
    for m in models:
        model_dir = models_dir / m["name"]
        if is_extracted(model_dir) and not args.force:
            print(f"[跳过] {m['name']} 已存在于 {model_dir}")
            continue
        print(f"[下载] {m['name']}")
        tar_path = models_dir / f"{m['name']}.tar"
        try:
            download(m["url"], tar_path)
            extract(tar_path, model_dir)
            tar_path.unlink(missing_ok=True)
            if not is_extracted(model_dir):
                raise RuntimeError("解压后未找到 inference.json，请检查 tar 包结构")
            print(f"[完成] {m['name']} -> {model_dir}")
        except Exception as exc:  # noqa: BLE001
            failed.append(m["name"])
            print(f"[失败] {m['name']}：{exc}", file=sys.stderr)

    if failed:
        print(f"\n以下模型下载失败：{', '.join(failed)}", file=sys.stderr)
        return 1
    print("\n全部模型就绪。验证：")
    print(f"  python verify_ocr.py --models-dir \"{models_dir}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
