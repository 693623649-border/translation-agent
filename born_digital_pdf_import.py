"""Deterministically import a born-digital PDF into semantic chapters.

This module deliberately does not OCR and does not infer document semantics
from visual typography.  A usable embedded text layer is mandatory.  PDF
outline destinations are the only automatic chapter boundaries; a document
without an outline becomes one chapter instead of guessing from font sizes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any, Iterable

import fitz

from publication_semantics import (
    markdown_footnote_contract_sha256,
    parse_markdown_footnotes,
    semantic_audit_summary,
)


SCHEMA_VERSION = 1
IMPORTER_VERSION = "born-digital-pdf-semantic-v1"


class BornDigitalPdfError(ValueError):
    """Raised for an invalid source or an unsafe semantic reconstruction."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _quality_metrics(document: fitz.Document) -> dict[str, Any]:
    pages: list[dict[str, Any]] = []
    total_chars = 0
    replacement_chars = 0
    control_chars = 0
    text_pages = 0
    substantial_pages = 0
    image_only_pages: list[int] = []
    empty_pages: list[int] = []
    for index, page in enumerate(document, start=1):
        text = page.get_text("text", sort=True)
        visible = "".join(character for character in text if not character.isspace())
        chars = len(visible)
        replacements = visible.count("\ufffd") + visible.count("\x00")
        controls = sum(
            unicodedata.category(character) == "Cc"
            for character in visible
            if character not in "\n\r\t"
        )
        image_count = len(page.get_images(full=True))
        if chars:
            text_pages += 1
        if chars >= 40:
            substantial_pages += 1
        if chars < 20 and image_count:
            image_only_pages.append(index)
        elif not chars:
            empty_pages.append(index)
        total_chars += chars
        replacement_chars += replacements
        control_chars += controls
        pages.append(
            {
                "pdf_page": index,
                "character_count": chars,
                "image_count": image_count,
                "replacement_character_count": replacements,
                "control_character_count": controls,
            }
        )
    count = document.page_count
    boundary_empty_pages: list[int] = []
    for page_number in range(1, count + 1):
        if page_number in empty_pages:
            boundary_empty_pages.append(page_number)
        else:
            break
    for page_number in range(count, 0, -1):
        if page_number in empty_pages and page_number not in boundary_empty_pages:
            boundary_empty_pages.append(page_number)
        elif page_number not in empty_pages:
            break
    candidate_count = max(1, count - len(boundary_empty_pages))
    text_ratio = text_pages / candidate_count if count else 0.0
    substantial_ratio = substantial_pages / candidate_count if count else 0.0
    corrupt_ratio = (
        (replacement_chars + control_chars) / total_chars if total_chars else 1.0
    )
    image_only_ratio = len(image_only_pages) / count if count else 1.0
    return {
        "page_count": count,
        "text_page_count": text_pages,
        "substantial_text_page_count": substantial_pages,
        "total_character_count": total_chars,
        "average_characters_per_page": round(total_chars / count, 3) if count else 0.0,
        "text_page_ratio": round(text_ratio, 6),
        "substantial_text_page_ratio": round(substantial_ratio, 6),
        "corrupt_character_ratio": round(corrupt_ratio, 8),
        "image_only_page_ratio": round(image_only_ratio, 6),
        "image_only_pages": image_only_pages,
        "empty_pages": empty_pages,
        "boundary_empty_pages": sorted(boundary_empty_pages),
        "quality_candidate_page_count": candidate_count,
        "pages": pages,
    }


