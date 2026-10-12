#!/usr/bin/env python3
"""Prepare and record human page reviews for scanned-PDF OCR releases."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scan_layout import prepare_scan_pages

from ocr_quality_gate import (
    ocr_structure_anomalies,
    quotation_signature,  # noqa: E402
    calibrated_confidence_threshold,
    calibrated_disagreement_threshold,
    character_error_rate,
    parse_ocr_notes,
    read_json_object,
    review_sample_pages,
    sha256_text,
    write_json_atomic,
)

REVIEW_PATH = Path("audit/ocr-human-review.json")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _body_pages(output_dir: Path) -> list[int]:
    manifest = json.loads((output_dir / "chapters.json").read_text(encoding="utf-8"))
    pages: set[int] = set()
    for item in manifest:
        if not isinstance(item, dict):
            continue
        start = item.get("pdf_page")
        end = item.get("end_pdf_page") or start
        if isinstance(start, int) and isinstance(end, int) and start > 0 and end >= start:
            pages.update(range(start, end + 1))
    return sorted(pages)


def _records(output_dir: Path) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for path in sorted((output_dir / "pages").glob("page_*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        page = value.get("pdf_page")
        if isinstance(page, int):
            result[page] = value
    return result


def _load_review(output_dir: Path) -> dict[str, Any]:
    return read_json_object(output_dir / REVIEW_PATH)


def initialize(output_dir: Path, source_pdf: Path) -> dict[str, Any]:
    source_hash = _sha256_file(source_pdf)
    records = _records(output_dir)
    body_pages = [page for page in _body_pages(output_dir) if page in records]
    if not body_pages:
        raise ValueError("chapters.json has no body pages with OCR checkpoints")
    models = sorted({str(records[page].get("ocr_model") or "") for page in body_pages})
    if len(models) != 1 or not models[0].startswith("paddleocr-local/PP-OCRv6-medium-"):
        raise ValueError("review ledger requires one PP-OCRv6 medium primary model")
    model_id = models[0]
    old = _load_review(output_dir)
    fallback = read_json_object(output_dir / "audit" / "ocr-fallbacks.json")
    fallback_is_current = (
        fallback.get("source_pdf_sha256") == source_hash
        and fallback.get("primary_model_id") == model_id
    )
    if not fallback_is_current:
        fallback = {}
    fallback_pages = {
        int(page) for page in fallback.get("review_pages", [])
        if isinstance(page, int) or (isinstance(page, str) and page.isdigit())
    }
    suggestions = read_json_object(output_dir / "audit" / "ocr-review-suggestions.json")
    additional_review_pages: set[int] = set()
    suggestions_by_page: dict[int, dict[str, Any]] = {}
    if suggestions:
        if (
            suggestions.get("source_pdf_sha256") != source_hash
            or suggestions.get("primary_model_id") != model_id
        ):
            raise ValueError("OCR review suggestions do not match the current PDF and model")
        for item in suggestions.get("pages", []):
            if not isinstance(item, dict) or not isinstance(item.get("pdf_page"), int):
                continue
            page = int(item["pdf_page"])
            record = records.get(page)
            notes = parse_ocr_notes(record.get("notes")) if record else {}
            if (
                page not in body_pages
                or item.get("image_sha256") != notes.get("source_image_sha256")
                or item.get("ocr_text_sha256") != sha256_text(str(record.get("text") or ""))
            ):
                raise ValueError(f"OCR review suggestion for page {page} is stale")
            additional_review_pages.add(page)
            suggestions_by_page[page] = item
    calibration = read_json_object(output_dir / "audit" / "ocr-calibration.json")
    if (
        calibration.get("source_pdf_sha256") == source_hash
        and calibration.get("primary_model_id") == model_id
    ):
        threshold = calibration.get("confidence_threshold")
    else:
        threshold = None
    for page in body_pages:
        notes = parse_ocr_notes(records[page].get("notes"))
        score = notes.get("score_avg")
        if notes.get("hard_failure") is True or (
            isinstance(threshold, (int, float))
            and not isinstance(threshold, bool)
            and isinstance(score, (int, float))
            and score <= threshold
            and page not in {
                int(value)
                for value in fallback.get("attempted_pages", [])
                if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
            }
        ):
            fallback_pages.add(page)
    if (
        old.get("source_pdf_sha256") == source_hash
        and old.get("primary_model_id") == model_id
        and old.get("body_pages") == body_pages
        and isinstance(old.get("sample_pages"), list)
    ):
        sample = [int(page) for page in old["sample_pages"] if isinstance(page, int)]
        reviews = old.get("human_reviews", [])
    else:
        sample = review_sample_pages(body_pages, source_sha256=source_hash)
        reviews = []
    prepared = prepare_scan_pages(list(records.values()))
    structure_anomalies = ocr_structure_anomalies({page: prepared[page]["text"] for page in body_pages})
    additional_review_pages.update(structure_anomalies)
    required = sorted(set(sample) | fallback_pages | additional_review_pages)
    bindings: dict[str, dict[str, Any]] = {}
    for page in required:
        record = records.get(page)
        if record is None:
            continue
        notes = parse_ocr_notes(record.get("notes"))
        fallback_entry = next(
            (
                item for item in fallback.get("pages", [])
                if isinstance(item, dict) and item.get("pdf_page") == page
            ),
            {},
        )
        binding = {
            "image_sha256": str(notes.get("source_image_sha256") or ""),
            "ocr_text_sha256": sha256_text(str(record.get("text") or "")),
            "confidence": notes.get("score_avg"),
            "reasons": sorted(set(fallback_entry.get("reasons", [])) | set(structure_anomalies.get(page, []))),
            "fallback_text": fallback_entry.get("fallback_text", ""),
        }
        if page in suggestions_by_page:
            binding["agent_suggestion"] = suggestions_by_page[page].get("suggestion", "")
        bindings[str(page)] = binding
    payload = {
        "schema_version": 1,
        "source_pdf": str(source_pdf.resolve()),
        "source_pdf_sha256": source_hash,
        "primary_model_id": model_id,
        "body_pages": body_pages,
        "body_page_count": len(body_pages),
        "sample_pages": sample,
        "fallback_review_pages": sorted(fallback_pages),
        "additional_review_pages": sorted(additional_review_pages),
        "required_pages": required,
        "page_bindings": bindings,
        "human_reviews": reviews if isinstance(reviews, list) else [],
    }
    valid_bindings = payload["page_bindings"]
    payload["human_reviews"] = [
        review
        for review in payload["human_reviews"]
        if isinstance(review, dict)
        and str(review.get("pdf_page")) in valid_bindings
        and review.get("image_sha256") == valid_bindings[str(review.get("pdf_page"))]["image_sha256"]
        and review.get("ocr_text_sha256") == valid_bindings[str(review.get("pdf_page"))]["ocr_text_sha256"]
    ]
    write_json_atomic(output_dir / REVIEW_PATH, payload)
    return payload


def confirm_page(
    output_dir: Path,
    page: int,
    reviewer: str,
    *,
    matches_image: bool,
    corrected_text: str = "",
    confirm_page: int | None = None,
) -> None:
    if confirm_page != page:
        raise ValueError(f"explicit confirmation required: pass --confirm-page {page}")
    ledger_path = output_dir / REVIEW_PATH
    ledger = _load_review(output_dir)
    if page not in ledger.get("required_pages", []):
        raise ValueError(f"page {page} is not in the current review plan; run init first")
    if not reviewer.strip():
        raise ValueError("reviewer must identify the human who checked the source image")
    if matches_image == bool(corrected_text.strip()):
        raise ValueError("choose exactly one: --matches-image or --corrected-text")
    records = _records(output_dir)
    record = records.get(page)
    binding = ledger.get("page_bindings", {}).get(str(page), {})
    if record is None:
        raise ValueError(f"page {page} has no OCR checkpoint")
    raw_text = str(record.get("text") or "")
    notes = parse_ocr_notes(record.get("notes"))
    image_hash = str(notes.get("source_image_sha256") or "")
    raw_hash = sha256_text(raw_text)
    if not image_hash or binding.get("image_sha256") != image_hash:
        raise ValueError("source image hash is missing or changed; rebuild the review plan")
    if binding.get("ocr_text_sha256") != raw_hash:
        raise ValueError("primary OCR text changed; rebuild the review plan")
    if not matches_image and not corrected_text.strip():
        raise ValueError("a full corrected transcription is required when OCR differs")

    from book_pipeline import PageRecord, load_page_records, save_page_record

    current = next((item for item in load_page_records(output_dir) if item.pdf_page == page), None)
    if current is None:
        current = PageRecord(pdf_page=page, text=raw_text, language=str(record.get("language") or "unknown"), ocr_model=str(record.get("ocr_model") or ""), notes=str(record.get("notes") or ""))
    if matches_image:
        if not raw_text.strip():
            raise ValueError("an empty OCR page cannot be accepted as matching; provide a correction")
        if current.proofread_provider == "human-ocr-review":
            current.proofread_text = ""
            current.proofread_source_sha256 = ""
            current.proofread_provider = ""
            current.proofread_model = ""
            current.proofread_language = ""
            current.proofread_prompt_version = ""
            current.proofread_fingerprint = ""
    else:
        current.proofread_text = corrected_text.strip()
        current.proofread_source_sha256 = current.text_sha256
        current.proofread_provider = "human-ocr-review"
        current.proofread_model = reviewer.strip()
        current.proofread_language = current.language or "unknown"
        current.proofread_prompt_version = "human-review-v1"
        current.proofread_fingerprint = sha256_text(current.proofread_text)
    save_page_record(output_dir, current)

    reviews = [item for item in ledger.get("human_reviews", []) if item.get("pdf_page") != page]
    reviews.append(
        {
            "pdf_page": page,
            "confirmed": True,
            "reviewer": reviewer.strip(),
            "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source_pdf_sha256": ledger["source_pdf_sha256"],
            "image_sha256": image_hash,
            "ocr_text_sha256": raw_hash,
            "matches_image": matches_image,
            "corrected_text": "" if matches_image else corrected_text.strip(),
            "corrected_text_sha256": "" if matches_image else sha256_text(corrected_text.strip()),
        }
    )
    ledger["human_reviews"] = sorted(reviews, key=lambda item: int(item.get("pdf_page", 0)))
    write_json_atomic(ledger_path, ledger)


def calibrate(output_dir: Path) -> dict[str, Any]:
    ledger = _load_review(output_dir)
    if not ledger:
        raise ValueError("run init first")
    reviews = {
        int(item["pdf_page"]): item
        for item in ledger.get("human_reviews", [])
        if isinstance(item, dict) and item.get("confirmed") is True
    }
    labels = []
    for page_text, binding in ledger.get("page_bindings", {}).items():
        page = int(page_text)
        review = reviews.get(page)
        if review is None:
            continue
        confidence = binding.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            labels.append({"confidence": confidence, "matches_image": review.get("matches_image")})
    confidence_threshold = calibrated_confidence_threshold(labels)

    fallback = read_json_object(output_dir / "audit" / "ocr-fallbacks.json")
    fallback_by_page = {
        int(item["pdf_page"]): item
        for item in fallback.get("pages", [])
        if isinstance(item, dict) and isinstance(item.get("pdf_page"), int)
    }
    pairs = []
    for page, review in reviews.items():
        item = fallback_by_page.get(page)
        if item and isinstance(item.get("cer"), (int, float)):
            pairs.append({"model_cer": item["cer"], "primary_matches_image": review.get("matches_image")})
    disagreement_threshold = calibrated_disagreement_threshold(pairs)
    payload = {
        "schema_version": 1,
        "source_pdf_sha256": ledger["source_pdf_sha256"],
        "primary_model_id": ledger["primary_model_id"],
        "human_label_count": len(labels),
        "paired_label_count": len(pairs),
        "minimum_pages": 10,
        "confidence_threshold": confidence_threshold,
        "disagreement_cer_threshold": disagreement_threshold,
        "calibrated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "provenance": "human_reviews in ocr-human-review.json",
    }
    write_json_atomic(output_dir / "audit" / "ocr-calibration.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init", help="create a hash-bound review plan after chapters are mapped")
    init_parser.add_argument("--output-dir", required=True, type=Path)
    init_parser.add_argument("--source-pdf", required=True, type=Path)
    confirm_parser = subparsers.add_parser("confirm", help="record one human full-page image review")
    confirm_parser.add_argument("--output-dir", required=True, type=Path)
    confirm_parser.add_argument("--page", required=True, type=int)
    confirm_parser.add_argument("--reviewer", required=True)
    confirm_parser.add_argument("--confirm-page", required=True, type=int)
    result = confirm_parser.add_mutually_exclusive_group(required=True)
    result.add_argument("--matches-image", action="store_true")
    result.add_argument("--corrected-text", type=Path)
    calibration_parser = subparsers.add_parser("calibrate", help="derive thresholds from confirmed human labels")
    calibration_parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    try:
        if args.command == "init":
            result_value = initialize(output_dir, args.source_pdf.resolve())
        elif args.command == "confirm":
            corrected = args.corrected_text.read_text(encoding="utf-8") if args.corrected_text else ""
            confirm_page(
                output_dir,
                args.page,
                args.reviewer,
                matches_image=args.matches_image,
                corrected_text=corrected,
                confirm_page=args.confirm_page,
            )
            result_value = {"confirmed_page": args.page}
        else:
            result_value = calibrate(output_dir)
        print(json.dumps(result_value, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
