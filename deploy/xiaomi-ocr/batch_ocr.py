#!/usr/bin/env python3
"""Batch Xiaomi-OCR-0 page images into PageRecord-compatible checkpoints.

Container usage (script is volume-mounted read-only at run time):
  python3 /opt/xiaomi-ocr-tools/batch_ocr.py \
    --images-dir /workspace/io/<job>/images \
    --output-dir /workspace/io/<job> \
    --models-dir /workspace/models \
    --task text \
    --model-id xiaomi-ocr-local/Xiaomi-OCR-0-text-dpi200-max3000-horizontal-v3

Runs the model through vLLM's offline engine (single process, no HTTP
service), submitting every pending page in one ``llm.chat()`` call so vLLM
batches them internally.  Prompts, sampling settings (greedy + top_p 0.8 +
non-thinking chat template), image pre-fit (LANCZOS to 2048px) and the
repetition collapse guard / retry are ported from the model's official
pipeline (github.com/SeerRay-Lab/Xiaomi-OCR-0, Apache-2.0).

The per-page output JSON contract matches the local PaddleOCR batch script:
{"pdf_page", "text", "language", "notes", "ocr_model"}.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any

PAGE_IMAGE_RE = re.compile(r"^page_(\d+)\.(?:jpe?g|png)$", re.IGNORECASE)
BLANK_TEXT = "[空白页]"
MAX_IMAGE_EDGE = 2048
MODEL_REPO_ID = "SeerRay-Lab/Xiaomi-OCR-0"
MODEL_DIR_NAME = MODEL_REPO_ID.replace("/", "--")
MAX_MODEL_LEN = 16384

# Verbatim task prompts from the official pipeline (e2e_img2md.py).
PROMPTS = {
    "text": "Extract the text in the image.",
    "table": "Parse the table in the image into OTSL.",
    "formula": "Identify the formula in the image and represent it using LATEX format.",
    "document": (
        "Extract all information from the main body of the document image and represent it "
        "in markdown format, ignoring headers and footers. Tables should be expressed in OTSL "
        "format, formulas in the document should be represented using LATEX format, and the "
        "parsing should be organized according to the reading order."
    ),
}


# ─────────────────────────── official preprocessing ───────────────────────────


def fit_image(image: Any, max_edge: int = MAX_IMAGE_EDGE) -> Any:
    """Downscale to the model's input bound preserving aspect ratio (official fit_image)."""
    if image.width > max_edge or image.height > max_edge:
        image.thumbnail((max_edge, max_edge))
    return image


def encode_image_base64(image_path: Path) -> str:
    """Encode an RGB PNG within the model's 2048x2048 input bound (official encode)."""
    from PIL import Image

    with Image.open(image_path) as source:
        image = fit_image(source.convert("RGB"))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ─────────────────────── official repetition collapse guard ──────────────────


def _normalize_whitespace(text: str) -> str:
    return " ".join(text.split())


def _longest_same_character_run(text: str) -> dict[str, Any]:
    best_character = ""
    best_length = 0
    current_character = ""
    current_length = 0
    for character in text:
        if character.isspace():
            current_character = ""
            current_length = 0
        elif character == current_character:
            current_length += 1
        else:
            current_character = character
            current_length = 1
        if current_length > best_length:
            best_character = character
            best_length = current_length
    return {"character": best_character, "length": best_length}


