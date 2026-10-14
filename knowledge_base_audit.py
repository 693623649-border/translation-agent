"""Shared structural audit for reader-facing knowledge bases.

Both release gates have to agree on what a release-ready knowledge base is:

* ``publication_verifier`` runs ``knowledge_base.structure`` for the PDF/Word
  full profile;
* ``epub_publication_verifier`` runs the same rules inside the EPUB-native
  profile (``knowledge_base.structure`` again, but in the EPUB report).

The derivation rules live here once so the two gates cannot drift: five-field
rows, 40-character lowercase SHA-1 ids, per-chapter stable ids derived from the
source file name, contiguous chapter order, coverage of every chapter with a
non-empty reader body, and a lossless in-order rejoin of the chapter Markdown.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

import rag_knowledge_base


KB_FIELDS = frozenset({"id", "title", "chapter_id", "chapter_order", "content"})
ROW_ID = re.compile(r"[0-9a-f]{40}")
DEFAULT_ROW_SOURCE = "compiled"


def reader_body(markdown: str) -> str:
    """Return reader content without its validated document H1."""

    lines = markdown.splitlines()
    return "\n".join(lines[1:]).strip() if lines else ""


def chapter_expectation(
    *,
    chapter_id: str,
    title: str,
    order: int,
    markdown: str,
    reviewed_override: bool = False,
) -> dict[str, Any]:
    """Describe one chapter as the knowledge-base publisher sees it."""

    return {
        "id": str(chapter_id),
        "title": str(title),
        "order": int(order),
        "body": reader_body(str(markdown)),
        "row_source": "reviewed" if reviewed_override else DEFAULT_ROW_SOURCE,
    }


def _issue(code: str, message: str, **context: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "message": message}
    for key, value in context.items():
        if value is not None:
            payload[key] = value
    return payload


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_rows(text: str, *, path: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse JSONL rows, reporting every line that is not a JSON object."""

    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(
                _issue(
                    "knowledge_base_invalid_jsonl",
                    "knowledge base line is not valid JSON.",
                    path=path or None,
                    line=line_number,
                    detail=str(exc),
                )
            )
            continue
        if not isinstance(row, dict):
            issues.append(
                _issue(
                    "knowledge_base_row_invalid",
                    "knowledge base line is not a JSON object.",
                    path=path or None,
                    line=line_number,
                )
            )
            continue
        rows.append(row)
    return rows, issues


