"""Deterministic semantic reconstruction for reader-facing publications.

The OCR layer is intentionally page based, while Markdown/EPUB/DOCX are
continuous reader formats.  This module owns the boundary between those two
representations.  In particular, it turns page-local note markers and note
definitions into stable Markdown footnotes before physical page markers are
discarded.

The extractor is deliberately fail-closed: a page-local definition needs
exactly one matching marker earlier on that physical page.  A second,
explicit pass may join a circled marker to a definition on the immediately
following physical page, but only when both page-boundary and layout guards
prove a unique pairing.  Plausible definitions without an unambiguous marker
remain visible in the body and produce an audit issue that can be resolved by
a reviewed semantic override in a future pass.  No content is silently
guessed or lost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Sequence


LEGACY_NOTE_START = re.compile(
    r"(?m)^[ \t]*(?P<token>\[[ \t]*(?P<square>\d{1,4})[ \t]*\]|"
    r"［[ \t]*(?P<full_square>\d{1,4})[ \t]*］|"
    r"〔[ \t]*(?P<corner>\d{1,4})[ \t]*〕)"
    r"[ \t]*(?=\S)"
)
_CIRCLED_NOTE_LABELS = {
    "①": "1",
    "②": "2",
    "③": "3",
    "④": "4",
    "⑤": "5",
    "⑥": "6",
    "⑦": "7",
    "⑧": "8",
    "⑨": "9",
    "⑩": "10",
    "⑪": "11",
    "⑫": "12",
    "⑬": "13",
    "⑭": "14",
    "⑮": "15",
    "⑯": "16",
    "⑰": "17",
    "⑱": "18",
    "⑲": "19",
    "⑳": "20",
    "㉑": "21",
    "㉒": "22",
    "㉓": "23",
    "㉔": "24",
    "㉕": "25",
    "㉖": "26",
    "㉗": "27",
    "㉘": "28",
    "㉙": "29",
    "㉚": "30",
    "㉛": "31",
    "㉜": "32",
    "㉝": "33",
    "㉞": "34",
    "㉟": "35",
    "㊱": "36",
    "㊲": "37",
    "㊳": "38",
    "㊴": "39",
    "㊵": "40",
    "㊶": "41",
    "㊷": "42",
    "㊸": "43",
    "㊹": "44",
    "㊺": "45",
    "㊻": "46",
    "㊼": "47",
    "㊽": "48",
    "㊾": "49",
    "㊿": "50",
}
_CIRCLED_NOTE_TOKEN_PATTERN = (
    "(?:" + "|".join(re.escape(token) for token in _CIRCLED_NOTE_LABELS) + ")"
)
CIRCLED_NOTE_START = re.compile(
    rf"(?m)^[ \t]*(?P<token>{_CIRCLED_NOTE_TOKEN_PATTERN})[ \t]*(?=\S|$)"
)
_ANY_CIRCLED_NOTE = re.compile(_CIRCLED_NOTE_TOKEN_PATTERN)
MARKDOWN_NOTE_START = re.compile(
    r"^[ \t]*\[\^(?P<id>[^\]\s]+)\]:[ \t]*(?P<text>.*)$"
)
_MARKDOWN_NOTE_BLOCK_START = re.compile(
    r"(?m)^[ \t]*\[\^[^\]\s]+\]:[ \t]*"
)
_MARKDOWN_FOOTNOTE_SECTION_HEADING = re.compile(r"^[ \t]*注释：[ \t]*$")
MARKDOWN_REFERENCE = re.compile(r"\[\^(?P<id>[^\]\s]+)\]")


@dataclass(frozen=True)
class SemanticIssue:
    code: str
    message: str
    source_page: str
    note_label: str | None = None
    blocking: bool = True
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticFootnote:
    stable_id: str
    label: str
    text: str
    source_page: str
    confidence: float
    reconstruction_scope: str = "page-local"
    reference_source_page: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SemanticPage:
    body: str
    footnotes: tuple[SemanticFootnote, ...] = ()
    issues: tuple[SemanticIssue, ...] = ()

    @property
    def release_blocked(self) -> bool:
        return any(issue.blocking for issue in self.issues)

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "footnotes": [item.to_dict() for item in self.footnotes],
            "issues": [item.to_dict() for item in self.issues],
            "release_blocked": self.release_blocked,
        }


@dataclass(frozen=True)
class MarkdownFootnoteInventory:
    body: str
    definitions: tuple[tuple[str, str], ...]
    references: tuple[str, ...]
    duplicate_definitions: tuple[str, ...]
    missing_definitions: tuple[str, ...]
    unused_definitions: tuple[str, ...]
    duplicate_references: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not (
            self.duplicate_definitions
            or self.missing_definitions
            or self.unused_definitions
            or self.duplicate_references
        )

    def definition_map(self) -> dict[str, str]:
        return dict(self.definitions)


@dataclass(frozen=True)
class _PageNoteCandidate:
    start: int
    content_start: int
    token: str
    label: str
    circled: bool = False
    marker_on_own_line: bool = False


def _join_visual_lines(value: str) -> str:
    """Join OCR visual wraps without inserting spaces between CJK glyphs."""

    result = ""
    for raw in value.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not result:
            result = line
        elif (
            result[-1].isascii()
            and result[-1].isalnum()
            and line[0].isascii()
            and line[0].isalnum()
        ):
            result += " " + line
        else:
            result += line
    return result.strip()


def _strip_trailing_page_furniture(value: str) -> str:
    lines = value.rstrip().splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines[-1] = re.sub(
            r"[ \t]*(?:[●©◎]\s*)?\d{1,4}\s*(?:[●©◎])?[ \t]*$",
            "",
            lines[-1],
        ).rstrip()
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def _stable_note_id(source_page: str, label: str, occurrence: int) -> str:
    safe_page = re.sub(r"[^A-Za-z0-9_-]+", "-", source_page).strip("-") or "page"
    suffix = f"-{occurrence}" if occurrence > 1 else ""
    return f"{safe_page}-n{label}{suffix}"


def _legacy_note_reference_pattern(label: str) -> re.Pattern[str]:
    """Match visually equivalent bracket styles for one numeric note label.

    Chinese publications commonly mix ASCII square brackets, lenticular
    brackets, and ASCII/full-width parentheses between an inline marker and
    its page-bottom definition.  They carry the same semantic label.  Plain
    numbers are deliberately excluded so years, list items, and page numbers
    can never become footnote landings by accident.
    """

    escaped = re.escape(label)
    return re.compile(
        rf"(?:\[{escaped}\]|［{escaped}］|〔{escaped}〕|\({escaped}\)|（{escaped}）)"
    )


def _circled_note_label(token: str) -> str:
    """Return the canonical label for one explicitly supported circled glyph."""

    try:
        return _CIRCLED_NOTE_LABELS[token]
    except KeyError as exc:
        raise ValueError(f"unsupported circled note marker: {token!r}") from exc


def _marker_is_at_visual_line_start(value: str, start: int) -> bool:
    """Return whether a marker is only preceded by indentation on its line."""

    line_start = value.rfind("\n", 0, start) + 1
    return not value[line_start:start].strip(" \t")


def _note_candidates(value: str) -> list[_PageNoteCandidate]:
    candidates: list[_PageNoteCandidate] = []
    for match in LEGACY_NOTE_START.finditer(value):
        label = (
            match.group("square")
            or match.group("full_square")
            or match.group("corner")
            or ""
        )
        candidates.append(
            _PageNoteCandidate(
                start=match.start(),
                content_start=match.end(),
                token=match.group("token"),
                label=label,
            )
        )
    for match in CIRCLED_NOTE_START.finditer(value):
        line_end = value.find("\n", match.end())
        if line_end < 0:
            line_end = len(value)
        token = match.group("token")
        candidates.append(
            _PageNoteCandidate(
                start=match.start(),
                content_start=match.end(),
                token=token,
                label=_circled_note_label(token),
                circled=True,
                marker_on_own_line=not value[match.end() : line_end].strip(),
            )
        )
    return sorted(candidates, key=lambda item: item.start)


def _note_definition_spans(
    value: str,
    candidates: Sequence[_PageNoteCandidate],
) -> list[tuple[int, int]]:
    """Bound numeric note definitions before existing Markdown notes.

    Reviewed overlays can place a reconstructed numeric page note immediately
    before one or more pre-existing ``[^id]:`` definitions.  Those Markdown
    blocks are authoritative semantic data and must never be consumed as the
    tail of the preceding numeric definition.
    """

    markdown_starts = [
        match.start() for match in _MARKDOWN_NOTE_BLOCK_START.finditer(value)
    ]
    spans: list[tuple[int, int]] = []
    for index, candidate in enumerate(candidates):
        end = candidates[index + 1].start if index + 1 < len(candidates) else len(value)
        markdown_end = next(
            (start for start in markdown_starts if candidate.start < start < end),
            None,
        )
        if markdown_end is not None:
            end = markdown_end
        spans.append((candidate.start, end))
    return spans


def _ordinary_circled_list_candidates(
    candidates: Sequence[_PageNoteCandidate],
    reference_spans: Sequence[Sequence[tuple[int, int]]],
    *,
    text_length: int,
) -> set[int]:
    """Identify obvious early, ascending circled lists conservatively.

    A cross-referenced list (for example, ``①和②`` followed by two list
    items) is syntactically close to a footnote block.  We therefore keep an
    ascending run when its preceding markers are clustered, as list
    cross-references normally are.  An early unreferenced run whose item text
    begins on the marker lines is likewise preserved.  Real page-note
    references tend to be spread through the body, while this guard also
    ensures a line-start list marker is never counted as its own landing.
    """

    list_indices: set[int] = set()
    run: list[int] = []

    def flush() -> None:
        if len(run) < 2:
            return
        first = candidates[run[0]]
        landing_starts = [
            start
            for index in run
            for start, _end in reference_spans[index]
        ]
        clustered_cross_references = (
            len(landing_starts) >= 2
            and max(landing_starts) - min(landing_starts) <= 80
        )
        early_unreferenced_list = (
            not landing_starts
            and all(not candidates[index].marker_on_own_line for index in run)
            and first.start <= int(text_length * 0.42)
        )
        if clustered_cross_references or early_unreferenced_list:
            list_indices.update(run)

    for index, candidate in enumerate(candidates):
        if not candidate.circled:
            flush()
            run = []
            continue
        if run:
            previous = candidates[run[-1]]
            consecutive = int(candidate.label) == int(previous.label) + 1
            if not consecutive:
                flush()
                run = []
        run.append(index)
    flush()
    return list_indices


def reconstruct_page_footnotes(text: str, *, source_page: str) -> SemanticPage:
    """Move only unambiguous page-local legacy notes into semantic footnotes.

    A high-confidence note has one definition-like line and exactly one equal
    marker before it on the same physical page.  Candidates without a marker
    are retained verbatim and reported as blocking instead of being attached to
    the nearest sentence by guesswork.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    candidates = _note_candidates(normalized)
    if not candidates:
        return SemanticPage(body=normalized)

    definition_spans = _note_definition_spans(normalized, candidates)
    candidate_notes: list[str] = []
    candidate_references: list[list[tuple[int, int]]] = []
    for index, candidate in enumerate(candidates):
        end = definition_spans[index][1]
        raw_note = _strip_trailing_page_furniture(
            normalized[candidate.content_start : end]
        )
        candidate_notes.append(_join_visual_lines(raw_note))
        reference_pattern = (
            re.compile(re.escape(candidate.token))
            if candidate.circled
            else _legacy_note_reference_pattern(candidate.label)
        )
        candidate_references.append(
            [
                (item.start(), item.end())
                for item in reference_pattern.finditer(
                    normalized[: candidate.start]
                )
                if not any(
                    start <= item.start() < stop
                    for start, stop in definition_spans
                )
                and not (
                    candidate.circled
                    and _marker_is_at_visual_line_start(normalized, item.start())
                )
            ]
        )

    ordinary_circled_list_indices = _ordinary_circled_list_candidates(
        candidates,
        candidate_references,
        text_length=len(normalized),
    )
    accepted: list[tuple[int, int, tuple[int, int], SemanticFootnote]] = []
    issues: list[SemanticIssue] = []
    occurrences: Counter[str] = Counter()

    for index, candidate in enumerate(candidates):
        end = definition_spans[index][1]
        note_text = candidate_notes[index]
        prior_references = candidate_references[index]
        if (
            len(prior_references) == 1
            and note_text
            and index not in ordinary_circled_list_indices
        ):
            occurrences[candidate.label] += 1
            stable_id = _stable_note_id(
                source_page,
                candidate.label,
                occurrences[candidate.label],
            )
            accepted.append(
                (
                    candidate.start,
                    end,
                    prior_references[0],
                    SemanticFootnote(
                        stable_id=stable_id,
                        label=candidate.label,
                        text=note_text,
                        source_page=source_page,
                        confidence=1.0,
                    ),
                )
            )
            continue

        if index in ordinary_circled_list_indices:
            continue

        # Report a candidate only when it looks like a bottom-of-page note.
        # This avoids turning ordinary numbered lists into semantic failures.
        located_late = candidate.start >= max(0, int(len(normalized) * 0.42))
        citation_like = bool(
            note_text
            and (
                located_late
                or candidate.marker_on_own_line
                or candidate.token.startswith("〔")
                or re.search(r"[《》,.，。;；:]|\b(?:p|pp|ibid)\.", note_text, re.I)
            )
        )
        if citation_like:
            code = (
                "semantic_footnote_reference_missing"
                if not prior_references
                else "semantic_footnote_reference_ambiguous"
            )
            issues.append(
                SemanticIssue(
                    code=code,
                    message=(
                        "疑似脚注定义没有可证明的正文引用落点。"
                        if not prior_references
                        else "疑似脚注定义对应多个正文标记，无法确定引用落点。"
                    ),
                    source_page=source_page,
                    note_label=candidate.label,
                    blocking=not candidate.circled,
                    evidence={
                        "marker": candidate.token,
                        "reference_count": len(prior_references),
                        "note_preview": note_text[:200],
                        "notation": "circled" if candidate.circled else "bracketed",
                    },
                )
            )

    if not accepted:
        return SemanticPage(body=normalized, issues=tuple(issues))

    edits: list[tuple[int, int, str]] = []
    for start, end, reference_span, note in accepted:
        edits.append((*reference_span, f"[^{note.stable_id}]"))
        edits.append((start, end, ""))
    body = normalized
    for start, end, replacement in sorted(edits, reverse=True):
        body = body[:start] + replacement + body[end:]
    body = re.sub(r"\n[ \t]+\n", "\n\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return SemanticPage(
        body=body,
        footnotes=tuple(item[3] for item in accepted),
        issues=tuple(issues),
    )