def _repeating_suffix(
    text: str,
    min_repeats: int,
    min_repeated_chars: int,
    min_ratio: float,
    max_unit_chars: int,
) -> dict[str, Any] | None:
    if len(text) < min_repeated_chars:
        return None
    best: dict[str, Any] | None = None
    max_unit = min(max_unit_chars, len(text) // min_repeats)
    for unit_length in range(1, max_unit + 1):
        unit = text[-unit_length:]
        cursor = len(text) - unit_length
        count = 1
        while cursor >= unit_length and text[cursor - unit_length:cursor] == unit:
            cursor -= unit_length
            count += 1
        repeated_chars = count * unit_length
        ratio = repeated_chars / len(text)
        if count < min_repeats or repeated_chars < min_repeated_chars or ratio < min_ratio:
            continue
        candidate = {
            "unit": unit,
            "unit_chars": unit_length,
            "repeats": count,
            "repeated_chars": repeated_chars,
            "ratio": ratio,
        }
        if best is None or repeated_chars > best["repeated_chars"]:
            best = candidate
    return best


def _dominant_repeated_line(
    text: str,
    min_repeats: int,
    min_repeated_chars: int,
    min_ratio: float,
) -> dict[str, Any] | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < min_repeats:
        return None
    line, count = Counter(lines).most_common(1)[0]
    repeated_chars = len(line) * count
    ratio = count / len(lines)
    if count < min_repeats or repeated_chars < min_repeated_chars or ratio < min_ratio:
        return None
    return {
        "line": line,
        "repeats": count,
        "total_lines": len(lines),
        "repeated_chars": repeated_chars,
        "ratio": ratio,
    }


def analyze_repetition(
    text: str,
    finish_reason: str | None = None,
    same_character_run: int = 128,
    min_repeats: int = 8,
    min_repeated_chars: int = 512,
    min_ratio: float = 0.5,
    max_unit_chars: int = 256,
) -> dict[str, Any]:
    """Official repetition_guard.analyze_repetition (verbatim logic)."""
    normalized = _normalize_whitespace(text)
    non_whitespace_length = len(normalized.replace(" ", ""))
    reasons: list[str] = []
    if finish_reason == "length":
        reasons.append("finish_reason_length")
    same_character = _longest_same_character_run(text)
    same_character_ratio = same_character["length"] / non_whitespace_length if non_whitespace_length else 0.0
    if same_character["length"] >= same_character_run and (
        same_character["length"] >= min_repeated_chars or same_character_ratio >= min_ratio
    ):
        reasons.append("same_character_run")
    suffix = _repeating_suffix(
        normalized,
        min_repeats=min_repeats,
        min_repeated_chars=min_repeated_chars,
        min_ratio=min_ratio,
        max_unit_chars=max_unit_chars,
    )
    if suffix is not None:
        reasons.append("repeating_suffix")
    repeated_line = _dominant_repeated_line(
        text, min_repeats=min_repeats, min_repeated_chars=min_repeated_chars, min_ratio=max(min_ratio, 0.8)
    )
    if repeated_line is not None:
        reasons.append("dominant_repeated_line")
    return {
        "suspicious": bool(reasons),
        "reasons": reasons,
        "characters": len(text),
        "non_whitespace_characters": non_whitespace_length,
        "finish_reason": finish_reason,
    }


def select_ocr_candidate(
    primary: dict[str, Any], retry: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """Return only a clean candidate as usable; retain diagnostics for failures."""
    primary_text = str(primary.get("text") or "").strip()
    primary_analysis = analyze_repetition(primary_text, finish_reason=primary.get("finish_reason"))
    retry_text = str((retry or {}).get("text") or "").strip()
    retry_analysis = (
        analyze_repetition(retry_text, finish_reason=retry.get("finish_reason"))
        if retry is not None and retry_text
        else None
    )
    retry_needed = not primary_text or primary_analysis["suspicious"]
    if not retry_needed:
        return primary_text, {
            "status": "passed", "hard_failure": False,
            "primary_finish_reason": primary.get("finish_reason"),
            "primary_reasons": [], "retry_attempted": retry is not None,
            "retry_finish_reason": retry.get("finish_reason") if retry else None,
            "retry_reasons": retry_analysis["reasons"] if retry_analysis else [],
            "retry_adopted": False,
        }
    if retry_analysis is not None and not retry_analysis["suspicious"]:
        return retry_text, {
            "status": "passed", "hard_failure": False,
            "primary_finish_reason": primary.get("finish_reason"),
            "primary_reasons": primary_analysis["reasons"] or ["empty_primary"],
            "retry_attempted": True,
            "retry_finish_reason": retry.get("finish_reason"),
            "retry_reasons": [], "retry_adopted": True,
        }
    reasons = list(primary_analysis["reasons"])
    if not primary_text:
        reasons.append("empty_primary")
    if retry is None:
        reasons.append("retry_missing")
    elif not retry_text:
        reasons.append("retry_empty")
    elif retry_analysis is not None:
        reasons.extend(f"retry_{reason}" for reason in retry_analysis["reasons"])
    return retry_text or primary_text, {
        "status": "failed", "hard_failure": True,
        "failure_reason": ";".join(dict.fromkeys(reasons)) or "unresolved_candidate",
        "primary_finish_reason": primary.get("finish_reason"),
        "primary_reasons": primary_analysis["reasons"],
        "retry_attempted": retry is not None,
        "retry_finish_reason": retry.get("finish_reason") if retry else None,
        "retry_reasons": retry_analysis["reasons"] if retry_analysis else (["empty_retry"] if retry else []),
        "retry_adopted": False,
    }


def clean_truncated_repeats(
    text: str,
    min_text_len: int = 8000,
    max_period: int = 200,
    min_period: int = 1,
    min_repeat_chars: int = 100,
    min_repeat_times: int = 5,
) -> str:
    """Official _clean_truncated_repeats: keep one period plus the remainder."""
    n = len(text)
    if n < min_text_len:
        return text
    max_period = min(max_period, n - 1)
    for unit_len in range(min_period, max_period + 1):
        if text[n - 1] != text[n - 1 - unit_len]:
            continue
        match_len = 1
        idx = n - 2
        while idx >= unit_len and text[idx] == text[idx - unit_len]:
            match_len += 1
            idx -= 1
        total_len = match_len + unit_len
        repeat_times = total_len // unit_len
        tail_len = total_len % unit_len
        if repeat_times >= min_repeat_times and total_len >= min_repeat_chars:
            return text[: n - total_len + unit_len] + text[n - tail_len:]
    return text


# ────────────────── official markdown postprocess (document task) ─────────────

OTSL_NL = "<nl>"
OTSL_FCEL = "<fcel>"
OTSL_ECEL = "<ecel>"
OTSL_LCEL = "<lcel>"
OTSL_UCEL = "<ucel>"
OTSL_XCEL = "<xcel>"
OTSL_FIND_PATTERN = re.compile(
    r"(?:<fcel>|<ecel>|<nl>|<lcel>|<ucel>|<xcel>).*?(?=(?:<fcel>|<ecel>|<nl>|<lcel>|<ucel>|<xcel>)|$)",
    flags=re.DOTALL,
)
OTSL_TAG_RE = re.compile(r"<(?:fcel|ecel|nl|lcel|ucel|xcel)>")


def _otsl_pad_to_sqr(otsl_str: str) -> str:
    otsl_str = otsl_str.strip()
    if OTSL_NL not in otsl_str:
        return otsl_str + OTSL_NL
    rows: list[dict[str, Any]] = []
    for line in otsl_str.split(OTSL_NL):
        if not line:
            continue
        cells = OTSL_FIND_PATTERN.findall(line)
        if not cells:
            continue
        min_len = 0
        for idx, cell in enumerate(cells):
            if cell.startswith(OTSL_FCEL):
                min_len = idx + 1
        rows.append({"cells": cells, "min_len": min_len})
    if not rows:
        return OTSL_NL
    width = max(max(row["min_len"] for row in rows), max(len(row["cells"]) for row in rows))
    repaired = []
    for row in rows:
        cells = row["cells"][:width]
        cells += [OTSL_ECEL] * (width - len(cells))
        repaired.append("".join(cells))
    return OTSL_NL.join(repaired) + OTSL_NL


def _otsl_to_html(otsl_content: str) -> str:
    import html

    otsl_content = _otsl_pad_to_sqr(otsl_content)
    row_items: list[list[tuple[str, str]]] = []
    for line in otsl_content.split(OTSL_NL):
        if not line.strip():
            continue
        items: list[tuple[str, str]] = []
        for cell in OTSL_FIND_PATTERN.findall(line):
            match = re.match(r"(<fcel>|<ecel>|<lcel>|<ucel>|<xcel>)(.*)", cell, flags=re.DOTALL)
            if match:
                items.append((match.group(1), match.group(2)))
        if items:
            row_items.append(items)
    if not row_items:
        return ""
    max_cols = max(len(row) for row in row_items)
    for row in row_items:
        row.extend([(OTSL_ECEL, "")] * (max_cols - len(row)))
    used: set[tuple[int, int]] = set()
    body = ""
    for row_idx, row in enumerate(row_items):
        body += "<tr>"
        for col_idx, (token, text) in enumerate(row):
            if (row_idx, col_idx) in used or token not in {OTSL_FCEL, OTSL_ECEL}:
                continue
            colspan = 1
            while col_idx + colspan < max_cols and row[col_idx + colspan][0] in {OTSL_LCEL, OTSL_XCEL}:
                used.add((row_idx, col_idx + colspan))
                colspan += 1
            rowspan = 1
            while row_idx + rowspan < len(row_items) and row_items[row_idx + rowspan][col_idx][0] in {OTSL_UCEL, OTSL_XCEL}:
                used.add((row_idx + rowspan, col_idx))
                rowspan += 1
            attrs = ""
            if rowspan > 1:
                attrs += f' rowspan="{rowspan}"'
            if colspan > 1:
                attrs += f' colspan="{colspan}"'
            body += f"<td{attrs}>{html.escape(text.strip())}</td>"
        body += "</tr>"
    return f"<table>{body}</table>"


def postprocess_math_delimiters(markdown: str) -> str:
    """Official postprocess_math_delimiters (verbatim logic)."""
    if not markdown:
        return markdown
    markdown = markdown.replace("\\[\\[", "\\[").replace("\\]\\]", "\\]")
    markdown = re.sub(r"\$\$?\s*\\\[\s*(.*?)\s*\\\]\s*\$\$?", r" $$ \1 $$ ", markdown, flags=re.DOTALL)
    markdown = re.sub(r"\$\s*\\\(\s*(.*?)\s*\\\)\s*\$", r" $ \1 $", markdown, flags=re.DOTALL)
    markdown = re.sub(r"\\\[\s*(.*?)\s*\\\]", r" $$ \1 $$ ", markdown, flags=re.DOTALL)
    markdown = re.sub(r"\\\(\s*(.*?)\s*\\\)", r" $ \1 $", markdown, flags=re.DOTALL)
    return markdown


def _split_markdown_blocks(markdown: str) -> list[list[str]]:
    parts = re.split(r"(\n\s*\n)", markdown)
    blocks = []
    for i in range(0, len(parts), 2):
        text = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        blocks.append([text, sep])
    return blocks


def postprocess_otsl_tables(markdown: str) -> str:
    if not markdown or not OTSL_TAG_RE.search(markdown):
        return markdown
    blocks = _split_markdown_blocks(markdown)
    in_fence = False
    converted = []
    for text, sep in blocks:
        stripped = text.strip()
        fence_count = stripped.count("```")
        should_skip = in_fence
        if fence_count % 2 == 1:
            in_fence = not in_fence
            should_skip = True
        if not should_skip and OTSL_TAG_RE.search(stripped):
            try:
                table_html = _otsl_to_html(stripped)
                if table_html:
                    text = table_html
            except Exception:  # noqa: BLE001 - official behaviour keeps original on failure
                pass
        converted.append(text + sep)
    return "".join(converted)


def postprocess_output(raw: str, task: str) -> str:
    text = clean_truncated_repeats(raw)
    if task == "document":
        text = postprocess_math_delimiters(text)
        text = postprocess_otsl_tables(text)
    return text


# ─────────────────────────────── page plumbing ────────────────────────────────


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


def _detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\uac00-\ud7af]", text):
        return "ko"
    if re.search(r"[\u3400-\u9fff]", text):
        return "und"
    return "en" if re.search(r"[A-Za-z]", text) else "und"


def _checkpoint_matches(path: Path, model_id: str, task: str) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        notes = json.loads(record.get("notes", "{}"))
        text = str(record.get("text") or "").strip()
        return (
            record.get("ocr_model") == model_id
            and notes.get("task") == task
            and notes.get("engine") == "xiaomi-ocr-local"
            and notes.get("engine_version") == 3
            and notes.get("status") != "failed"
            and notes.get("hard_failure") is not True
            and notes.get("finish_reason") != "length"
            and (text != BLANK_TEXT or notes.get("visual_blank") is True)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True)
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


def _write_page(
    output_dir: Path,
    page_number: int,
    text: str,
    model_id: str,
    notes: dict[str, Any],
) -> None:
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


# ─────────────────────────────── vLLM engine ──────────────────────────────────


def build_engine(models_dir: Path, gpu_memory_utilization: float, max_num_seqs: int) -> Any:
    from vllm import LLM

    model_path = models_dir / MODEL_DIR_NAME
    if not (model_path / "config.json").is_file():
        raise SystemExit(f"model weights not found under {model_path}; run download_models.py first")
    return LLM(
        model=str(model_path),
        max_model_len=MAX_MODEL_LEN,
        gpu_memory_utilization=gpu_memory_utilization,
        max_num_seqs=max_num_seqs,
        trust_remote_code=True,
    )


def _chat_messages(data_urls: list[str], prompt: str) -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ]
        for url in data_urls
    ]


