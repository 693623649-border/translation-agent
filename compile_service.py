"""Typed, argv-free chapter compilation boundary.

The compiler implementation still lives in :mod:`book_pipeline` while the
legacy phase layer is being retired.  This module owns the deterministic
compile contract so both the CLI and DAG call the same validation and output
path without replaying an argparse entry point.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence

from pipeline_profiles import ModelIdentity

__all__ = [
    "ChapterCompileRequest",
    "ChapterCompileResult",
    "CompilePageRecord",
    "run_chapter_compile",
]


class CompilePageRecord(Protocol):
    """Checkpoint fields used by compile validation before legacy assembly."""

    pdf_page: int
    ocr_model: str


@dataclass(frozen=True)
class ChapterCompileRequest:
    """All inputs that affect the canonical chapter bundle."""

    source_pdf: Path
    output_dir: Path
    page_records: Sequence[CompilePageRecord]
    source_page_count: int
    publication_title: str
    toc_path: Path | None = None
    page_offset: int | None = None
    printed_pages_per_pdf_page: int | None = None
    granularity: str | None = None
    require_complete_ocr: bool = False
    required_ocr_model_prefix: str | None = None
    require_translation: bool = False
    expected_translation_identity: ModelIdentity | None = None

    @property
    def resolved_source_pdf(self) -> Path:
        return self.source_pdf.expanduser().resolve()

    @property
    def resolved_output_dir(self) -> Path:
        return self.output_dir.expanduser().resolve()

    @property
    def resolved_toc_path(self) -> Path:
        if self.toc_path is not None:
            return self.toc_path.expanduser().resolve()
        return self.resolved_output_dir / "toc.json"


@dataclass(frozen=True)
class ChapterCompileResult:
    """Canonical chapter outputs plus the resolved compile decisions."""

    request: ChapterCompileRequest
    manifest: list[dict[str, Any]]
    knowledge_rows: list[dict[str, Any]]
    toc_payload: dict[str, Any]
    granularity: str

    @property
    def manifest_path(self) -> Path:
        return self.request.resolved_output_dir / "chapters.json"

    @property
    def chapter_dir(self) -> Path:
        return self.request.resolved_output_dir / "chapters"


def _legacy_compiler() -> Any:
    """Load the implementation lazily to avoid a book_pipeline import cycle."""

    return importlib.import_module("book_pipeline")


def run_chapter_compile(request: ChapterCompileRequest) -> ChapterCompileResult:
    """Validate checkpoints and compile the canonical chapter bundle."""

    compiler = _legacy_compiler()
    source_pdf = request.resolved_source_pdf
    output_dir = request.resolved_output_dir
    records = list(request.page_records)
    if not records:
        raise ValueError(
            "No page OCR records found. Run --phase ocr or import existing OCR first."
        )

    if request.require_complete_ocr:
        cached_pages = {record.pdf_page for record in records}
        missing_pages = [
            page
            for page in range(1, request.source_page_count + 1)
            if page not in cached_pages
        ]
        extra_pages = sorted(
            page
            for page in cached_pages
            if page < 1 or page > request.source_page_count
        )
        if missing_pages or extra_pages:
            preview = missing_pages[:20]
            suffix = "..." if len(missing_pages) > len(preview) else ""
            raise ValueError(
                "Complete OCR is required but cached page numbers do not exactly "
                f"match the source PDF: missing={preview}{suffix}, "
                f"extra={extra_pages[:20]}"
            )

    if request.required_ocr_model_prefix:
        required_prefixes = compiler.parse_model_prefixes(
            request.required_ocr_model_prefix
        )
        wrong_models = [
            (record.pdf_page, record.ocr_model)
            for record in records
            if not required_prefixes
            or not record.ocr_model.startswith(required_prefixes)
        ]
        if wrong_models:
            preview = wrong_models[:12]
            suffix = "..." if len(wrong_models) > len(preview) else ""
            raise ValueError(
                f"OCR model prefix {request.required_ocr_model_prefix!r} is required, "
                f"but cached pages do not match: {preview}{suffix}"
            )

    toc_path = request.resolved_toc_path
    toc_payload = compiler.load_toc(toc_path)
    if (
        not isinstance(toc_payload.get("page_offset"), int)
        or request.page_offset is not None
        or request.printed_pages_per_pdf_page is not None
    ):
        toc_payload = compiler.apply_page_mapping(
            toc_payload,
            records,
            page_offset=request.page_offset,
            source_page_count=request.source_page_count,
            printed_pages_per_pdf_page=request.printed_pages_per_pdf_page,
        )
        compiler.write_json(toc_path, toc_payload)

    compile_granularity = compiler.resolve_compile_granularity(
        output_dir,
        toc_payload,
        request.granularity,
    )
    manifest, knowledge_rows = compiler.compile_chapters(
        source_pdf,
        output_dir,
        records,
        toc_payload,
        granularity=compile_granularity,
        publication_title=request.publication_title,
        require_translation=request.require_translation,
        expected_translation_identity=request.expected_translation_identity,
    )
    return ChapterCompileResult(
        request=request,
        manifest=manifest,
        knowledge_rows=knowledge_rows,
        toc_payload=toc_payload,
        granularity=compile_granularity,
    )