_PHYSICAL_SOURCE_PAGE = re.compile(
    r"^pdf-(?P<pdf>\d+)-physical-(?P<physical>\d+)$"
)
_CIRCLED_LIST_SIGNAL = re.compile(
    r"(?:如下|下列|分为|包括|分别|可分|类型|方面|步骤|阶段|一是|二是|三是)"
    r"[^。！？!?\n]{0,48}$"
)
_CITATION_SIGNAL = re.compile(
    r"(?:参见|参看|见《|引自|同上|全集|文集|卷|第\s*\d+\s*页|"
    r"\b(?:p|pp|ibid)\.?\b|《[^》]+》)",
    re.I,
)


def _source_pages_are_adjacent(
    previous: str,
    current: str,
    *,
    physical_pages_per_pdf_page: int,
) -> bool:
    """Prove adjacency from canonical physical-page identifiers.

    Unknown identifiers deliberately do not fall back to list position.  This
    prevents a caller that filtered or reordered pages from silently creating
    a cross-page landing.
    """

    previous_match = _PHYSICAL_SOURCE_PAGE.fullmatch(previous)
    current_match = _PHYSICAL_SOURCE_PAGE.fullmatch(current)
    if previous_match is None or current_match is None:
        return False
    previous_pdf = int(previous_match.group("pdf"))
    current_pdf = int(current_match.group("pdf"))
    previous_physical = int(previous_match.group("physical"))
    current_physical = int(current_match.group("physical"))
    return (
        current_pdf == previous_pdf
        and current_physical == previous_physical + 1
        and current_physical <= physical_pages_per_pdf_page
    ) or (
        current_pdf == previous_pdf + 1
        and current_physical == 1
        and previous_physical == physical_pages_per_pdf_page
    )