def _quality_issues(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    page_count = int(metrics["page_count"])
    if page_count < 1:
        issues.append(
            {
                "code": "pdf_empty",
                "message": "PDF 没有页面。",
                "blocking": True,
                "evidence": {},
            }
        )
        return issues
    if int(metrics["total_character_count"]) < max(40, page_count * 20):
        issues.append(
            {
                "code": "pdf_text_layer_too_sparse",
                "message": "PDF 内嵌文字层总量不足，不能安全作为语义源；请改用 OCR。",
                "blocking": True,
                "evidence": {
                    "total_character_count": metrics["total_character_count"],
                    "minimum_required": max(40, page_count * 20),
                },
            }
        )
    minimum_text_ratio = 0.6 if page_count >= 10 else 0.75
    if float(metrics["text_page_ratio"]) < minimum_text_ratio:
        issues.append(
            {
                "code": "pdf_text_layer_page_coverage_low",
                "message": "具有可读文字层的页面比例过低，疑似扫描 PDF；请改用 OCR。",
                "blocking": True,
                "evidence": {
                    "text_page_ratio": metrics["text_page_ratio"],
                    "minimum_required": minimum_text_ratio,
                    "image_only_pages": metrics["image_only_pages"][:40],
                },
            }
        )
    if float(metrics["image_only_page_ratio"]) > (0.25 if page_count >= 10 else 0.0):
        issues.append(
            {
                "code": "pdf_image_only_coverage_high",
                "message": "图片页占比过高，不能证明文字层完整；请改用 OCR。",
                "blocking": True,
                "evidence": {
                    "image_only_page_ratio": metrics["image_only_page_ratio"],
                    "image_only_pages": metrics["image_only_pages"][:40],
                },
            }
        )
    if float(metrics["corrupt_character_ratio"]) > 0.01:
        issues.append(
            {
                "code": "pdf_text_layer_corrupt_glyphs",
                "message": "PDF 文字层包含过多损坏或控制字符；请改用 OCR。",
                "blocking": True,
                "evidence": {
                    "corrupt_character_ratio": metrics["corrupt_character_ratio"]
                },
            }
        )
    if metrics["empty_pages"]:
        issues.append(
            {
                "code": "pdf_empty_pages_retained",
                "message": "检测到空白页；空白页不产生正文，但已保留在审计中。",
                "blocking": False,
                "evidence": {
                    "pages": metrics["empty_pages"][:40],
                    "boundary_empty_pages_excluded_from_coverage": metrics[
                        "boundary_empty_pages"
                    ][:40],
                },
            }
        )
    return issues


def _escape_cell(value: str) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def _table_markdown(page: fitz.Page) -> tuple[list[tuple[fitz.Rect, str]], list[dict[str, Any]]]:
    rendered: list[tuple[fitz.Rect, str]] = []
    issues: list[dict[str, Any]] = []
    try:
        finder = page.find_tables()
    except Exception as exc:
        issues.append(
            {
                "code": "pdf_table_detection_unavailable",
                "message": "本页表格检测失败，正文文字仍保留。",
                "blocking": False,
                "evidence": {"error": str(exc)[:200]},
            }
        )
        return rendered, issues
    for position, table in enumerate(finder.tables, start=1):
        rows = table.extract()
        if not rows or max((len(row) for row in rows), default=0) < 2:
            continue
        width = max(len(row) for row in rows)
        clean = [[_escape_cell(cell) for cell in row] + [""] * (width - len(row)) for row in rows]
        lines = [
            "| " + " | ".join(clean[0]) + " |",
            "| " + " | ".join(["---"] * width) + " |",
            *("| " + " | ".join(row) + " |" for row in clean[1:]),
        ]
        rendered.append((fitz.Rect(table.bbox), "\n".join(lines)))
    return rendered, issues


def _span_text(span: dict[str, Any], links: list[dict[str, Any]]) -> tuple[str, int]:
    value = str(span.get("text") or "")
    if not value:
        return "", 0
    flags = int(span.get("flags") or 0)
    rendered = value
    if flags & (1 << 1):
        rendered = f"*{rendered}*"
    if flags & (1 << 4):
        rendered = f"**{rendered}**"
    superscript_count = int(bool(flags & 1))
    if superscript_count:
        rendered = f"<sup>{rendered}</sup>"
    box = fitz.Rect(span.get("bbox") or (0, 0, 0, 0))
    matching = [
        item
        for item in links
        if item.get("kind") == fitz.LINK_URI
        and item.get("uri")
        and fitz.Rect(item["from"]).intersects(box)
    ]
    if len(matching) == 1:
        rendered = f"[{rendered}]({matching[0]['uri']})"
    return rendered, superscript_count


def _join_lines(lines: list[str]) -> str:
    result = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if not result:
            result = line
        elif result.endswith("-") and re.match(r"^[A-Za-z]", line):
            result = result[:-1] + line
        elif result[-1:].isascii() and line[:1].isascii():
            result += " " + line
        else:
            result += line
    return result.strip()


def _page_markdown(page: fitz.Page) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    tables, issues = _table_markdown(page)
    links = page.get_links()
    dictionary = page.get_text("dict", sort=True)
    items: list[tuple[float, float, str]] = []
    superscripts = 0
    for box, markdown in tables:
        items.append((box.y0, box.x0, markdown))
    for block in dictionary.get("blocks", []):
        if int(block.get("type", -1)) != 0:
            continue
        block_box = fitz.Rect(block.get("bbox") or (0, 0, 0, 0))
        if any(table_box.contains(block_box) or table_box.intersects(block_box) for table_box, _ in tables):
            continue
        lines: list[str] = []
        for line in block.get("lines", []):
            pieces: list[str] = []
            for span in line.get("spans", []):
                rendered, count = _span_text(span, links)
                superscripts += count
                pieces.append(rendered)
            joined = "".join(pieces).strip()
            if joined:
                lines.append(joined)
        paragraph = _join_lines(lines)
        if paragraph:
            items.append((block_box.y0, block_box.x0, paragraph))
    items.sort(key=lambda item: (round(item[0], 2), round(item[1], 2)))
    return "\n\n".join(item[2] for item in items).strip(), {
        "table_count": len(tables),
        "uri_link_count": sum(item.get("kind") == fitz.LINK_URI for item in links),
        "visible_superscript_count": superscripts,
    }, issues


def _safe_title(value: str, fallback: str) -> str:
    title = " ".join(str(value or "").split()).strip()
    return title[:240] or fallback


def _slug(value: str) -> str:
    result = re.sub(r"[^\w\-一-龥]+", "_", value, flags=re.UNICODE).strip("_")
    return result[:80] or "chapter"


def _chapter_boundaries(document: fitz.Document, book_title: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    outline = document.get_toc(simple=True)
    if not outline:
        return [
            {"level": 1, "title": book_title, "start_page": 1, "source": "single-document-fallback"}
        ], [
            {
                "code": "pdf_outline_missing_single_chapter",
                "message": "PDF 没有 outline；为避免猜测标题结构，整本按一个章节导入。",
                "blocking": False,
                "evidence": {},
            }
        ]
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    seen_pages: dict[int, str] = {}
    for level, title, page_number, *_ in outline:
        page_number = int(page_number)
        if not 1 <= page_number <= document.page_count:
            issues.append(
                {
                    "code": "pdf_outline_destination_invalid",
                    "message": "PDF outline 包含无效页码，无法安全分章。",
                    "blocking": True,
                    "evidence": {"title": title, "pdf_page": page_number},
                }
            )
            continue
        if page_number in seen_pages:
            issues.append(
                {
                    "code": "pdf_outline_same_page_ambiguous",
                    "message": "同一页存在多个 outline 目标且没有可靠坐标，无法确定章节边界。",
                    "blocking": True,
                    "evidence": {"pdf_page": page_number, "titles": [seen_pages[page_number], title]},
                }
            )
            continue
        seen_pages[page_number] = str(title)
        entries.append(
            {
                "level": max(1, min(6, int(level))),
                "title": _safe_title(title, f"Page {page_number}"),
                "start_page": page_number,
                "source": "pdf-outline",
            }
        )
    entries.sort(key=lambda item: item["start_page"])
    if entries and entries[0]["start_page"] > 1:
        entries.insert(0, {"level": 1, "title": book_title, "start_page": 1, "source": "pre-outline-frontmatter"})
    return entries, issues


def _translation_units(chapter_id: str, markdown: str, source_pages: str) -> list[dict[str, Any]]:
    blocks = [block.strip() for block in re.split(r"\n{2,}", markdown.strip()) if block.strip()]
    units: list[dict[str, Any]] = []
    for sequence, block in enumerate(blocks, start=1):
        source_sha = _sha256_bytes(block.encode())
        digest = hashlib.sha256(f"{chapter_id}\0{sequence}\0{source_sha}".encode()).hexdigest()[:16]
        kind = "heading" if re.match(r"^#{1,6}\s", block) else "table" if block.startswith("| ") else "paragraph"
        units.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": f"{chapter_id}-u{sequence:04d}-{digest}",
                "chapter_id": chapter_id,
                "sequence": sequence,
                "kind": kind,
                "source_pages": source_pages,
                "source_sha256": source_sha,
                "source_markdown": block,
            }
        )
    return units


