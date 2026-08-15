"""Typed application service for deterministic publication verification.

The verifier itself intentionally knows nothing about command-line arguments or
the pipeline graph.  This module is the small shared boundary that converts an
explicit release contract into one verifier invocation.  CLI and DAG adapters
remain responsible for resolving profiles, materialising graph artifacts, and
deciding how a failed quality gate is surfaced to their callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from publication_verifier import verify_publication


PublicationArtifact = Literal[
    "epub",
    "docx",
    "knowledge_base",
    "bookmarked_pdf",
]
PublicationProfile = Literal["full", "word"]

EPUB: PublicationArtifact = "epub"
DOCX: PublicationArtifact = "docx"
KNOWLEDGE_BASE: PublicationArtifact = "knowledge_base"
BOOKMARKED_PDF: PublicationArtifact = "bookmarked_pdf"
PUBLICATION_ARTIFACTS = frozenset(
    {EPUB, DOCX, KNOWLEDGE_BASE, BOOKMARKED_PDF}
)


@dataclass(frozen=True)
class PublicationVerificationRequest:
    """Complete, argv-free contract for one deterministic quality-gate run."""

    output_dir: Path
    source_pdf: Path | None = None
    book_title: str | None = None
    expected_language: str | None = None
    expected_translation_fingerprint: str | None = None
    require_translation: bool = False
    required_artifacts: frozenset[PublicationArtifact] = PUBLICATION_ARTIFACTS
    require_docx_render: bool = True
    require_all_reviewed: bool = False
    publication_profile: PublicationProfile = "full"
    chapter_ids: tuple[str, ...] = ()
    report_path: Path | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "require_translation",
            "require_docx_render",
            "require_all_reviewed",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"{field_name} must be a boolean")
        if self.publication_profile not in {"full", "word"}:
            raise ValueError("publication_profile must be 'full' or 'word'")
        if self.require_translation and (
            not isinstance(self.expected_translation_fingerprint, str)
            or not self.expected_translation_fingerprint.strip()
        ):
            raise ValueError(
                "require_translation requires an expected translation fingerprint"
            )

        artifacts = frozenset(self.required_artifacts)
        unknown = set(artifacts) - set(PUBLICATION_ARTIFACTS)
        if unknown:
            raise ValueError(
                f"unknown required publication artifacts: {sorted(unknown)}"
            )
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in self.chapter_ids
        ):
            raise ValueError("chapter_ids must contain non-empty strings")
        chapter_ids = tuple(value.strip() for value in self.chapter_ids)

        output_dir = Path(self.output_dir).expanduser().resolve()
        source_pdf = (
            Path(self.source_pdf).expanduser().resolve()
            if self.source_pdf is not None
            else None
        )
        report_path = (
            Path(self.report_path).expanduser().resolve()
            if self.report_path is not None
            else None
        )
        object.__setattr__(self, "output_dir", output_dir)
        object.__setattr__(self, "source_pdf", source_pdf)
        object.__setattr__(self, "required_artifacts", artifacts)
        object.__setattr__(self, "chapter_ids", chapter_ids)
        object.__setattr__(self, "report_path", report_path)

    @property
    def resolved_report_path(self) -> Path:
        if self.report_path is not None:
            return self.report_path
        filename = (
            "chapter-report.json"
            if self.chapter_ids
            else (
                "word-release-report.json"
                if self.publication_profile == "word"
                else "release-report.json"
            )
        )
        return self.output_dir / "audit" / filename


@dataclass(frozen=True)
class PublicationVerificationResult:
    """Structured service result; a failed gate is data, not an exception."""

    request: PublicationVerificationRequest
    report: Mapping[str, Any]

    @property
    def ok(self) -> bool:
        return self.report.get("ok") is True

    @property
    def report_path(self) -> Path:
        return self.request.resolved_report_path


def run_publication_verification(
    request: PublicationVerificationRequest,
) -> PublicationVerificationResult:
    """Execute exactly one deterministic verifier run for ``request``."""

    required = request.required_artifacts
    report = verify_publication(
        request.output_dir,
        source_pdf=request.source_pdf,
        book_title=request.book_title,
        expected_language=request.expected_language,
        expected_translation_fingerprint=(
            request.expected_translation_fingerprint
            if request.require_translation
            else None
        ),
        require_translation=request.require_translation,
        require_epub=EPUB in required,
        require_docx=DOCX in required,
        require_docx_render=(
            DOCX in required and request.require_docx_render
        ),
        require_knowledge_base=KNOWLEDGE_BASE in required,
        require_bookmarked_pdf=BOOKMARKED_PDF in required,
        require_all_reviewed=request.require_all_reviewed,
        publication_profile=request.publication_profile,
        chapter_ids=request.chapter_ids or None,
        report_path=request.resolved_report_path,
    )
    return PublicationVerificationResult(request=request, report=report)


__all__ = [
    "BOOKMARKED_PDF",
    "DOCX",
    "EPUB",
    "KNOWLEDGE_BASE",
    "PUBLICATION_ARTIFACTS",
    "PublicationArtifact",
    "PublicationProfile",
    "PublicationVerificationRequest",
    "PublicationVerificationResult",
    "run_publication_verification",
]