def _inline_circled_spans(value: str, token: str) -> list[tuple[int, int]]:
    return [
        (match.start(), match.end())
        for match in re.finditer(re.escape(token), value)
        if not _marker_is_at_visual_line_start(value, match.start())
    ]


def _cross_page_definition_end(
    value: str,
    candidates: Sequence[_PageNoteCandidate],
    index: int,
    *,
    early: bool,
) -> int | None:
    """Return a provable definition boundary, or ``None`` when ambiguous."""

    candidate = candidates[index]
    if early:
        # An early definition is safe only when a blank line explicitly ends
        # its prefix block.  Without that separator we cannot distinguish the
        # footnote from the following page body and therefore keep everything.
        separator = re.search(r"\n[ \t]*\n", value[candidate.content_start :])
        if separator is None:
            return None
        return candidate.content_start + separator.start()
    return (
        candidates[index + 1].start
        if index + 1 < len(candidates)
        else len(value)
    )


def reconstruct_adjacent_page_footnotes(
    pages: Sequence[SemanticPage],
    *,
    source_pages: Sequence[str],
    physical_pages_per_pdf_page: int = 1,
) -> tuple[SemanticPage, ...]:
    """Resolve uniquely proven circled notes across adjacent physical pages.

    This pass consumes the results of :func:`reconstruct_page_footnotes` and
    handles the narrow case where a circled reference occurs near the end of
    one page while its definition is laid out at the start or in the footnote
    region of the next page.  It intentionally does *not* infer bracketed
    notes, repair OCR-confused labels, skip pages, or attach ordinary circled
    list markers.

    The automatic contract is fail-closed:

    * canonical source identifiers prove that the physical pages are adjacent;
    * the current page has one unresolved circled definition for the label;
    * the previous page has exactly one inline marker for that label, located
      in its final 45 percent, and no same-label definition;
    * clustered circled glyphs and list-introduction language are rejected;
    * an early definition needs an explicit blank-line end boundary, while a
      late definition must begin in the final 45 percent of its page.
    """

    if len(pages) != len(source_pages):
        raise ValueError("pages and source_pages must have the same length")
    if physical_pages_per_pdf_page < 1:
        raise ValueError("physical_pages_per_pdf_page must be positive")
    results = list(pages)
    for current_index in range(1, len(results)):
        previous_source = source_pages[current_index - 1]
        current_source = source_pages[current_index]
        if not _source_pages_are_adjacent(
            previous_source,
            current_source,
            physical_pages_per_pdf_page=physical_pages_per_pdf_page,
        ):
            continue
        previous = results[current_index - 1]
        current = results[current_index]
        current_candidates = _note_candidates(current.body)
        if not current_candidates:
            continue

        current_definition_spans = _note_definition_spans(
            current.body,
            current_candidates,
        )
        current_reference_spans: list[list[tuple[int, int]]] = []
        for candidate in current_candidates:
            pattern = (
                re.compile(re.escape(candidate.token))
                if candidate.circled
                else _legacy_note_reference_pattern(candidate.label)
            )
            current_reference_spans.append(
                [
                    (match.start(), match.end())
                    for match in pattern.finditer(current.body[: candidate.start])
                    if not any(
                        start <= match.start() < stop
                        for start, stop in current_definition_spans
                    )
                    and not (
                        candidate.circled
                        and _marker_is_at_visual_line_start(
                            current.body,
                            match.start(),
                        )
                    )
                ]
            )
        ordinary_list_indices = _ordinary_circled_list_candidates(
            current_candidates,
            current_reference_spans,
            text_length=len(current.body),
        )
        missing_circled_labels = {
            issue.note_label
            for issue in current.issues
            if issue.code == "semantic_footnote_reference_missing"
            and issue.evidence.get("notation") == "circled"
        }
        candidate_counts = Counter(
            candidate.label
            for candidate in current_candidates
            if candidate.circled
        )
        previous_line_candidates = {
            candidate.label
            for candidate in _note_candidates(previous.body)
            if candidate.circled
        }
        previous_footnote_labels = {note.label for note in previous.footnotes}
        current_footnote_labels = {note.label for note in current.footnotes}

        previous_edits: list[tuple[int, int, str]] = []
        current_edits: list[tuple[int, int, str]] = []
        cross_page_notes: list[SemanticFootnote] = []
        resolved_labels: set[str] = set()
        for candidate_index, candidate in enumerate(current_candidates):
            if (
                not candidate.circled
                or candidate.label not in missing_circled_labels
                or candidate_counts[candidate.label] != 1
                or candidate_index in ordinary_list_indices
                or candidate.label in current_footnote_labels
            ):
                continue

            page_fraction = candidate.start / max(1, len(current.body))
            early = page_fraction <= 0.18
            late = page_fraction >= 0.55
            if not (early or late):
                continue
            definition_end = _cross_page_definition_end(
                current.body,
                current_candidates,
                candidate_index,
                early=early,
            )
            if definition_end is None:
                continue
            note_text = _join_visual_lines(
                _strip_trailing_page_furniture(
                    current.body[candidate.content_start : definition_end]
                )
            )
            if not note_text:
                continue
            if early and not _CITATION_SIGNAL.search(note_text):
                continue

            previous_spans = _inline_circled_spans(previous.body, candidate.token)
            if len(previous_spans) != 1:
                continue
            reference_start, reference_end = previous_spans[0]
            if reference_start < int(len(previous.body) * 0.55):
                continue
            if (
                candidate.label in previous_line_candidates
                or candidate.label in previous_footnote_labels
            ):
                continue

            # Multiple nearby circled glyphs are characteristic of a cross-
            # referenced list.  One distant, unrelated note marker does not
            # invalidate this otherwise unique landing.
            nearby_circled = [
                match.start()
                for match in _ANY_CIRCLED_NOTE.finditer(previous.body)
                if match.start() != reference_start
                and abs(match.start() - reference_start) <= 96
            ]
            if nearby_circled:
                continue
            line_start = previous.body.rfind("\n", 0, reference_start) + 1
            prefix = previous.body[line_start:reference_start]
            if _CIRCLED_LIST_SIGNAL.search(prefix):
                continue

            stable_id = _stable_note_id(current_source, candidate.label, 1)
            previous_edits.append(
                (reference_start, reference_end, f"[^{stable_id}]")
            )
            current_edits.append((candidate.start, definition_end, ""))
            cross_page_notes.append(
                SemanticFootnote(
                    stable_id=stable_id,
                    label=candidate.label,
                    text=note_text,
                    source_page=current_source,
                    confidence=0.99,
                    reconstruction_scope="adjacent-page",
                    reference_source_page=previous_source,
                )
            )
            resolved_labels.add(candidate.label)

        if not cross_page_notes:
            continue

        previous_body = previous.body
        for start, end, replacement in sorted(previous_edits, reverse=True):
            previous_body = previous_body[:start] + replacement + previous_body[end:]
        current_body = current.body
        for start, end, replacement in sorted(current_edits, reverse=True):
            current_body = current_body[:start] + replacement + current_body[end:]
        current_body = re.sub(r"\n[ \t]+\n", "\n\n", current_body)
        current_body = re.sub(r"\n{3,}", "\n\n", current_body).strip()
        remaining_issues = tuple(
            issue
            for issue in current.issues
            if not (
                issue.code == "semantic_footnote_reference_missing"
                and issue.evidence.get("notation") == "circled"
                and issue.note_label in resolved_labels
            )
        )
        results[current_index - 1] = SemanticPage(
            body=previous_body,
            footnotes=previous.footnotes,
            issues=previous.issues,
        )
        results[current_index] = SemanticPage(
            body=current_body,
            footnotes=(*current.footnotes, *cross_page_notes),
            issues=remaining_issues,
        )
    return tuple(results)