def audit_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_name: str,
    chapters: Sequence[Mapping[str, Any]],
    path: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Audit parsed rows against the chapter expectations.

    ``chapters`` carries the entries built by :func:`chapter_expectation`.
    Returned issue codes match the PDF/Word gate so both reports speak the same
    language.
    """

    issues: list[dict[str, Any]] = []
    location = path or None
    by_id = {str(chapter["id"]): chapter for chapter in chapters}
    rows_by_chapter: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    row_ids: list[str] = []
    encountered_orders: list[int] = []

    for index, row in enumerate(rows, start=1):
        keys = set(row)
        if keys != KB_FIELDS:
            issues.append(
                _issue(
                    "knowledge_base_fields_invalid",
                    "knowledge base rows must use exactly the five canonical fields.",
                    path=location,
                    line=index,
                    expected=sorted(KB_FIELDS),
                    actual=sorted(keys),
                )
            )
        chapter_id_value = row.get("chapter_id")
        row_id_value = row.get("id")
        order_value = row.get("chapter_order")
        content_value = row.get("content")
        title_value = row.get("title")
        chapter_id = chapter_id_value if isinstance(chapter_id_value, str) else ""
        row_id = row_id_value if isinstance(row_id_value, str) else ""
        order_valid = isinstance(order_value, int) and not isinstance(order_value, bool)
        row_ids.append(row_id)
        for field_name, valid in (
            ("id", isinstance(row_id_value, str)),
            ("title", isinstance(title_value, str)),
            ("chapter_id", isinstance(chapter_id_value, str)),
            ("chapter_order", order_valid),
            ("content", isinstance(content_value, str)),
        ):
            if not valid:
                issues.append(
                    _issue(
                        "knowledge_base_field_type_invalid",
                        "knowledge base field type does not match the schema.",
                        path=location,
                        line=index,
                        field=field_name,
                    )
                )
        chapter = by_id.get(chapter_id)
        if chapter is None:
            issues.append(
                _issue(
                    "knowledge_base_unknown_chapter",
                    "knowledge base row points at a chapter outside the manifest.",
                    path=location,
                    line=index,
                    chapter_id=chapter_id,
                )
            )
        else:
            if isinstance(title_value, str) and title_value != str(chapter["title"]):
                issues.append(
                    _issue(
                        "knowledge_base_title_mismatch",
                        "knowledge base title does not match the chapter display title.",
                        path=location,
                        line=index,
                        chapter_id=chapter_id,
                        expected=str(chapter["title"]),
                        actual=title_value,
                    )
                )
            if order_valid and int(order_value) != int(chapter["order"]):
                issues.append(
                    _issue(
                        "knowledge_base_order_mismatch",
                        "knowledge base chapter order does not match the manifest.",
                        path=location,
                        line=index,
                        chapter_id=chapter_id,
                        expected=int(chapter["order"]),
                        actual=int(order_value),
                    )
                )
            rows_by_chapter[chapter_id].append(row)
        if not ROW_ID.fullmatch(row_id):
            issues.append(
                _issue(
                    "knowledge_base_id_invalid",
                    "knowledge base ids must be 40-character lowercase SHA-1 digests.",
                    path=location,
                    line=index,
                    id=row_id,
                )
            )
        if not str(content_value or "").strip():
            issues.append(
                _issue(
                    "knowledge_base_content_empty",
                    "knowledge base content must not be empty.",
                    path=location,
                    line=index,
                )
            )
        if order_valid:
            encountered_orders.append(int(order_value))

    duplicates = sorted(
        identifier for identifier, count in Counter(row_ids).items() if count > 1
    )
    if duplicates:
        issues.append(
            _issue(
                "knowledge_base_duplicate_ids",
                "knowledge base ids must be unique.",
                path=location,
                values=duplicates,
            )
        )
    if encountered_orders != sorted(encountered_orders):
        issues.append(
            _issue(
                "knowledge_base_row_order_invalid",
                "knowledge base rows must follow chapter order.",
                path=location,
                actual=encountered_orders,
            )
        )

    empty_chapter_ids = {
        str(chapter["id"])
        for chapter in chapters
        if not str(chapter.get("body") or "").strip()
    }
    missing_chapters = [
        str(chapter["id"])
        for chapter in chapters
        if str(chapter["id"]) not in empty_chapter_ids
        and not rows_by_chapter.get(str(chapter["id"]))
    ]
    if missing_chapters:
        issues.append(
            _issue(
                "knowledge_base_chapter_missing",
                "knowledge base does not cover every chapter with reader content.",
                path=location,
                values=missing_chapters,
            )
        )

    content_match_count = 0
    for chapter in chapters:
        chapter_id = str(chapter["id"])
        chapter_rows = rows_by_chapter.get(chapter_id) or []
        chunks = [str(row.get("content") or "").strip() for row in chapter_rows]
        if not chunks:
            continue
        expected = str(chapter.get("body") or "")
        row_source = str(chapter.get("row_source") or DEFAULT_ROW_SOURCE)
        expected_ids = [
            hashlib.sha1(
                f"{source_name}:{chapter_id}:{row_source}:{chunk_index}".encode(
                    "utf-8"
                )
            ).hexdigest()
            for chunk_index in range(1, len(chunks) + 1)
        ]
        actual_ids = [str(row.get("id") or "") for row in chapter_rows]
        if actual_ids != expected_ids:
            issues.append(
                _issue(
                    "knowledge_base_stable_ids_mismatch",
                    "knowledge base ids do not match the stable SHA-1 derivation.",
                    path=location,
                    chapter_id=chapter_id,
                    expected=expected_ids,
                    actual=actual_ids,
                )
            )
        # ``split_text`` normally separates chunks on paragraph boundaries, but
        # a single paragraph longer than the chunk budget is cut inside the
        # paragraph.  Validate ordered, lossless coverage without inventing one
        # fixed join delimiter for both cases.
        cursor = 0
        coverage_ok = True
        for chunk in chunks:
            position = expected.find(chunk, cursor)
            if position < 0 or expected[cursor:position].strip():
                coverage_ok = False
                break
            cursor = position + len(chunk)
        if expected[cursor:].strip():
            coverage_ok = False
        if coverage_ok:
            content_match_count += 1
        else:
            issues.append(
                _issue(
                    "knowledge_base_chapter_content_mismatch",
                    "knowledge base chunks do not rebuild the chapter Markdown.",
                    path=location,
                    chapter_id=chapter_id,
                    expected_sha256=_sha256_text(expected),
                    actual_sha256=_sha256_text("\n\n".join(chunks)),
                )
            )

    covered_chapter_count = sum(
        1 for chapter in chapters if rows_by_chapter.get(str(chapter["id"]))
    )
    metrics: dict[str, Any] = {
        "path": path,
        "chunk_count": len(rows),
        "covered_chapter_count": covered_chapter_count,
        "content_match_chapter_count": content_match_count,
        "chapter_count": len(chapters),
        "empty_chapter_count": len(empty_chapter_ids),
    }
    return issues, metrics


def audit_knowledge_base(
    path: Path | str,
    *,
    source_name: str,
    chapters: Sequence[Mapping[str, Any]],
    check_manifest: bool = True,
) -> dict[str, Any]:
    """Audit one ``knowledge_base.jsonl`` without loading a model or network.

    The returned mapping always carries ``present`` and ``issues``; ``metrics``
    is populated once the file could be read.
    """

    target = Path(path)
    report: dict[str, Any] = {
        "present": False,
        "path": str(target),
        "issues": [],
        "metrics": {},
    }
    if not target.is_file():
        return report
    report["present"] = True
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        report["issues"] = [
            _issue("knowledge_base_unreadable", str(exc), path=str(target))
        ]
        return report
    rows, issues = read_rows(text, path=str(target))
    audit_issues, metrics = audit_rows(
        rows, source_name=source_name, chapters=chapters, path=str(target)
    )
    report["issues"] = [*issues, *audit_issues]
    report["metrics"] = metrics
    if check_manifest:
        report["issues"].extend(_manifest_issues(target, metrics))
    return report


def _manifest_issues(target: Path, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_path = rag_knowledge_base.manifest_path_for(target)
    metrics["rag_manifest_path"] = str(manifest_path)
    try:
        if not rag_knowledge_base.rag_manifest_is_current(target):
            raise rag_knowledge_base.RagError("RAG manifest is missing or stale")
        manifest = rag_knowledge_base.read_rag_manifest(target)
        status = str(manifest["retrieval"]["embedding"]["status"])
    except Exception as exc:  # RagError, format drift, unreadable manifest
        metrics["rag_lexical_status"] = "invalid"
        metrics["rag_embedding_status"] = "invalid"
        return [
            _issue(
                "knowledge_base_rag_manifest_invalid",
                "RAG manifest is missing, stale, or out of sync with the knowledge base.",
                path=str(manifest_path),
                detail=str(exc),
            )
        ]
    metrics["rag_lexical_status"] = "ready"
    metrics["rag_embedding_status"] = status or "invalid"
    return []


def chapter_expectations(
    manifest: Iterable[Mapping[str, Any]],
    chapters: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Pair a chapter manifest with reader Markdown into audit expectations."""

    expectations: list[dict[str, Any]] = []
    for item, chapter in zip(manifest, chapters):
        expectations.append(
            chapter_expectation(
                chapter_id=str(item.get("id") or ""),
                title=str(item.get("display_title") or item.get("title") or ""),
                order=int(item.get("sequence") or 0),
                markdown=str(chapter.get("markdown") or ""),
                reviewed_override=item.get("reviewed_override") is True,
            )
        )
    return expectations
