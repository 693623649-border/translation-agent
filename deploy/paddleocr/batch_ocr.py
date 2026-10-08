#!/usr/bin/env python3
"""Batch PaddleOCR images into PageRecord-compatible checkpoints.

Container usage:
  python /opt/paddleocr-tools/batch_ocr.py \
    --images-dir /workspace/io/images \
    --output-dir /workspace/io/ocr \
    --models-dir /workspace/models \
    --det-variant server --rec-variant server \
    --det-mode paddle_fp32 --rec-mode paddle_fp16 \
    --rec-batch 16 --det-len 736 \
    --model-id paddleocr-local/server-det32-server-rec16-b16
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Iterable

from bench_ocr import build_ocr

PAGE_IMAGE_RE = re.compile(r"^page_(\d+)\.(?:jpe?g|png)$", re.IGNORECASE)
BLANK_TEXT = "[空白页]"


def _page_number(path: Path) -> int:
    match = PAGE_IMAGE_RE.match(path.name)
    if not match:
        raise ValueError(f"Unsupported page image name: {path.name}")
    return int(match.group(1))


def _find_page_images(images_dir: Path) -> list[Path]:
    paths = [
        path
        for path in images_dir.iterdir()
        if path.is_file() and PAGE_IMAGE_RE.match(path.name)
    ]
    return sorted(paths, key=lambda item: (_page_number(item), item.name.lower()))


def _box_to_bounds(box: Any) -> tuple[float, float, float, float]:
    values: list[float] = []

    def collect(value: Any) -> None:
        if hasattr(value, "tolist"):
            collect(value.tolist())
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)
        elif isinstance(value, (int, float)):
            values.append(float(value))

    collect(box)
    if len(values) >= 8:
        xs = values[0::2]
        ys = values[1::2]
        return min(xs), min(ys), max(xs), max(ys)
    if len(values) >= 4:
        x1, y1, x2, y2 = values[:4]
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    return 0.0, 0.0, 0.0, 0.0


def _jsonable(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _resolve_direction(boxes: Iterable[Any], requested: str) -> str:
    if requested not in {"horizontal", "vertical", "auto"}:
        raise ValueError(f"Unsupported reading direction: {requested}")
    if requested != "auto":
        return requested
    bounds = [_box_to_bounds(box) for box in boxes]
    usable = [(x2 - x1, y2 - y1) for x1, y1, x2, y2 in bounds if x2 > x1 and y2 > y1]
    # Square glyphs and short headings are ambiguous: retain horizontal order.
    tall = sum(height >= width * 2 for width, height in usable)
    return "vertical" if usable and tall / len(usable) >= 0.6 else "horizontal"


def _detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    # Han alone cannot distinguish Japanese from Chinese reliably.
    if re.search(r"[\u3400-\u9fff]", text):
        return "und"
    return "en" if re.search(r"[A-Za-z]", text) else "und"


def _checkpoint_matches(path: Path, model_id: str, direction: str) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        notes = json.loads(record.get("notes", "{}"))
        return (record.get("ocr_model") == model_id
                and notes.get("reading_direction_requested") == direction
                and notes.get("ordering_version") == 3)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _ordered_lines(texts: Iterable[Any], scores: Iterable[Any], boxes: Iterable[Any], reading_direction: str = "horizontal") -> tuple[list[str], list[float], list[Any]]:
    rows: list[tuple[int, float, float, float, float, str, float, Any]] = []
    text_values = list(texts)
    score_values = list(scores)
    box_values = list(boxes)
    for index, text in enumerate(text_values):
        normalized = str(text).strip()
        if not normalized:
            continue
        score = score_values[index] if index < len(score_values) else 0.0
        box = box_values[index] if index < len(box_values) else None
        x1, y1, x2, y2 = _box_to_bounds(box)
        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            numeric_score = 0.0
        rows.append((index, x1, y1, x2, y2, normalized, numeric_score, _jsonable(box)))

    if not rows:
        return [], [], []

    direction = _resolve_direction([row[7] for row in rows], reading_direction)
    if direction == "vertical":
        # Split only at a blank horizontal band across ALL boxes. A short
        # column ending early must not split neighbouring continuous columns.
        min_gap = max(8.0, statistics.median(max(1.0, row[3] - row[1]) for row in rows) * 1.25)
        regions: list[list[Any]] = []
        bottom = float("-inf")
        for row in sorted(rows, key=lambda item: (item[2], item[4])):
            if not regions or row[2] - bottom >= min_gap:
                regions.append([])
            regions[-1].append(row)
            bottom = max(bottom, row[4])
        ordered = []
        for region in regions:
            columns: list[list[Any]] = []
            for row in sorted(region, key=lambda item: (-(item[1] + item[3]) / 2, item[2])):
                centre = (row[1] + row[3]) / 2
                width = max(1.0, row[3] - row[1])
                candidates = []
                for column in columns:
                    col_centre = statistics.median((item[1] + item[3]) / 2 for item in column)
                    col_width = statistics.median(max(1.0, item[3] - item[1]) for item in column)
                    distance = abs(centre - col_centre)
                    if distance <= min(width, col_width) * 0.5:
                        candidates.append((distance, column))
                if candidates:
                    min(candidates, key=lambda item: item[0])[1].append(row)
                else:
                    columns.append([row])
            columns.sort(key=lambda col: -statistics.median((item[1] + item[3]) / 2 for item in col))
            ordered.extend(row for col in columns for row in sorted(col, key=lambda item: (item[2], item[0])))
        rows = ordered
    else:
        median_height = statistics.median(max(1.0, row[4] - row[2]) for row in rows)
        row_band = max(8.0, median_height * 0.55)
        rows.sort(key=lambda row: (round(row[2] / row_band), row[1], row[2], row[0]))

    ordered_texts = [row[5] for row in rows]
    ordered_scores = [row[6] for row in rows]
    ordered_boxes = [row[7] for row in rows]
    return ordered_texts, ordered_scores, ordered_boxes


def _extract_result(raw: Any) -> dict[str, Any]:
    if isinstance(raw, list):
        if not raw:
            return {}
        raw = raw[0]
    if isinstance(raw, dict):
        return raw
    result: dict[str, Any] = {}
    for name in ("rec_texts", "rec_scores", "rec_boxes"):
        value = getattr(raw, name, None)
        if value is not None:
            result[name] = value
    return result


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _checkpoint_path(output_dir: Path, page_number: int) -> Path:
    return output_dir / "pages" / f"page_{page_number:04d}.json"


def _markdown_path(output_dir: Path, page_number: int) -> Path:
    return output_dir / "pages" / f"page_{page_number:04d}.md"


def _write_page(output_dir: Path, page_number: int, text: str, model_id: str, notes: dict[str, Any]) -> None:
    payload = {
        "pdf_page": page_number,
        "text": text,
        "language": _detect_language(text),
        "notes": json.dumps(notes, ensure_ascii=False, separators=(",", ":")),
        "ocr_model": model_id,
    }
    _write_json_atomic(_checkpoint_path(output_dir, page_number), payload)
    md_path = _markdown_path(output_dir, page_number)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = md_path.with_name(f".{md_path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
        os.replace(temporary, md_path)
    finally:
        temporary.unlink(missing_ok=True)


def _predict_one(ocr: Any, image: Path) -> dict[str, Any]:
    prediction = ocr.predict(str(image))
    if hasattr(prediction, "__iter__") and not isinstance(prediction, (dict, list, tuple, str, bytes)):
        prediction = list(prediction)
    return _extract_result(prediction)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch PaddleOCR page images into PageRecord JSON files.")
    parser.add_argument("--images-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--det-variant", default="server", choices=("server", "mobile"))
    parser.add_argument("--rec-variant", default="server", choices=("server", "mobile"))
    parser.add_argument("--det-mode", default="paddle_fp32")
    parser.add_argument("--rec-mode", default="paddle_fp16")
    parser.add_argument("--rec-batch", type=int, default=16)
    parser.add_argument("--det-len", type=int, default=736)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--reading-direction", choices=("horizontal", "vertical", "auto"), default="horizontal")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.shard_count < 1:
        raise SystemExit("--shard-count must be >= 1")
    if args.shard_index < 0 or args.shard_index >= args.shard_count:
        raise SystemExit("--shard-index must satisfy 0 <= index < shard-count")
    if args.rec_batch < 1:
        raise SystemExit("--rec-batch must be >= 1")
    if not args.images_dir.is_dir():
        raise SystemExit(f"Images directory does not exist: {args.images_dir}")
    if not args.models_dir.is_dir():
        raise SystemExit(f"Models directory does not exist: {args.models_dir}")

    all_images = _find_page_images(args.images_dir)
    images = [
        image
        for offset, image in enumerate(all_images)
        if offset % args.shard_count == args.shard_index
    ]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "model_id": args.model_id,
        "images_dir": str(args.images_dir),
        "output_dir": str(args.output_dir),
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "total_images": len(all_images),
        "shard_images": len(images),
        "processed": 0,
        "skipped": 0,
        "blank": 0,
        "failed": 0,
        "failures": [],
        "elapsed_seconds": 0.0,
    }

    started = time.perf_counter()
    if not images:
        summary["elapsed_seconds"] = 0.0
        summary_path = args.output_dir / f"batch_summary_shard_{args.shard_index:02d}.json"
        _write_json_atomic(summary_path, summary)
        print(f"[batch-ocr] no images for shard {args.shard_index}/{args.shard_count}", flush=True)
        return 0

    print(
        "[batch-ocr] building OCR "
        f"det={args.det_variant}/{args.det_mode} "
        f"rec={args.rec_variant}/{args.rec_mode} "
        f"batch={args.rec_batch} det_len={args.det_len}",
        flush=True,
    )
    ocr = build_ocr(
        args.models_dir,
        args.det_variant,
        args.rec_variant,
        args.det_mode,
        args.rec_mode,
        args.rec_batch,
        args.det_len,
    )

    for image in images:
        page_number = _page_number(image)
        checkpoint = _checkpoint_path(args.output_dir, page_number)
        if not args.force and _checkpoint_matches(checkpoint, args.model_id, args.reading_direction):
            summary["skipped"] += 1
            print(f"[batch-ocr] skip page={page_number} existing={checkpoint}", flush=True)
            continue

        page_started = time.perf_counter()
        try:
            result = _predict_one(ocr, image)
            texts, scores, boxes = _ordered_lines(
                result.get("rec_texts", []),
                result.get("rec_scores", []),
                result.get("rec_boxes", []),
                args.reading_direction,
            )
            text = "\n".join(texts).strip()
            if not text:
                text = BLANK_TEXT
                summary["blank"] += 1

            elapsed = time.perf_counter() - page_started
            notes = {
                "engine": "paddleocr-local",
                "source_image": image.name,
                "elapsed_seconds": round(elapsed, 3),
                "line_count": len(texts),
                "score_min": round(min(scores), 6) if scores else None,
                "score_avg": round(sum(scores) / len(scores), 6) if scores else None,
                "box_count": len(boxes),
                "ordering_version": 3,
                "reading_direction_requested": args.reading_direction,
                "reading_direction": _resolve_direction(boxes, args.reading_direction),
                "lines": [{"text": line, "score": score, "box": box}
                          for line, score, box in zip(texts, scores, boxes)],
            }
            _write_page(args.output_dir, page_number, text, args.model_id, notes)
            summary["processed"] += 1
            print(
                f"[batch-ocr] page={page_number} lines={len(texts)} chars={len(text)} elapsed={elapsed:.2f}s",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            failure = {
                "page": page_number,
                "image": str(image),
                "error": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
            summary["failures"].append(failure)
            print(f"[batch-ocr] failed page={page_number}: {exc}", file=sys.stderr, flush=True)

    summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    summary_path = args.output_dir / f"batch_summary_shard_{args.shard_index:02d}.json"
    _write_json_atomic(summary_path, summary)
    print(
        "[batch-ocr] summary "
        f"processed={summary['processed']} skipped={summary['skipped']} "
        f"blank={summary['blank']} failed={summary['failed']} "
        f"elapsed={summary['elapsed_seconds']}s",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
