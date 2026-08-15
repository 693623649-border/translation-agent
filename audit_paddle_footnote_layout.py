#!/usr/bin/env python3
"""Read-only geometric footnote recovery audit for local PaddleOCR.

The script deliberately does not update PageRecord or publication artifacts.
It asks the already-running Paddle service for bounded ``layout_lines``, finds
a lower-page small-type region, then optionally re-OCRs the footer and body
bands at higher effective resolution.  Only explicit recognized markers are
reported; missing marker numbers are never inferred from sequence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import statistics
import tempfile
import threading
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

import fitz
from PIL import Image

from local_ocr.footnote_layout import (
    detect_footer_region,
    detect_footer_separators,
    detect_inline_circle_candidates,
    footer_region_from_separator,
    leading_footer_marker_number,
    leading_marker_number,
    marker_numbers,
    parse_layout_lines,
)
from local_ocr.protocol import request


def _parse_pages(value: str, page_count: int) -> tuple[int, ...]:
    if not value.strip():
        return tuple(range(1, page_count + 1))
    pages: set[int] = set()
    for item in value.split(","):
        token = item.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"invalid page range: {token}")
            start, end = int(start_text), int(end_text)
            if start < 1 or end < start:
                raise ValueError(f"invalid page range: {token}")
            pages.update(range(start, end + 1))
        elif token.isdigit() and int(token) >= 1:
            pages.add(int(token))
        else:
            raise ValueError(f"invalid page number: {token}")
    if not pages:
        raise ValueError("page selection is empty")
    outside = sorted(page for page in pages if page > page_count)
    if outside:
        raise ValueError(f"pages exceed the PDF page count: {outside}")
    return tuple(sorted(pages))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _render_pages(
    pdf_path: Path,
    pages: Iterable[int],
    destination: Path,
    *,
    dpi: int,
) -> dict[int, tuple[Path, int, int]]:
    rendered: dict[int, tuple[Path, int, int]] = {}
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
    with fitz.open(pdf_path) as document:
        for page_number in pages:
            pixmap = document[page_number - 1].get_pixmap(
                matrix=matrix,
                colorspace=fitz.csRGB,
                alpha=False,
            )
            path = destination / f"page_{page_number:04d}.jpg"
            pixmap.save(path, jpg_quality=95)
            rendered[page_number] = (path, pixmap.width, pixmap.height)
    return rendered


def _ocr(
    socket_path: Path,
    image_path: Path,
    *,
    timeout: float,
    model_identity: str,
) -> Mapping[str, Any]:
    response = request(
        socket_path,
        {
            "op": "ocr",
            "request_id": f"footnote-audit-{os.getpid()}-{uuid.uuid4().hex}",
            "image_path": str(image_path.resolve(strict=True)),
            "reading_direction": "horizontal",
            "horizontal_columns": 1,
            "timeout": timeout,
        },
        timeout=timeout + 5.0,
    )
    if not response.get("ok"):
        raise RuntimeError(str(response.get("error") or "Paddle OCR failed"))
    if response.get("model_identity") != model_identity:
        raise RuntimeError("Paddle service identity changed during the audit")
    return response


def _ocr_allow_blank(
    socket_path: Path,
    image_path: Path,
    *,
    timeout: float,
    model_identity: str,
) -> Mapping[str, Any]:
    try:
        return _ocr(
            socket_path,
            image_path,
            timeout=timeout,
            model_identity=model_identity,
        )
    except RuntimeError as exc:
        if "recognized only empty text regions" not in str(exc):
            raise
        return {
            "ok": True,
            "text": "",
            "metadata": {
                "layout_line_count": 0,
                "layout_lines_truncated": False,
                "layout_lines": [],
            },
        }


def _unique(values: Iterable[int]) -> list[int]:
    return list(dict.fromkeys(values))


def _separator_payload(separator: Any) -> dict[str, Any]:
    return {
        "bbox": [
            separator.left,
            separator.top,
            separator.right,
            separator.bottom,
        ],
        "center": [
            (separator.left + separator.right) / 2.0,
            separator.y,
        ],
        "width": separator.width,
        "density": separator.density,
        "bridged": separator.bridged,
    }


def _save_scaled_crop(
    source: Image.Image,
    box: tuple[int, int, int, int],
    destination: Path,
    *,
    scale: float,
) -> tuple[int, int]:
    crop = source.crop(box)
    if scale != 1.0:
        crop = crop.resize(
            (
                max(1, round(crop.width * scale)),
                max(1, round(crop.height * scale)),
            ),
            Image.Resampling.LANCZOS,
        )
    crop.save(destination, "JPEG", quality=95)
    return crop.size


def _audit_page(
    page_number: int,
    rendered: tuple[Path, int, int],
    *,
    socket_path: Path,
    model_identity: str,
    timeout: float,
    footer_scale: float,
    marker_scale: float,
    body_scale: float,
    body_bands: int,
    crop_dir: Path,
    expected_labels: tuple[str, ...],
) -> dict[str, Any]:
    image_path, width, height = rendered
    full = _ocr_allow_blank(
        socket_path,
        image_path,
        timeout=timeout,
        model_identity=model_identity,
    )
    metadata = full.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("Paddle response has no metadata object")
    lines = parse_layout_lines(metadata, page_width=width, page_height=height)
    with Image.open(image_path) as opened:
        grayscale = opened.convert("L")
        separators = detect_footer_separators(grayscale)
    pixel_regions = []
    separator_diagnostics: list[dict[str, Any]] = []
    for separator in separators:
        diagnostic = _separator_payload(separator)
        diagnostic["selected"] = False
        diagnostic["rejection_reasons"] = []
        # A footnote rule may be high on a page with a very long note block,
        # but upper-page title/table strokes are not plausible boundaries.
        if separator.y < height * 0.45:
            diagnostic["rejection_reasons"].append("above_lower_page_boundary")
            separator_diagnostics.append(diagnostic)
            continue
        candidate = footer_region_from_separator(
            lines,
            separator,
            page_width=width,
            page_height=height,
        )
        if candidate is None:
            diagnostic["rejection_reasons"].append(
                "no_mappable_body_and_footer_layout"
            )
            separator_diagnostics.append(diagnostic)
            continue
        size_ratio = candidate.footer_median_height / candidate.body_median_height
        diagnostic["mapped_footer_to_body_height_ratio"] = size_ratio
        diagnostic["mapped_definition_numbers"] = list(candidate.definition_numbers)
        diagnostic["mapped_body_marker_numbers"] = list(
            candidate.body_marker_numbers
        )
        semantic_signal = bool(
            candidate.definition_numbers or candidate.body_marker_numbers
        )
        # A rule without any explicit marker remains useful only when Paddle
        # independently confirms a smaller-type lower block. This rejects
        # tables and decorative rules without introducing page-specific logic.
        if not semantic_signal and size_ratio > 0.92:
            diagnostic["rejection_reasons"].append(
                "no_explicit_marker_and_footer_not_smaller"
            )
            separator_diagnostics.append(diagnostic)
            continue
        paired = len(
            set(candidate.definition_numbers) & set(candidate.body_marker_numbers)
        )
        selection_score = (
            len(candidate.definition_numbers) * 6.0
            + paired * 4.0
            + max(0.0, 1.0 - size_ratio) * 5.0
            + (1.5 if not separator.bridged else 0.0)
            + candidate.confidence
        )
        diagnostic["selection_score"] = selection_score
        separator_diagnostics.append(diagnostic)
        pixel_regions.append((selection_score, candidate, separator))
    if pixel_regions:
        _, region, selected_separator = max(
            pixel_regions,
            key=lambda item: (item[0], -item[1].boundary_y),
        )
        region_evidence = "printed_horizontal_separator_and_paddle_polygons"
    else:
        region = detect_footer_region(lines, page_width=width, page_height=height)
        selected_separator = None
        region_evidence = "paddle_polygon_gap_and_type_size"
    if region is None and separators:
        plausible = [
            separator
            for separator in separators
            if separator.y >= height * 0.45 and separator.left <= width * 0.42
        ]
        exact = [separator for separator in plausible if not separator.bridged]
        if exact:
            selected_separator = max(exact, key=lambda separator: separator.width)
        elif plausible:
            # Whole-page Paddle can miss a one-line tiny footer completely.
            # On such pages footer typography creates false bridged rows below
            # the real rule, so use the lowest plausible rule as a crop trigger.
            selected_separator = max(plausible, key=lambda separator: separator.y)
        else:
            selected_separator = None
        if selected_separator is not None:
            region = footer_region_from_separator(
                lines,
                selected_separator,
                page_width=width,
                page_height=height,
                allow_empty_footer=True,
            )
            if region is not None:
                region_evidence = "printed_horizontal_separator_triggered_crop"
    if selected_separator is not None:
        selected_bbox = _separator_payload(selected_separator)["bbox"]
        for diagnostic in separator_diagnostics:
            if diagnostic["bbox"] == selected_bbox:
                diagnostic["selected"] = True
                break
    for diagnostic in separator_diagnostics:
        if not diagnostic["selected"] and not diagnostic["rejection_reasons"]:
            diagnostic["rejection_reasons"].append(
                "lower_rank_than_selected_separator"
            )
    item: dict[str, Any] = {
        "page": page_number,
        "status": "no_geometric_footer",
        "full_layout_line_count": len(lines),
        "full_text_marker_numbers": _unique(marker_numbers(str(full.get("text") or ""))),
        "separator_candidate_count": len(separators),
        "separator_candidates": separator_diagnostics,
        "expected_reviewed_labels": list(expected_labels),
        "expected_reviewed_count": len(expected_labels),
        "inline_circle_candidate_count": 0,
        "inline_circle_selected_count": 0,
        "inline_circle_candidates": [],
        "footer_note_start_candidate_count": 0,
        "footer_note_start_selected_count": 0,
        "footer_note_start_candidates": [],
        "rejection_reasons": [],
    }
    if region is None:
        item["rejection_reasons"].append("no_geometric_footer_region")
        return item

    margin = max(4, round(height * 0.006))
    footer_top = max(0, round(region.gap_top - margin))
    if selected_separator is not None:
        # Whole-page Paddle often omits the last one-line note.  A horizontal
        # rule is strong enough to justify auditing the complete footer down
        # to the page-number margin instead of clipping at detected polygons.
        footer_bottom = min(height, round(height * 0.965))
    else:
        footer_bottom = min(
            height,
            round(
                min(
                    height * 0.965,
                    (
                        max(line.bottom for line in region.lines) + margin
                        if region.lines
                        else height * 0.94
                    ),
                )
            ),
        )
    with Image.open(image_path) as opened:
        source = opened.convert("RGB")
        footer_path = crop_dir / f"page_{page_number:04d}_footer.jpg"
        footer_size = _save_scaled_crop(
            source,
            (0, footer_top, width, footer_bottom),
            footer_path,
            scale=footer_scale,
        )
        footer = _ocr_allow_blank(
            socket_path,
            footer_path,
            timeout=timeout,
            model_identity=model_identity,
        )
        footer_metadata = footer.get("metadata")
        if not isinstance(footer_metadata, Mapping):
            raise ValueError("Paddle footer response has no metadata object")
        footer_layout = parse_layout_lines(
            footer_metadata,
            page_width=footer_size[0],
            page_height=footer_size[1],
        )
        supplemental_definitions = _unique(
            value
            for line in footer_layout
            for value in (leading_marker_number(line.text),)
            if value is not None
        )
        marker_left = round(width * 0.08)
        marker_right = round(width * 0.55)
        marker_path = crop_dir / f"page_{page_number:04d}_footer_markers.jpg"
        marker_size = _save_scaled_crop(
            source,
            (marker_left, footer_top, marker_right, footer_bottom),
            marker_path,
            scale=marker_scale,
        )
        marker_footer = _ocr_allow_blank(
            socket_path,
            marker_path,
            timeout=timeout,
            model_identity=model_identity,
        )
        marker_metadata = marker_footer.get("metadata")
        if not isinstance(marker_metadata, Mapping):
            raise ValueError("Paddle marker-column response has no metadata object")
        marker_layout = parse_layout_lines(
            marker_metadata,
            page_width=marker_size[0],
            page_height=marker_size[1],
        )
        marker_column_definitions = _unique(
            value
            for line in marker_layout
            for value in (leading_footer_marker_number(line.text),)
            if value is not None
        )
        supplemental_definitions = _unique(
            supplemental_definitions + marker_column_definitions
        )
        footer_note_start_candidates = []
        for line in marker_layout:
            recognized_label = leading_footer_marker_number(line.text)
            relative_left = line.left / max(1.0, marker_size[0])
            if recognized_label is None and relative_left > 0.20:
                continue
            source_bbox = [
                marker_left + line.left / marker_scale,
                footer_top + line.top / marker_scale,
                marker_left + line.right / marker_scale,
                footer_top + line.bottom / marker_scale,
            ]
            footer_note_start_candidates.append(
                {
                    "bbox": source_bbox,
                    "center": [
                        (source_bbox[0] + source_bbox[2]) / 2.0,
                        (source_bbox[1] + source_bbox[3]) / 2.0,
                    ],
                    "score": (
                        1.0
                        if recognized_label is not None
                        else max(0.0, 1.0 - relative_left / 0.20) * line.score
                    ),
                    "recognized_label": recognized_label,
                    "text": line.text[:500],
                }
            )
        supplemental_footer_height = (
            statistics.median(line.height for line in footer_layout) / footer_scale
            if footer_layout
            else None
        )

        supplemental_body: list[int] = []
        body_excerpt_lengths: list[int] = []
        if body_bands:
            body_top = max(0, round(height * 0.04))
            body_bottom = max(body_top + 1, round(region.gap_top))
            span = body_bottom - body_top
            overlap = max(2, round(span / body_bands * 0.05))
            for band_index in range(body_bands):
                y0 = body_top + round(span * band_index / body_bands)
                y1 = body_top + round(span * (band_index + 1) / body_bands)
                if band_index:
                    y0 = max(body_top, y0 - overlap)
                if band_index + 1 < body_bands:
                    y1 = min(body_bottom, y1 + overlap)
                band_path = crop_dir / (
                    f"page_{page_number:04d}_body_{band_index + 1:02d}.jpg"
                )
                _save_scaled_crop(
                    source,
                    (0, y0, width, y1),
                    band_path,
                    scale=body_scale,
                )
                band = _ocr_allow_blank(
                    socket_path,
                    band_path,
                    timeout=timeout,
                    model_identity=model_identity,
                )
                band_text = str(band.get("text") or "")
                body_excerpt_lengths.append(len(band_text))
                supplemental_body.extend(marker_numbers(band_text))

        circle_candidates = detect_inline_circle_candidates(
            source.convert("L"),
            lines,
            boundary_y=region.gap_top,
            max_candidates=max(64, len(expected_labels) * 12),
        )

    geometric_definitions = list(region.definition_numbers)
    geometric_body = list(region.body_marker_numbers)
    definition_numbers = _unique(geometric_definitions + supplemental_definitions)
    body_numbers = _unique(
        supplemental_body if body_bands else geometric_body
    )
    matched = sorted(set(definition_numbers) & set(body_numbers))
    missing_inline = sorted(set(definition_numbers) - set(body_numbers))
    missing_definition = sorted(set(body_numbers) - set(definition_numbers))
    supplemental_size_ratio = (
        supplemental_footer_height / region.body_median_height
        if supplemental_footer_height is not None
        else None
    )
    rejected_rule_only = bool(
        selected_separator is not None
        and not definition_numbers
        and not body_numbers
        and (
            supplemental_size_ratio is None
            or (
                selected_separator.left > width * 0.32
                and selected_separator.y < height * 0.65
            )
        )
    )
    if rejected_rule_only:
        status = "no_geometric_footer"
        item["rejection_reasons"].append("rule_only_rejected_after_crop")
    elif definition_numbers and not missing_inline and not missing_definition:
        status = "closed_explicit_marker_set"
    elif definition_numbers or body_numbers:
        status = "explicit_marker_mismatch"
    else:
        status = "geometry_only_no_explicit_marker"
    footer_text = str(footer.get("text") or "")
    selected_inline = sorted(
        sorted(circle_candidates, key=lambda candidate: candidate.score, reverse=True)[
            : len(expected_labels)
        ],
        key=lambda candidate: (candidate.center_y, candidate.center_x),
    )
    selected_footer = sorted(
        sorted(
            footer_note_start_candidates,
            key=lambda candidate: float(candidate["score"]),
            reverse=True,
        )[: len(expected_labels)],
        key=lambda candidate: (candidate["center"][1], candidate["center"][0]),
    )
    inline_assignments = dict(zip(selected_inline, expected_labels))
    for label, candidate in zip(expected_labels, selected_footer):
        candidate["assigned_reviewed_label"] = label
    item.update(
        {
            "status": status,
            "geometry": {
                "evidence": region_evidence,
                "boundary_y": region.boundary_y,
                "boundary_ratio": region.boundary_y / height,
                "gap_top": region.gap_top,
                "gap_bottom": region.gap_bottom,
                "body_median_height": region.body_median_height,
                "footer_median_height": region.footer_median_height,
                "footer_to_body_height_ratio": (
                    region.footer_median_height / region.body_median_height
                ),
                "confidence": region.confidence,
                "separator": (
                    {
                        "top": selected_separator.top,
                        "bottom": selected_separator.bottom,
                        "left": selected_separator.left,
                        "right": selected_separator.right,
                        "width": selected_separator.width,
                        "density": selected_separator.density,
                        "bridged": selected_separator.bridged,
                    }
                    if selected_separator is not None
                    else None
                ),
            },
            "geometric_definition_numbers": geometric_definitions,
            "geometric_body_marker_numbers": geometric_body,
            "supplemental_definition_numbers": supplemental_definitions,
            "supplemental_body_marker_numbers": _unique(supplemental_body),
            "definition_numbers": definition_numbers,
            "body_marker_numbers": body_numbers,
            "matched_numbers": matched,
            "missing_inline_numbers": missing_inline,
            "missing_definition_numbers": missing_definition,
            "footer_crop": {
                "source_box": [0, footer_top, width, footer_bottom],
                "scale": footer_scale,
                "ocr_line_count": footer_metadata.get("layout_line_count"),
                "text": footer_text[:8000],
                "text_truncated": len(footer_text) > 8000,
            },
            "marker_column_crop": {
                "source_box": [marker_left, footer_top, marker_right, footer_bottom],
                "scale": marker_scale,
                "ocr_line_count": marker_metadata.get("layout_line_count"),
                "definition_numbers": marker_column_definitions,
                "text": str(marker_footer.get("text") or "")[:4000],
            },
            "expected_reviewed_labels": list(expected_labels),
            "expected_reviewed_count": len(expected_labels),
            "inline_circle_candidate_count": len(circle_candidates),
            "inline_circle_selected_count": len(selected_inline),
            "inline_circle_candidates": [
                {
                    "bbox": [
                        candidate.left,
                        candidate.top,
                        candidate.right,
                        candidate.bottom,
                    ],
                    "center": [candidate.center_x, candidate.center_y],
                    "normalized_center": [
                        candidate.center_x / width,
                        candidate.center_y / height,
                    ],
                    "score": candidate.score,
                    "selected_by_expected_count": candidate in selected_inline,
                    "assigned_reviewed_label": inline_assignments.get(candidate),
                }
                for candidate in circle_candidates
            ],
            "footer_note_start_candidate_count": len(
                footer_note_start_candidates
            ),
            "footer_note_start_selected_count": len(selected_footer),
            "footer_note_start_candidates": footer_note_start_candidates,
            "candidate_selection_contract": (
                "positions_only; top-N uses reviewed expected_count; footer labels "
                "are assigned only from reviewed sequence; no PageRecord write"
            ),
            "body_band_count": body_bands,
            "body_band_ocr_char_counts": body_excerpt_lengths,
            "supplemental_footer_to_body_height_ratio": supplemental_size_ratio,
            "rule_only_rejected_after_crop": rejected_rule_only,
        }
    )
    return item


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dry-run Paddle rec_polys footnote recovery without PageRecord writes"
    )
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--pages",
        default="",
        help="comma-separated 1-based pages/ranges; default is the complete PDF",
    )
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--footer-scale", type=float, default=1.5)
    parser.add_argument("--marker-scale", type=float, default=2.0)
    parser.add_argument("--body-scale", type=float, default=1.25)
    parser.add_argument(
        "--body-bands",
        type=int,
        default=0,
        help="0 disables supplemental inline scans; 4 is a practical review pass",
    )
    parser.add_argument(
        "--keep-crops",
        type=Path,
        help="retain rendered pages/crops here; default uses an auto-removed temp directory",
    )
    parser.add_argument(
        "--ground-truth",
        type=Path,
        help="optional reviewed circled_page_map used only for expected counts/labels",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    pdf_path = args.pdf.resolve(strict=True)
    socket_path = args.socket.resolve(strict=True)
    if args.dpi < 72 or args.dpi > 600:
        raise ValueError("dpi must be between 72 and 600")
    if args.jobs < 1 or args.jobs > 64:
        raise ValueError("jobs must be between 1 and 64")
    if args.body_bands < 0 or args.body_bands > 12:
        raise ValueError("body-bands must be between 0 and 12")
    if not (1.0 <= args.footer_scale <= 3.0):
        raise ValueError("footer-scale must be between 1 and 3")
    if not (1.0 <= args.marker_scale <= 3.0):
        raise ValueError("marker-scale must be between 1 and 3")
    if not (1.0 <= args.body_scale <= 3.0):
        raise ValueError("body-scale must be between 1 and 3")

    health = request(socket_path, {"op": "health"}, timeout=10.0)
    if not health.get("ok") or not health.get("ready"):
        raise RuntimeError(f"Paddle service is not ready: {health!r}")
    model_identity = str(health.get("model_identity") or "")
    if not model_identity:
        raise RuntimeError("Paddle service health has no model identity")

    with fitz.open(pdf_path) as document:
        page_count = document.page_count
    pages = _parse_pages(args.pages, page_count)
    ground_truth_map: dict[int, tuple[str, ...]] = {}
    ground_truth_legacy_map: dict[int, tuple[str, ...]] = {}
    ground_truth_audit_status = ""
    ground_truth_sha256 = ""
    if args.ground_truth is not None:
        ground_truth_path = args.ground_truth.resolve(strict=True)
        ground_truth_payload = json.loads(ground_truth_path.read_text(encoding="utf-8"))
        raw_page_map = ground_truth_payload.get("circled_page_map")
        if not isinstance(raw_page_map, list):
            raise ValueError("ground truth has no circled_page_map array")
        for raw_item in raw_page_map:
            if not isinstance(raw_item, Mapping):
                raise ValueError("ground truth page item is not an object")
            page_number = int(raw_item["pdf_page"])
            labels = raw_item.get("labels")
            if not isinstance(labels, list) or not all(
                isinstance(label, (str, int)) for label in labels
            ):
                raise ValueError(f"ground truth labels are invalid on page {page_number}")
            ground_truth_map[page_number] = tuple(str(label) for label in labels)
        raw_legacy_map = ground_truth_payload.get("legacy_note_pages", [])
        if not isinstance(raw_legacy_map, list):
            raise ValueError("ground truth legacy_note_pages is not an array")
        for raw_item in raw_legacy_map:
            if not isinstance(raw_item, Mapping):
                raise ValueError("ground truth legacy page item is not an object")
            page_number = int(raw_item["pdf_page"])
            labels = raw_item.get("labels")
            if not isinstance(labels, list) or not all(
                isinstance(label, (str, int)) for label in labels
            ):
                raise ValueError(
                    f"ground truth legacy labels are invalid on page {page_number}"
                )
            ground_truth_legacy_map[page_number] = tuple(
                str(label) for label in labels
            )
        method = ground_truth_payload.get("method")
        if isinstance(method, Mapping):
            ground_truth_audit_status = str(
                method.get("sequence_validation") or ""
            )
        ground_truth_sha256 = _sha256(ground_truth_path)

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.keep_crops is None:
        temporary = tempfile.TemporaryDirectory(prefix="paddle-footnote-audit-")
        crop_dir = Path(temporary.name)
    else:
        crop_dir = args.keep_crops.resolve()
        crop_dir.mkdir(parents=True, exist_ok=True)
    try:
        rendered = _render_pages(pdf_path, pages, crop_dir, dpi=args.dpi)
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        lock = threading.Lock()

        def process(page_number: int) -> None:
            try:
                result = _audit_page(
                    page_number,
                    rendered[page_number],
                    socket_path=socket_path,
                    model_identity=model_identity,
                    timeout=args.timeout,
                    footer_scale=args.footer_scale,
                    marker_scale=args.marker_scale,
                    body_scale=args.body_scale,
                    body_bands=args.body_bands,
                    crop_dir=crop_dir,
                    expected_labels=ground_truth_map.get(page_number, ()),
                )
                with lock:
                    results.append(result)
            except BaseException as exc:  # retain all page failures in the audit
                with lock:
                    failures.append(
                        {
                            "page": page_number,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
            list(executor.map(process, pages))
        results.sort(key=lambda item: int(item["page"]))
        failures.sort(key=lambda item: int(item["page"]))
        status_counts = Counter(str(item["status"]) for item in results)
        geometric_footer_pages = [
            int(item["page"])
            for item in results
            if item["status"] != "no_geometric_footer"
        ]
        selected_page_set = set(pages)
        reviewed_circled_pages = sorted(
            selected_page_set & set(ground_truth_map)
        )
        reviewed_legacy_pages = sorted(
            selected_page_set & set(ground_truth_legacy_map)
        )
        accepted_page_set = set(geometric_footer_pages)
        circled_true_positive_pages = sorted(
            accepted_page_set & set(reviewed_circled_pages)
        )
        circled_missed_pages = sorted(
            set(reviewed_circled_pages) - accepted_page_set
        )
        unexpected_accepted_pages = sorted(
            accepted_page_set
            - set(reviewed_circled_pages)
            - set(reviewed_legacy_pages)
        )
        accepted_legacy_only_pages = sorted(
            accepted_page_set
            & set(reviewed_legacy_pages)
            - set(reviewed_circled_pages)
        )
        reviewed_expected_label_count = sum(
            len(ground_truth_map[page]) for page in reviewed_circled_pages
        )
        reviewed_results = [
            item for item in results if int(item["page"]) in ground_truth_map
        ]
        inline_selected_count = sum(
            int(item.get("inline_circle_selected_count", 0))
            for item in reviewed_results
        )
        footer_selected_count = sum(
            int(item.get("footer_note_start_selected_count", 0))
            for item in reviewed_results
        )
        circled_page_recall = (
            len(circled_true_positive_pages) / len(reviewed_circled_pages)
            if reviewed_circled_pages
            else None
        )
        circled_page_precision = (
            len(circled_true_positive_pages)
            / max(1, len(accepted_page_set) - len(accepted_legacy_only_pages))
            if accepted_page_set
            else None
        )
        report = {
            "schema_version": 2,
            "mode": "read-only-paddle-layout-footnote-recovery-dry-run",
            "source_pdf": str(pdf_path),
            "source_pdf_sha256": _sha256(pdf_path),
            "pdf_page_count": page_count,
            "selected_pages": list(pages),
            "selected_page_count": len(pages),
            "geometric_footer_pages": geometric_footer_pages,
            "geometric_footer_page_spec": ",".join(
                str(page) for page in geometric_footer_pages
            ),
            "model_identity": model_identity,
            "ground_truth_sha256": ground_truth_sha256,
            "ground_truth_audit_status": ground_truth_audit_status,
            "settings": {
                "dpi": args.dpi,
                "jobs": args.jobs,
                "footer_scale": args.footer_scale,
                "marker_scale": args.marker_scale,
                "body_scale": args.body_scale,
                "body_bands": args.body_bands,
            },
            "summary": {
                "completed_page_count": len(results),
                "failure_count": len(failures),
                "geometric_footer_count": sum(
                    item["status"] != "no_geometric_footer" for item in results
                ),
                "closed_explicit_marker_page_count": status_counts[
                    "closed_explicit_marker_set"
                ],
                "explicit_marker_mismatch_page_count": status_counts[
                    "explicit_marker_mismatch"
                ],
                "geometry_only_page_count": status_counts[
                    "geometry_only_no_explicit_marker"
                ],
                "no_geometric_footer_page_count": status_counts[
                    "no_geometric_footer"
                ],
                "matched_marker_count": sum(
                    len(item.get("matched_numbers", [])) for item in results
                ),
                "missing_inline_marker_count": sum(
                    len(item.get("missing_inline_numbers", [])) for item in results
                ),
                "missing_definition_marker_count": sum(
                    len(item.get("missing_definition_numbers", [])) for item in results
                ),
                "reviewed_circled_page_count": len(reviewed_circled_pages),
                "reviewed_legacy_page_count": len(reviewed_legacy_pages),
                "accepted_legacy_only_page_count": len(
                    accepted_legacy_only_pages
                ),
                "circled_true_positive_page_count": len(
                    circled_true_positive_pages
                ),
                "circled_missed_page_count": len(circled_missed_pages),
                "circled_page_recall": circled_page_recall,
                "circled_page_precision_excluding_reviewed_legacy": (
                    circled_page_precision
                ),
                "unexpected_accepted_page_count": len(
                    unexpected_accepted_pages
                ),
                "reviewed_expected_label_count": reviewed_expected_label_count,
                "inline_circle_selected_count": inline_selected_count,
                "footer_note_start_selected_count": footer_selected_count,
            },
            "ground_truth_comparison": {
                "circled_true_positive_pages": circled_true_positive_pages,
                "circled_missed_pages": circled_missed_pages,
                "reviewed_legacy_pages": reviewed_legacy_pages,
                "accepted_legacy_only_pages": accepted_legacy_only_pages,
                "unexpected_accepted_pages": unexpected_accepted_pages,
                "warning": (
                    "ground truth declares its full 205-page audit in progress; "
                    "precision is provisional"
                    if "in progress" in ground_truth_audit_status.lower()
                    else ""
                ),
            },
            "failures": failures,
            "pages": results,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary_report = args.report.with_name(
            f".{args.report.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        temporary_report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_report, args.report)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"report={args.report}")
        return 0 if not failures else 2
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