def append_markdown_footnotes(
    markdown_text: str,
    footnotes: Sequence[SemanticFootnote],
    *,
    existing_definitions: Sequence[tuple[str, str]] = (),
) -> str:
    definitions = [
        (str(note_id), str(note_text))
        for note_id, note_text in existing_definitions
    ]
    definitions.extend((note.stable_id, note.text) for note in footnotes)
    if not definitions:
        return markdown_text.rstrip() + "\n"

    definitions_by_id: dict[str, list[str]] = {}
    for note_id, note_text in definitions:
        definitions_by_id.setdefault(note_id, []).append(note_text)
    duplicate_ids = sorted(
        note_id
        for note_id, texts in definitions_by_id.items()
        if len(texts) > 1
    )
    conflicting_text_ids = sorted(
        note_id
        for note_id, texts in definitions_by_id.items()
        if len(set(texts)) > 1
    )
    if duplicate_ids:
        raise ValueError(
            "Markdown footnote definitions are not unique: "
            f"duplicate_ids={duplicate_ids}, "
            f"conflicting_text_ids={conflicting_text_ids}"
        )

    rendered = "\n\n".join(
        f"[^{note_id}]: {note_text}" for note_id, note_text in definitions
    )
    return markdown_text.rstrip() + "\n\n" + rendered + "\n"