def import_born_digital_pdf(source: Path, output_dir: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise BornDigitalPdfError(f"input must be an existing PDF: {source}")
    with fitz.open(source) as document:
        if not document.is_pdf:
            raise BornDigitalPdfError(f"input is not a PDF: {source}")
        metadata = dict(document.metadata or {})
        book_title = _safe_title(metadata.get("title"), source.stem)
        author = _safe_title(metadata.get("author"), "") if metadata.get("author") else ""
        quality = _quality_metrics(document)
        issues = _quality_issues(quality)
        source_record = {
            "path": str(source),
            "sha256": _sha256_bytes(source.read_bytes()),
            "metadata": {"title": book_title, "author": author},
            "text_layer_quality": quality,
        }
        if any(issue["blocking"] for issue in issues):
            audit = {
                "schema_version": SCHEMA_VERSION,
                "status": "blocked",
                "release_blocked": True,
                "generated_by": "core.source.pdf.text+core.reconstruct.semantic",
                "contract_mode": "born-digital-pdf-text-layer",
                "importer_version": IMPORTER_VERSION,
                "source": source_record,
                "summary": {
                    "chapter_count": 0,
                    "footnote_count": 0,
                    "issue_count": len(issues),
                    "blocking_issue_count": sum(issue["blocking"] for issue in issues),
                    "release_blocked": True,
                },
                "issues": issues,
                "chapters": [],
            }
            audit_path = output_dir / "audit" / "semantic-reconstruction.json"
            _atomic_json(audit_path, audit)
            return {"status": "blocked", "release_blocked": True, "chapter_count": 0, "audit": str(audit_path)}

        boundaries, outline_issues = _chapter_boundaries(document, book_title)
        issues.extend(outline_issues)
        pages: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = [
            _page_markdown(page) for page in document
        ]

    chapter_dir = output_dir / "chapters"
    source_dir = output_dir / "semantic" / "source_chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    toc_entries: list[dict[str, Any]] = []
    audit_chapters: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    expected: set[str] = set()
    for sequence, boundary in enumerate(boundaries, start=1):
        start = int(boundary["start_page"])
        end = int(boundaries[sequence]["start_page"]) - 1 if sequence < len(boundaries) else len(pages)
        chapter_id = f"pdf-text-{sequence:04d}"
        title = str(boundary["title"])
        filename = f"{sequence:03d}_{_slug(title)}.md"
        expected.add(filename)
        page_bodies = [pages[index - 1][0] for index in range(start, end + 1) if pages[index - 1][0]]
        markdown = f"# {title}\n\n" + "\n\n".join(page_bodies)
        markdown = markdown.rstrip() + "\n"
        inventory = parse_markdown_footnotes(markdown)
        chapter_issues = [
            {**issue, "source_page": f"pdf-{start:04d}-{end:04d}"}
            for index in range(start, end + 1)
            for issue in pages[index - 1][2]
        ]
        source_pages = f"{start}-{end}"
        chapter_units = _translation_units(chapter_id, markdown, source_pages)
        metrics = {
            "table_count": sum(pages[index - 1][1]["table_count"] for index in range(start, end + 1)),
            "uri_link_count": sum(pages[index - 1][1]["uri_link_count"] for index in range(start, end + 1)),
            "visible_superscript_count": sum(pages[index - 1][1]["visible_superscript_count"] for index in range(start, end + 1)),
        }
        if metrics["visible_superscript_count"] and not inventory.definitions:
            chapter_issues.append(
                {
                    "code": "pdf_visible_superscript_unresolved",
                    "message": (
                        "检测到可见上标，但 PDF 文字层不能证明其脚注定义和引用落点；"
                        "必须人工复核或改用保留版面的重建入口。"
                    ),
                    "source_page": f"pdf-{start:04d}-{end:04d}",
                    "note_label": None,
                    "blocking": True,
                    "evidence": {
                        "visible_superscript_count": metrics[
                            "visible_superscript_count"
                        ]
                    },
                }
            )
        _atomic_text(chapter_dir / filename, markdown)
        _atomic_text(source_dir / filename, markdown)
        units.extend(chapter_units)
        manifest.append(
            {
                "id": chapter_id,
                "sequence": sequence,
                "level": int(boundary["level"]),
                "index": "",
                "kind": (
                    "frontmatter"
                    if boundary["source"] == "pre-outline-frontmatter"
                    else "chapter"
                    if int(boundary["level"]) == 1
                    else "section"
                ),
                "title": title,
                "display_title": title,
                "filename": filename,
                "printed_page": None,
                "pdf_page": start,
                "end_pdf_page": end,
                "granularity": "all",
                "source_format": "pdf-text-layer",
                "source_pages": source_pages,
                "source_boundary": boundary["source"],
                "reviewed_override": False,
                "semantic_footnote_count": len(inventory.definitions),
                "semantic_issue_count": len(chapter_issues),
            }
        )
        toc_entries.append(
            {
                "id": chapter_id,
                "index": "",
                "title": title,
                "level": int(boundary["level"]),
                "kind": manifest[-1]["kind"],
                "printed_page": None,
                "pdf_page": start,
                "end_pdf_page": end,
            }
        )
        audit_chapters.append(
            {
                "chapter_id": chapter_id,
                "filename": filename,
                "source_pages": source_pages,
                "markdown_sha256": _sha256_bytes(markdown.encode()),
                "footnote_contract_sha256": markdown_footnote_contract_sha256(markdown),
                "footnote_count": len(inventory.definitions),
                "translation_unit_count": len(chapter_units),
                **metrics,
                "footnote_mode": "visible-superscript-preserved-no-inferred-definition",
                "issues": chapter_issues,
                "release_blocked": any(issue.get("blocking", True) for issue in chapter_issues),
            }
        )
    for directory in (chapter_dir, source_dir):
        for stale in directory.glob("*.md"):
            if stale.name not in expected:
                stale.unlink()
    summary = semantic_audit_summary(audit_chapters)
    root_blocking = sum(bool(issue.get("blocking", True)) for issue in issues)
    summary["issue_count"] += len(issues)
    summary["blocking_issue_count"] += root_blocking
    summary["release_blocked"] = bool(summary["blocking_issue_count"])
    summary.update(
        {
            "translation_unit_count": len(units),
            "table_count": sum(item["table_count"] for item in audit_chapters),
            "uri_link_count": sum(item["uri_link_count"] for item in audit_chapters),
            "visible_superscript_count": sum(item["visible_superscript_count"] for item in audit_chapters),
        }
    )
    audit = {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked" if summary["release_blocked"] else "passed",
        "release_blocked": summary["release_blocked"],
        "generated_by": "core.source.pdf.text+core.reconstruct.semantic",
        "contract_mode": "born-digital-pdf-text-layer",
        "importer_version": IMPORTER_VERSION,
        "source": source_record,
        "summary": summary,
        "issues": issues,
        "chapters": audit_chapters,
    }
    _atomic_json(output_dir / "chapters.json", manifest)
    _atomic_json(
        output_dir / "toc.json",
        {
            "schema_version": SCHEMA_VERSION,
            "toc_pdf_pages": [],
            "page_offset": 0,
            "printed_pages_per_pdf_page": 1,
            "offset_evidence": [
                {
                    "source": "pdf-outline",
                    "message": (
                        "Born-digital PDF chapter boundaries use one-based "
                        "physical PDF destinations directly."
                    ),
                }
            ],
            "entries": toc_entries,
        },
    )
    _atomic_json(output_dir / "audit" / "semantic-reconstruction.json", audit)
    _atomic_text(output_dir / "semantic" / "translation-units.jsonl", "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units))
    return {
        "status": audit["status"],
        "release_blocked": audit["release_blocked"],
        "chapter_count": len(manifest),
        "translation_unit_count": len(units),
        "title": book_title,
        "author": author,
        "manifest": str((output_dir / "chapters.json").resolve()),
        "toc": str((output_dir / "toc.json").resolve()),
        "chapters": str(chapter_dir.resolve()),
        "translation_units": str((output_dir / "semantic" / "translation-units.jsonl").resolve()),
        "audit": str((output_dir / "audit" / "semantic-reconstruction.json").resolve()),
    }


def _jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BornDigitalPdfError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise BornDigitalPdfError(f"translation unit must be an object: {path}:{line_number}")
        records.append(value)
    return records


def apply_translations(output_dir: Path, translations_path: Path) -> dict[str, Any]:
    """Apply hash-bound translations while preserving the semantic structure."""

    output_dir = output_dir.expanduser().resolve()
    reconstruction_path = output_dir / "audit" / "semantic-reconstruction.json"
    try:
        reconstruction = json.loads(reconstruction_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BornDigitalPdfError(
            "semantic reconstruction audit is missing or invalid"
        ) from exc
    if not isinstance(reconstruction, dict):
        raise BornDigitalPdfError("semantic reconstruction audit root must be an object")
    summary_value = reconstruction.get("summary")
    if (
        reconstruction.get("status") == "blocked"
        or reconstruction.get("release_blocked") is True
        or (
            isinstance(summary_value, dict)
            and summary_value.get("release_blocked") is True
        )
    ):
        raise BornDigitalPdfError(
            "semantic reconstruction is blocked; translations cannot be applied"
        )
    source_units = _jsonl(output_dir / "semantic" / "translation-units.jsonl")
    translated_units = _jsonl(translations_path.expanduser().resolve())
    source_by_id = {str(item.get("id") or ""): item for item in source_units}
    translated_by_id = {str(item.get("id") or ""): item for item in translated_units}
    if len(source_by_id) != len(source_units) or len(translated_by_id) != len(translated_units):
        raise BornDigitalPdfError("source or translated unit ids are duplicated")
    if set(source_by_id) != set(translated_by_id):
        raise BornDigitalPdfError("translation unit set does not match the imported source")
    by_chapter: dict[str, list[tuple[int, str]]] = {}
    for unit_id, source in source_by_id.items():
        translated = translated_by_id[unit_id]
        source_text = str(source.get("source_markdown") or "")
        source_sha = _sha256_bytes(source_text.encode())
        if source_sha != source.get("source_sha256") or translated.get("source_sha256") != source_sha:
            raise BornDigitalPdfError(f"translation source hash mismatch: {unit_id}")
        value = str(translated.get("translated_markdown") or "").strip()
        if not value:
            raise BornDigitalPdfError(f"translated_markdown is empty: {unit_id}")
        by_chapter.setdefault(str(source["chapter_id"]), []).append(
            (int(source["sequence"]), value)
        )
    manifest_path = output_dir / "chapters.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit_chapters: list[dict[str, Any]] = []
    for item in manifest:
        chapter_id = str(item["id"])
        parts = [value for _, value in sorted(by_chapter.get(chapter_id, []))]
        if not parts:
            raise BornDigitalPdfError(f"translation is missing chapter: {chapter_id}")
        markdown = "\n\n".join(parts).rstrip() + "\n"
        source_markdown = (
            output_dir / "semantic" / "source_chapters" / item["filename"]
        ).read_text(encoding="utf-8")
        source_inventory = parse_markdown_footnotes(source_markdown)
        translated_inventory = parse_markdown_footnotes(markdown)
        if (
            source_inventory.references != translated_inventory.references
            or tuple(note_id for note_id, _ in source_inventory.definitions)
            != tuple(note_id for note_id, _ in translated_inventory.definitions)
            or not translated_inventory.valid
        ):
            raise BornDigitalPdfError(
                f"translation changed the footnote contract: {chapter_id}"
            )
        heading = re.match(r"^#\s+(.+?)\s*$", markdown.splitlines()[0])
        if heading:
            item["title"] = heading.group(1).strip()
            item["display_title"] = heading.group(1).strip()
        item["translation_applied"] = True
        _atomic_text(output_dir / "chapters" / item["filename"], markdown)
        audit_chapters.append(
            {
                "chapter_id": chapter_id,
                "filename": item["filename"],
                "source_markdown_sha256": _sha256_bytes(source_markdown.encode()),
                "translated_markdown_sha256": _sha256_bytes(markdown.encode()),
                "markdown_sha256": _sha256_bytes(markdown.encode()),
                "footnote_contract_sha256": markdown_footnote_contract_sha256(markdown),
                "footnote_count": len(translated_inventory.definitions),
                "translation_unit_count": len(parts),
                "issues": [],
                "release_blocked": False,
            }
        )
    _atomic_json(manifest_path, manifest)
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "release_blocked": False,
        "generated_by": "core.source.pdf.text+core.pages.translate+core.reconstruct.semantic",
        "contract_mode": "born-digital-pdf-translated-markdown",
        "summary": {
            "chapter_count": len(audit_chapters),
            "translation_unit_count": sum(item["translation_unit_count"] for item in audit_chapters),
            "issue_count": 0,
            "blocking_issue_count": 0,
            "release_blocked": False,
        },
        "chapters": audit_chapters,
    }
    _atomic_json(output_dir / "audit" / "semantic-translation.json", report)
    reconstruction["status"] = "passed"
    reconstruction["release_blocked"] = False
    reconstruction["generated_by"] = (
        "core.source.pdf.text+core.pages.translate+core.reconstruct.semantic"
    )
    reconstruction["contract_mode"] = "born-digital-pdf-translated-markdown"
    reconstruction["translated_audit"] = "audit/semantic-translation.json"
    reconstruction["summary"] = {
        "chapter_count": len(audit_chapters),
        "footnote_count": sum(item["footnote_count"] for item in audit_chapters),
        "translation_unit_count": report["summary"]["translation_unit_count"],
        "issue_count": 0,
        "blocking_issue_count": 0,
        "release_blocked": False,
    }
    reconstruction["issues"] = []
    reconstruction["chapters"] = audit_chapters
    _atomic_json(reconstruction_path, reconstruction)
    return {**report["summary"], "status": "passed", "manifest": str(manifest_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a born-digital PDF into semantic chapters without OCR.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="extract the verified PDF text layer")
    importer.add_argument("source")
    importer.add_argument("-o", "--output-dir", required=True)
    apply = subparsers.add_parser("apply-translations", help="validate and apply translated JSONL units")
    apply.add_argument("-o", "--output-dir", required=True)
    apply.add_argument("translations")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            result = import_born_digital_pdf(Path(args.source), Path(args.output_dir))
        else:
            result = apply_translations(Path(args.output_dir), Path(args.translations))
    except (OSError, RuntimeError, BornDigitalPdfError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["release_blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