def run_batch(
    engine: Any,
    images: list[Path],
    prompt: str,
    *,
    max_tokens: int,
    presence_penalty: float,
) -> list[dict[str, Any]]:
    """One engine pass over pending pages; returns per-page dicts."""
    from vllm import SamplingParams

    data_urls = [f"data:image/png;base64,{encode_image_base64(path)}" for path in images]
    sampling = SamplingParams(
        temperature=0.0,
        top_p=0.8,
        max_tokens=max_tokens,
        presence_penalty=presence_penalty,
    )
    outputs = engine.chat(
        _chat_messages(data_urls, prompt),
        sampling_params=sampling,
        use_tqdm=False,
        chat_template_kwargs={"enable_thinking": False},
    )
    results: list[dict[str, Any]] = []
    for output in outputs:
        generated = output.outputs[0]
        results.append(
            {
                "text": generated.text.strip(),
                "finish_reason": generated.finish_reason,
                "completion_tokens": len(generated.token_ids),
            }
        )
    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch Xiaomi-OCR-0 page images into PageRecord JSON files.")
    parser.add_argument("--images-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--task", default="text", choices=tuple(PROMPTS), help="OCR task prompt (default: text)")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--max-tokens", type=int, default=4096, help="Output budget per page (default: 4096)")
    parser.add_argument("--gpu-memory-utilization", type=float, default=0.45)
    parser.add_argument("--max-num-seqs", type=int, default=16)
    parser.add_argument("--retry-presence-penalty", type=float, default=1.5)
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
    if args.max_tokens < 256:
        raise SystemExit("--max-tokens must be >= 256")
    if not 0.05 <= args.gpu_memory_utilization <= 0.95:
        raise SystemExit("--gpu-memory-utilization must be in [0.05, 0.95]")
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
        "task": args.task,
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
        "repetition_retried": 0,
        "repetition_adopted": 0,
        "failures": [],
        "elapsed_seconds": 0.0,
    }

    started = time.perf_counter()
    pending = []
    for image in images:
        page_number = _page_number(image)
        checkpoint = _checkpoint_path(args.output_dir, page_number)
        if not args.force and _checkpoint_matches(checkpoint, args.model_id, args.task):
            summary["skipped"] += 1
            print(f"[batch-ocr] skip page={page_number} existing={checkpoint}", flush=True)
        else:
            pending.append((page_number, image))

    if not pending:
        summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        summary_path = args.output_dir / f"batch_summary_shard_{args.shard_index:02d}.json"
        _write_json_atomic(summary_path, summary)
        print(f"[batch-ocr] nothing to do for shard {args.shard_index}/{args.shard_count}", flush=True)
        return 0

    print(
        f"[batch-ocr] building vLLM engine model={MODEL_DIR_NAME} task={args.task} "
        f"gpu_mem={args.gpu_memory_utilization} max_seqs={args.max_num_seqs}",
        flush=True,
    )
    engine = build_engine(args.models_dir, args.gpu_memory_utilization, args.max_num_seqs)
    prompt = PROMPTS[args.task]

    pages = [page for page, _ in pending]
    pending_images = [image for _, image in pending]
    print(f"[batch-ocr] primary pass pages={len(pages)}", flush=True)
    pass_started = time.perf_counter()
    try:
        primary = run_batch(
            engine,
            pending_images,
            prompt,
            max_tokens=args.max_tokens,
            presence_penalty=0.0,
        )
    except Exception as exc:  # noqa: BLE001
        summary["failed"] = len(pending)
        summary["failures"].append({"pages": pages[:20], "error": str(exc), "traceback": traceback.format_exc(limit=8)})
        summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(args.output_dir / f"batch_summary_shard_{args.shard_index:02d}.json", summary)
        print(f"[batch-ocr] engine failure: {exc}", file=sys.stderr, flush=True)
        return 1
    print(f"[batch-ocr] primary pass done in {time.perf_counter() - pass_started:.1f}s", flush=True)

    # Repetition collapse retry (official mechanism): suspicious pages are
    # re-generated once with presence_penalty; the retry wins only if it is
    # itself clean, otherwise the primary output is kept.
    retry_indices = [
        index
        for index, item in enumerate(primary)
        if not str(item.get("text") or "").strip()
        or analyze_repetition(item["text"], finish_reason=item["finish_reason"])["suspicious"]
    ]
    retried: dict[int, dict[str, Any]] = {}
    retry_batch_error: str | None = None
    if retry_indices:
        summary["repetition_retried"] = len(retry_indices)
        print(f"[batch-ocr] repetition retry pages={[pages[i] for i in retry_indices]}", flush=True)
        try:
            retry_results = run_batch(
                engine,
                [pending_images[i] for i in retry_indices],
                prompt,
                max_tokens=args.max_tokens,
                presence_penalty=args.retry_presence_penalty,
            )
            for slot, index in enumerate(retry_indices):
                retried[index] = retry_results[slot]
        except Exception as exc:  # noqa: BLE001
            retry_batch_error = f"{type(exc).__name__}: {exc}"
            print(f"[batch-ocr] repetition retry failed: {retry_batch_error}", file=sys.stderr, flush=True)

    for index, (page_number, image) in enumerate(pending):
        item = primary[index]
        retry_item = retried.get(index)
        text, candidate = select_ocr_candidate(item, retry_item)
        candidate["primary_candidate_text"] = str(item.get("text") or "").strip()
        candidate["retry_candidate_text"] = str((retry_item or {}).get("text") or "").strip()
        if index in retry_indices and retry_item is None:
            candidate["retry_attempted"] = True
            candidate.update({
                "status": "failed", "hard_failure": True,
                "failure_reason": retry_batch_error or "retry_result_missing",
            })
            text = str(item.get("text") or "").strip()
        notes: dict[str, Any] = {
            "engine": "xiaomi-ocr-local",
            "engine_version": 3,
            "task": args.task,
            "source_image": image.name,
            "finish_reason": candidate.get("retry_finish_reason") if candidate.get("retry_adopted") else item["finish_reason"],
            "completion_tokens": item["completion_tokens"],
            "reading_direction_requested": args.reading_direction,
            **candidate,
        }
        try:
            if text and candidate.get("status") == "passed":
                text = postprocess_output(text, args.task).strip()
                if not text:
                    notes.update({"status": "failed", "hard_failure": True,
                                  "failure_reason": "empty_after_postprocess"})
            if candidate.get("status") != "passed" or not text:
                notes.update({"status": "failed", "hard_failure": True})
                notes.setdefault("failure_reason", "empty_or_unresolved_ocr_candidate")
                summary["failed"] += 1
                summary["failures"].append({
                    "page": page_number,
                    "error": notes["failure_reason"],
                    "primary_finish_reason": item.get("finish_reason"),
                    "retry_finish_reason": retry_item.get("finish_reason") if retry_item else None,
                })
            else:
                summary["processed"] += 1
                summary["repetition_adopted"] += int(bool(candidate.get("retry_adopted")))
            notes["chars"] = len(text)
            _write_page(args.output_dir, page_number, text, args.model_id, notes)
            print(
                f"[batch-ocr] page={page_number} chars={len(text)} tokens={item['completion_tokens']} "
                f"finish={item['finish_reason']} status={notes['status']} "
                f"elapsed={time.perf_counter() - started:.1f}s",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            summary["failures"].append(
                {"page": page_number, "image": str(image), "error": str(exc), "traceback": traceback.format_exc(limit=8)}
            )
            print(f"[batch-ocr] failed page={page_number}: {exc}", file=sys.stderr, flush=True)

    summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    _write_json_atomic(args.output_dir / f"batch_summary_shard_{args.shard_index:02d}.json", summary)
    print(
        "[batch-ocr] summary "
        f"processed={summary['processed']} skipped={summary['skipped']} "
        f"blank={summary['blank']} failed={summary['failed']} "
        f"repetition_retried={summary['repetition_retried']} adopted={summary['repetition_adopted']} "
        f"elapsed={summary['elapsed_seconds']}s",
        flush=True,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
