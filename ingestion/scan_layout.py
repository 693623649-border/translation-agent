"""Conservative source layout for vertical scans; raw OCR remains untouched."""
from __future__ import annotations

import hashlib
import json
import re
import statistics

VERSION = "scan-layout-v2"


def _notes(record: dict) -> dict:
    value = record.get("notes", {})
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _geometry(record: dict) -> tuple[list[dict], list[dict], float]:
    lines = [x for x in _notes(record).get("lines", []) if isinstance(x, dict)
             and isinstance(x.get("box"), list) and len(x["box"]) == 4]
    tall = [x for x in lines if x["box"][3] - x["box"][1] >
            5 * max(1, x["box"][2] - x["box"][0]) and len(x.get("text", "")) > 8]
    if not tall:
        return lines, [], 1.0
    width = statistics.median(x["box"][2] - x["box"][0] for x in tall)
    clusters: list[list[dict]] = []
    for line in sorted(tall, key=lambda x: (x["box"][0] + x["box"][2]) / 2):
        if not clusters or line["box"][0] - clusters[-1][-1]["box"][2] > width * 4:
            clusters.append([])
        clusters[-1].append(line)
    body = max(clusters, key=lambda xs: (len(xs), sum(len(x["text"]) for x in xs)))
    return lines, body, width


def prepare_scan_page(record: dict) -> dict:
    notes = _notes(record)
    raw = str(record.get("text") or "")
    corrected = str(record.get("proofread_text") or "")
    correction_current = bool(corrected and record.get("proofread_source_sha256") ==
                              hashlib.sha256(raw.encode("utf-8")).hexdigest())
    if correction_current:
        raw = corrected
    if (notes.get("engine") != "paddleocr-local" or notes.get("reading_direction") != "vertical"
            or correction_current):
        return {"text": raw, "structure": [], "version": VERSION}
    lines, main, width = _geometry(record)
    if len(main) < 2:
        return {"text": raw, "structure": [], "version": VERSION}
    left = min(x["box"][0] for x in main)
    right = max(x["box"][2] for x in main)
    bottom = max(x["box"][3] for x in main)
    baseline = statistics.median(x["box"][1] for x in main)
    pitch = statistics.median((x["box"][3] - x["box"][1]) / max(1, len(x["text"])) for x in main)
    folios = [(x["box"][1], x["box"][3]) for x in lines if str(x.get("text", "")).strip().isdigit()
              and x["box"][1] > bottom + width * .2]
    structure = []
    body = []
    for line in lines:
        x1, y1, x2, y2 = line["box"]
        text = str(line.get("text", ""))
        role = "body"
        if text.strip().isdigit() and y1 > bottom + width * .2:
            role = "folio"
        elif (y1 > bottom + width * .2 and y2 - y1 <= width * 2
              and any(y1 <= end and y2 >= start for start, end in folios)):
            # A footer may be OCR-fragmented into single glyphs or vary on
            # every page. Folio-band geometry, not exact repetition, binds it.
            role = "running_header"
        elif x2 < left - width * 3 or x1 > right + width * 3:
            role = "margin_heading"
        structure.append({"role": role, "text": text, "box": line["box"]})
        if role == "body":
            body.append(line)
    parts: list[str] = []
    previous = None
    for line in body:
        # Column wrap is not a paragraph; indentation and a short preceding
        # column are the available image-grounded paragraph boundary signals.
        indent = line["box"][1] - baseline
        new_paragraph = bool(previous and (
            indent > pitch * .6 or
            (previous["box"][3] < bottom * .85 and indent > pitch * .2)
        ))
        if new_paragraph:
            parts.append("\n\n")
        parts.append(line["text"].strip())
        previous = line
    return {"text": "".join(parts), "structure": structure, "version": VERSION}


def prepare_scan_pages(records: list[dict]) -> dict[int, dict]:
    return {int(record["pdf_page"]): prepare_scan_page(record)
            for record in records if isinstance(record.get("pdf_page"), int)}


def translation_input_sha256(record: dict, prepared: dict[int, dict]) -> str:
    """Bind source preparation and the maximum three-page context window.

    This is a per-page input identity, independent of the selected model.
    Include physical boundaries because spread translation submits each half
    separately. A wider-than-needed context window conservatively invalidates
    nearby translations when corrected context could change their meaning.
    """
    page = int(record["pdf_page"])
    ordered = sorted(prepared)
    position = ordered.index(page)
    payload = {
        "layout_version": VERSION,
        "context": [(p, prepared[p]["text"]) for p in ordered[max(0, position - 3):position + 4]],
        "physical_page_texts": record.get("physical_page_texts", []),
        "proofread_physical_page_texts": record.get("proofread_physical_page_texts", []),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def reader_layout_issues(records: list[dict], chapters: dict[str, str],
                         chapter_pages: dict[str, tuple[int, int]] | None = None) -> list[dict]:
    """Flag image-positioned footer copies in reader prose; never delete text."""
    # Raw geometry remains evidence even when a proofreading overlay is used.
    raw_records = [{**r, "proofread_text": ""} for r in records]
    layout = prepare_scan_pages(raw_records)
    issues = []
    for chapter_id, markdown in chapters.items():
        bounds = (chapter_pages or {}).get(chapter_id)
        if bounds is None and str(chapter_id).isdigit():
            bounds = (int(chapter_id), int(chapter_id))
        headers, folios, fragments = set(), set(), set()
        for page, value in layout.items():
            if bounds and not bounds[0] <= page <= bounds[1]:
                continue
            for line in value['structure']:
                text = line['text'].strip()
                if line['role'] == 'folio':
                    folios.add(text)
                elif line['role'] == 'running_header':
                    (headers if len(text) >= 4 else fragments).add(text)
        blocks = [p.strip() for p in re.split(r'\n\s*\n', markdown)
                  if p.strip() and not p.lstrip().startswith('#')]
        fragment_run = []
        for block in blocks + ['']:
            if block in fragments:
                fragment_run.append(block)
                continue
            if len(fragment_run) >= 2:
                issues.append({'chapter_id': chapter_id, 'code': 'reader_footer_fragments',
                               'text': '\n'.join(fragment_run)})
            fragment_run = []
            if block in headers or block in folios:
                issues.append({'chapter_id': chapter_id, 'code': 'reader_header_footer_copy',
                               'text': block})
    return issues
