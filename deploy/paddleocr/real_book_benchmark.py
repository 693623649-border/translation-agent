#!/usr/bin/env python3
"""Benchmark PaddleOCR configs on rendered real book pages.

The synthetic benchmark in bench_ocr.py is useful for smoke testing. This
script measures real page images and compares every candidate against a
server/server fp32 baseline.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from bench_ocr import build_ocr

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def normalize_text(text: str) -> str:
    text = text.casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def char_agreement(a: str, b: str) -> float:
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    denom = max(len(a_norm), len(b_norm), 1)
    return max(0.0, 1.0 - (_levenshtein(a_norm, b_norm) / denom))


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    weight = pos - lo
    return values[lo] * (1 - weight) + values[hi] * weight


class VramSampler:
    def __init__(self, interval_sec: float = 0.25) -> None:
        self.interval_sec = interval_sec
        self.peak_mb: int | None = None
        self.samples_mb: list[int] = []
        self.available = True
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                        "-i",
                        "0",
                    ],
                    timeout=3,
                )
                used_mb = int(out.decode("utf-8", errors="replace").split()[0])
                self.samples_mb.append(used_mb)
                self.peak_mb = used_mb if self.peak_mb is None else max(self.peak_mb, used_mb)
            except Exception:  # noqa: BLE001 - benchmark metadata should not fail OCR.
                self.available = False
            self._stop.wait(self.interval_sec)

    def __enter__(self) -> "VramSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def get_result_value(result: Any, key: str, default: Any) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def as_list(value: Any) -> list[Any]:
    """Convert Paddle/NumPy result vectors without testing array truthiness."""
    if value is None:
        return []
    return list(value)


def normalize_box(box: Any) -> Any:
    if hasattr(box, "tolist"):
        return box.tolist()
    if isinstance(box, tuple):
        return [normalize_box(item) for item in box]
    if isinstance(box, list):
        return [normalize_box(item) for item in box]
    return box


def flatten_prediction(raw: Any) -> dict[str, Any]:
    pages = raw if isinstance(raw, list) else [raw]
    texts: list[str] = []
    scores: list[float | None] = []
    boxes: list[Any] = []
    for page in pages:
        page_texts = as_list(get_result_value(page, "rec_texts", []))
        page_scores = as_list(get_result_value(page, "rec_scores", []))
        page_boxes = as_list(get_result_value(page, "rec_boxes", []))
        texts.extend(str(item) for item in page_texts)
        scores.extend(float(item) if item is not None else None for item in page_scores)
        boxes.extend(normalize_box(item) for item in page_boxes)
    return {"rec_texts": texts, "rec_scores": scores, "rec_boxes": boxes}


def predict_one(ocr: Any, image: Path) -> tuple[dict[str, Any], float]:
    start = time.perf_counter()
    raw = list(ocr.predict(str(image)))
    elapsed = time.perf_counter() - start
    return flatten_prediction(raw), elapsed


def discover_images(images_dir: Path) -> list[Path]:
    images = [
        path
        for path in images_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(images, key=lambda path: str(path).lower())


def config_matrix(kind: str) -> list[dict[str, Any]]:
    baseline = {
        "name": "baseline-server-det32-server-rec32-b16",
        "det_variant": "server",
        "rec_variant": "server",
        "det_mode": "paddle_fp32",
        "rec_mode": "paddle_fp32",
        "rec_batch": 16,
        "det_len": 736,
        "baseline": True,
    }
    candidates = [
        ("server-det32-server-rec16-b8", "server", "server", "paddle_fp32", "paddle_fp16", 8, 736),
        ("server-det32-server-rec16-b16", "server", "server", "paddle_fp32", "paddle_fp16", 16, 736),
        ("server-det32-server-rec16-b32", "server", "server", "paddle_fp32", "paddle_fp16", 32, 736),
        ("mobile-det16-mobile-rec16-b32", "mobile", "mobile", "paddle_fp16", "paddle_fp16", 32, 736),
    ]
    if kind == "selected":
        candidates = [candidates[1]]
    if kind == "full":
        candidates.extend(
            [
                ("mobile-det32-mobile-rec16-b32", "mobile", "mobile", "paddle_fp32", "paddle_fp16", 32, 736),
                ("mobile-det16-server-rec16-b32", "mobile", "server", "paddle_fp16", "paddle_fp16", 32, 736),
                ("server-det32-mobile-rec16-b32", "server", "mobile", "paddle_fp32", "paddle_fp16", 32, 736),
            ]
        )
    configs = [baseline]
    configs.extend(
        {
            "name": name,
            "det_variant": det_variant,
            "rec_variant": rec_variant,
            "det_mode": det_mode,
            "rec_mode": rec_mode,
            "rec_batch": rec_batch,
            "det_len": det_len,
            "baseline": False,
        }
        for name, det_variant, rec_variant, det_mode, rec_mode, rec_batch, det_len in candidates
    )
    return configs


def page_text(page: dict[str, Any]) -> str:
    return "".join(page["rec_texts"])


def line_coverage(candidate_lines: list[str], baseline_lines: list[str]) -> float:
    baseline_norm = [normalize_text(line) for line in baseline_lines if normalize_text(line)]
    candidate_norm = [normalize_text(line) for line in candidate_lines if normalize_text(line)]
    if not baseline_norm:
        return 1.0
    used: set[int] = set()
    hits = 0
    for baseline_line in baseline_norm:
        best_score = 0.0
        best_index = -1
        for idx, candidate_line in enumerate(candidate_norm):
            if idx in used:
                continue
            score = char_agreement(baseline_line, candidate_line)
            if score > best_score:
                best_score = score
                best_index = idx
        if best_index >= 0 and best_score >= 0.90:
            hits += 1
            used.add(best_index)
    return hits / len(baseline_norm)


def quality_metrics(
    pages: list[dict[str, Any]],
    baseline_pages: list[dict[str, Any]],
    agreement_threshold: float,
    line_coverage_threshold: float,
) -> dict[str, Any]:
    agreements: list[float] = []
    coverages: list[float] = []
    empty_anomalies: list[dict[str, Any]] = []
    page_metrics: list[dict[str, Any]] = []
    for idx, (candidate, baseline) in enumerate(zip(pages, baseline_pages, strict=True)):
        baseline_text = page_text(baseline)
        candidate_text = page_text(candidate)
        agreement = char_agreement(candidate_text, baseline_text)
        coverage = line_coverage(candidate["rec_texts"], baseline["rec_texts"])
        baseline_nonempty = bool(normalize_text(baseline_text))
        candidate_nonempty = bool(normalize_text(candidate_text))
        if baseline_nonempty != candidate_nonempty:
            empty_anomalies.append(
                {
                    "page_index": idx,
                    "image": candidate["image"],
                    "baseline_nonempty": baseline_nonempty,
                    "candidate_nonempty": candidate_nonempty,
                }
            )
        agreements.append(agreement)
        coverages.append(coverage)
        page_metrics.append(
            {
                "image": candidate["image"],
                "char_agreement": agreement,
                "line_coverage": coverage,
                "baseline_lines": len(baseline["rec_texts"]),
                "candidate_lines": len(candidate["rec_texts"]),
            }
        )
    min_agreement = min(agreements) if agreements else 1.0
    min_coverage = min(coverages) if coverages else 1.0
    passed = (
        min_agreement >= agreement_threshold
        and min_coverage >= line_coverage_threshold
        and not empty_anomalies
    )
    return {
        "passed": passed,
        "min_char_agreement": min_agreement,
        "mean_char_agreement": statistics.fmean(agreements) if agreements else 1.0,
        "min_line_coverage": min_coverage,
        "mean_line_coverage": statistics.fmean(coverages) if coverages else 1.0,
        "empty_anomalies": empty_anomalies,
        "page_metrics": page_metrics,
    }


def summarize_timing(page_latencies: list[float], page_count: int, wall_sec: float) -> dict[str, Any]:
    return {
        "pages": page_count,
        "wall_sec": wall_sec,
        "pages_per_sec": page_count / wall_sec if wall_sec > 0 else None,
        "latency_sec_p50": percentile(page_latencies, 0.50),
        "latency_sec_p95": percentile(page_latencies, 0.95),
        "latency_sec_mean": statistics.fmean(page_latencies) if page_latencies else None,
    }


def run_config(config: dict[str, Any], models_dir: Path, images: list[Path], passes: int) -> dict[str, Any]:
    print(f">>> {config['name']}", flush=True)
    ocr = build_ocr(
        models_dir,
        config["det_variant"],
        config["rec_variant"],
        config["det_mode"],
        config["rec_mode"],
        config["rec_batch"],
        config["det_len"],
    )
    try:
        predict_one(ocr, images[0])
        gc.collect()
        page_results: list[dict[str, Any]] = []
        page_latencies: list[float] = []
        with VramSampler() as vram:
            wall_start = time.perf_counter()
            for pass_index in range(passes):
                for image in images:
                    prediction, elapsed = predict_one(ocr, image)
                    page_results.append(
                        {
                            "pass": pass_index,
                            "image": str(image),
                            "latency_sec": elapsed,
                            **prediction,
                        }
                    )
                    page_latencies.append(elapsed)
            wall_sec = time.perf_counter() - wall_start
        return {
            "config": config,
            "status": "ok",
            "timing": summarize_timing(page_latencies, len(page_results), wall_sec),
            "vram": {
                "available": vram.available,
                "peak_mb": vram.peak_mb,
                "samples_mb": vram.samples_mb,
            },
            "pages": page_results,
        }
    except Exception as exc:  # noqa: BLE001 - matrix should continue after a bad candidate.
        return {"config": config, "status": "error", "error": repr(exc), "pages": []}
    finally:
        del ocr
        gc.collect()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark PaddleOCR configs on real book page images.")
    parser.add_argument("--images-dir", required=True, help="Directory containing rendered page images.")
    parser.add_argument("--models-dir", default="/workspace/models", help="PaddleOCR model directory.")
    parser.add_argument("--output-json", required=True, help="Path for the benchmark JSON report.")
    parser.add_argument(
        "--matrix",
        choices=["selected", "quick", "full"],
        default="quick",
        help="Candidate matrix size; selected runs only the baseline and server-rec-fp16-b16.",
    )
    parser.add_argument("--passes", type=int, default=1, help="Number of timed passes over all images.")
    parser.add_argument("--agreement-threshold", type=float, default=0.997)
    parser.add_argument("--line-coverage-threshold", type=float, default=0.995)
    args = parser.parse_args()

    images_dir = Path(args.images_dir)
    models_dir = Path(args.models_dir)
    output_json = Path(args.output_json)
    if args.passes < 1:
        parser.error("--passes must be >= 1")
    images = discover_images(images_dir)
    if not images:
        parser.error(f"No images found under {images_dir}")

    results: list[dict[str, Any]] = []
    baseline_pages: list[dict[str, Any]] | None = None
    for config in config_matrix(args.matrix):
        result = run_config(config, models_dir, images, args.passes)
        if result["status"] == "ok":
            if config["baseline"]:
                result["quality"] = {
                    "passed": True,
                    "min_char_agreement": 1.0,
                    "mean_char_agreement": 1.0,
                    "min_line_coverage": 1.0,
                    "mean_line_coverage": 1.0,
                    "empty_anomalies": [],
                    "page_metrics": [],
                }
                baseline_pages = result["pages"]
            elif baseline_pages is not None:
                result["quality"] = quality_metrics(
                    result["pages"],
                    baseline_pages,
                    args.agreement_threshold,
                    args.line_coverage_threshold,
                )
            print(
                "    pages/s={pages_per_sec:.3f} p50={latency_sec_p50:.3f}s p95={latency_sec_p95:.3f}s "
                "peak_vram={peak}MB pass={passed}".format(
                    pages_per_sec=result["timing"]["pages_per_sec"] or 0.0,
                    latency_sec_p50=result["timing"]["latency_sec_p50"] or 0.0,
                    latency_sec_p95=result["timing"]["latency_sec_p95"] or 0.0,
                    peak=result["vram"]["peak_mb"],
                    passed=result.get("quality", {}).get("passed"),
                ),
                flush=True,
            )
        else:
            print(f"    ERROR {result['error']}", flush=True)
        results.append(result)

    candidates = [
        result
        for result in results
        if result["status"] == "ok" and not result["config"].get("baseline") and result.get("quality", {}).get("passed")
    ]
    selected = None
    if candidates:
        selected = max(candidates, key=lambda item: item["timing"]["pages_per_sec"] or 0.0)["config"]["name"]

    payload = {
        "images_dir": str(images_dir),
        "models_dir": str(models_dir),
        "matrix": args.matrix,
        "passes": args.passes,
        "thresholds": {
            "agreement": args.agreement_threshold,
            "line_coverage": args.line_coverage_threshold,
            "no_empty_anomalies": True,
        },
        "image_count": len(images),
        "images": [str(image) for image in images],
        "baseline": "baseline-server-det32-server-rec32-b16",
        "selected_config": selected,
        "results": results,
    }
    atomic_write_json(output_json, payload)
    print(f"Wrote {output_json}", flush=True)
    return 0 if selected else 2


if __name__ == "__main__":
    raise SystemExit(main())
