#!/usr/bin/env python3
"""固定种子分层抽取 OCR 质量样本，并以 450 DPI（约 6.25x）渲染校对切片。

数据流：
pages/page_*.json --(过滤非空白有效页)--> 按全书区间分层
  --(random.Random(seed) 每层抽取)--> 样本页清单
  --(PyMuPDF 450 DPI 裁切上半页)--> work/.../ocr_gold_pages/page_XXXX.png
  --(人工按图转写 gold)--> audit/ocr-quality-samples.json

脚本只生成抽样清单和校对图，不生成 gold，避免把模型输出当作人工证据。
支持非空分层自适应合并，在候选页不足 10 区间时仍能稳定抽取。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import pymupdf

try:
    from translation_agent.pipeline.book_pipeline import PageStore
except ModuleNotFoundError:  # pragma: no cover
    _ROOT = Path(__file__).resolve().parents[1]
    if (_ROOT / "translation_agent").is_dir():
        sys.path.insert(0, str(_ROOT))
    from translation_agent.pipeline.book_pipeline import PageStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260923)
    parser.add_argument("--dpi", type=int, default=450, help="校对切片渲染 DPI，默认 450 (6.25x)")
    parser.add_argument("--min-chars", type=int, default=100, help="候选页最小文本字符数，默认 100")
    parser.add_argument("--samples", type=int, default=10, help="抽样目标数量，默认 10")
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    work_dir = args.work_dir.expanduser().resolve()
    image_dir = work_dir / "ocr_gold_pages"
    image_dir.mkdir(parents=True, exist_ok=True)

    records = PageStore(output_dir).load_all()
    candidates = [
        record.pdf_page
        for record in records
        if record.pdf_page >= 10
        and len(record.text.strip()) >= args.min_chars
        and record.text.strip() not in {"[空白页]", "[BLANK]"}
    ]
    if len(candidates) < 3:
        raise RuntimeError(f"not enough candidate body pages (need >=3): {len(candidates)}")

    target_count = min(args.samples, len(candidates))
    start, end = min(candidates), max(candidates)
    buckets: list[list[int]] = [[] for _ in range(target_count)]
    for page in candidates:
        index = min(target_count - 1, target_count * (page - start) // max(1, end - start + 1))
        buckets[index].append(page)

    rng = random.Random(args.seed)
    selected = []
    # 允许空桶回退到候选池，避免因局部空白直接崩溃
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        selected.append({"decile": index, "pdf_page": rng.choice(bucket), "candidates": len(bucket)})

    # 若非空桶少于目标，从全局剩余候选按随机种子补齐
    chosen_pages = {s["pdf_page"] for s in selected}
    remaining = [p for p in candidates if p not in chosen_pages]
    rng.shuffle(remaining)
    while len(selected) < target_count and remaining:
        p = remaining.pop()
        selected.append({"decile": -1, "pdf_page": p, "candidates": len(candidates)})
    selected.sort(key=lambda item: item["pdf_page"])

    document = pymupdf.open(args.pdf.expanduser().resolve())
    for item in selected:
        page = document.load_page(item["pdf_page"] - 1)
        # 上部 60% 覆盖竖排开头的右起数列；足够逐字核对小假名与标点。
        clip = pymupdf.Rect(0, 0, page.rect.width, page.rect.height * 0.60)
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(args.dpi / 72, args.dpi / 72), clip=clip, alpha=False)
        path = image_dir / f"page_{item['pdf_page']:04d}.png"
        pixmap.save(path)
        item["image"] = str(path)
    document.close()

    manifest = {
        "seed": args.seed,
        "dpi": args.dpi,
        "candidate_range": [start, end],
        "candidate_count": len(candidates),
        "samples": selected,
    }
    path = work_dir / "ocr_sample_pages.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
