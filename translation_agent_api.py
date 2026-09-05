from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from book_pipeline import (
    build_parser,
    main,
    output_status,
    resolve_expected_ocr_model_exact,
    resolve_expected_ocr_model_prefix,
    resolve_expected_translation_identity,
    resolve_proofread_identity,
    resolve_toc_api_base,
)
from pipeline_profiles import load_pipeline_profiles
from pipeline_graph import GraphRunResult, Recipe, load_recipe
from pipeline_graph.book import (
    BookGraphConfigurationError,
    BookGraphOptions,
    NODE_PROOFREAD,
    NODE_TEXT_EXTRACT,
    NODE_TOC_OUTLINE,
    PreparedBookGraph,
    prepare_book_graph,
    semantic_status_for_args,
)
from rag_knowledge_base import (
    EmbeddingProvider,
    RagContext,
    RagEmbeddingMetadata,
    RagKnowledgeBase,
    ZhipuEmbeddingProvider,
    build_embedding_index,
)


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
    author: str | None = None
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
    rag_embed: bool | None = None
    generate_bookmarked_pdf: bool = True
    verify_publication: bool = True
    verification_profile: str | None = None
    verification_report: Path | str | None = None
    verification_chapter_ids: tuple[str, ...] = ()
    require_all_reviewed: bool = False

    def to_argv(self) -> list[str]:
        if self.verification_chapter_ids and self.phase != "verify":
            raise ValueError(
                "verification_chapter_ids are only valid when phase='verify'."
            )
        if self.rag_embed is True and not self.generate_knowledge_base:
            raise ValueError(
                "rag_embed=True requires generate_knowledge_base=True."
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
            ("--author", self.author),
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
            ("--verification-profile", self.verification_profile),
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
        if self.rag_embed is True:
            argv.append("--rag-embed")
        elif self.rag_embed is False:
            argv.append("--no-rag-embed")
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


@dataclass(frozen=True)
class GraphRunRequest:
    """Graph controls layered over a backward-compatible :class:`RunRequest`.

    Recipe files contain topology selections only.  Model endpoints and
    credential environment-variable names remain in ``pipeline.config``.
    """

    pipeline: RunRequest
    recipe: Path | str | None = None
    targets: tuple[str, ...] = ()
    enable_nodes: tuple[str, ...] = ()
    disable_nodes: tuple[str, ...] = ()
    include_proofread: bool = False
    toc_source: str = "pipeline"
    force_nodes: tuple[str, ...] = ()
    force_all: bool = False
    adopt_existing_output: bool = False
    source_mode: str | None = None
    text_pdf_sort: bool = False
    text_pdf_reflow: bool = False
    text_pdf_strip_leading_page_number_offset: int | None = None
    plugin_allowlist: tuple[str, ...] = ()

    def resolved_recipe(self) -> Recipe | None:
        recipe = load_recipe(self.recipe) if self.recipe is not None else None
        if recipe is None:
            unsupported = set(self.enable_nodes) - {
                NODE_PROOFREAD,
                NODE_TOC_OUTLINE,
            }
            if unsupported:
                raise ValueError(
                    "external or custom enabled nodes require a Recipe: "
                    f"{sorted(unsupported)}"
                )
            return None
        if not (self.targets or self.enable_nodes or self.disable_nodes):
            return recipe
        return Recipe(
            id=recipe.id,
            targets=self.targets or recipe.targets,
            enable=tuple(dict.fromkeys((*recipe.enable, *self.enable_nodes))),
            disable=tuple(dict.fromkeys((*recipe.disable, *self.disable_nodes))),
            required_plugins=recipe.required_plugins,
        )

    def graph_options(self, *, recipe: Recipe | None = None) -> BookGraphOptions:
        enabled = set(self.enable_nodes)
        forced = set(self.force_nodes)
        if recipe is None:
            recipe = self.resolved_recipe()
        recipe_selects_text_pdf = bool(
            recipe is not None and NODE_TEXT_EXTRACT in recipe.enable
        )
        if self.source_mode == "scanned-pdf" and recipe_selects_text_pdf:
            raise BookGraphConfigurationError(
                "explicit source_mode='scanned-pdf' conflicts with Recipe enabling "
                "core.pages.text_extract"
            )
        source_mode = self.source_mode or (
            "text-pdf" if recipe_selects_text_pdf else "scanned-pdf"
        )
        return BookGraphOptions(
            include_proofread=(
                self.include_proofread or NODE_PROOFREAD in enabled
            ),
            toc_source=(
                "outline" if NODE_TOC_OUTLINE in enabled else self.toc_source
            ),
            disabled_nodes=frozenset(self.disable_nodes),
            target_artifacts=frozenset(self.targets),
            force_nodes=frozenset(forced),
            force_all=self.force_all,
            adopt_existing_output=self.adopt_existing_output,
            source_mode=source_mode,
            text_pdf_sort=self.text_pdf_sort,
            text_pdf_reflow=self.text_pdf_reflow,
            text_pdf_strip_leading_page_number_offset=(
                self.text_pdf_strip_leading_page_number_offset
            ),
        )


def status_for_request(request: RunRequest) -> dict:
    """Read checkpoint status using the request's selected model identity."""

    argv = request.to_argv()
    output_dir = Path(request.output_dir).expanduser().resolve()
    expected_identity = None
    expected_proofread_identity = None
    expected_ocr_prefix = request.required_ocr_model_prefix
    expected_ocr_exact = None
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
            expected_ocr_exact = resolve_expected_ocr_model_exact(
                args,
                ocr_profile,
            )
        else:
            expected_ocr_exact = resolve_expected_ocr_model_exact(args)
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
    if output_dir.exists():
        status = output_status(
            output_dir,
            expected_translation_identity=expected_identity,
            expected_proofread_identity=expected_proofread_identity,
            expected_ocr_model_prefix=expected_ocr_prefix,
            expected_ocr_model_exact=expected_ocr_exact,
        )
        status.update(semantic_status_for_args(output_dir, args))
        return status
    return {}


def run_book(request: RunRequest) -> RunResult:
    """Run one pipeline request without accepting or serializing raw API keys."""

    exit_code = main(request.to_argv())
    output_dir = Path(request.output_dir).expanduser().resolve()
    status = status_for_request(request)
    return RunResult(exit_code=exit_code, output_dir=output_dir, status=status)


def prepare_graph(request: GraphRunRequest) -> PreparedBookGraph:
    """Resolve a replaceable graph without executing any pipeline node."""

    recipe = request.resolved_recipe()
    return prepare_book_graph(
        request.pipeline.to_argv(),
        options=request.graph_options(recipe=recipe),
        recipe=recipe,
        plugin_allowlist=request.plugin_allowlist,
    )


def plan_graph(request: GraphRunRequest) -> tuple[str, ...]:
    """Return the dependency-ordered node names for a graph request."""

    return tuple(node.name for node in prepare_graph(request).plan())


def run_graph(request: GraphRunRequest) -> GraphRunResult:
    """Execute a graph request with resumable node fingerprints."""

    return prepare_graph(request).execute()


def build_knowledge_base_embedding_index(
    output_dir: Path | str,
    provider: EmbeddingProvider | None = None,
    *,
    batch_size: int = 64,
) -> RagEmbeddingMetadata:
    """Attach an embedding provider to one published RAG knowledge base."""

    knowledge_base_path = (
        Path(output_dir).expanduser().resolve() / "knowledge_base.jsonl"
    )
    return build_embedding_index(
        knowledge_base_path,
        provider or ZhipuEmbeddingProvider(),
        batch_size=batch_size,
    )


def retrieve_knowledge_base_context(
    output_dir: Path | str,
    query: str,
    *,
    top_k: int = 5,
    max_chars: int = 12_000,
    chapter_ids: tuple[str, ...] = (),
    embedding_provider: EmbeddingProvider | None = None,
    auto_route: bool = False,
    mode: str | None = None,
    book_ids: tuple[str, ...] = (),
    authors: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
    per_book_cap: int | None = None,
    candidate_depth: int = 30,
) -> RagContext:
    """Retrieve citation-labelled chunks for downstream prompt augmentation.

    Retrieval-tuning arguments (mode/routing/caps) forward to
    ``RagKnowledgeBase.retrieve_context`` so the high-level API keeps parity
    with the ``translation-agent-kb retrieve`` CLI.
    """

    knowledge_base_path = (
        Path(output_dir).expanduser().resolve() / "knowledge_base.jsonl"
    )
    knowledge_base = RagKnowledgeBase.open(knowledge_base_path)
    return knowledge_base.retrieve_context(
        query,
        top_k=top_k,
        max_chars=max_chars,
        chapter_ids=(set(chapter_ids) if chapter_ids else None),
        embedding_provider=embedding_provider,
        auto_route=auto_route,
        mode=mode,
        book_ids=set(book_ids) if book_ids else None,
        authors=set(authors) if authors else None,
        languages=set(languages) if languages else None,
        per_book_cap=per_book_cap,
        candidate_depth=candidate_depth,
    )
