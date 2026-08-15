"""Versioned canonical intermediate representation for document semantics."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


SEMANTIC_SCHEMA_VERSION = 1
BLOCK_KINDS = frozenset(
    {
        "heading",
        "paragraph",
        "quote",
        "list_item",
        "table",
        "image",
        "footnote_definition",
        "pagebreak",
        "index_entry",
        "bibliography_entry",
    }
)
REVIEW_DECISIONS = frozenset({"accepted", "rejected", "replaced"})
_SHA256 = frozenset("0123456789abcdef")


class SemanticContractError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256 for character in value)
    )


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticContractError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SemanticContractError(f"{field_name} must be null or a non-empty string")
    return value


def _integer(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise SemanticContractError(
            f"{field_name} must be an integer greater than or equal to {minimum}"
        )
    return value


@dataclass(frozen=True)
class SourceLocator:
    adapter: str
    source: str
    page: int | None = None
    href: str | None = None
    anchor: str | None = None
    block_index: int | None = None

    def __post_init__(self) -> None:
        _required_string(self.adapter, "locator adapter")
        _required_string(self.source, "locator source")
        _optional_string(self.href, "locator href")
        _optional_string(self.anchor, "locator anchor")
        if self.page is not None:
            _integer(self.page, "locator page", minimum=1)
        if self.block_index is not None:
            _integer(self.block_index, "locator block_index", minimum=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "source": self.source,
            "page": self.page,
            "href": self.href,
            "anchor": self.anchor,
            "block_index": self.block_index,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SourceLocator":
        if not isinstance(value, Mapping):
            raise SemanticContractError("locator must be an object")
        allowed = {"adapter", "source", "page", "href", "anchor", "block_index"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SemanticContractError(f"locator has unknown fields: {unknown}")
        return cls(
            adapter=_required_string(value.get("adapter"), "locator adapter"),
            source=_required_string(value.get("source"), "locator source"),
            page=(
                _integer(value["page"], "locator page", minimum=1)
                if value.get("page") is not None
                else None
            ),
            href=_optional_string(value.get("href"), "locator href"),
            anchor=_optional_string(value.get("anchor"), "locator anchor"),
            block_index=(
                _integer(
                    value["block_index"],
                    "locator block_index",
                    minimum=0,
                )
                if value.get("block_index") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class SemanticBlock:
    id: str
    kind: str
    markdown: str
    locators: tuple[SourceLocator, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or self.kind not in BLOCK_KINDS or not self.markdown.strip():
            raise SemanticContractError(
                "semantic block requires id, supported kind and non-empty markdown"
            )
        if not self.locators:
            raise SemanticContractError(f"semantic block {self.id!r} has no locator")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "markdown": self.markdown,
            "locators": [item.to_dict() for item in self.locators],
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SemanticBlock":
        allowed = {"id", "kind", "markdown", "locators", "metadata"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SemanticContractError(f"semantic block has unknown fields: {unknown}")
        raw_locators = value.get("locators")
        if not isinstance(raw_locators, Sequence) or isinstance(raw_locators, (str, bytes)):
            raise SemanticContractError("semantic block locators must be an array")
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise SemanticContractError("semantic block metadata must be an object")
        return cls(
            id=str(value.get("id") or ""),
            kind=str(value.get("kind") or ""),
            markdown=str(value.get("markdown") or ""),
            locators=tuple(SourceLocator.from_dict(item) for item in raw_locators),
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class SemanticChapter:
    id: str
    sequence: int
    title: str
    blocks: tuple[SemanticBlock, ...]

    def __post_init__(self) -> None:
        if not self.id or self.sequence < 1 or not self.title.strip():
            raise SemanticContractError("chapter id, positive sequence and title are required")
        block_ids = [block.id for block in self.blocks]
        if len(set(block_ids)) != len(block_ids):
            raise SemanticContractError(f"chapter {self.id!r} has duplicate block ids")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "title": self.title,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SemanticChapter":
        unknown = sorted(set(value) - {"id", "sequence", "title", "blocks"})
        if unknown:
            raise SemanticContractError(f"chapter has unknown fields: {unknown}")
        raw_blocks = value.get("blocks")
        if not isinstance(raw_blocks, Sequence) or isinstance(raw_blocks, (str, bytes)):
            raise SemanticContractError("chapter blocks must be an array")
        return cls(
            id=str(value.get("id") or ""),
            sequence=int(value.get("sequence") or 0),
            title=str(value.get("title") or ""),
            blocks=tuple(SemanticBlock.from_dict(item) for item in raw_blocks),
        )


@dataclass(frozen=True)
class DocumentSemantic:
    schema_version: int
    id: str
    source_mode: str
    source_sha256: str
    source_language: str
    chapters: tuple[SemanticChapter, ...]
    assets: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SEMANTIC_SCHEMA_VERSION:
            raise SemanticContractError("unsupported DocumentSemantic schema")
        if not self.id or not self.source_mode or not self.source_language:
            raise SemanticContractError("document identity and languages are required")
        if len(self.source_sha256) != 64:
            raise SemanticContractError("document source_sha256 must be SHA-256 hex")
        sequences = [chapter.sequence for chapter in self.chapters]
        if sequences != list(range(1, len(sequences) + 1)):
            raise SemanticContractError("chapter sequence must be contiguous and ordered")
        chapter_ids = [chapter.id for chapter in self.chapters]
        if len(set(chapter_ids)) != len(chapter_ids):
            raise SemanticContractError("document has duplicate chapter ids")
        block_ids = [block.id for chapter in self.chapters for block in chapter.blocks]
        if len(set(block_ids)) != len(block_ids):
            raise SemanticContractError("document has duplicate global block ids")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "source_mode": self.source_mode,
            "source_sha256": self.source_sha256,
            "source_language": self.source_language,
            "chapters": [chapter.to_dict() for chapter in self.chapters],
            "assets": [dict(asset) for asset in self.assets],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DocumentSemantic":
        allowed = {
            "schema_version",
            "id",
            "source_mode",
            "source_sha256",
            "source_language",
            "chapters",
            "assets",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SemanticContractError(f"document has unknown fields: {unknown}")
        raw_chapters = value.get("chapters")
        raw_assets = value.get("assets", ())
        if not isinstance(raw_chapters, Sequence) or isinstance(raw_chapters, (str, bytes)):
            raise SemanticContractError("document chapters must be an array")
        if not isinstance(raw_assets, Sequence) or isinstance(raw_assets, (str, bytes)):
            raise SemanticContractError("document assets must be an array")
        return cls(
            schema_version=int(value.get("schema_version") or 0),
            id=str(value.get("id") or ""),
            source_mode=str(value.get("source_mode") or ""),
            source_sha256=str(value.get("source_sha256") or ""),
            source_language=str(value.get("source_language") or ""),
            chapters=tuple(SemanticChapter.from_dict(item) for item in raw_chapters),
            assets=tuple(dict(item) for item in raw_assets),
        )


@dataclass(frozen=True)
class TranslationUnit:
    schema_version: int
    id: str
    chapter_id: str
    sequence: int
    kind: str
    source_markdown: str
    source_sha256: str
    locators: tuple[SourceLocator, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SEMANTIC_SCHEMA_VERSION:
            raise SemanticContractError("unsupported TranslationUnit schema")
        _required_string(self.id, "translation unit id")
        _required_string(self.chapter_id, "translation unit chapter_id")
        _integer(self.sequence, "translation unit sequence", minimum=1)
        if self.kind not in BLOCK_KINDS:
            raise SemanticContractError(f"unsupported translation unit kind: {self.kind}")
        _required_string(self.source_markdown, "translation unit source_markdown")
        if not _is_sha256(self.source_sha256):
            raise SemanticContractError(
                f"translation unit source_sha256 is invalid: {self.id}"
            )
        if sha256_text(self.source_markdown) != self.source_sha256:
            raise SemanticContractError(f"translation unit source hash is stale: {self.id}")
        if not self.locators or not all(
            isinstance(locator, SourceLocator) for locator in self.locators
        ):
            raise SemanticContractError(f"translation unit has no source locator: {self.id}")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the strict canonical source-unit record."""

        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "chapter_id": self.chapter_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "source_markdown": self.source_markdown,
            "source_sha256": self.source_sha256,
            "locators": [locator.to_dict() for locator in self.locators],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TranslationUnit":
        """Read only the canonical record shape; use the normalizer for v1 legacy rows."""

        if not isinstance(value, Mapping):
            raise SemanticContractError("translation unit must be an object")
        allowed = {
            "schema_version",
            "id",
            "chapter_id",
            "sequence",
            "kind",
            "source_markdown",
            "source_sha256",
            "locators",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SemanticContractError(
                f"translation unit has unknown fields: {unknown}"
            )
        raw_locators = value.get("locators")
        if not isinstance(raw_locators, Sequence) or isinstance(
            raw_locators, (str, bytes)
        ):
            raise SemanticContractError("translation unit locators must be an array")
        return cls(
            schema_version=_integer(
                value.get("schema_version"),
                "translation unit schema_version",
                minimum=1,
            ),
            id=_required_string(value.get("id"), "translation unit id"),
            chapter_id=_required_string(
                value.get("chapter_id"),
                "translation unit chapter_id",
            ),
            sequence=_integer(
                value.get("sequence"),
                "translation unit sequence",
                minimum=1,
            ),
            kind=_required_string(value.get("kind"), "translation unit kind"),
            source_markdown=_required_string(
                value.get("source_markdown"),
                "translation unit source_markdown",
            ),
            source_sha256=_required_string(
                value.get("source_sha256"),
                "translation unit source_sha256",
            ),
            locators=tuple(
                locator
                if isinstance(locator, SourceLocator)
                else SourceLocator.from_dict(locator)
                for locator in raw_locators
            ),
        )


def normalize_translation_unit_record(
    value: Mapping[str, Any],
) -> TranslationUnit:
    """Normalize canonical or legacy adapter rows into one strict unit.

    Historical EPUB rows used ``source_href`` and the non-canonical ``list``
    kind. Historical text-PDF rows used ``source_pages``.  Readers keep
    accepting those schema-v1 rows, while all current writers emit locators
    and canonical kinds.
    """

    if not isinstance(value, Mapping):
        raise SemanticContractError("translation unit must be an object")
    canonical_keys = {
        "schema_version",
        "id",
        "chapter_id",
        "sequence",
        "kind",
        "source_markdown",
        "source_sha256",
        "locators",
    }
    legacy_keys = {"source_href", "source_pages"}
    unknown = sorted(set(value) - canonical_keys - legacy_keys)
    if unknown:
        raise SemanticContractError(
            f"translation unit has unknown fields: {unknown}"
        )

    normalized = {key: value[key] for key in canonical_keys if key in value}
    if normalized.get("kind") == "list":
        normalized["kind"] = "list_item"

    raw_locators = normalized.get("locators")
    legacy_locator_fields = legacy_keys & set(value)
    if raw_locators is not None and legacy_locator_fields:
        raise SemanticContractError(
            "translation unit must not mix canonical and legacy locator fields"
        )
    if raw_locators is None:
        has_source_href = "source_href" in value
        has_source_pages = "source_pages" in value
        if has_source_href == has_source_pages:
            raise SemanticContractError(
                "legacy translation unit requires exactly one source_href or source_pages"
            )
        sequence = _integer(
            normalized.get("sequence"),
            "translation unit sequence",
            minimum=1,
        )
        if has_source_href:
            href = _required_string(value.get("source_href"), "legacy source_href")
            locator = SourceLocator(
                adapter="epub",
                source=href,
                href=href,
                block_index=sequence - 1,
            )
        else:
            pages = _required_string(value.get("source_pages"), "legacy source_pages")
            if not re.fullmatch(r"[1-9]\d*(?:-[1-9]\d*)?", pages):
                raise SemanticContractError("legacy source_pages is invalid")
            start_text, _, end_text = pages.partition("-")
            if end_text and int(end_text) < int(start_text):
                raise SemanticContractError("legacy source_pages range is invalid")
            locator = SourceLocator(
                adapter="text-pdf",
                source=f"pages:{pages}",
                page=(
                    int(start_text)
                    if not end_text or end_text == start_text
                    else None
                ),
                block_index=sequence - 1,
            )
        normalized["locators"] = [locator.to_dict()]
    return TranslationUnit.from_dict(normalized)


@dataclass(frozen=True)
class ReviewDecision:
    schema_version: int
    issue_id: str
    unit_id: str | None
    source_sha256: str
    reviewer: str
    decision: str
    timestamp: str
    replacement_markdown: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SEMANTIC_SCHEMA_VERSION:
            raise SemanticContractError("unsupported ReviewDecision schema")
        if (
            not self.issue_id
            or len(self.source_sha256) != 64
            or not self.reviewer
            or not self.timestamp
            or self.decision not in REVIEW_DECISIONS
        ):
            raise SemanticContractError("invalid review decision")
        if self.decision == "replaced" and not self.replacement_markdown:
            raise SemanticContractError("replaced review decision requires replacement_markdown")

    def matches_source(self, source_markdown: str) -> bool:
        return sha256_text(source_markdown) == self.source_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issue_id": self.issue_id,
            "unit_id": self.unit_id,
            "source_sha256": self.source_sha256,
            "reviewer": self.reviewer,
            "decision": self.decision,
            "timestamp": self.timestamp,
            "replacement_markdown": self.replacement_markdown,
        }


def append_review_decision(path: Path, decision: ReviewDecision) -> None:
    """Append and fsync one immutable decision without rewriting prior reviews."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(decision.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("failed to append review decision")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