def strip_markdown_footnote_section_headings(text: str) -> str:
    """Remove only a standalone ``注释：`` immediately introducing definitions.

    The heading is layout furniture when its next nonblank physical line is a
    standard Markdown footnote definition.  Inline prose and a standalone
    heading followed by ordinary text remain byte-for-byte present.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    output: list[str] = []
    for index, line in enumerate(lines):
        if _MARKDOWN_FOOTNOTE_SECTION_HEADING.fullmatch(line) is not None:
            next_nonblank = next(
                (
                    lines[candidate]
                    for candidate in range(index + 1, len(lines))
                    if lines[candidate].strip()
                ),
                None,
            )
            if (
                next_nonblank is not None
                and MARKDOWN_NOTE_START.match(next_nonblank) is not None
            ):
                continue
        output.append(line)
    rendered = "\n".join(output)
    if normalized.endswith("\n"):
        rendered += "\n"
    return rendered


def parse_markdown_footnotes(markdown_text: str) -> MarkdownFootnoteInventory:
    """Extract standard Markdown footnotes without depending on HTML rendering."""

    lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    body_lines: list[str] = []
    definitions: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        match = MARKDOWN_NOTE_START.match(lines[index])
        if match is None:
            body_lines.append(lines[index])
            index += 1
            continue
        note_id = match.group("id")
        note_lines = [match.group("text")]
        index += 1
        while index < len(lines):
            line = lines[index]
            if re.match(r"^(?: {2,}|\t)\S", line):
                note_lines.append(line.lstrip())
                index += 1
                continue
            if not line.strip() and index + 1 < len(lines):
                lookahead = index + 1
                while lookahead < len(lines) and not lines[lookahead].strip():
                    lookahead += 1
                if lookahead < len(lines) and re.match(
                    r"^(?: {2,}|\t)\S", lines[lookahead]
                ):
                    note_lines.append("")
                    index += 1
                    continue
            break
        definitions.append((note_id, _join_visual_lines("\n".join(note_lines))))

    body = "\n".join(body_lines)
    references = tuple(match.group("id") for match in MARKDOWN_REFERENCE.finditer(body))
    definition_ids = [item[0] for item in definitions]
    definition_counts = Counter(definition_ids)
    reference_counts = Counter(references)
    sort_key = lambda value: (0, int(value)) if value.isdigit() else (1, value)
    return MarkdownFootnoteInventory(
        body=body.rstrip() + ("\n" if markdown_text.endswith("\n") else ""),
        definitions=tuple(definitions),
        references=references,
        duplicate_definitions=tuple(
            sorted((key for key, count in definition_counts.items() if count > 1), key=sort_key)
        ),
        missing_definitions=tuple(
            sorted(set(references) - set(definition_ids), key=sort_key)
        ),
        unused_definitions=tuple(
            sorted(set(definition_ids) - set(references), key=sort_key)
        ),
        duplicate_references=tuple(
            sorted((key for key, count in reference_counts.items() if count > 1), key=sort_key)
        ),
    )


def markdown_footnotes_to_docx_markers(
    markdown_text: str,
    *,
    namespace: str,
) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Return definition-free Markdown and document-unique OOXML markers."""

    inventory = parse_markdown_footnotes(markdown_text)
    if not inventory.valid:
        raise ValueError(
            "Markdown footnotes are not a one-to-one closed set: "
            f"duplicate_definitions={list(inventory.duplicate_definitions)}, "
            f"missing_definitions={list(inventory.missing_definitions)}, "
            f"unused_definitions={list(inventory.unused_definitions)}, "
            f"duplicate_references={list(inventory.duplicate_references)}"
        )
    definitions = inventory.definition_map()
    safe_namespace = re.sub(r"[^A-Za-z0-9_-]+", "-", namespace).strip("-") or "chapter"
    notes: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        local_id = match.group("id")
        document_id = f"{safe_namespace}-{local_id}"
        notes.append((document_id, definitions[local_id]))
        marker = f"[[FN:{document_id}]]"
        # ``[[FN:id]](text)`` is parsed by Markdown as a link whose label is
        # ``[FN:id]``.  The OOXML patcher then cannot find its stable marker.
        # Escape only an immediately following ASCII opening parenthesis;
        # Markdown removes the escape while retaining the visible character.
        if match.end() < len(match.string) and match.string[match.end()] == "(":
            marker += "\\"
        return marker

    body = MARKDOWN_REFERENCE.sub(replace, inventory.body)
    return body, tuple(notes)


