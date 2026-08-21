"""Deterministic semantic reconstruction for reader-facing publications.

The OCR layer is intentionally page based, while Markdown/EPUB/DOCX are
continuous reader formats.  This module owns the boundary between those two
representations.  In particular, it turns page-local note markers and note
definitions into stable Markdown footnotes before physical page markers are
discarded.

The extractor is deliberately fail-closed: only a definition with exactly one
matching marker earlier on the same physical page is moved automatically.
Plausible definitions without an unambiguous marker remain visible in the
body and produce a blocking audit issue that can be resolved by a reviewed
semantic override in a future pass.  No content is silently guessed or lost.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Iterable, Sequence


LEGACY_NOTE_START = re.compile(
    r"(?m)^[ \t]*(?P<token>\[(?P<square>\d{1,4})\]|〔(?P<corner>\d{1,4})〕)"
    r"[ \t]*(?=\S)"
)
MARKDOWN_NOTE_START = re.compile(
    r"^[ \t]*\[\^(?P<id>[^\]\s]+)\]:[ \t]*(?P<text>.*)$"
)
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
class MarkdownFootnotePruneResult:
    markdown: str
    removed: tuple[tuple[str, int], ...]
    remaining_count: int

    @property
    def removed_ids(self) -> tuple[str, ...]:
        return tuple(note_id for note_id, _ in self.removed)


@dataclass(frozen=True)
class MarkdownPageMarkerPruneResult:
    markdown: str
    removed: tuple[int, ...]


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


def reconstruct_page_footnotes(text: str, *, source_page: str) -> SemanticPage:
    """Move only unambiguous page-local legacy notes into semantic footnotes.

    A high-confidence note has one definition-like line and exactly one equal
    marker before it on the same physical page.  Candidates without a marker
    are retained verbatim and reported as blocking instead of being attached to
    the nearest sentence by guesswork.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    starts = [
        match
        for match in LEGACY_NOTE_START.finditer(normalized)
        # A citation year can begin a wrapped physical page, for example
        # ``[1971], pp. 35-40``.  Four-digit calendar years are bibliography
        # content, never page-local footnote labels.  The translation-side
        # footnote gate applies the same 1800-2099 exclusion.
        if not 1800
        <= int(match.group("square") or match.group("corner") or "0")
        <= 2099
    ]
    if not starts:
        return SemanticPage(body=normalized)

    definition_spans = [
        (
            match.start(),
            starts[index + 1].start() if index + 1 < len(starts) else len(normalized),
        )
        for index, match in enumerate(starts)
    ]
    accepted: list[
        tuple[re.Match[str], int, str, tuple[int, int], SemanticFootnote]
    ] = []
    issues: list[SemanticIssue] = []
    occurrences: Counter[str] = Counter()

    for index, match in enumerate(starts):
        token = match.group("token")
        label = match.group("square") or match.group("corner") or ""
        end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
        raw_note = _strip_trailing_page_furniture(normalized[match.end() : end])
        note_text = _join_visual_lines(raw_note)
        prior_references = [
            item
            for item in re.finditer(re.escape(token), normalized[: match.start()])
            if not any(
                start <= item.start() < stop
                for start, stop in definition_spans
            )
        ]
        if len(prior_references) == 1 and note_text:
            occurrences[label] += 1
            stable_id = _stable_note_id(source_page, label, occurrences[label])
            reference = prior_references[0]
            accepted.append(
                (
                    match,
                    end,
                    token,
                    (reference.start(), reference.end()),
                    SemanticFootnote(
                        stable_id=stable_id,
                        label=label,
                        text=note_text,
                        source_page=source_page,
                        confidence=1.0,
                    ),
                )
            )
            continue

        # Report a candidate only when it looks like a bottom-of-page note.
        # This avoids turning ordinary numbered lists into semantic failures.
        located_late = match.start() >= max(0, int(len(normalized) * 0.42))
        citation_like = bool(
            note_text
            and (
                located_late
                or token.startswith("〔")
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
                    note_label=label,
                    evidence={
                        "marker": token,
                        "reference_count": len(prior_references),
                        "note_preview": note_text[:200],
                    },
                )
            )

    if not accepted:
        return SemanticPage(body=normalized, issues=tuple(issues))

    edits: list[tuple[int, int, str]] = []
    for match, end, _token, reference_span, note in accepted:
        edits.append((*reference_span, f"[^{note.stable_id}]"))
        edits.append((match.start(), end, ""))
    body = normalized
    for start, end, replacement in sorted(edits, reverse=True):
        body = body[:start] + replacement + body[end:]
    body = re.sub(r"\n[ \t]+\n", "\n\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return SemanticPage(
        body=body,
        footnotes=tuple(item[4] for item in accepted),
        issues=tuple(issues),
    )


def append_markdown_footnotes(
    markdown_text: str,
    footnotes: Sequence[SemanticFootnote],
) -> str:
    if not footnotes:
        return markdown_text.rstrip() + "\n"
    definitions = "\n\n".join(
        f"[^{note.stable_id}]: {note.text}" for note in footnotes
    )
    return markdown_text.rstrip() + "\n\n" + definitions + "\n"


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


