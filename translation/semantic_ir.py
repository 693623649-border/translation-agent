"""Versioned canonical intermediate representation for document semantics."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
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


class SemanticContractError(ValueError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceLocator:
    adapter: str
    source: str
    page: int | None = None
    href: str | None = None
    anchor: str | None = None
    block_index: int | None = None

    def __post_init__(self) -> None:
        if not self.adapter or not self.source:
            raise SemanticContractError("locator adapter and source are required")
        if self.page is not None and self.page < 1:
            raise SemanticContractError("locator page must be positive")
        if self.block_index is not None and self.block_index < 0:
            raise SemanticContractError("locator block_index must not be negative")

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
        allowed = {"adapter", "source", "page", "href", "anchor", "block_index"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SemanticContractError(f"locator has unknown fields: {unknown}")
        return cls(
            adapter=str(value.get("adapter") or ""),
            source=str(value.get("source") or ""),
            page=int(value["page"]) if value.get("page") is not None else None,
            href=str(value["href"]) if value.get("href") is not None else None,
            anchor=(
                str(value["anchor"]) if value.get("anchor") is not None else None
            ),
            block_index=(
                int(value["block_index"])
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
        if self.schema_version != SEMANTIC_SCHEMA_VERSION:
            raise SemanticContractError("unsupported TranslationUnit schema")
        if not self.id or not self.chapter_id or self.sequence < 1:
            raise SemanticContractError("translation unit identity is invalid")
        if self.kind not in BLOCK_KINDS:
            raise SemanticContractError(f"unsupported translation unit kind: {self.kind}")
        if sha256_text(self.source_markdown) != self.source_sha256:
            raise SemanticContractError(f"translation unit source hash is stale: {self.id}")
        if not self.locators:
            raise SemanticContractError(f"translation unit has no source locator: {self.id}")


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
