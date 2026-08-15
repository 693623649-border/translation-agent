"""Single product-level compiler and executor for :class:`RunSpec`.

This module is the only place where the stable product contract is translated
into the current Graph API or the bounded EPUB adapter workflow.  CLI and Web
clients should call :func:`plan_runspec` / :func:`execute_runspec` instead of
re-implementing target resolution or adapter orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from product_contracts import APP_VERSION, CONTRACT_SCHEMA_VERSION, RunSpec


SEMANTIC_CACHE_DIRNAME = ".translation-cache"

PDF_PHASES = frozenset(
    {
        "all",
        "ocr",
        "proofread",
        "translate",
        "toc",
        "compile",
        "epub",
        "docx",
        "verify",
        "status",
    }
)
TEXT_PDF_PHASES = PDF_PHASES - {"ocr", "proofread"}
EPUB_PHASES = frozenset({"all"})

PDF_TARGETS = frozenset(
    {
        "source.pdf",
        "pages.import.complete",
        "pages.raw",
        "pages.proofread",
        "pages.translated",
        "toc.mapped",
        "chapters.markdown",
        "chapters.semantic",
        "chapters.reader",
        "publication.epub",
        "publication.docx",
        "publication.knowledge_base",
        "publication.reference_pdf",
        "publication.report",
        "publication.word_report",
        "pipeline.status",
    }
)
EPUB_TARGETS = frozenset({"publication.epub", "publication.docx"})
DEFAULT_EPUB_TARGETS = ("publication.epub", "publication.docx")
DIRECT_PUBLICATION_TARGETS = frozenset(
    {
        "publication.epub",
        "publication.docx",
        "publication.knowledge_base",
        "publication.reference_pdf",
    }
)
REPORT_TARGETS = frozenset({"publication.report", "publication.word_report"})

_BOOLEAN_OPTIONS = frozenset(
    {
        "adopt_existing_output",
        "force_all",
        "force_legacy",
        "generate_docx",
        "generate_epub",
        "generate_knowledge_base",
        "generate_reference_pdf",
        "include_proofread",
        "keep_page_images",
        "require_all_reviewed",
        "require_complete_ocr",
        "require_translation",
        "text_pdf_reflow",
        "text_pdf_sort",
    }
)
_INTEGER_OPTIONS = frozenset(
    {
        "end_page",
        "front_matter_pages",
        "ocr_concurrency",
        "page_offset",
        "printed_pages_per_pdf_page",
        "proofread_concurrency",
        "start_page",
        "text_pdf_strip_leading_page_number_offset",
        "translation_concurrency",
        "translation_max_chars",
        "translation_max_tokens",
        "translation_retries",
    }
)
_STRING_OPTIONS = frozenset(
    {
        "glossary",
        "granularity",
        "ocr_profile",
        "ocr_reading_direction",
        "proofread_profile",
        "source_language",
        "toc_pages",
        "toc_profile",
        "toc_source",
        "translation_profile",
    }
)
_FLOAT_OPTIONS = frozenset({"translation_temperature"})
_SEQUENCE_OPTIONS = frozenset({"force_nodes"})
KNOWN_OPTIONS = (
    _BOOLEAN_OPTIONS
    | _INTEGER_OPTIONS
    | _STRING_OPTIONS
    | _FLOAT_OPTIONS
    | _SEQUENCE_OPTIONS
)


class RunResolutionError(ValueError):
    """Raised before execution when a RunSpec has no unambiguous product plan."""


class RunExecutionError(ValueError):
    """Raised when a resolved run cannot safely complete."""


ProgressCallback = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class PlanStep:
    name: str
    executor: str
    version: str | None = None
    requires: tuple[str, ...] = ()
    provides: tuple[str, ...] = ()
    cache: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "executor": self.executor,
        }
        if self.version is not None:
            payload["version"] = self.version
        if self.requires:
            payload["requires"] = list(self.requires)
        if self.provides:
            payload["provides"] = list(self.provides)
        if self.cache is not None:
            payload["cache"] = self.cache
        return payload


@dataclass(frozen=True)
class ResolvedRun:
    spec: RunSpec
    targets: tuple[str, ...]
    backend: str
    release_profile: str

    @property
    def semantic_cache_dir(self) -> Path:
        return (
            Path(self.spec.output_dir).expanduser().resolve()
            / SEMANTIC_CACHE_DIRNAME
        )


@dataclass(frozen=True)
class RunPlan:
    source_mode: str
    targets: tuple[str, ...]
    backend: str
    release_profile: str
    nodes: tuple[PlanStep, ...]
    schema_version: int = CONTRACT_SCHEMA_VERSION
    app_version: str = APP_VERSION

    @property
    def node_names(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "source_mode": self.source_mode,
            "targets": list(self.targets),
            "backend": self.backend,
            "release_profile": self.release_profile,
            "nodes": [node.to_dict() for node in self.nodes],
        }


@dataclass(frozen=True)
class RunExecutionResult:
    status: str
    source_mode: str
    targets: tuple[str, ...]
    release_profile: str
    run_id: str | None = None
    release_ready: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CONTRACT_SCHEMA_VERSION
    app_version: str = APP_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "app_version": self.app_version,
            "status": self.status,
            "source_mode": self.source_mode,
            "targets": list(self.targets),
            "release_profile": self.release_profile,
            "release_ready": self.release_ready,
            "run_id": self.run_id,
            **dict(self.details),
        }


def _option_error(name: str, expected: str) -> RunResolutionError:
    return RunResolutionError(f"RunSpec.options.{name} must be {expected}")


def _validate_options(options: Mapping[str, Any]) -> None:
    unknown = sorted(set(options) - KNOWN_OPTIONS)
    if unknown:
        raise RunResolutionError(f"RunSpec.options has unknown fields: {unknown}")
    for name, value in options.items():
        if value is None:
            continue
        if name in _BOOLEAN_OPTIONS and type(value) is not bool:
            raise _option_error(name, "a boolean")
        if name in _INTEGER_OPTIONS and type(value) is not int:
            raise _option_error(name, "an integer")
        if name in _STRING_OPTIONS and (
            not isinstance(value, str) or not value.strip()
        ):
            raise _option_error(name, "a non-empty string")
        if name in _FLOAT_OPTIONS and (
            type(value) not in {int, float} or isinstance(value, bool)
        ):
            raise _option_error(name, "a number")
        if name in _SEQUENCE_OPTIONS and (
            not isinstance(value, (list, tuple))
            or not all(isinstance(item, str) and item for item in value)
        ):
            raise _option_error(name, "an array of non-empty strings")

    positive = {
        "start_page",
        "end_page",
        "ocr_concurrency",
        "printed_pages_per_pdf_page",
        "proofread_concurrency",
        "translation_concurrency",
        "translation_max_chars",
        "translation_max_tokens",
    }
    for name in positive:
        value = options.get(name)
        if value is not None and value < 1:
            raise RunResolutionError(f"RunSpec.options.{name} must be at least 1")
    retries = options.get("translation_retries")
    if retries is not None and retries < 1:
        raise RunResolutionError(
            "RunSpec.options.translation_retries must be at least 1"
        )
    front_matter = options.get("front_matter_pages")
    if front_matter is not None and front_matter < 0:
        raise RunResolutionError(
            "RunSpec.options.front_matter_pages must not be negative"
        )
    temperature = options.get("translation_temperature")
    if temperature is not None and not 0 <= float(temperature) <= 2:
        raise RunResolutionError(
            "RunSpec.options.translation_temperature must be between 0 and 2"
        )
    if options.get("toc_source") not in {None, "pipeline", "outline"}:
        raise RunResolutionError(
            "RunSpec.options.toc_source must be 'pipeline' or 'outline'"
        )


def _release_profile(spec: RunSpec, targets: tuple[str, ...]) -> str:
    if spec.source_mode == "epub":
        return "draft-only"
    if "publication.word_report" in targets:
        return "word"
    if "publication.report" in targets:
        return "full"
    return "draft"


def _validate_source_shape(spec: RunSpec) -> None:
    if spec.source is None:
        if spec.source_mode == "epub" or spec.phase not in {
            "epub",
            "docx",
            "verify",
            "status",
        }:
            raise RunResolutionError(
                f"source_mode={spec.source_mode!r} phase={spec.phase!r} "
                "requires a source file"
            )
        return
    suffix = Path(spec.source).suffix.casefold()
    expected = ".epub" if spec.source_mode == "epub" else ".pdf"
    if suffix != expected:
        raise RunResolutionError(
            f"source_mode={spec.source_mode!r} requires a {expected} source"
        )


def _validate_targets(spec: RunSpec, targets: tuple[str, ...]) -> None:
    supported = EPUB_TARGETS if spec.source_mode == "epub" else PDF_TARGETS
    unsupported = sorted(set(targets) - supported)
    if unsupported:
        raise RunResolutionError(
            f"source_mode={spec.source_mode!r} does not support targets: "
            f"{unsupported}"
        )
    if len(set(targets)) != len(targets):
        raise RunResolutionError("resolved targets must not contain duplicates")
    if set(REPORT_TARGETS) <= set(targets):
        raise RunResolutionError(
            "choose either publication.report or publication.word_report, not both"
        )
    requested_reports = set(targets) & REPORT_TARGETS
    if requested_reports and not spec.verify:
        raise RunResolutionError(
            "release report targets require verify=true; use direct publication "
            "targets for drafts"
        )
    requested_direct = set(targets) & DIRECT_PUBLICATION_TARGETS
    if spec.verify and requested_direct and not requested_reports:
        recommendation = (
            "publication.word_report"
            if requested_direct == {"publication.docx"}
            else "publication.report"
        )
        raise RunResolutionError(
            "verify=true cannot target unverified publication artifacts directly; "
            f"target {recommendation!r} or set verify=false for drafts"
        )


def _graph_request(
    spec: RunSpec,
    targets: tuple[str, ...],
    *,
    load_dotenv: bool = True,
):
    from translation_agent_api import GraphRunRequest, RunRequest

    options = dict(spec.options)
    requested_publications = set(targets)
    if requested_publications:
        generate_epub = "publication.epub" in requested_publications
        generate_docx = "publication.docx" in requested_publications
        generate_kb = "publication.knowledge_base" in requested_publications
        generate_pdf = "publication.reference_pdf" in requested_publications
    else:
        generate_epub = generate_docx = generate_kb = generate_pdf = True
    if "publication.report" in requested_publications:
        generate_epub = bool(options.get("generate_epub", True))
        generate_docx = bool(options.get("generate_docx", True))
        generate_kb = bool(options.get("generate_knowledge_base", True))
        generate_pdf = bool(options.get("generate_reference_pdf", True))
    if "publication.word_report" in requested_publications:
        generate_epub = generate_kb = generate_pdf = False
        generate_docx = True

    pipeline = RunRequest(
        input_pdf=(
            spec.source
            if spec.source is not None and spec.source_mode != "epub"
            else None
        ),
        output_dir=spec.output_dir,
        phase=spec.phase,
        config=spec.config,
        ocr_profile=options.get("ocr_profile"),
        toc_profile=options.get("toc_profile"),
        proofread_profile=options.get("proofread_profile"),
        translation_profile=options.get("translation_profile"),
        title=spec.title,
        author=spec.author,
        start_page=options.get("start_page"),
        end_page=options.get("end_page"),
        translate_non_chinese=spec.translate,
        source_language=str(options.get("source_language") or "auto"),
        target_language=spec.target_language,
        ocr_concurrency=options.get("ocr_concurrency"),
        proofread_concurrency=options.get("proofread_concurrency"),
        translation_concurrency=options.get("translation_concurrency"),
        granularity=options.get("granularity"),
        toc_pages=options.get("toc_pages"),
        page_offset=options.get("page_offset"),
        printed_pages_per_pdf_page=options.get("printed_pages_per_pdf_page"),
        front_matter_pages=options.get("front_matter_pages"),
        ocr_reading_direction=options.get("ocr_reading_direction"),
        keep_page_images=bool(options.get("keep_page_images", False)),
        force=bool(options.get("force_legacy", False)),
        require_complete_ocr=bool(options.get("require_complete_ocr", True)),
        require_translation=bool(options.get("require_translation", spec.translate)),
        generate_epub=generate_epub,
        generate_docx=generate_docx,
        generate_knowledge_base=generate_kb,
        generate_bookmarked_pdf=generate_pdf,
        verify_publication=spec.verify,
        require_all_reviewed=bool(options.get("require_all_reviewed", False)),
    )
    return GraphRunRequest(
        pipeline=pipeline,
        load_dotenv=load_dotenv,
        recipe=spec.recipe,
        targets=targets,
        include_proofread=bool(options.get("include_proofread", False)),
        toc_source=str(options.get("toc_source") or "pipeline"),
        force_nodes=tuple(options.get("force_nodes", ())),
        force_all=bool(options.get("force_all", False)),
        adopt_existing_output=bool(options.get("adopt_existing_output", False)),
        source_mode=(spec.source_mode if spec.source_mode != "epub" else None),
        text_pdf_sort=bool(options.get("text_pdf_sort", False)),
        text_pdf_reflow=bool(options.get("text_pdf_reflow", False)),
        text_pdf_strip_leading_page_number_offset=options.get(
            "text_pdf_strip_leading_page_number_offset"
        ),
    )


def _emit(
    callback: ProgressCallback | None,
    event: str,
    **data: Any,
) -> None:
    if callback is not None:
        callback(
            {
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "event": event,
                **data,
            }
        )


class RunExecutionService:
    """Compile and execute RunSpec with one source/target capability policy."""

    def __init__(self, *, load_dotenv: bool = True) -> None:
        if type(load_dotenv) is not bool:
            raise TypeError("load_dotenv must be a boolean")
        self.load_dotenv = load_dotenv

    def resolve(self, spec: RunSpec) -> ResolvedRun:
        if not isinstance(spec, RunSpec):
            raise RunResolutionError("run must be described by a RunSpec")
        _validate_options(spec.options)
        phases = EPUB_PHASES if spec.source_mode == "epub" else (
            TEXT_PDF_PHASES if spec.source_mode == "text-pdf" else PDF_PHASES
        )
        if spec.phase not in phases:
            raise RunResolutionError(
                f"source_mode={spec.source_mode!r} does not support phase "
                f"{spec.phase!r}; expected one of {sorted(phases)}"
            )
        _validate_source_shape(spec)
        if spec.source_mode == "epub" and spec.recipe is not None:
            raise RunResolutionError(
                "EPUB adapter runs do not support Graph Recipe files yet; "
                "select publication.epub/publication.docx targets explicitly"
            )
        if spec.source_mode == "epub" and spec.verify:
            raise RunResolutionError(
                "EPUB-native release verification is not available; set "
                "verify=false (CLI: --no-verify) to explicitly authorize "
                "draft artifacts"
            )
        if (
            spec.source_mode != "epub"
            and spec.verify
            and not spec.targets
            and spec.phase in {"epub", "docx"}
        ):
            raise RunResolutionError(
                f"phase={spec.phase!r} produces an unverified draft; set "
                "verify=false or use phase='all' with a release report target"
            )
        targets = spec.targets or (
            DEFAULT_EPUB_TARGETS if spec.source_mode == "epub" else ()
        )
        _validate_targets(spec, targets)
        resolved_spec = spec if targets == spec.targets else replace(spec, targets=targets)
        return ResolvedRun(
            spec=resolved_spec,
            targets=tuple(targets),
            backend=("epub-adapter+graph-publish" if spec.source_mode == "epub" else "graph"),
            release_profile=_release_profile(resolved_spec, tuple(targets)),
        )

    def plan(self, spec: RunSpec | ResolvedRun) -> RunPlan:
        resolved = spec if isinstance(spec, ResolvedRun) else self.resolve(spec)
        plan, _prepared = self._prepare_plan(resolved)
        return plan

    def _prepare_plan(self, resolved: ResolvedRun) -> tuple[RunPlan, Any | None]:
        """Compile once and retain the exact Graph instance for execution."""

        if resolved.spec.source_mode == "epub":
            nodes: list[PlanStep] = [
                PlanStep("adapter.epub.semantic_import", "adapter")
            ]
            if resolved.spec.translate:
                nodes.extend(
                    [
                        PlanStep("adapter.semantic.translate", "adapter"),
                        PlanStep("adapter.epub.apply_translations", "adapter"),
                    ]
                )
            if "publication.epub" in resolved.targets:
                nodes.append(PlanStep("core.publish.epub", "graph"))
            if "publication.docx" in resolved.targets:
                nodes.append(PlanStep("core.publish.docx", "graph"))
            return (
                RunPlan(
                    source_mode=resolved.spec.source_mode,
                    targets=resolved.targets,
                    backend=resolved.backend,
                    release_profile=resolved.release_profile,
                    nodes=tuple(nodes),
                ),
                None,
            )

        from translation_agent_api import prepare_graph

        prepared = prepare_graph(
            _graph_request(
                resolved.spec,
                resolved.targets,
                load_dotenv=self.load_dotenv,
            )
        )
        prepared_targets = tuple(sorted(prepared.targets))
        # Recipes may provide the targets when RunSpec.targets is empty.  Apply
        # the same source/verification capability gate to those resolved
        # targets before returning a plan or mutating the workspace.
        _validate_targets(resolved.spec, prepared_targets)
        nodes = tuple(
            PlanStep(
                name=node.name,
                executor="graph",
                version=node.version,
                requires=tuple(sorted(node.requires)),
                provides=tuple(sorted(node.provides)),
                cache=node.cache,
            )
            for node in prepared.plan()
        )
        return (
            RunPlan(
                source_mode=resolved.spec.source_mode,
                targets=prepared_targets,
                backend=resolved.backend,
                release_profile=_release_profile(resolved.spec, prepared_targets),
                nodes=nodes,
            ),
            prepared,
        )

    def execute(
        self,
        spec: RunSpec | ResolvedRun,
        *,
        progress: ProgressCallback | None = None,
    ) -> RunExecutionResult:
        resolved = spec if isinstance(spec, ResolvedRun) else self.resolve(spec)
        # Planning is an execution precondition.  It keeps configuration and
        # dependency failures ahead of any adapter mutation.
        plan, prepared = self._prepare_plan(resolved)
        _emit(
            progress,
            "run_planned",
            source_mode=resolved.spec.source_mode,
            targets=list(plan.targets),
            nodes=list(plan.node_names),
        )
        if resolved.spec.source_mode == "epub":
            return self._execute_epub(resolved, progress=progress)

        if prepared is None:  # pragma: no cover - guarded by source_mode above
            raise AssertionError("PDF execution requires a prepared Graph")
        # Execute the exact graph instance whose targets and node order were
        # returned above.  Re-parsing Recipe/config here would create a TOCTOU
        # window in which plan and execution could mean different things.
        result = prepared.execute()
        missing_targets = sorted(set(plan.targets) - set(result.values))
        if missing_targets:
            raise RunExecutionError(
                "Graph completed without requested plan targets: "
                f"{missing_targets}"
            )
        release_ready = self._verified_release_ready(resolved.spec, plan)
        if plan.release_profile in {"word", "full"} and not release_ready:
            raise RunExecutionError(
                "Graph verifier did not produce a release-ready "
                f"{plan.release_profile!r} report"
            )
        _emit(
            progress,
            "run_finished",
            run_id=result.run_id,
            status="passed",
            release_ready=release_ready,
        )
        return RunExecutionResult(
            status="passed",
            source_mode=resolved.spec.source_mode,
            targets=plan.targets,
            release_profile=plan.release_profile,
            release_ready=release_ready,
            run_id=result.run_id,
            details={
                "plan": list(result.plan),
                "executed": list(result.executed),
                "skipped": list(result.skipped),
                "state": str(result.state_path),
                "events": str(result.events_path),
            },
        )

    @staticmethod
    def _verified_release_ready(spec: RunSpec, plan: RunPlan) -> bool:
        """Trust only the verifier's materialized report, never intent flags."""

        report_name = {
            "word": "word-release-report.json",
            "full": "release-report.json",
        }.get(plan.release_profile)
        if report_name is None:
            return False
        verifier_node = {
            "word": "core.publication.verify.word",
            "full": "core.publication.verify",
        }[plan.release_profile]
        if verifier_node not in plan.node_names:
            return False
        report_path = (
            Path(spec.output_dir).expanduser().resolve() / "audit" / report_name
        )
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(payload, Mapping)
            and payload.get("release_ready") is True
            and payload.get("mode") == "full"
            and payload.get("publication_profile") == plan.release_profile
        )

    def _execute_epub(
        self,
        resolved: ResolvedRun,
        *,
        progress: ProgressCallback | None,
    ) -> RunExecutionResult:
        from epub_semantic_import import import_epub

        spec = resolved.spec
        output = Path(spec.output_dir).expanduser().resolve()
        source = Path(str(spec.source)).expanduser().resolve()
        _emit(progress, "adapter_started", adapter="epub.semantic_import")
        imported = import_epub(source, output)
        if imported.get("release_blocked") is True or imported.get("status") in {
            "blocked",
            "failed",
        }:
            raise RunExecutionError(
                "EPUB semantic reconstruction is blocked; review its audit"
            )
        _emit(progress, "adapter_finished", adapter="epub.semantic_import")

        translation_result: Mapping[str, Any] | None = None
        apply_result: Mapping[str, Any] | None = None
        if spec.translate:
            from epub_semantic_import import apply_translations
            from semantic_translation_runner import (
                RUNNER_VERSION,
                _deepseek_request,
                load_glossary,
                translate_units,
            )

            options = dict(spec.options)
            profile = None
            if spec.config is not None:
                from pipeline_profiles import load_pipeline_profiles

                profile = load_pipeline_profiles(spec.config).for_stage(
                    "translation",
                    options.get("translation_profile"),
                )
            elif options.get("translation_profile"):
                raise RunExecutionError(
                    "translation_profile requires a RunSpec.config profile file"
                )

            model = profile.model if profile else "deepseek-v4-flash"
            provider = profile.provider if profile else "deepseek"
            base_url = (
                profile.base_url if profile and profile.base_url else "https://api.deepseek.com"
            )
            prompt_profile = profile.name if profile else RUNNER_VERSION
            thinking = profile.thinking if profile else "disabled"
            timeout = profile.timeout if profile else 120
            concurrency = int(
                options.get("translation_concurrency")
                or (profile.concurrency if profile else 16)
            )
            credential_env = (
                profile.credential_env if profile else "DEEPSEEK_API_KEY"
            )
            credential = os.getenv(credential_env or "", "")
            if not credential:
                raise RunExecutionError(
                    f"missing credential environment variable: {credential_env}"
                )
            temperature = float(options.get("translation_temperature", 0.0))
            request = _deepseek_request(
                api_key=credential,
                base_url=base_url,
                model=model,
                timeout=timeout,
                thinking=thinking,
                temperature=temperature,
                max_tokens=int(options.get("translation_max_tokens", 32768)),
            )
            glossary_value = options.get("glossary")
            glossary = (
                load_glossary(Path(str(glossary_value)).expanduser())
                if glossary_value
                else {}
            )
            translations = output / "semantic" / "translations.jsonl"
            _emit(progress, "adapter_started", adapter="semantic.translate")
            translation_result = translate_units(
                output / "semantic" / "translation-units.jsonl",
                translations,
                target_language=spec.target_language,
                glossary=glossary,
                model=model,
                provider=provider,
                base_url=base_url,
                prompt_profile=prompt_profile,
                thinking=thinking,
                temperature=temperature,
                cache_dir=resolved.semantic_cache_dir,
                request=request,
                max_chars=int(options.get("translation_max_chars", 9000)),
                concurrency=concurrency,
                retries=int(options.get("translation_retries", 3)),
                progress=lambda done, total, cached: _emit(
                    progress,
                    "translation_progress",
                    completed=done,
                    total=total,
                    cached=cached,
                ),
            )
            _emit(progress, "adapter_finished", adapter="semantic.translate")
            apply_result = apply_translations(
                output,
                translations,
                target_language=spec.target_language,
                glossary=glossary,
            )
            if apply_result.get("release_blocked") is True or apply_result.get(
                "status"
            ) in {"blocked", "failed"}:
                raise RunExecutionError(
                    "EPUB translated semantic reconstruction is blocked; review its audit"
                )

        graph_runs: list[dict[str, Any]] = []
        from translation_agent_api import run_graph

        for artifact, phase in (
            ("publication.epub", "epub"),
            ("publication.docx", "docx"),
        ):
            if artifact not in resolved.targets:
                continue
            phase_spec = replace(spec, phase=phase, targets=())
            result = run_graph(
                _graph_request(
                    phase_spec,
                    (artifact,),
                    load_dotenv=self.load_dotenv,
                )
            )
            if artifact not in result.values:
                raise RunExecutionError(
                    f"Graph publisher completed without requested target {artifact!r}"
                )
            graph_runs.append(
                {
                    "artifact": artifact,
                    "run_id": result.run_id,
                    "executed": list(result.executed),
                    "skipped": list(result.skipped),
                }
            )

        run_id = graph_runs[-1]["run_id"] if graph_runs else None
        _emit(progress, "run_finished", run_id=run_id, status="passed")
        return RunExecutionResult(
            status="passed",
            source_mode=spec.source_mode,
            targets=resolved.targets,
            release_profile=resolved.release_profile,
            release_ready=False,
            run_id=run_id,
            details={
                "publication_status": "draft",
                "reason": (
                    "EPUB-native release verification is not available; "
                    "artifacts remain drafts"
                ),
                "ingest": dict(imported),
                "translation": (
                    dict(translation_result) if translation_result is not None else None
                ),
                "apply": dict(apply_result) if apply_result is not None else None,
                "publications": graph_runs,
                "semantic_cache": str(resolved.semantic_cache_dir),
            },
        )


_DEFAULT_SERVICE = RunExecutionService()


def plan_runspec(spec: RunSpec) -> RunPlan:
    """Compile a RunSpec without mutating its source or output workspace."""

    return _DEFAULT_SERVICE.plan(spec)


def execute_runspec(
    spec: RunSpec,
    *,
    progress: ProgressCallback | None = None,
) -> RunExecutionResult:
    """Execute exactly the plan selected by the shared product compiler."""

    return _DEFAULT_SERVICE.execute(spec, progress=progress)
