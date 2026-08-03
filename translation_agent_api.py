from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from book_pipeline import (
    build_parser,
    main,
    output_status,
    resolve_expected_ocr_model_prefix,
    resolve_expected_translation_identity,
    resolve_proofread_identity,
    resolve_toc_api_base,
)
from pipeline_profiles import load_pipeline_profiles


@dataclass(frozen=True)
class RunRequest:
    output_dir: Path | str
    input_pdf: Path | str | None = None
    phase: str = "all"
    config: Path | str | None = None
    ocr_profile: str | None = None
    toc_profile: str | None = None
    proofread_profile: str | None = None
    translation_profile: str | None = None
    title: str | None = None
    start_page: int | None = None
    end_page: int | None = None
    translate_non_chinese: bool = False
    source_language: str = "auto"
    target_language: str = "简体中文"
    ocr_concurrency: int | None = None
    proofread_language: str = "ja"
    proofread_concurrency: int | None = None
    proofread_delay: float | None = None
    proofread_max_chars: int | None = None
    translation_concurrency: int | None = None
    granularity: str | None = None
    toc_pages: str | None = None
    page_offset: int | None = None
    printed_pages_per_pdf_page: int | None = None
    front_matter_pages: int | None = None
    ocr_reading_direction: str | None = None
    keep_page_images: bool = False
    force: bool = False
    require_complete_ocr: bool = True
    require_translation: bool | None = None
    required_ocr_model_prefix: str | None = None
    generate_epub: bool = True
    generate_docx: bool = True
    generate_knowledge_base: bool = True
    generate_bookmarked_pdf: bool = True
    verify_publication: bool = True
    verification_report: Path | str | None = None
    verification_chapter_ids: tuple[str, ...] = ()
    require_all_reviewed: bool = False

    def to_argv(self) -> list[str]:
        if self.verification_chapter_ids and self.phase != "verify":
            raise ValueError(
                "verification_chapter_ids are only valid when phase='verify'."
            )
        argv: list[str] = []
        if self.input_pdf is not None:
            argv.append(str(self.input_pdf))
        argv.extend(["--output-dir", str(self.output_dir), "--phase", self.phase])
        pairs = (
            ("--config", self.config),
            ("--ocr-profile", self.ocr_profile),
            ("--toc-profile", self.toc_profile),
            ("--proofread-profile", self.proofread_profile),
            ("--translation-profile", self.translation_profile),
            ("--title", self.title),
            ("--start-page", self.start_page),
            ("--end-page", self.end_page),
            ("--translation-source-language", self.source_language),
            ("--target-language", self.target_language),
            ("--ocr-concurrency", self.ocr_concurrency),
            ("--proofread-language", self.proofread_language),
            ("--proofread-concurrency", self.proofread_concurrency),
            ("--proofread-delay", self.proofread_delay),
            ("--proofread-max-chars", self.proofread_max_chars),
            ("--translation-concurrency", self.translation_concurrency),
            ("--granularity", self.granularity),
            ("--toc-pages", self.toc_pages),
            ("--page-offset", self.page_offset),
            ("--printed-pages-per-pdf-page", self.printed_pages_per_pdf_page),
            ("--front-matter-pages", self.front_matter_pages),
            ("--ocr-reading-direction", self.ocr_reading_direction),
            ("--required-ocr-model-prefix", self.required_ocr_model_prefix),
            ("--report", self.verification_report),
        )
        for option, value in pairs:
            if value is not None:
                argv.extend([option, str(value)])
        if self.translate_non_chinese:
            argv.append("--translate-non-chinese")
        if self.require_complete_ocr:
            argv.append("--require-complete-ocr")
        if self.require_translation is True or (
            self.require_translation is None
            and self.translate_non_chinese
            and self.phase in {"all", "compile"}
        ):
            argv.append("--require-translation")
        if self.keep_page_images:
            argv.append("--keep-page-images")
        if self.force:
            argv.append("--force")
        if not self.generate_epub:
            argv.append("--no-epub")
        if not self.generate_docx:
            argv.append("--no-docx")
        if not self.generate_knowledge_base:
            argv.append("--no-kb")
        if not self.generate_bookmarked_pdf:
            argv.append("--no-bookmarked-pdf")
        if not self.verify_publication:
            argv.append("--no-verify")
        if self.require_all_reviewed:
            argv.append("--require-all-reviewed")
        for chapter_id in self.verification_chapter_ids:
            argv.extend(["--chapter-id", str(chapter_id)])
        return argv


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    output_dir: Path
    status: dict

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def status_for_request(request: RunRequest) -> dict:
    """Read checkpoint status using the request's selected model identity."""

    argv = request.to_argv()
    output_dir = Path(request.output_dir).expanduser().resolve()
    expected_identity = None
    expected_proofread_identity = None
    expected_ocr_prefix = request.required_ocr_model_prefix
    if output_dir.exists():
        args = build_parser().parse_args(argv)
        toc_profile = None
        proofread_profile = None
        translation_profile = None
        if args.config:
            profiles = load_pipeline_profiles(args.config)
            ocr_profile = profiles.for_stage("ocr", args.ocr_profile)
            toc_profile = profiles.for_stage("toc", args.toc_profile)
            proofread_profile = profiles.for_stage(
                "proofread",
                args.proofread_profile,
            )
            translation_profile = profiles.for_stage(
                "translation",
                args.translation_profile,
            )
            expected_ocr_prefix = resolve_expected_ocr_model_prefix(
                args,
                ocr_profile,
            )
        expected_identity = resolve_expected_translation_identity(
            args,
            toc_profile=toc_profile,
            translation_profile=translation_profile,
        )
        expected_proofread_identity = resolve_proofread_identity(
            args,
            glm_api_base=resolve_toc_api_base(args, toc_profile=toc_profile),
            profile=proofread_profile,
        )
    return (
        output_status(
            output_dir,
            expected_translation_identity=expected_identity,
            expected_proofread_identity=expected_proofread_identity,
            expected_ocr_model_prefix=expected_ocr_prefix,
        )
        if output_dir.exists()
        else {}
    )


def run_book(request: RunRequest) -> RunResult:
    """Run one pipeline request without accepting or serializing raw API keys."""

    exit_code = main(request.to_argv())
    output_dir = Path(request.output_dir).expanduser().resolve()
    status = status_for_request(request)
    return RunResult(exit_code=exit_code, output_dir=output_dir, status=status)