def markdown_footnote_contract_sha256(markdown_text: str) -> str:
    """Fingerprint only the reference-definition contract, not prose cleanup."""

    inventory = parse_markdown_footnotes(markdown_text)
    payload = {
        "references": list(inventory.references),
        "definitions": [list(item) for item in inventory.definitions],
        "duplicate_definitions": list(inventory.duplicate_definitions),
        "missing_definitions": list(inventory.missing_definitions),
        "unused_definitions": list(inventory.unused_definitions),
        "duplicate_references": list(inventory.duplicate_references),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def semantic_audit_summary(
    chapters: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    values = list(chapters)
    issue_count = sum(len(item.get("issues") or []) for item in values)
    blocking_count = sum(
        1
        for item in values
        for issue in item.get("issues") or []
        if bool(issue.get("blocking", True))
    )
    footnote_count = sum(int(item.get("footnote_count") or 0) for item in values)
    adjacent_page_footnote_count = sum(
        1
        for chapter in values
        for page in chapter.get("pages") or []
        for note in page.get("footnotes") or []
        if note.get("reconstruction_scope") == "adjacent-page"
    )
    return {
        "chapter_count": len(values),
        "footnote_count": footnote_count,
        "adjacent_page_footnote_count": adjacent_page_footnote_count,
        "issue_count": issue_count,
        "blocking_issue_count": blocking_count,
        "release_blocked": blocking_count > 0,
    }


__all__ = [
    "MarkdownFootnoteInventory",
    "SemanticFootnote",
    "SemanticIssue",
    "SemanticPage",
    "append_markdown_footnotes",
    "markdown_footnote_contract_sha256",
    "markdown_footnotes_to_docx_markers",
    "parse_markdown_footnotes",
    "reconstruct_adjacent_page_footnotes",
    "reconstruct_page_footnotes",
    "semantic_audit_summary",
    "strip_markdown_footnote_section_headings",
]
