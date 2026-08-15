#!/usr/bin/env python3
"""Benchmark the isolated PaddleOCR service on representative PDF pages."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import fitz

from book_pipeline import render_pdf_page
from ocr_backends.paddle_local import PaddleLocalOCR
from local_ocr.runtime_paths import ensure_private_directory
from pipeline_profiles import load_pipeline_profiles


def _pages(value: str, total: int) -> list[int]:
    if not value.strip():
        return list(range(1, min(total, 100) + 1))
    selected: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            left, right = token.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(token)
        if start < 1 or end < start or end > total:
            raise ValueError(f"invalid page range {token!r}; PDF has {total} pages")
        selected.update(range(start, end + 1))
    if not selected:
        raise ValueError("at least one benchmark page is required")
    return sorted(selected)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure PaddleOCR render and inference throughput without publishing artifacts."
    )
    parser.add_argument("pdf")
    parser.add_argument("--config", default="pipeline.local-gpu.toml")
    parser.add_argument("--profile", default="")
    parser.add_argument(
        "--pages",
        default="",
        help="Comma-separated PDF pages/ranges; default is the first up to 100 pages.",
    )
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--render-workers", type=int, default=8)
    parser.add_argument("--instances-per-device", type=int)
    parser.add_argument("--rec-batch", type=int)
    parser.add_argument("--warmup", type=int, default=4)
    # Match book_pipeline.py so a benchmark without render overrides measures
    # the same image inputs as a normal OCR run.
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-image-side", type=int, default=3000)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--output", help="Optional JSON result path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdf_path = Path(args.pdf).expanduser().resolve(strict=True)
    with fitz.open(pdf_path) as document:
        page_numbers = _pages(args.pages, document.page_count)

    profiles = load_pipeline_profiles(args.config)
    profile = profiles.get(args.profile) if args.profile else profiles.for_stage("ocr")
    if profile is None or profile.adapter != "paddleocr-local":
        raise ValueError("benchmark requires a paddleocr-local OCR Profile")
    runtime = dict(profile.runtime)
    if args.instances_per_device is not None:
        runtime["instances_per_device"] = max(1, args.instances_per_device)
    if args.rec_batch is not None:
        runtime["text_recognition_batch_size"] = max(1, args.rec_batch)
    run_id = uuid.uuid4().hex
    staging = Path(tempfile.mkdtemp(prefix="translation-agent-paddle-bench-", dir="/tmp"))
    runtime.update(
        {
            "auto_start": True,
            "persistent": False,
            "socket_path": str(staging / "paddle.sock"),
            "spool_dir": str(staging / "spool"),
        }
    )
    profile = replace(profile, runtime=runtime, base_url=f"unix://{staging / 'paddle.sock'}")
    image_dir = ensure_private_directory(staging / "images")
    render_started = time.perf_counter()
    client: PaddleLocalOCR | None = None
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.render_workers)) as executor:
            futures = {
                executor.submit(
                    render_pdf_page,
                    pdf_path,
                    page,
                    image_dir / f"page_{page:04d}.jpg",
                    dpi=args.dpi,
                    max_side=args.max_image_side,
                    quality=args.jpeg_quality,
                    optimize=False,
                ): page
                for page in page_numbers
            }
            for future in as_completed(futures):
                future.result()
        render_seconds = time.perf_counter() - render_started

        client = PaddleLocalOCR.from_profile(
            profile,
            reading_direction=profile.reading_direction or "horizontal",
            horizontal_columns=int(profile.content.get("horizontal_columns", 1)),
            dpi=args.dpi,
            max_image_side=args.max_image_side,
            jpeg_quality=args.jpeg_quality,
        )
        warmup_pages = page_numbers[: max(0, min(args.warmup, len(page_numbers)))]
        for page in warmup_pages:
            client.ocr_image(image_dir / f"page_{page:04d}.jpg")

        inference_started = time.perf_counter()
        latencies: list[float] = []
        character_counts: list[int] = []
        scores: list[float] = []

        def infer(page: int) -> tuple[float, int, float]:
            started = time.perf_counter()
            text, _ = client.ocr_image(image_dir / f"page_{page:04d}.jpg")
            elapsed = time.perf_counter() - started
            metadata = getattr(text, "ocr_metadata", {})
            return elapsed, len(text), float(metadata.get("mean_score", 0.0))

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = [executor.submit(infer, page) for page in page_numbers]
            for future in as_completed(futures):
                elapsed, characters, score = future.result()
                latencies.append(elapsed)
                character_counts.append(characters)
                scores.append(score)
        inference_seconds = time.perf_counter() - inference_started
        health = client.health()

        result: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "pdf": str(pdf_path),
            "pages": page_numbers,
            "page_count": len(page_numbers),
            "model_identity": client.ocr_model,
            "service_identity": client.service_identity,
            "devices": list(runtime.get("devices", ())),
            "instances_per_device": int(runtime.get("instances_per_device", 1)),
            "text_recognition_batch_size": int(
                runtime.get("text_recognition_batch_size", 1)
            ),
            "workers": max(1, args.workers),
            "render": {
                "dpi": args.dpi,
                "max_image_side": args.max_image_side,
                "jpeg_quality": args.jpeg_quality,
                "workers": max(1, args.render_workers),
                "seconds": render_seconds,
                "pages_per_second": len(page_numbers) / render_seconds,
            },
            "inference": {
                "seconds": inference_seconds,
                "pages_per_second": len(page_numbers) / inference_seconds,
                "latency_mean_seconds": statistics.mean(latencies),
                "latency_p50_seconds": _percentile(latencies, 0.50),
                "latency_p95_seconds": _percentile(latencies, 0.95),
                "characters": sum(character_counts),
                "mean_confidence": statistics.mean(scores),
            },
            "health": health,
        }
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        print(encoded, end="")
        if args.output:
            output = Path(args.output).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        return 0
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            shutil.rmtree(staging)


if __name__ == "__main__":
    raise SystemExit(main())