def prune_long_markdown_footnotes(
    markdown_text: str,
    *,
    minimum_characters: int = 150,
) -> MarkdownFootnotePruneResult:
    """Remove long footnotes while preserving a closed Markdown contract.

    The threshold is inclusive and counts the normalized definition text,
    including meaningful spaces.  Both the body reference and its complete
    definition are removed.  Short definitions retain their original source
    formatting instead of being reconstructed from parsed text.
    """

    if minimum_characters < 1:
        raise ValueError("minimum_characters must be positive")

    inventory = parse_markdown_footnotes(markdown_text)
    if not inventory.valid:
        raise ValueError(
            "Markdown footnotes are not a one-to-one closed set: "
            f"duplicate_definitions={list(inventory.duplicate_definitions)}, "
            f"missing_definitions={list(inventory.missing_definitions)}, "
            f"unused_definitions={list(inventory.unused_definitions)}, "
            f"duplicate_references={list(inventory.duplicate_references)}"
        )

    removed = tuple(
        (note_id, len(text.strip()))
        for note_id, text in inventory.definitions
        if len(text.strip()) >= minimum_characters
    )
    if not removed:
        return MarkdownFootnotePruneResult(
            markdown=markdown_text,
            removed=(),
            remaining_count=len(inventory.definitions),
        )

    removed_ids = {note_id for note_id, _ in removed}
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    had_final_newline = normalized.endswith("\n")
    lines = normalized.splitlines()
    output_lines: list[str] = []
    index = 0
    while index < len(lines):
        match = MARKDOWN_NOTE_START.match(lines[index])
        if match is None:
            output_lines.append(
                MARKDOWN_REFERENCE.sub(
                    lambda reference: (
                        "" if reference.group("id") in removed_ids else reference.group(0)
                    ),
                    lines[index],
                )
            )
            index += 1
            continue

        end = index + 1
        while end < len(lines):
            line = lines[end]
            if re.match(r"^(?: {2,}|\t)\S", line):
                end += 1
                continue
            if not line.strip():
                lookahead = end + 1
                while lookahead < len(lines) and not lines[lookahead].strip():
                    lookahead += 1
                if lookahead < len(lines) and re.match(
                    r"^(?: {2,}|\t)\S", lines[lookahead]
                ):
                    end += 1
                    continue
            break

        if match.group("id") not in removed_ids:
            output_lines.extend(lines[index:end])
        index = end

    rendered = "\n".join(output_lines)
    if had_final_newline:
        rendered += "\n"

    pruned = parse_markdown_footnotes(rendered)
    expected_remaining = len(inventory.definitions) - len(removed)
    if not pruned.valid or len(pruned.definitions) != expected_remaining:
        raise RuntimeError("long-footnote pruning broke the Markdown footnote contract")
    if removed_ids & (set(pruned.references) | {item[0] for item in pruned.definitions}):
        raise RuntimeError("long-footnote pruning left removed IDs in the Markdown")

    return MarkdownFootnotePruneResult(
        markdown=rendered,
        removed=removed,
        remaining_count=expected_remaining,
    )


def prune_standalone_page_markers(
    markdown_text: str,
) -> MarkdownPageMarkerPruneResult:
    """Remove a high-confidence monotonic run of standalone EPUB page labels.

    A candidate must be an unindented 1-3 digit paragraph outside fenced code.
    At least three candidates must form a strictly increasing sequence whose
    adjacent gaps are no larger than 50.  Ambiguous single numbers, years,
    list syntax, tables, and code examples remain untouched.
    """

    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    had_final_newline = normalized.endswith("\n")
    lines = normalized.splitlines()
    candidates: list[tuple[int, int]] = []
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if fence_match is not None:
            token = fence_match.group(1)
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        match = re.fullmatch(r"[ \t]{0,3}(\d{1,3})[ \t]*", line)
        if match is not None:
            value = int(match.group(1))
            if value > 0:
                candidates.append((index, value))

    values = tuple(value for _, value in candidates)
    if len(values) < 3 or any(
        not (0 < current - previous <= 50)
        for previous, current in zip(values, values[1:])
    ):
        return MarkdownPageMarkerPruneResult(markdown=markdown_text, removed=())

    removed_indexes = {index for index, _ in candidates}
    rendered = "\n".join(
        line for index, line in enumerate(lines) if index not in removed_indexes
    )
    if had_final_newline:
        rendered += "\n"
    return MarkdownPageMarkerPruneResult(markdown=rendered, removed=values)


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
        return f"[[FN:{document_id}]]"

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
    return {
        "chapter_count": len(values),
        "footnote_count": footnote_count,
        "issue_count": issue_count,
        "blocking_issue_count": blocking_count,
        "release_blocked": blocking_count > 0,
    }


__all__ = [
    "MarkdownFootnoteInventory",
    "MarkdownFootnotePruneResult",
    "MarkdownPageMarkerPruneResult",
    "SemanticFootnote",
    "SemanticIssue",
    "SemanticPage",
    "append_markdown_footnotes",
    "markdown_footnote_contract_sha256",
    "markdown_footnotes_to_docx_markers",
    "parse_markdown_footnotes",
    "prune_long_markdown_footnotes",
    "prune_standalone_page_markers",
    "reconstruct_page_footnotes",
    "semantic_audit_summary",
]
