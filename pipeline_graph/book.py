"""Book-pipeline adapters for the lightweight DAG runtime.

The first graph generation deliberately wraps the proven phase functions in
``book_pipeline``.  Page JSON, model identities, CAS updates, stage locks, and
all reader artifact formats therefore remain compatible.  The orchestration
and publishers are independent nodes, which is the seam used by custom
recipes and trusted plugins.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import urllib.parse
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

import fitz

import book_pipeline as legacy
import extract_textbook_layer
import rag_knowledge_base
from pipeline_profiles import load_pipeline_profiles

from .core import (
    GraphContext,
    GraphExecutor,
    GraphRunResult,
    NodeResult,
    NodeSpec,
    PipelineGraph,
    stable_fingerprint,
)
from .recipe import NodeRegistry, Recipe


GRAPH_ADAPTER_VERSION = "book-graph-v2"

NODE_SOURCE = "core.source.inspect"
NODE_PAGES_IMPORT = "core.pages.import"
NODE_PAGES_LOAD = "core.pages.load"
NODE_OCR = "core.pages.ocr"
NODE_TEXT_EXTRACT = "core.pages.text_extract"
NODE_PROOFREAD = "core.pages.proofread"
NODE_TRANSLATE = "core.pages.translate"
NODE_TOC_LOAD = "core.toc.load"
NODE_TOC_PIPELINE = "core.toc.resolve"
NODE_TOC_OUTLINE = "core.toc.from_outline"
NODE_CHAPTERS_LOAD = "core.chapters.load"
NODE_COMPILE = "core.chapters.compile"
NODE_SEMANTIC = "core.reconstruct.semantic"
NODE_SANITIZE = "core.publication.sanitize"
NODE_KB = "core.publish.knowledge_base"
NODE_EPUB = "core.publish.epub"
NODE_DOCX = "core.publish.docx"
NODE_REFERENCE_PDF = "core.publish.reference_pdf"
NODE_VERIFY = "core.publication.verify"
NODE_VERIFY_WORD = "core.publication.verify.word"
NODE_STATUS = "core.pipeline.status"

ART_SOURCE = "source.pdf"
ART_PAGES_IMPORTED = "pages.import.complete"
ART_PAGES_RAW = "pages.raw"
ART_PAGES_PROOFREAD = "pages.proofread"
ART_PAGES_TRANSLATED = "pages.translated"
ART_TOC = "toc.mapped"
ART_CHAPTERS = "chapters.markdown"
ART_SEMANTIC_CHAPTERS = "chapters.semantic"
ART_READER_CHAPTERS = "chapters.reader"
ART_KB = "publication.knowledge_base"
ART_EPUB = "publication.epub"
ART_DOCX = "publication.docx"
ART_REFERENCE_PDF = "publication.reference_pdf"
ART_REPORT = "publication.report"
ART_WORD_REPORT = "publication.word_report"
ART_STATUS = "pipeline.status"

SEMANTIC_AUDIT_SCHEMA_VERSION = 1
SEMANTIC_AUDIT_RELATIVE_PATH = Path("audit/semantic-reconstruction.json")
DRAFT_SEMANTIC_AUDIT_RELATIVE_PATH = Path(
    ".pipeline_graph/draft-semantic-audit.json"
)

KNOWN_NODE_NAMES = frozenset(
    {
        NODE_SOURCE,
        NODE_PAGES_IMPORT,
        NODE_PAGES_LOAD,
        NODE_OCR,
        NODE_TEXT_EXTRACT,
        NODE_PROOFREAD,
        NODE_TRANSLATE,
        NODE_TOC_LOAD,
        NODE_TOC_PIPELINE,
        NODE_TOC_OUTLINE,
        NODE_CHAPTERS_LOAD,
        NODE_COMPILE,
        NODE_SEMANTIC,
        NODE_SANITIZE,
        NODE_KB,
        NODE_EPUB,
        NODE_DOCX,
        NODE_REFERENCE_PDF,
        NODE_VERIFY,
        NODE_VERIFY_WORD,
        NODE_STATUS,
    }
)
PUBLISHER_ARTIFACTS = {
    NODE_KB: ART_KB,
    NODE_EPUB: ART_EPUB,
    NODE_DOCX: ART_DOCX,
    NODE_REFERENCE_PDF: ART_REFERENCE_PDF,
}


class BookGraphConfigurationError(ValueError):
    """Raised before execution when graph and legacy options conflict."""


class LegacyStageError(RuntimeError):
    """Raised when a wrapped ``book_pipeline`` phase returns a failure code."""

    def __init__(self, phase: str, exit_code: int) -> None:
        super().__init__(f"book_pipeline phase {phase!r} exited with {exit_code}")
        self.phase = phase
        self.exit_code = exit_code


class SemanticReconstructionError(ValueError):
    """Raised when chapter semantics are unsafe for reader publication."""


class SourceBindingError(RuntimeError):
    """Raised when one output directory is reused for different PDF bytes."""


@dataclass(frozen=True)
class BookGraphOptions:
    """Non-secret graph controls layered over the existing pipeline argv."""

    include_proofread: bool = False
    toc_source: str = "pipeline"
    disabled_nodes: frozenset[str] = field(default_factory=frozenset)
    target_artifacts: frozenset[str] = field(default_factory=frozenset)
    force_nodes: frozenset[str] = field(default_factory=frozenset)
    force_all: bool = False
    adopt_existing_output: bool = False
    source_mode: str = "scanned-pdf"
    text_pdf_sort: bool = False
    text_pdf_reflow: bool = False
    text_pdf_strip_leading_page_number_offset: int | None = None

    def __post_init__(self) -> None:
        if self.source_mode not in {"scanned-pdf", "text-pdf"}:
            raise BookGraphConfigurationError(
                "source_mode must be 'scanned-pdf' or 'text-pdf'"
            )
        if self.source_mode != "text-pdf" and (
            self.text_pdf_sort
            or self.text_pdf_reflow
            or self.text_pdf_strip_leading_page_number_offset is not None
        ):
            raise BookGraphConfigurationError(
                "text_pdf_* options require source_mode='text-pdf'"
            )
        if self.toc_source not in {"pipeline", "outline"}:
            raise BookGraphConfigurationError(
                "toc_source must be 'pipeline' or 'outline'"
            )
        unknown = set(self.disabled_nodes) - KNOWN_NODE_NAMES
        if unknown:
            raise BookGraphConfigurationError(
                f"unknown disabled graph nodes: {sorted(unknown)}"
            )
        if self.text_pdf_strip_leading_page_number_offset is not None and (
            self.text_pdf_strip_leading_page_number_offset < 0
        ):
            raise BookGraphConfigurationError(
                "text_pdf_strip_leading_page_number_offset must be non-negative"
            )


@dataclass(frozen=True)
class PreparedBookGraph:
    """A mutable node registry paired with its context and selected outputs."""

    graph: PipelineGraph
    context: GraphContext
    targets: frozenset[str]
    options: BookGraphOptions
    require_existing_output: bool = False

    def plan(self) -> tuple[NodeSpec, ...]:
        return self.graph.plan(
            available=self.context.values,
            targets=self.targets,
        )

    def execute(self) -> GraphRunResult:
        if self.require_existing_output and not self.context.output_dir.is_dir():
            raise FileNotFoundError(
                f"Output directory does not exist: {self.context.output_dir}"
            )
        _assert_control_inputs_unchanged(self.context)
        force: bool | Iterable[str]
        if self.options.force_all:
            force = True
        else:
            force = self.options.force_nodes
        return GraphExecutor(self.graph).execute(
            self.context,
            targets=self.targets,
            force=force,
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _file_artifact(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": str(path.resolve()),
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _pages_artifact(output_dir: Path) -> dict[str, Any]:
    paths = sorted((output_dir / "pages").glob("page_*.json"))
    if not paths:
        raise FileNotFoundError(
            f"No page checkpoints found under {output_dir / 'pages'}"
        )
    return {
        "path": str((output_dir / "pages").resolve()),
        "sha256": _files_digest(paths),
        "count": len(paths),
    }


def _import_source_artifact(path: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    extracted = path / "extracted_pages.json"
    if extracted.is_file():
        candidates.append(extracted)
    # Hash every possible legacy source.  import_existing_ocr may fall back
    # when extracted_pages.json exists but decodes to an empty list.
    candidates.extend(sorted((path / "_checkpoints").glob("page_*.json")))
    candidates.extend(sorted((path / "pages").glob("page_*.json")))
    if not candidates:
        raise FileNotFoundError(
            "No extracted_pages.json, _checkpoints/page_*.json, or "
            f"pages/page_*.json found under {path}"
        )
    return {
        "path": str(path.resolve()),
        "sha256": _files_digest(candidates),
        "count": len(candidates),
        "page_numbers": _import_candidate_page_numbers(path),
    }


def _import_candidate_page_numbers(path: Path) -> list[int]:
    """Mirror the legacy importer's source priority without copying content."""

    candidates: list[dict[str, Any]] = []
    extracted = path / "extracted_pages.json"
    if extracted.is_file():
        value = json.loads(extracted.read_text(encoding="utf-8"))
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    if not candidates:
        for checkpoint in sorted((path / "_checkpoints").glob("page_*.json")):
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
    if not candidates:
        for checkpoint in sorted((path / "pages").glob("page_*.json")):
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
    page_numbers: set[int] = set()
    for item in candidates:
        page_number = item.get("pdf_page", item.get("page_number"))
        text = str(
            item.get("text")
            or item.get("ocr_text")
            or item.get("embedded_text")
            or ""
        ).strip()
        if isinstance(page_number, int) and page_number >= 1 and text:
            page_numbers.add(page_number)
    return sorted(page_numbers)


def _chapters_artifact(
    output_dir: Path,
    *,
    chapter_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    resolved_manifest_path = manifest_path or (output_dir / "chapters.json")
    manifest = json.loads(resolved_manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError(
            f"Invalid or empty chapter manifest: {resolved_manifest_path}"
        )
    resolved_chapter_dir = chapter_dir or (output_dir / "chapters")
    filenames = _validated_chapter_filenames(manifest, resolved_manifest_path)
    paths = [resolved_manifest_path]
    for filename in filenames:
        chapter_path = _chapter_path(resolved_chapter_dir, filename)
        if not chapter_path.is_file():
            raise FileNotFoundError(chapter_path)
        paths.append(chapter_path)
    return {
        "manifest": str(resolved_manifest_path.resolve()),
        "chapter_dir": str(resolved_chapter_dir.resolve()),
        "sha256": _files_digest(paths),
        "count": len(manifest),
    }


def _validated_chapter_filenames(
    manifest: list[Any],
    manifest_path: Path,
) -> list[str]:
    """Return safe, unique, single-file chapter names from a manifest."""

    filenames: list[str] = []
    seen: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError(f"Invalid chapter manifest entry in {manifest_path}")
        filename = item["filename"]
        if (
            not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or Path(filename).is_absolute()
            or Path(filename).name != filename
        ):
            raise ValueError(
                f"Unsafe chapter filename {filename!r} in {manifest_path}"
            )
        if filename in seen:
            raise ValueError(
                f"Duplicate chapter filename {filename!r} in {manifest_path}"
            )
        seen.add(filename)
        filenames.append(filename)
    return filenames


def _chapter_path(chapter_dir: Path, filename: str) -> Path:
    root = chapter_dir.expanduser().resolve()
    candidate = (root / filename).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Chapter path escapes declared directory: {filename!r}"
        ) from exc
    return candidate


def _require_output_directory(output_dir: Path, path: Path) -> Path:
    root = output_dir.expanduser().resolve()
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Publication directory escapes output root: {resolved}") from exc
    return resolved


def _draft_chapters_artifact(output_dir: Path) -> dict[str, Any]:
    draft_dir = _require_output_directory(
        output_dir,
        output_dir / ".pipeline_graph" / "chapter_drafts",
    )
    return _chapters_artifact(
        output_dir,
        chapter_dir=draft_dir,
        manifest_path=draft_dir / "chapters.json",
    )


def _snapshot_chapter_drafts(output_dir: Path) -> None:
    """Create the immutable compile→sanitize boundary for graph execution."""

    manifest = json.loads(
        (output_dir / "chapters.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest, list):
        raise ValueError("chapters.json root must be a list")
    source_dir = _require_output_directory(output_dir, output_dir / "chapters")
    draft_dir = _require_output_directory(
        output_dir,
        output_dir / ".pipeline_graph" / "chapter_drafts",
    )
    draft_dir.mkdir(parents=True, exist_ok=True)
    filenames = _validated_chapter_filenames(
        manifest,
        output_dir / "chapters.json",
    )
    expected: set[str] = set()
    for filename in filenames:
        expected.add(filename)
        source = _chapter_path(source_dir, filename)
        if not source.is_file():
            raise FileNotFoundError(source)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}.",
            suffix=".tmp",
            dir=draft_dir,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, draft_dir / filename)
        finally:
            temporary.unlink(missing_ok=True)
    for stale in draft_dir.glob("*.md"):
        if stale.name not in expected:
            stale.unlink()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".chapters.json.",
        suffix=".tmp",
        dir=draft_dir,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(output_dir / "chapters.json", temporary)
        os.replace(temporary, draft_dir / "chapters.json")
    finally:
        temporary.unlink(missing_ok=True)


def _artifact_is_current(
    builder: Any,
) -> Any:
    def validate(context: GraphContext, outputs: Mapping[str, Any]) -> bool:
        try:
            current = builder(context.output_dir)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if len(outputs) != 1:
            return False
        saved = next(iter(outputs.values()))
        return isinstance(saved, dict) and saved == current

    return validate


def _compile_inputs_are_current(page_artifact: str) -> Any:
    """Validate immutable drafts and the canonical legacy input slots."""

    draft_validator = _artifact_is_current(_draft_chapters_artifact)

    def validate(context: GraphContext, outputs: Mapping[str, Any]) -> bool:
        if not draft_validator(context, outputs):
            return False
        pages = context.require(page_artifact)
        if not isinstance(pages, dict) or not pages.get("path"):
            return False
        source_dir = Path(str(pages["path"])).expanduser().resolve()
        source_paths = sorted(source_dir.glob("page_*.json"))
        canonical_paths = sorted((context.output_dir / "pages").glob("page_*.json"))
        if not source_paths or not canonical_paths:
            return False
        source_digest = _files_digest(source_paths)
        if pages.get("sha256") and pages.get("sha256") != source_digest:
            return False
        if _files_digest(canonical_paths) != source_digest:
            return False
        toc = context.require(ART_TOC)
        try:
            toc_source = _validated_file_from_artifact(toc, name=ART_TOC)
        except (OSError, ValueError):
            return False
        canonical_toc = context.output_dir / "toc.json"
        return (
            canonical_toc.is_file()
            and _sha256_file(canonical_toc) == _sha256_file(toc_source)
        )

    return validate


def _single_file_is_current(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    if len(outputs) != 1:
        return False
    saved = next(iter(outputs.values()))
    if not isinstance(saved, dict) or not saved.get("path"):
        return False
    path = Path(str(saved["path"]))
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(context.output_dir)
    except (OSError, ValueError):
        return False
    return (
        resolved.is_file()
        and str(resolved) == str(path)
        and saved.get("sha256") == _sha256_file(resolved)
    )


def _knowledge_base_is_current(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    """Require both canonical JSONL and its RAG discovery/index metadata."""

    if not _single_file_is_current(context, outputs):
        return False
    saved = next(iter(outputs.values()))
    path = Path(str(saved["path"]))
    if not rag_knowledge_base.rag_manifest_is_current(path):
        return False
    args = _parsed_args(context)
    if not rag_knowledge_base.zhipu_embedding_enabled(
        getattr(args, "rag_embed", None)
    ):
        return True
    try:
        manifest = rag_knowledge_base.read_rag_manifest(path)
    except rag_knowledge_base.RagError:
        return False
    embedding = manifest["retrieval"]["embedding"]
    return (
        embedding["status"] == "ready"
        and embedding["provider"] == "zhipu"
        and embedding["model"] == os.getenv(
            "ZHIPU_EMBEDDING_MODEL",
            "embedding-3",
        )
        and embedding["dimensions"]
        == int(os.getenv("ZHIPU_EMBEDDING_DIMENSIONS", "2048"))
    )


def _import_source_is_current(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    saved = outputs.get(ART_PAGES_IMPORTED)
    if not isinstance(saved, dict) or not saved.get("path"):
        return False
    args = _parsed_args(context)
    if not args.import_ocr_dir:
        return False
    expected_source = Path(args.import_ocr_dir).expanduser().resolve()
    if str(expected_source) != str(saved.get("path")):
        return False
    try:
        current = _import_source_artifact(expected_source)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if current.get("sha256") != saved.get("sha256"):
        return False
    page_numbers = saved.get("page_numbers")
    if not isinstance(page_numbers, list) or page_numbers != current.get("page_numbers"):
        return False
    management_path = (
        context.output_dir / ".pipeline_graph" / "import_identity.json"
    )
    try:
        management = json.loads(management_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    managed_pages = management.get("managed_pages") if isinstance(management, dict) else None
    if (
        not isinstance(managed_pages, dict)
        or management.get("source_sha256") != current.get("sha256")
        or {int(key) for key in managed_pages if str(key).isdigit()}
        != set(page_numbers)
    ):
        return False
    for page_number in page_numbers:
        if not isinstance(page_number, int) or page_number < 1:
            return False
        destination = (
            context.output_dir / "pages" / f"page_{page_number:04d}.json"
        )
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (
            not isinstance(payload, dict)
            or payload.get("pdf_page") != page_number
            or not str(payload.get("text") or "").strip()
        ):
            return False
    return True


def _pipeline_args(context: GraphContext) -> list[str]:
    value = context.require("pipeline.argv")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BookGraphConfigurationError("pipeline.argv must be a list of strings")
    return list(value)


def _assert_control_inputs_unchanged(context: GraphContext) -> None:
    """Reject a reused Prepared graph after its config file has changed."""

    argv = _pipeline_args(context)
    expected = context.fingerprints.get("pipeline.argv")
    current = stable_fingerprint(_sanitized_control_argv(argv))
    if expected is not None and current != expected:
        raise BookGraphConfigurationError(
            "Pipeline arguments or configuration content changed after the graph "
            "was prepared; prepare a new graph before executing it."
        )


def _sanitized_control_argv(argv: Iterable[str]) -> list[str]:
    """Return cache-safe CLI control data for external node dependencies."""

    secrets = {
        "--api-key",
        "--ocr-api-key",
        "--translation-api-key",
        "--api-key-env",
        "--ocr-api-key-env",
        "--translation-api-key-env",
    }
    operational_values = {
        "--concurrency",
        "--ocr-concurrency",
        "--proofread-concurrency",
        "--translation-concurrency",
        "--api-timeout",
        "--translation-api-timeout",
        "--ocr-delay",
        "--proofread-delay",
        "--translation-delay",
    }
    endpoints = {"--api-base", "--ocr-api-base", "--translation-api-base"}
    values = list(argv)
    output: list[str] = []
    index = 0
    while index < len(values):
        item = values[index]
        option, separator, inline_value = item.partition("=")
        if option in secrets or option in operational_values:
            # Credentials and pure throughput tuning do not change content.
            # Omit both option and value so adding a tuning override is as
            # cache-neutral as leaving its default in place.
            if not separator and index + 1 < len(values):
                index += 1
        elif option in endpoints:
            if separator:
                output.append(f"{option}={_safe_endpoint(inline_value)}")
            else:
                output.append(option)
                if index + 1 < len(values):
                    output.append(_safe_endpoint(values[index + 1]))
                    index += 1
        elif option == "--config":
            if separator:
                config_value = inline_value
            elif index + 1 < len(values):
                config_value = values[index + 1]
                index += 1
            else:
                config_value = ""
            config_path = Path(config_value).expanduser().resolve()
            config_digest = (
                _sha256_file(config_path) if config_path.is_file() else "missing"
            )
            output.extend(["--config", f"{config_path}#sha256={config_digest}"])
        else:
            output.append(item)
        index += 1
    return output


def _parsed_args(context: GraphContext) -> Any:
    _assert_control_inputs_unchanged(context)
    return legacy.build_parser().parse_args(_pipeline_args(context))


def _selected_model_profiles(args: Any) -> dict[str, Any]:
    if not args.config:
        return {stage: None for stage in ("ocr", "toc", "proofread", "translation")}
    profiles = load_pipeline_profiles(args.config)
    return {
        "ocr": profiles.for_stage("ocr", args.ocr_profile),
        "toc": profiles.for_stage("toc", args.toc_profile),
        "proofread": profiles.for_stage("proofread", args.proofread_profile),
        "translation": profiles.for_stage(
            "translation", args.translation_profile
        ),
    }


def _profile_model_semantics(profile: Any) -> dict[str, Any] | None:
    """Return cache-relevant model settings, never credentials or tuning."""

    if profile is None:
        return None
    return {
        "adapter": profile.adapter,
        "provider": profile.provider,
        "base_url": _safe_endpoint(profile.base_url),
        "model": profile.model,
        "thinking": profile.thinking,
        # A command can select a different adapter implementation.  Hash it so
        # command contents (which should never contain credentials) are not
        # copied into graph metadata.
        "command_sha256": stable_fingerprint(list(profile.command)),
        "reading_direction": profile.reading_direction,
    }


def _safe_endpoint(value: Any) -> str:
    """Return an endpoint identity with credentials and signed queries removed."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname
        if not parsed.scheme or hostname is None:
            raise ValueError("not an absolute endpoint")
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = parsed.port
        netloc = f"{host}:{port}" if port is not None else host
        path_identity = (
            f"/path-sha256:{stable_fingerprint(parsed.path.rstrip('/'))}"
            if parsed.path and parsed.path != "/"
            else ""
        )
        return f"{parsed.scheme.lower()}://{netloc}{path_identity}"
    except (TypeError, ValueError):
        # An opaque/invalid endpoint must never be serialized verbatim because
        # it may contain an embedded bearer token.
        return f"opaque-sha256:{stable_fingerprint(raw)}"


def _ocr_segmentation_semantics() -> dict[str, Any]:
    split_value = os.getenv("CODING_PLAN_SPLIT_SPREADS", "1").strip().lower()
    return {
        "split_spreads": split_value not in {"0", "false", "no", "off"},
        "vertical_page_rows": max(
            1,
            int(os.getenv("CODING_PLAN_VERTICAL_PAGE_ROWS", "1")),
        ),
        "vertical_page_columns": max(
            1,
            int(os.getenv("CODING_PLAN_VERTICAL_PAGE_COLUMNS", "1")),
        ),
        "minimum_ocr_chars": max(
            0,
            int(os.getenv("CODING_PLAN_MIN_OCR_CHARS", "0")),
        ),
        "spread_segments": max(
            2,
            int(os.getenv("CODING_PLAN_SPREAD_SEGMENTS", "2")),
        ),
    }


def _ocr_stage_semantics(args: Any) -> dict[str, Any]:
    profile = _selected_model_profiles(args)["ocr"]
    reading_direction = legacy.resolve_ocr_reading_direction(args, profile)
    backend = profile.adapter if profile is not None else args.ocr_backend
    if backend == "coding-plan-mcp":
        identity: dict[str, Any] = {
            "backend": backend,
            "model": (
                profile.model
                if profile is not None
                else os.getenv("Z_AI_VISION_MODEL", "glm-4.6v")
            ),
            "reading_direction": reading_direction,
            "command_sha256": (
                stable_fingerprint(list(profile.command))
                if profile is not None and profile.command
                else stable_fingerprint(args.ocr_command)
            ),
            "prompt_version": f"{reading_direction}-v2",
            "mode": os.getenv("Z_AI_MODE", "ZHIPU").strip().upper(),
            "max_output_tokens": max(
                1,
                int(os.getenv("Z_AI_VISION_MODEL_MAX_TOKENS", "4096")),
            ),
        }
        segmentation: dict[str, Any] | None = _ocr_segmentation_semantics()
    elif backend == "glm-ocr":
        identity = {
            "backend": backend,
            "provider": profile.provider if profile is not None else "glm",
            "base_url": _safe_endpoint(
                profile.base_url
                if profile is not None and profile.base_url
                else args.ocr_api_base
            ),
            "model": profile.model if profile is not None else args.ocr_model,
        }
        segmentation = None
    else:
        identity = {
            "backend": backend,
            "language": args.tesseract_language,
            "psm": args.tesseract_psm,
        }
        segmentation = None
    return {
        "identity": identity,
        "profile": _profile_model_semantics(profile),
        "segmentation": segmentation,
        "dpi": args.dpi,
        "max_image_side": args.max_image_side,
        "jpeg_quality": args.jpeg_quality,
    }


def _proofread_stage_semantics(args: Any) -> dict[str, Any]:
    selected = _selected_model_profiles(args)
    toc_base = legacy.resolve_toc_api_base(args, toc_profile=selected["toc"])
    identity = legacy.resolve_proofread_identity(
        args,
        glm_api_base=toc_base,
        profile=selected["proofread"],
    )
    return {
        "identity": identity.fingerprint,
        "profile": _profile_model_semantics(selected["proofread"]),
        "language": args.proofread_language,
        "max_chars": args.proofread_max_chars,
    }


def _translation_stage_semantics(args: Any) -> dict[str, Any]:
    selected = _selected_model_profiles(args)
    identity = legacy.resolve_expected_translation_identity(
        args,
        toc_profile=selected["toc"],
        translation_profile=selected["translation"],
    )
    return {
        "identity": identity.fingerprint,
        "profile": _profile_model_semantics(selected["translation"]),
        "source_language": args.translation_source_language,
        "target_language": args.target_language,
        "max_chars": args.translation_max_chars,
    }


def _toc_stage_semantics(args: Any) -> dict[str, Any]:
    selected = _selected_model_profiles(args)
    profile = selected["toc"]
    toc_base = legacy.resolve_toc_api_base(args, toc_profile=profile)
    toc_json = Path(args.toc_json).expanduser().resolve() if args.toc_json else None
    return {
        "mode": "manual" if toc_json is not None else "model",
        "manual_sha256": (
            _sha256_file(toc_json)
            if toc_json is not None and toc_json.is_file()
            else None
        ),
        "profile": _profile_model_semantics(profile),
        "api_mode": args.api_mode,
        "base_url": _safe_endpoint(toc_base),
        "model": profile.model if profile is not None else args.text_model,
        "thinking": profile.thinking if profile is not None else "disabled",
        "prompt_version": "book-toc-v1",
        "front_matter_pages": args.front_matter_pages,
        "toc_pages": args.toc_pages,
        "page_offset": args.page_offset,
        "printed_pages_per_pdf_page": args.printed_pages_per_pdf_page,
    }


def _remove_option(
    argv: list[str],
    option: str,
    *,
    takes_value: bool = False,
) -> list[str]:
    output: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == option:
            index += 2 if takes_value else 1
            continue
        if item.startswith(f"{option}="):
            index += 1
            continue
        output.append(item)
        index += 1
    return output


def _phase_argv(
    context: GraphContext,
    phase: str,
    *,
    add: Iterable[str] = (),
    remove: Iterable[tuple[str, bool]] = (),
) -> list[str]:
    argv = _remove_option(_pipeline_args(context), "--phase", takes_value=True)
    # Import is a first-class graph node.  Replaying it in every thin wrapper
    # can overwrite fresh proofread/translation overlays between stages.
    argv = _remove_option(argv, "--import-ocr-dir", takes_value=True)
    for option, takes_value in remove:
        argv = _remove_option(argv, option, takes_value=takes_value)
    argv.extend(["--phase", phase])
    # ``add`` is an argv fragment, not a set of independent tokens.  A value
    # may legitimately equal another option's existing value (for example
    # ``--ocr-model glm-ocr --ocr-cache-model glm-ocr``).
    argv.extend(add)
    return argv


def _run_phase(
    context: GraphContext,
    phase: str,
    *,
    add: Iterable[str] = (),
    remove: Iterable[tuple[str, bool]] = (),
) -> None:
    try:
        exit_code = _call_legacy_main(
            _phase_argv(context, phase, add=add, remove=remove)
        )
    except SystemExit as exc:
        try:
            exit_code = int(exc.code)
        except (TypeError, ValueError):
            exit_code = 1
    if exit_code:
        raise LegacyStageError(phase, exit_code)


def _call_legacy_main(argv: list[str]) -> int:
    # GraphExecutor already owns the same output-directory lock.  Calling the
    # private unlocked implementation avoids a nested lock without using a
    # process-global environment bypass (which would be unsafe across threads).
    return legacy._main_unlocked(argv)


def _source_fingerprint(context: GraphContext) -> dict[str, Any]:
    args = _parsed_args(context)
    path = Path(args.input).expanduser().resolve() if args.input else None
    source_mode = _source_mode(context)
    return {
        "adapter": GRAPH_ADAPTER_VERSION,
        "source_adapter": _source_adapter(source_mode),
        "source_mode": source_mode,
        "path": str(path) if path is not None else None,
        "source_sha256": _sha256_file(path) if path and path.is_file() else None,
    }


def _reviewed_fingerprint(context: GraphContext) -> dict[str, Any]:
    args = _parsed_args(context)
    reviewed = context.output_dir / "reviewed_chapters"
    return {
        "adapter": GRAPH_ADAPTER_VERSION,
        "compiler": "chapters-v3",
        "reviewed_sha256": _files_digest(reviewed.glob("*.md"))
        if reviewed.is_dir()
        else None,
        "publication": {
            "title": _book_title(context),
            "target_language": args.target_language,
        },
        "compile": {
            "granularity": args.granularity,
            "page_offset": args.page_offset,
            "printed_pages_per_pdf_page": args.printed_pages_per_pdf_page,
            "mapping_contract": "toc-remap-v1",
            "require_complete_ocr": args.require_complete_ocr,
            "required_ocr_model_prefix": args.required_ocr_model_prefix,
            "require_translation": args.require_translation,
        },
        "translation": _translation_stage_semantics(args),
        "prompt_versions": {
            "translation": legacy.TRANSLATION_PROMPT_VERSION,
            "proofread": legacy.PROOFREAD_PROMPT_VERSION,
        },
    }


def _toc_fingerprint(context: GraphContext) -> dict[str, Any]:
    return {
        "adapter": GRAPH_ADAPTER_VERSION,
        "toc": _toc_stage_semantics(_parsed_args(context)),
    }


def _sanitize_fingerprint(context: GraphContext) -> dict[str, Any]:
    args = _parsed_args(context)
    return {
        "adapter": GRAPH_ADAPTER_VERSION,
        "sanitizer": "publication-metadata-v3",
        "title": _book_title(context),
    }


def _publisher_fingerprint(kind: str) -> Any:
    def fingerprint(context: GraphContext) -> dict[str, Any]:
        args = _parsed_args(context)
        value: dict[str, Any] = {
            "adapter": GRAPH_ADAPTER_VERSION,
            "publisher": (
                f"{kind}-v3"
                if kind in {"docx", "knowledge-base"}
                else f"{kind}-v2"
            ),
            "title": _book_title(context),
        }
        if kind == "docx":
            value["author"] = args.author
        if kind == "epub":
            value["language"] = args.target_language
        if kind == "knowledge-base":
            source = context.require(ART_SOURCE)
            value["source_filename"] = Path(str(source["path"])).name
            value["rag_embedding_enabled"] = (
                rag_knowledge_base.zhipu_embedding_enabled(
                    getattr(args, "rag_embed", None)
                )
            )
            value["rag_embedding_model"] = os.getenv(
                "ZHIPU_EMBEDDING_MODEL",
                "embedding-3",
            )
            value["rag_embedding_dimensions"] = os.getenv(
                "ZHIPU_EMBEDDING_DIMENSIONS",
                "2048",
            )
        return value

    return fingerprint


def _source_mode(context: GraphContext) -> str:
    source_mode = str(context.config.get("source_mode") or "")
    if source_mode not in {"scanned-pdf", "text-pdf"}:
        raise SourceBindingError(
            f"Invalid source mode in Graph context: {source_mode!r}"
        )
    return source_mode


def _source_adapter(source_mode: str) -> str:
    return NODE_TEXT_EXTRACT if source_mode == "text-pdf" else NODE_OCR


def _infer_legacy_source_mode(output_dir: Path) -> str:
    """Infer only identities that old checkpoints can prove without guessing."""

    saw_text_layer = False
    saw_other = False
    for page_path in sorted((output_dir / "pages").glob("page_*.json")):
        try:
            payload = json.loads(page_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        model = payload.get("ocr_model") if isinstance(payload, dict) else None
        if isinstance(model, str) and model.startswith(
            extract_textbook_layer.TEXT_LAYER_MODEL
        ):
            saw_text_layer = True
        else:
            # Text-layer importers have always written their exact identity.
            # Missing or older model fields therefore belong to the historical
            # scanned-PDF lane, unless mixed with explicit text-layer pages.
            saw_other = True
    if saw_text_layer and saw_other:
        raise SourceBindingError(
            "Legacy source binding has mixed text-layer and OCR/unknown page "
            "identities; refusing to guess a source mode. Use a new output directory."
        )
    # Version-1 bindings predate the text-PDF graph adapter.  With no page
    # identity proving a later text-layer run, retain their historical
    # scanned-PDF identity instead of adopting the request being validated.
    return "text-pdf" if saw_text_layer else "scanned-pdf"


def _source_binding_payload(
    *,
    path: Path,
    source_sha: str,
    page_count: int,
    source_mode: str,
    adopted_existing_output: bool,
    migrated_legacy_binding: bool = False,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "path": str(path),
        "sha256": source_sha,
        "page_count": page_count,
        "adapter": _source_adapter(source_mode),
        "source_mode": source_mode,
        "adopted_existing_output": adopted_existing_output,
        "migrated_legacy_binding": migrated_legacy_binding,
    }


def _validate_and_bind_source_artifact(
    context: GraphContext,
    artifact: Mapping[str, Any],
) -> Path:
    """Enforce source hash binding even when a plugin provides ``source.pdf``."""

    path = _validated_file_from_artifact(artifact, name=ART_SOURCE)
    if path.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"Input must be a PDF: {path}")
    with fitz.open(path) as document:
        page_count = document.page_count
    declared_count = artifact.get("page_count")
    if declared_count is not None and int(declared_count) != page_count:
        raise SourceBindingError(
            f"source.pdf page_count mismatch: declared={declared_count}, actual={page_count}"
        )
    source_sha = _sha256_file(path)
    source_mode = _source_mode(context)
    source_adapter = _source_adapter(source_mode)
    binding_path = context.output_dir / ".pipeline_graph" / "source.json"
    if binding_path.exists():
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceBindingError(
                f"Cannot read source binding {binding_path}: {exc}"
            ) from exc
        bound_sha = binding.get("sha256") if isinstance(binding, dict) else None
        if bound_sha != source_sha:
            raise SourceBindingError(
                "Output directory is already bound to a different source PDF. "
                f"Use a new output directory instead of reusing {context.output_dir}."
            )
        schema_version = (
            binding.get("schema_version") if isinstance(binding, dict) else None
        )
        bound_mode = (
            binding.get("source_mode") if isinstance(binding, dict) else None
        )
        bound_adapter = (
            binding.get("adapter") if isinstance(binding, dict) else None
        )
        migrated = False
        if schema_version == 1 and bound_mode is None and bound_adapter is None:
            bound_mode = _infer_legacy_source_mode(context.output_dir)
            bound_adapter = _source_adapter(bound_mode)
            migrated = True
        elif schema_version != 2:
            raise SourceBindingError(
                "Unsupported or incomplete source binding identity; use a new "
                f"output directory instead of reusing {context.output_dir}."
            )
        if bound_mode != source_mode or bound_adapter != source_adapter:
            raise SourceBindingError(
                "Output directory is already bound to a different source adapter: "
                f"bound={bound_mode!r}/{bound_adapter!r}, "
                f"requested={source_mode!r}/{source_adapter!r}. "
                "Use a new output directory; source modes cannot share checkpoints."
            )
        if migrated:
            legacy.write_json(
                binding_path,
                _source_binding_payload(
                    path=path,
                    source_sha=source_sha,
                    page_count=page_count,
                    source_mode=source_mode,
                    adopted_existing_output=bool(
                        binding.get("adopted_existing_output")
                    ),
                    migrated_legacy_binding=True,
                ),
            )
        return path

    existing_pages = sorted((context.output_dir / "pages").glob("page_*.json"))
    existing_pipeline_outputs = bool(existing_pages) or any(
        (context.output_dir / name).exists()
        for name in ("toc.json", "chapters.json")
    )
    if existing_pipeline_outputs:
        if not bool(context.config.get("adopt_existing_output")):
            raise SourceBindingError(
                "This output directory contains legacy checkpoints but has no "
                "source hash binding. Refusing to guess their PDF. Re-run with "
                "--adopt-existing-output only after confirming the source, or "
                "use a new output directory."
            )
        page_numbers: list[int] = []
        for page_path in existing_pages:
            try:
                page_numbers.append(int(page_path.stem.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                raise SourceBindingError(
                    f"Cannot adopt malformed page checkpoint name: {page_path}"
                ) from None
        page_numbers.sort()
        if page_numbers != list(range(1, page_count + 1)):
            raise SourceBindingError(
                "Adopting an unbound legacy output requires an exact 1..N "
                f"checkpoint set for this {page_count}-page PDF; found "
                f"{len(page_numbers)} page files."
            )
        print(
            "[graph-warning] adopting existing complete checkpoints for "
            f"source_sha256={source_sha}",
            flush=True,
        )
    legacy.write_json(
        binding_path,
        _source_binding_payload(
            path=path,
            source_sha=source_sha,
            page_count=page_count,
            source_mode=source_mode,
            adopted_existing_output=existing_pipeline_outputs,
        ),
    )
    return path


def _source_handler(context: GraphContext) -> NodeResult:
    args = _parsed_args(context)
    if not args.input:
        raise BookGraphConfigurationError(
            f"graph phase {args.phase!r} requires an input PDF"
        )
    path = Path(args.input).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"Input must be an existing PDF: {path}")
    with fitz.open(path) as document:
        page_count = document.page_count
    artifact = {
        **_file_artifact(path),
        "page_count": page_count,
        "adapter": _source_adapter(_source_mode(context)),
        "source_mode": _source_mode(context),
    }
    _validate_and_bind_source_artifact(context, artifact)
    return NodeResult(
        outputs={ART_SOURCE: artifact},
        fingerprints={ART_SOURCE: str(artifact["sha256"])},
    )


def _import_pages_handler(context: GraphContext) -> NodeResult:
    args = _parsed_args(context)
    if not args.import_ocr_dir:
        raise BookGraphConfigurationError(
            "pages.import requires --import-ocr-dir"
        )
    source_dir = Path(args.import_ocr_dir).expanduser().resolve()
    artifact = _import_source_artifact(source_dir)
    management_path = (
        context.output_dir / ".pipeline_graph" / "import_identity.json"
    )
    try:
        previous = json.loads(management_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    previous_managed = (
        previous.get("managed_pages") if isinstance(previous, dict) else {}
    )
    if not isinstance(previous_managed, dict):
        previous_managed = {}
    imported = legacy.import_existing_ocr(source_dir, context.output_dir)
    expected_pages = set(artifact["page_numbers"])
    removed_pages: list[int] = []
    for raw_page, imported_sha in previous_managed.items():
        try:
            page_number = int(raw_page)
        except (TypeError, ValueError):
            continue
        if page_number in expected_pages or not imported_sha:
            continue
        destination = (
            context.output_dir / "pages" / f"page_{page_number:04d}.json"
        )
        # Delete only a page that this import node previously wrote and that
        # no later OCR/proofread/translation/manual operation has changed.
        if destination.is_file() and _sha256_file(destination) == imported_sha:
            destination.unlink()
            destination.with_suffix(".md").unlink(missing_ok=True)
            removed_pages.append(page_number)
    managed_pages: dict[str, str] = {}
    for page_number in sorted(expected_pages):
        destination = (
            context.output_dir / "pages" / f"page_{page_number:04d}.json"
        )
        if destination.is_file():
            managed_pages[str(page_number)] = _sha256_file(destination)
    legacy.write_json(
        management_path,
        {
            "schema_version": 1,
            "source_path": str(source_dir),
            "source_sha256": artifact["sha256"],
            "managed_pages": managed_pages,
        },
    )
    return NodeResult(
        outputs={ART_PAGES_IMPORTED: artifact},
        fingerprints={ART_PAGES_IMPORTED: str(artifact["sha256"])},
        metadata={
            "imported_pages": imported,
            "removed_stale_pages": sorted(removed_pages),
        },
    )


def _load_pages_handler(context: GraphContext) -> NodeResult:
    artifact = _pages_artifact(context.output_dir)
    return NodeResult(
        outputs={ART_PAGES_RAW: artifact},
        fingerprints={ART_PAGES_RAW: str(artifact["sha256"])},
    )


def _effective_ocr_cache_model(args: Any) -> str | None:
    """Resolve the exact OCR identity that legacy checkpoint reuse must match."""

    profile = _selected_model_profiles(args)["ocr"]
    return legacy.resolve_expected_ocr_model_exact(args, profile)


def _ocr_page_handler(context: GraphContext) -> NodeResult:
    _require_source_argument(context)
    args = _parsed_args(context)
    exact_model = _effective_ocr_cache_model(args)
    semantics = _stage_content_semantics(args, "ocr")
    selected_pages = _selected_stage_pages(context, args)
    force_for_identity = _stage_identity_changed(
        context,
        "ocr",
        semantics,
        selected_pages,
    )
    if args.skip_ocr and force_for_identity:
        raise BookGraphConfigurationError(
            "--skip-ocr cannot adopt changed OCR content settings; remove "
            "--skip-ocr so stale pages are regenerated"
        )
    if args.skip_ocr and exact_model:
        source = context.require(ART_SOURCE)
        page_count = int(source.get("page_count") or 0)
        first = max(1, int(args.start_page or 1))
        last = min(page_count, int(args.end_page or page_count))
        cached = {
            record.pdf_page: record
            for record in legacy.load_page_records(context.output_dir)
        }
        wrong = [
            (page, cached[page].ocr_model)
            for page in range(first, last + 1)
            if page in cached and cached[page].ocr_model != exact_model
        ]
        if wrong:
            raise BookGraphConfigurationError(
                "--skip-ocr found checkpoints from a different exact OCR "
                f"identity; examples={wrong[:8]!r}, expected={exact_model!r}"
            )
    add: tuple[str, ...] = ()
    if exact_model and not args.ocr_cache_model:
        add += ("--ocr-cache-model", exact_model)
    if force_for_identity:
        add += ("--force",)
    _run_phase(context, "ocr", add=add)
    _write_stage_identity(
        context,
        "ocr",
        semantics,
        selected_pages,
    )
    artifact = _pages_artifact(context.output_dir)
    return NodeResult(
        outputs={ART_PAGES_RAW: artifact},
        fingerprints={ART_PAGES_RAW: str(artifact["sha256"])},
        metadata={
            "effective_cache_model": exact_model,
            "semantic_identity_changed": force_for_identity,
        },
    )


def _text_pdf_fingerprint(options: BookGraphOptions) -> Any:
    def fingerprint(context: GraphContext) -> dict[str, Any]:
        source = context.require(ART_SOURCE)
        return {
            "adapter": GRAPH_ADAPTER_VERSION,
            "extractor": extract_textbook_layer.TEXT_LAYER_MODEL,
            "source_sha256": source.get("sha256") if isinstance(source, dict) else None,
            "sort": options.text_pdf_sort,
            "reflow": options.text_pdf_reflow,
            "strip_leading_page_number_offset": (
                options.text_pdf_strip_leading_page_number_offset
            ),
        }

    return fingerprint


def _text_pdf_handler(options: BookGraphOptions) -> Any:
    """Import a complete embedded PDF text layer without entering OCR."""

    def handler(context: GraphContext) -> NodeResult:
        source_path = _require_source_argument(context)
        args = _parsed_args(context)
        written, preserved, heading_issues = extract_textbook_layer.extract_text_layer(
            source_path,
            context.output_dir,
            force=bool(args.force),
            sort=options.text_pdf_sort,
            reflow=options.text_pdf_reflow,
            strip_leading_page_number_offset=(
                options.text_pdf_strip_leading_page_number_offset
            ),
        )
        if heading_issues:
            raise BookGraphConfigurationError(
                "text-pdf extraction produced unresolved heading hints"
            )
        artifact = _pages_artifact(context.output_dir)
        return NodeResult(
            outputs={ART_PAGES_RAW: artifact},
            fingerprints={ART_PAGES_RAW: str(artifact["sha256"])},
            metadata={
                "source_mode": "text-pdf",
                "written_pages": written,
                "preserved_pages": preserved,
                "ocr_called": False,
            },
        )

    return handler


def _stage_identity_path(context: GraphContext, stage: str) -> Path:
    return context.output_dir / ".pipeline_graph" / f"{stage}_identity.json"


def _stage_content_semantics(args: Any, stage: str) -> dict[str, Any]:
    builders = {
        "ocr": _ocr_stage_semantics,
        "proofread": _proofread_stage_semantics,
        "translation": _translation_stage_semantics,
    }
    try:
        return builders[stage](args)
    except KeyError as exc:
        raise ValueError(f"Unknown model stage: {stage}") from exc


def _stage_identity_changed(
    context: GraphContext,
    stage: str,
    semantics: Any,
    page_numbers: Iterable[int],
) -> bool:
    path = _stage_identity_path(context, stage)
    if not path.exists():
        # Migration path: legacy PageRecord identities still validate the
        # selected model.  Unknown render/chunk settings are explicitly
        # adopted once, then every later semantic change is enforced.
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    expected = stable_fingerprint(semantics)
    if payload.get("schema_version") == 1:
        return payload.get("fingerprint") != expected
    saved_pages = payload.get("page_fingerprints")
    if not isinstance(saved_pages, dict):
        return True
    return any(
        saved_pages.get(str(int(page))) != expected
        for page in page_numbers
    )


def _write_stage_identity(
    context: GraphContext,
    stage: str,
    semantics: Any,
    page_numbers: Iterable[int],
) -> None:
    path = _stage_identity_path(context, stage)
    current_fingerprint = stable_fingerprint(semantics)
    page_fingerprints: dict[str, str] = {}
    semantic_definitions: dict[str, Any] = {}
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    if isinstance(previous, dict):
        if previous.get("schema_version") == 2:
            saved_pages = previous.get("page_fingerprints")
            saved_definitions = previous.get("semantic_definitions")
            if isinstance(saved_pages, dict):
                page_fingerprints.update(
                    {
                        str(key): str(value)
                        for key, value in saved_pages.items()
                        if str(key).isdigit() and str(value)
                    }
                )
            if isinstance(saved_definitions, dict):
                semantic_definitions.update(saved_definitions)
        elif previous.get("schema_version") == 1 and previous.get("fingerprint"):
            # A v1 sidecar asserted one identity for every then-cached page.
            old_fingerprint = str(previous["fingerprint"])
            page_fingerprints.update(
                {
                    str(record.pdf_page): old_fingerprint
                    for record in legacy.load_page_records(context.output_dir)
                }
            )
            if "semantics" in previous:
                semantic_definitions[old_fingerprint] = previous["semantics"]
    for page in page_numbers:
        page_fingerprints[str(int(page))] = current_fingerprint
    semantic_definitions[current_fingerprint] = semantics
    legacy.write_json(
        path,
        {
            "schema_version": 2,
            "stage": stage,
            "page_fingerprints": dict(
                sorted(page_fingerprints.items(), key=lambda item: int(item[0]))
            ),
            "semantic_definitions": semantic_definitions,
        },
    )


def _selected_stage_pages(context: GraphContext, args: Any) -> list[int]:
    declared_inputs = set(context.values)
    if ART_SOURCE in declared_inputs:
        source = context.require(ART_SOURCE)
        page_count = int(source.get("page_count") or 0)
        if page_count <= 0 and source.get("path"):
            with fitz.open(Path(str(source["path"]))) as document:
                page_count = document.page_count
        candidates = list(range(1, page_count + 1))
    else:
        candidates = sorted(
            record.pdf_page
            for record in legacy.load_page_records(context.output_dir)
        )
    first = max(1, int(args.start_page or 1))
    last = int(args.end_page) if args.end_page is not None else None
    return [
        page
        for page in candidates
        if page >= first and (last is None or page <= last)
    ]


def semantic_status_for_args(output_dir: Path | str, args: Any) -> dict[str, Any]:
    """Report page-scoped Graph content-setting freshness for status/UI calls."""

    resolved_output = Path(output_dir).expanduser().resolve()
    available_pages = sorted(
        record.pdf_page for record in legacy.load_page_records(resolved_output)
    )
    first = max(1, int(args.start_page or 1))
    last = int(args.end_page) if args.end_page is not None else None
    selected_pages = [
        page
        for page in available_pages
        if page >= first and (last is None or page <= last)
    ]
    result: dict[str, Any] = {}
    for stage in ("ocr", "proofread", "translation"):
        path = resolved_output / ".pipeline_graph" / f"{stage}_identity.json"
        expected = stable_fingerprint(_stage_content_semantics(args, stage))
        fresh_count: int | None = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = None
        except (OSError, json.JSONDecodeError):
            fresh_count = 0
            payload = None
        if isinstance(payload, dict):
            if payload.get("schema_version") == 1:
                fresh_count = (
                    len(selected_pages)
                    if payload.get("fingerprint") == expected
                    else 0
                )
            elif isinstance(payload.get("page_fingerprints"), dict):
                saved = payload["page_fingerprints"]
                fresh_count = sum(
                    1
                    for page in selected_pages
                    if saved.get(str(page)) == expected
                )
            else:
                fresh_count = 0
        prefix = "translation" if stage == "translation" else stage
        result[f"{prefix}_pages_semantic_fresh"] = fresh_count
        result[f"{prefix}_semantic_stale"] = (
            fresh_count is not None and fresh_count != len(selected_pages)
        )
    return result


def _text_model_page_handler(
    phase: str,
    input_name: str,
    output_name: str,
) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        _materialize_pages_artifact(context, input_name)
        args = _parsed_args(context)
        semantic_stage = "translation" if phase == "translate" else phase
        semantics = _stage_content_semantics(args, semantic_stage)
        selected_pages = _selected_stage_pages(context, args)
        force_for_identity = _stage_identity_changed(
            context,
            semantic_stage,
            semantics,
            selected_pages,
        )
        _run_phase(
            context,
            phase,
            add=("--force",) if force_for_identity else (),
        )
        _write_stage_identity(
            context,
            semantic_stage,
            semantics,
            selected_pages,
        )
        artifact = _pages_artifact(context.output_dir)
        return NodeResult(
            outputs={output_name: artifact},
            fingerprints={output_name: str(artifact["sha256"])},
            metadata={"semantic_identity_changed": force_for_identity},
        )

    return handler


def _toc_file_handler(context: GraphContext) -> NodeResult:
    artifact = _file_artifact(context.output_dir / "toc.json")
    return NodeResult(
        outputs={ART_TOC: artifact},
        fingerprints={ART_TOC: str(artifact["sha256"])},
    )


def _require_source_argument(context: GraphContext) -> Path:
    source = context.require(ART_SOURCE)
    source_path = _validated_file_from_artifact(source, name=ART_SOURCE)
    args = _parsed_args(context)
    argument_path = (
        Path(args.input).expanduser().resolve() if args.input else None
    )
    if argument_path != source_path:
        raise BookGraphConfigurationError(
            "source.pdf providers must match the legacy input PDF argument"
        )
    _validate_and_bind_source_artifact(context, source)
    return source_path


def _toc_pipeline_handler(page_artifact: str) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        _require_source_argument(context)
        _materialize_pages_artifact(context, page_artifact)
        _run_phase(context, "toc")
        return _toc_file_handler(context)

    return handler


def _toc_outline_handler(context: GraphContext) -> NodeResult:
    source_path = _require_source_argument(context)
    with fitz.open(source_path) as document:
        outline = document.get_toc(simple=True)
    if not outline:
        raise ValueError(f"PDF has no usable outline: {source_path}")
    entries: list[dict[str, Any]] = []
    for position, row in enumerate(outline, start=1):
        level, title, pdf_page = int(row[0]), str(row[1]).strip(), int(row[2])
        if not title or pdf_page < 1:
            continue
        normalized = legacy.normalize_match_text(title)
        kind = (
            "part"
            if normalized in {"封面", "封底", "frontcover", "backcover"}
            else "other"
        )
        entries.append(
            {
                "id": f"outline-{position:04d}",
                "index": "",
                "title": title,
                "level": max(1, level),
                "kind": kind,
                "printed_page": None,
                "pdf_page": pdf_page,
                "end_pdf_page": None,
            }
        )
    payload = legacy.normalize_toc_payload(
        {
            "toc_pdf_pages": [],
            "page_offset": 0,
            "printed_pages_per_pdf_page": 1,
            "offset_evidence": [
                {
                    "source": "pdf-outline",
                    "entry_count": len(entries),
                }
            ],
            "entries": entries,
        }
    )
    legacy.write_json(context.output_dir / "toc.json", payload)
    return _toc_file_handler(context)


def _load_chapters_handler(context: GraphContext) -> NodeResult:
    reuse_draft = False
    reader_audit_path = _require_output_directory(
        context.output_dir,
        context.output_dir / ".pipeline_graph" / "reader-semantic-audit.json",
    )
    try:
        reader_audit = _read_semantic_audit(reader_audit_path)
        canonical = _chapters_artifact(context.output_dir)
        manifest_path = Path(str(canonical["manifest"]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        filenames = _validated_chapter_filenames(manifest, manifest_path)
        reader_digest = _files_digest(
            [
                _chapter_path(
                    Path(str(canonical["chapter_dir"])),
                    filename,
                )
                for filename in filenames
            ]
        )
        reuse_draft = bool(
            reader_audit.get("reader_chapters_sha256") == reader_digest
            and reader_audit.get("reader_manifest_sha256")
            == _sha256_file(manifest_path)
        )
        if reuse_draft:
            _draft_chapters_artifact(context.output_dir)
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        json.JSONDecodeError,
        SemanticReconstructionError,
    ):
        reuse_draft = False
    if not reuse_draft:
        _snapshot_chapter_drafts(context.output_dir)
    artifact = _draft_chapters_artifact(context.output_dir)
    return NodeResult(
        outputs={ART_CHAPTERS: artifact},
        fingerprints={ART_CHAPTERS: str(artifact["sha256"])},
    )


def _compile_handler(page_artifact: str, *, required_page_model: str | None = None) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        _require_source_argument(context)
        _materialize_pages_artifact(context, page_artifact)
        _materialize_toc_artifact(context)
        # Translation, all publishers, and the release gate are independent
        # graph nodes. Keep require-translation intact so the legacy guard
        # still validates every selected page against the model identity.
        _run_phase(
            context,
            "compile",
            add=(
                "--no-kb",
                "--no-epub",
                "--no-docx",
                "--no-bookmarked-pdf",
                "--no-verify",
                *(
                    ("--required-ocr-model-prefix", required_page_model)
                    if required_page_model
                    else ()
                ),
            ),
            remove=(
                ("--translate-non-chinese", False),
                *((
                    ("--required-ocr-model-prefix", True),
                ) if required_page_model else ()),
            ),
        )
        _snapshot_chapter_drafts(context.output_dir)
        artifact = _draft_chapters_artifact(context.output_dir)
        return NodeResult(
            outputs={ART_CHAPTERS: artifact},
            fingerprints={ART_CHAPTERS: str(artifact["sha256"])},
        )

    return handler


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _semantic_audit_path(output_dir: Path) -> Path:
    return _require_output_directory(
        output_dir,
        output_dir / SEMANTIC_AUDIT_RELATIVE_PATH,
    )


def _draft_semantic_audit_path(output_dir: Path) -> Path:
    return _require_output_directory(
        output_dir,
        output_dir / DRAFT_SEMANTIC_AUDIT_RELATIVE_PATH,
    )


def _read_semantic_audit(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SemanticReconstructionError(
            f"cannot read semantic reconstruction audit {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SemanticReconstructionError(
            f"semantic reconstruction audit must be an object: {path}"
        )
    if payload.get("schema_version") != SEMANTIC_AUDIT_SCHEMA_VERSION:
        raise SemanticReconstructionError(
            "unsupported semantic reconstruction audit schema in "
            f"{path}: {payload.get('schema_version')!r}"
        )
    status = payload.get("status")
    if status not in {"passed", "blocked"}:
        raise SemanticReconstructionError(
            f"semantic reconstruction audit has invalid status in {path}: {status!r}"
        )
    root_release_blocked = payload.get("release_blocked")
    if root_release_blocked is not None and not isinstance(
        root_release_blocked, bool
    ):
        raise SemanticReconstructionError(
            "semantic reconstruction audit release_blocked must be boolean: "
            f"{path}"
        )
    summary = payload.get("summary")
    chapters = payload.get("chapters")
    if not isinstance(summary, dict) or not isinstance(chapters, list):
        raise SemanticReconstructionError(
            f"semantic reconstruction audit has invalid summary/chapters in {path}"
        )
    release_blocked = summary.get("release_blocked")
    if not isinstance(release_blocked, bool):
        raise SemanticReconstructionError(
            "semantic reconstruction audit summary.release_blocked must be boolean: "
            f"{path}"
        )
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            raise SemanticReconstructionError(
                f"semantic reconstruction audit chapter {index} is not an object: {path}"
            )
        chapter_blocked = chapter.get("release_blocked")
        if not isinstance(chapter_blocked, bool):
            raise SemanticReconstructionError(
                "semantic reconstruction audit chapter release_blocked must be "
                f"boolean at index {index}: {path}"
            )
        issues = chapter.get("issues")
        if not isinstance(issues, list):
            raise SemanticReconstructionError(
                f"semantic reconstruction audit chapter issues must be a list: {path}"
            )
        for issue_index, issue in enumerate(issues, start=1):
            if not isinstance(issue, dict) or not isinstance(
                issue.get("blocking", True), bool
            ):
                raise SemanticReconstructionError(
                    "semantic reconstruction audit issue must be an object with a "
                    "boolean blocking flag at chapter "
                    f"{index}, issue {issue_index}: {path}"
                )
    return payload


def _semantic_audit_is_blocked(payload: Mapping[str, Any]) -> bool:
    summary = payload["summary"]
    chapters = payload["chapters"]
    assert isinstance(summary, dict)
    assert isinstance(chapters, list)
    return bool(
        payload.get("status") == "blocked"
        or payload.get("release_blocked") is True
        or summary.get("release_blocked") is True
        or any(
            chapter.get("release_blocked") is True
            or any(issue.get("blocking", True) for issue in chapter["issues"])
            for chapter in chapters
        )
    )


def _minimal_semantic_audit(
    chapter_artifact: Mapping[str, Any],
    manifest: list[dict[str, Any]],
    inventories: list[dict[str, Any]],
) -> dict[str, Any]:
    chapters = [
        {
            "chapter_id": str(item.get("id") or inventory["filename"]),
            "filename": inventory["filename"],
            "reviewed_override": bool(item.get("reviewed_override")),
            "footnote_count": inventory["footnote_count"],
            "markdown_sha256": inventory["markdown_sha256"],
            "footnote_contract_sha256": inventory[
                "footnote_contract_sha256"
            ],
            "pages": [],
            "issues": [],
            "release_blocked": False,
        }
        for item, inventory in zip(manifest, inventories)
    ]
    return {
        "schema_version": SEMANTIC_AUDIT_SCHEMA_VERSION,
        "status": "passed",
        "release_blocked": False,
        "generated_by": NODE_SEMANTIC,
        "contract_mode": "markdown-footnotes-only",
        "source_chapters_sha256": str(chapter_artifact["sha256"]),
        "summary": {
            "chapter_count": len(chapters),
            "footnote_count": sum(item["footnote_count"] for item in inventories),
            "issue_count": 0,
            "blocking_issue_count": 0,
            "release_blocked": False,
        },
        "chapters": chapters,
    }


def _semantic_artifact(
    chapter_artifact: Mapping[str, Any],
    audit_path: Path,
) -> dict[str, Any]:
    manifest_path = Path(str(chapter_artifact["manifest"])).expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise SemanticReconstructionError(
            f"invalid chapter manifest for semantic artifact: {manifest_path}"
        )
    return {
        "manifest": str(chapter_artifact["manifest"]),
        "chapter_dir": str(chapter_artifact["chapter_dir"]),
        "sha256": str(chapter_artifact["sha256"]),
        "count": len(manifest),
        "semantic_contract_version": SEMANTIC_AUDIT_SCHEMA_VERSION,
        "semantic_audit": str(audit_path.resolve()),
        "semantic_audit_sha256": _sha256_file(audit_path),
        "semantic_release_blocked": False,
    }


def _semantic_handler(*, allow_missing_audit: bool) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        _manifest_path, chapter_dir, manifest = _chapter_bundle(
            context,
            ART_CHAPTERS,
        )
        chapter_artifact = context.require(ART_CHAPTERS)
        if not isinstance(chapter_artifact, dict):
            raise SemanticReconstructionError(
                f"{ART_CHAPTERS} artifact must be an object"
            )

        inventories: list[dict[str, Any]] = []
        failures: list[str] = []
        for item in manifest:
            filename = str(item["filename"])
            chapter_path = _chapter_path(chapter_dir, filename)
            inventory = legacy.parse_markdown_footnotes(
                chapter_path.read_text(encoding="utf-8")
            )
            inventories.append(
                {
                    "filename": filename,
                    "footnote_count": len(inventory.definitions),
                    "markdown_sha256": _sha256_file(chapter_path),
                    "footnote_contract_sha256": (
                        legacy.markdown_footnote_contract_sha256(
                            chapter_path.read_text(encoding="utf-8")
                        )
                    ),
                }
            )
            if inventory.valid:
                continue
            failures.append(
                f"{filename}: duplicate_definitions="
                f"{list(inventory.duplicate_definitions)}, missing_definitions="
                f"{list(inventory.missing_definitions)}, unused_definitions="
                f"{list(inventory.unused_definitions)}, duplicate_references="
                f"{list(inventory.duplicate_references)}"
            )
        if failures:
            raise SemanticReconstructionError(
                "semantic reconstruction blocks publication because Markdown "
                "footnotes are not a one-to-one closed set: "
                + "; ".join(failures)
            )

        publication_audit_path = _semantic_audit_path(context.output_dir)
        audit_path = _draft_semantic_audit_path(context.output_dir)
        minimal_payload = _minimal_semantic_audit(
            chapter_artifact,
            manifest,
            inventories,
        )
        payload: dict[str, Any] | None = None
        if publication_audit_path.is_file():
            published_payload = _read_semantic_audit(publication_audit_path)
            is_reader_audit = bool(
                published_payload.get("reader_chapters_sha256")
            )
            if not is_reader_audit:
                payload = published_payload
            elif not audit_path.is_file():
                # One-time migration from graph adapter v2, which overwrote
                # the only audit with reader-byte evidence.
                payload = minimal_payload
        if payload is None and audit_path.is_file():
            payload = _read_semantic_audit(audit_path)
        if payload is not None:
            if _semantic_audit_is_blocked(payload):
                summary = payload["summary"]
                raise SemanticReconstructionError(
                    "semantic reconstruction blocks publication: "
                    f"{audit_path} reports release_blocked=true "
                    "(blocking_issue_count="
                    f"{summary.get('blocking_issue_count', 'unknown')})"
                )
            if (
                payload.get("generated_by") == NODE_SEMANTIC
                and payload.get("contract_mode") == "markdown-footnotes-only"
                and payload.get("source_chapters_sha256")
                != chapter_artifact["sha256"]
            ):
                payload = minimal_payload
            serialized = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            if not audit_path.is_file() or audit_path.read_text(
                encoding="utf-8"
            ) != serialized:
                _atomic_write_text(audit_path, serialized)
        elif allow_missing_audit:
            payload = minimal_payload
            _atomic_write_text(
                audit_path,
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
        else:
            raise SemanticReconstructionError(
                "semantic reconstruction audit is missing after chapter compile: "
                f"{publication_audit_path}"
            )

        payload = _read_semantic_audit(audit_path)
        if _semantic_audit_is_blocked(payload):
            summary = payload["summary"]
            raise SemanticReconstructionError(
                "semantic reconstruction blocks publication: "
                f"{audit_path} reports release_blocked=true "
                f"(blocking_issue_count={summary.get('blocking_issue_count', 'unknown')})"
            )

        expected_filenames = [str(item["filename"]) for item in manifest]
        audit_chapters = payload["chapters"]
        audit_filenames = [str(item.get("filename") or "") for item in audit_chapters]
        if audit_filenames != expected_filenames:
            raise SemanticReconstructionError(
                "semantic reconstruction audit does not match the chapter manifest: "
                f"expected={expected_filenames}, audit={audit_filenames}"
            )
        expected_footnotes = {
            item["filename"]: item["footnote_count"] for item in inventories
        }
        expected_contracts = {
            item["filename"]: item["footnote_contract_sha256"]
            for item in inventories
        }
        expected_markdown_digests = {
            item["filename"]: item["markdown_sha256"] for item in inventories
        }
        audit_footnotes: dict[str, int] = {}
        audit_contracts: dict[str, str] = {}
        audit_markdown_digests: dict[str, str] = {}
        for item in audit_chapters:
            count = item.get("footnote_count")
            if not isinstance(count, int) or count < 0:
                raise SemanticReconstructionError(
                    "semantic reconstruction audit footnote_count must be a "
                    f"non-negative integer for {item.get('filename')!r}"
                )
            audit_footnotes[str(item["filename"])] = count
            audit_contracts[str(item["filename"])] = str(
                item.get("footnote_contract_sha256") or ""
            )
            audit_markdown_digests[str(item["filename"])] = str(
                item.get("markdown_sha256") or ""
            )
        if audit_footnotes != expected_footnotes:
            raise SemanticReconstructionError(
                "semantic reconstruction audit footnote counts are stale: "
                f"expected={expected_footnotes}, audit={audit_footnotes}"
            )
        if audit_contracts != expected_contracts:
            raise SemanticReconstructionError(
                "semantic reconstruction audit footnote contracts are stale: "
                f"expected={expected_contracts}, audit={audit_contracts}"
            )
        if audit_markdown_digests != expected_markdown_digests:
            raise SemanticReconstructionError(
                "semantic_markdown_digest_stale: semantic reconstruction audit "
                "Markdown digests are missing or stale: "
                f"expected={expected_markdown_digests}, "
                f"audit={audit_markdown_digests}"
            )
        summary = payload["summary"]
        expected_summary_counts = {
            "chapter_count": len(manifest),
            "footnote_count": sum(expected_footnotes.values()),
        }
        actual_summary_counts = {
            name: summary.get(name) for name in expected_summary_counts
        }
        if actual_summary_counts != expected_summary_counts:
            raise SemanticReconstructionError(
                "semantic reconstruction audit summary is stale: "
                f"expected={expected_summary_counts}, audit={actual_summary_counts}"
            )

        artifact = _semantic_artifact(chapter_artifact, audit_path)
        semantic_fingerprint = stable_fingerprint(
            {
                "chapters": artifact["sha256"],
                "audit": artifact["semantic_audit_sha256"],
            }
        )
        return NodeResult(
            outputs={ART_SEMANTIC_CHAPTERS: artifact},
            fingerprints={ART_SEMANTIC_CHAPTERS: semantic_fingerprint},
            metadata={
                "chapter_count": len(manifest),
                "footnote_count": sum(
                    item["footnote_count"] for item in inventories
                ),
                "semantic_audit": str(audit_path),
            },
        )

    return handler


def _atomic_copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _validated_file_from_artifact(artifact: Any, *, name: str) -> Path:
    if not isinstance(artifact, dict) or not artifact.get("path"):
        raise ValueError(f"{name} artifact must contain a file path")
    path = Path(str(artifact["path"])).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    expected_sha = _required_artifact_sha256(artifact, name=name)
    if expected_sha != _sha256_file(path):
        raise ValueError(f"{name} artifact content no longer matches its digest")
    return path


def _required_artifact_sha256(artifact: Mapping[str, Any], *, name: str) -> str:
    value = artifact.get("sha256")
    if not (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    ):
        raise ValueError(f"{name} artifact must contain a SHA-256 digest")
    return value.lower()


def _validate_file_artifact_value(
    _context: GraphContext,
    value: Any,
) -> bool:
    _validated_file_from_artifact(value, name="file")
    return True


def _validate_source_artifact_value(
    _context: GraphContext,
    value: Any,
) -> bool:
    path = _validated_file_from_artifact(value, name=ART_SOURCE)
    if path.suffix.lower() != ".pdf":
        return False
    with fitz.open(path) as document:
        declared = value.get("page_count") if isinstance(value, dict) else None
        return declared is None or int(declared) == document.page_count


def _validate_pages_artifact_value(
    _context: GraphContext,
    value: Any,
) -> bool:
    if not isinstance(value, dict) or not value.get("path"):
        return False
    directory = Path(str(value["path"])).expanduser().resolve()
    if not directory.is_dir():
        return False
    paths = sorted(directory.glob("page_*.json"))
    if not paths:
        return False
    for path in paths:
        resolved = path.resolve()
        try:
            resolved.relative_to(directory)
        except ValueError:
            return False
        if not resolved.is_file():
            return False
    return _required_artifact_sha256(value, name="pages") == _files_digest(paths)


def _validate_chapter_artifact_value(
    _context: GraphContext,
    value: Any,
) -> bool:
    if not isinstance(value, dict):
        return False
    manifest_path = Path(str(value.get("manifest") or "")).expanduser().resolve()
    chapter_dir = Path(str(value.get("chapter_dir") or "")).expanduser().resolve()
    if not manifest_path.is_file() or not chapter_dir.is_dir():
        return False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        return False
    filenames = _validated_chapter_filenames(manifest, manifest_path)
    paths = [manifest_path]
    for filename in filenames:
        path = _chapter_path(chapter_dir, filename)
        if not path.is_file():
            return False
        paths.append(path)
    return (
        _required_artifact_sha256(value, name="chapter bundle")
        == _files_digest(paths)
    )


def _validate_semantic_chapter_artifact_value(
    context: GraphContext,
    value: Any,
) -> bool:
    if not _validate_chapter_artifact_value(context, value):
        return False
    if not isinstance(value, dict):
        return False
    if (
        value.get("semantic_contract_version")
        != SEMANTIC_AUDIT_SCHEMA_VERSION
        or value.get("semantic_release_blocked") is not False
    ):
        return False
    audit_path = Path(str(value.get("semantic_audit") or "")).expanduser().resolve()
    if audit_path != _draft_semantic_audit_path(context.output_dir):
        return False
    expected_sha = value.get("semantic_audit_sha256")
    if not (
        isinstance(expected_sha, str)
        and len(expected_sha) == 64
        and audit_path.is_file()
        and _sha256_file(audit_path) == expected_sha.lower()
    ):
        return False
    payload = _read_semantic_audit(audit_path)
    return not _semantic_audit_is_blocked(payload)


def _semantic_cache_is_current(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    if set(outputs) != {ART_SEMANTIC_CHAPTERS}:
        return False
    saved = outputs[ART_SEMANTIC_CHAPTERS]
    if not _validate_semantic_chapter_artifact_value(context, saved):
        return False
    chapters = context.require(ART_CHAPTERS)
    if not isinstance(chapters, dict):
        return False
    return saved == _semantic_artifact(
        chapters,
        _draft_semantic_audit_path(context.output_dir),
    )


def _validate_import_artifact_value(
    _context: GraphContext,
    value: Any,
) -> bool:
    if not isinstance(value, dict) or not value.get("path"):
        return False
    current = _import_source_artifact(
        Path(str(value["path"])).expanduser().resolve()
    )
    return (
        _required_artifact_sha256(value, name=ART_PAGES_IMPORTED)
        == current.get("sha256")
        and value.get("page_numbers") == current.get("page_numbers")
    )


def _book_artifact_validators() -> dict[str, Any]:
    file_artifacts = {
        ART_TOC,
        ART_KB,
        ART_EPUB,
        ART_DOCX,
        ART_REFERENCE_PDF,
        ART_REPORT,
        ART_WORD_REPORT,
    }
    validators: dict[str, Any] = {
        name: _validate_file_artifact_value for name in file_artifacts
    }
    validators.update(
        {
            ART_SOURCE: _validate_source_artifact_value,
            ART_PAGES_IMPORTED: _validate_import_artifact_value,
            ART_PAGES_RAW: _validate_pages_artifact_value,
            ART_PAGES_PROOFREAD: _validate_pages_artifact_value,
            ART_PAGES_TRANSLATED: _validate_pages_artifact_value,
            ART_CHAPTERS: _validate_chapter_artifact_value,
            ART_SEMANTIC_CHAPTERS: _validate_semantic_chapter_artifact_value,
            ART_READER_CHAPTERS: _validate_chapter_artifact_value,
        }
    )
    return validators


def _materialize_toc_artifact(context: GraphContext) -> Path:
    source = _validated_file_from_artifact(
        context.require(ART_TOC),
        name=ART_TOC,
    )
    target = context.output_dir / "toc.json"
    if source != target.resolve():
        _atomic_copy_file(source, target)
    return target


def _materialize_pages_artifact(context: GraphContext, artifact_name: str) -> Path:
    artifact = context.require(artifact_name)
    if not isinstance(artifact, dict) or not artifact.get("path"):
        raise ValueError(f"{artifact_name} artifact must contain a directory path")
    source_dir = Path(str(artifact["path"])).expanduser().resolve()
    paths = sorted(source_dir.glob("page_*.json")) if source_dir.is_dir() else []
    if not paths:
        raise FileNotFoundError(
            f"No page checkpoints in {artifact_name} artifact: {source_dir}"
        )
    expected_sha = _required_artifact_sha256(artifact, name=artifact_name)
    if expected_sha != _files_digest(paths):
        raise ValueError(
            f"{artifact_name} artifact content no longer matches its digest"
        )
    target_dir = (context.output_dir / "pages").resolve()
    if source_dir == target_dir:
        return target_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    expected_names = {path.name for path in paths}
    for source in paths:
        _atomic_copy_file(source, target_dir / source.name)
    for stale in target_dir.glob("page_*.json"):
        if stale.name not in expected_names:
            stale.unlink()
    return target_dir


def _publish_reader_bundle_to_canonical(context: GraphContext) -> None:
    """Make a replaceable reader bundle the canonical verifier input."""

    _manifest_path, chapter_dir, manifest = _chapter_bundle(
        context,
        ART_READER_CHAPTERS,
    )
    target_dir = _require_output_directory(
        context.output_dir,
        context.output_dir / "chapters",
    )
    filenames = _validated_chapter_filenames(manifest, _manifest_path)
    expected: set[str] = set()
    if chapter_dir.resolve() != target_dir:
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            expected.add(filename)
            _atomic_copy_file(
                _chapter_path(chapter_dir, filename),
                _chapter_path(target_dir, filename),
            )
        for stale in target_dir.glob("*.md"):
            if stale.name not in expected:
                stale.unlink()
    legacy.write_json(context.output_dir / "chapters.json", manifest)


def _reader_semantic_audit(
    context: GraphContext,
    *,
    chapter_dir: Path,
    manifest: list[dict[str, Any]],
) -> Path:
    """Record the post-sanitize chapter bytes without losing source evidence."""

    semantic_artifact = context.require(ART_SEMANTIC_CHAPTERS)
    if not isinstance(semantic_artifact, dict):
        raise SemanticReconstructionError(
            f"{ART_SEMANTIC_CHAPTERS} artifact must be an object"
        )
    source_audit_path = Path(
        str(semantic_artifact.get("semantic_audit") or "")
    ).expanduser().resolve()
    if source_audit_path != _draft_semantic_audit_path(context.output_dir):
        raise SemanticReconstructionError(
            "semantic reader audit requires the immutable draft audit artifact"
        )
    payload = _read_semantic_audit(source_audit_path)
    audit_by_filename = {
        str(item.get("filename") or ""): item
        for item in payload["chapters"]
        if isinstance(item, dict)
    }
    for item in manifest:
        filename = str(item["filename"])
        audited = audit_by_filename.get(filename)
        if audited is None:
            raise SemanticReconstructionError(
                f"semantic reconstruction audit is missing {filename!r}"
            )
        path = _chapter_path(chapter_dir, filename)
        markdown = path.read_text(encoding="utf-8")
        inventory = legacy.parse_markdown_footnotes(markdown)
        if not inventory.valid:
            raise SemanticReconstructionError(
                "publication sanitation changed the Markdown footnote contract: "
                f"{filename}"
            )
        source_digest = str(audited.get("source_markdown_sha256") or "")
        if not source_digest:
            source_digest = str(audited.get("markdown_sha256") or "")
        audited["source_markdown_sha256"] = source_digest
        audited["markdown_sha256"] = _sha256_file(path)
        audited["footnote_contract_sha256"] = (
            legacy.markdown_footnote_contract_sha256(markdown)
        )
        audited["footnote_count"] = len(inventory.definitions)
    payload["reader_chapters_sha256"] = _files_digest(
        [
            _chapter_path(chapter_dir, str(item["filename"]))
            for item in manifest
        ]
    )
    payload["reader_manifest_sha256"] = _sha256_file(
        context.output_dir / "chapters.json"
    )
    audit_path = _require_output_directory(
        context.output_dir,
        context.output_dir / ".pipeline_graph" / "reader-semantic-audit.json",
    )
    _atomic_write_text(
        audit_path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return audit_path


def _reader_chapters_artifact(output_dir: Path) -> dict[str, Any]:
    """Return reader chapters together with their independent audit evidence."""

    artifact = _chapters_artifact(output_dir)
    audit_path = _require_output_directory(
        output_dir,
        output_dir / ".pipeline_graph" / "reader-semantic-audit.json",
    )
    if not audit_path.is_file():
        raise FileNotFoundError(audit_path)
    artifact.update(
        {
            "reader_semantic_audit": str(audit_path),
            "reader_semantic_audit_sha256": _sha256_file(audit_path),
        }
    )
    return artifact


def _publication_target_path(context: GraphContext, artifact_name: str) -> Path:
    title_slug = legacy.slugify(_book_title(context))
    targets = {
        ART_KB: context.output_dir / "knowledge_base.jsonl",
        ART_EPUB: context.output_dir / f"{title_slug}.epub",
        ART_DOCX: context.output_dir / f"{title_slug}.docx",
        ART_REFERENCE_PDF: context.output_dir / f"{title_slug}_带目录.pdf",
    }
    try:
        target = targets[artifact_name].resolve()
    except KeyError as exc:
        raise ValueError(f"Unknown publication artifact: {artifact_name}") from exc
    try:
        target.relative_to(context.output_dir)
    except ValueError as exc:
        raise BookGraphConfigurationError(
            f"publication target escapes output directory: {target}"
        ) from exc
    return target


def _register_managed_publication(
    context: GraphContext,
    artifact_name: str,
    path: Path,
) -> None:
    """Record a publisher-owned file and retire an unchanged former title.

    Publication filenames include the effective title.  A title change must
    not leave the previous framework-generated DOCX/EPUB/reference PDF beside
    the new one, because the verifier correctly treats duplicate release files
    as ambiguous.  Only a file whose path and digest were recorded by this
    framework is removed; a user-modified former artifact is preserved and the
    quality gate is allowed to report the conflict.
    """

    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(context.output_dir)
    except ValueError as exc:
        raise BookGraphConfigurationError(
            f"managed publication must be inside {context.output_dir}: {resolved}"
        ) from exc
    if not resolved.is_file():
        raise FileNotFoundError(resolved)

    identity_path = (
        context.output_dir / ".pipeline_graph" / "publication_identity.json"
    )
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    managed = payload.get("managed") if isinstance(payload, dict) else None
    if not isinstance(managed, dict):
        managed = {}

    previous = managed.get(artifact_name)
    if isinstance(previous, dict) and previous.get("path"):
        previous_path = Path(str(previous["path"])).expanduser().resolve()
        try:
            previous_path.relative_to(context.output_dir)
        except ValueError:
            previous_path = resolved
        if previous_path != resolved and previous_path.is_file():
            expected_sha = previous.get("sha256")
            if expected_sha and _sha256_file(previous_path) == expected_sha:
                previous_path.unlink()

    managed[artifact_name] = {
        "path": str(resolved),
        "sha256": _sha256_file(resolved),
    }
    legacy.write_json(
        identity_path,
        {
            "schema_version": 1,
            "managed": managed,
        },
    )


def _publish_artifacts_to_canonical(
    context: GraphContext,
    artifact_names: Iterable[str],
) -> None:
    """Materialize replaceable publisher outputs into verifier-owned slots.

    Plugin nodes may stage files outside the publication root (a graph-owned
    subdirectory is recommended).  The legacy verifier intentionally validates
    canonical publication filenames, so this boundary copies them atomically.
    A noncanonical release file directly in the output root is rejected: it
    would remain a second candidate and invalidate the quality gate.
    """

    for artifact_name in sorted(artifact_names):
        source = _validated_file_from_artifact(
            context.require(artifact_name),
            name=artifact_name,
        )
        target = _publication_target_path(context, artifact_name)
        if source != target:
            if source.parent == context.output_dir:
                raise BookGraphConfigurationError(
                    f"{artifact_name} provider returned noncanonical root file "
                    f"{source.name!r}; stage custom outputs in a subdirectory "
                    f"or publish directly to {target.name!r}"
                )
            _atomic_copy_file(source, target)
        _register_managed_publication(context, artifact_name, target)


def _sanitize_handler(context: GraphContext) -> NodeResult:
    args = _parsed_args(context)
    _manifest_path, source_dir, manifest = _chapter_bundle(
        context,
        ART_SEMANTIC_CHAPTERS,
    )
    title = args.title or (
        Path(args.input).stem if args.input else context.output_dir.name
    )
    target_dir = _require_output_directory(
        context.output_dir,
        context.output_dir / "chapters",
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    changed = 0
    for item in manifest:
        filename = str(item["filename"])
        path = _chapter_path(target_dir, filename)
        source = _chapter_path(source_dir, filename).read_text(encoding="utf-8")
        # Human-reviewed chapters are already passed through the deliberately
        # narrow reviewed cleaner by compile_chapters.  Do not widen that
        # policy here or reviewed.exact would cease to be a useful invariant.
        if item.get("reviewed_override"):
            cleaned = source
        else:
            cleaned = legacy.strip_publication_metadata(
                source,
                publication_title=title,
                chapter_title=str(item.get("display_title") or ""),
            )
        previous = path.read_text(encoding="utf-8") if path.is_file() else None
        if cleaned != previous:
            _atomic_write_text(path, cleaned)
            changed += 1
    expected = {str(item["filename"]) for item in manifest}
    for stale in target_dir.glob("*.md"):
        if stale.name not in expected:
            stale.unlink()
    # The reader bundle is authoritative for every downstream publisher.  Its
    # manifest is published atomically together with the canonical chapter
    # directory instead of sharing the mutable compiler manifest by accident.
    legacy.write_json(context.output_dir / "chapters.json", manifest)
    reader_audit_path = _reader_semantic_audit(
        context,
        chapter_dir=target_dir,
        manifest=manifest,
    )
    # The legacy verifier consumes this canonical reader-audit location.  The
    # upstream semantic artifact points at the immutable draft audit instead,
    # so publication compatibility no longer mutates an upstream contract.
    _atomic_copy_file(reader_audit_path, _semantic_audit_path(context.output_dir))
    artifact = _reader_chapters_artifact(context.output_dir)
    return NodeResult(
        outputs={ART_READER_CHAPTERS: artifact},
        # Publishers consume reader bytes; audit-only metadata changes are
        # independently covered by chapters.semantic and the release gate.
        fingerprints={ART_READER_CHAPTERS: str(artifact["sha256"])},
        metadata={
            "changed_files": changed,
            "reader_semantic_audit": str(reader_audit_path),
        },
    )


def _book_title(context: GraphContext) -> str:
    args = _parsed_args(context)
    return args.title or (
        Path(args.input).stem if args.input else context.output_dir.name
    )


def _chapter_bundle(
    context: GraphContext,
    artifact_name: str,
) -> tuple[Path, Path, list[dict[str, Any]]]:
    artifact = context.require(artifact_name)
    if not isinstance(artifact, dict):
        raise ValueError(f"{artifact_name} artifact must be an object")
    manifest_path = Path(str(artifact.get("manifest") or ""))
    chapter_dir = Path(str(artifact.get("chapter_dir") or ""))
    if not manifest_path.is_file() or not chapter_dir.is_dir():
        raise FileNotFoundError(
            f"Invalid {artifact_name} bundle: {manifest_path}, {chapter_dir}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not manifest:
        raise ValueError(f"Invalid or empty chapter manifest: {manifest_path}")
    filenames = _validated_chapter_filenames(manifest, manifest_path)
    bundle_paths = [manifest_path]
    for filename in filenames:
        chapter_path = _chapter_path(chapter_dir, filename)
        if not chapter_path.is_file():
            raise FileNotFoundError(chapter_path)
        bundle_paths.append(chapter_path)
    expected_sha = _required_artifact_sha256(artifact, name=artifact_name)
    if expected_sha != _files_digest(bundle_paths):
        raise ValueError(
            f"{artifact_name} bundle content no longer matches its digest"
        )
    return manifest_path, chapter_dir, manifest


def _knowledge_base_handler(context: GraphContext) -> NodeResult:
    source_path = _require_source_argument(context)
    _manifest_path, chapter_dir, manifest = _chapter_bundle(
        context,
        ART_READER_CHAPTERS,
    )
    rows = legacy.build_knowledge_rows_from_manifest(
        source_path,
        chapter_dir,
        manifest,
    )
    path = _publication_target_path(context, ART_KB)
    legacy.write_knowledge_base(path, rows)
    args = _parsed_args(context)
    rag_knowledge_base.maybe_build_zhipu_embedding_index(
        path,
        requested=getattr(args, "rag_embed", None),
    )
    _register_managed_publication(context, ART_KB, path)
    artifact = _file_artifact(path)
    rag_manifest = rag_knowledge_base.manifest_path_for(path).resolve()
    rag_metadata = rag_knowledge_base.read_rag_manifest(path)
    embedding_status = rag_metadata["retrieval"]["embedding"]["status"]
    return NodeResult(
        outputs={ART_KB: artifact},
        fingerprints={ART_KB: str(artifact["sha256"])},
        metadata={
            "rows": len(rows),
            "rag_manifest": str(rag_manifest),
            "embedding_status": embedding_status,
        },
    )


def _docx_handler(chapter_artifact: str) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        _manifest_path, chapter_dir, manifest = _chapter_bundle(
            context,
            chapter_artifact,
        )
        path = _publication_target_path(context, ART_DOCX)
        legacy.build_docx(
            path,
            chapter_dir,
            manifest,
            book_title=_book_title(context),
            author=_parsed_args(context).author,
        )
        _register_managed_publication(context, ART_DOCX, path)
        artifact = _file_artifact(path)
        return NodeResult(
            outputs={ART_DOCX: artifact},
            fingerprints={ART_DOCX: str(artifact["sha256"])},
        )

    return handler


def _epub_handler(chapter_artifact: str) -> Any:
    def handler(context: GraphContext) -> NodeResult:
        args = _parsed_args(context)
        _manifest_path, chapter_dir, manifest = _chapter_bundle(
            context,
            chapter_artifact,
        )
        path = _publication_target_path(context, ART_EPUB)
        legacy.build_epub(
            path,
            chapter_dir,
            manifest,
            book_title=_book_title(context),
            language=(
                "zh-CN"
                if args.target_language == "简体中文"
                else args.target_language
            ),
        )
        _register_managed_publication(context, ART_EPUB, path)
        artifact = _file_artifact(path)
        return NodeResult(
            outputs={ART_EPUB: artifact},
            fingerprints={ART_EPUB: str(artifact["sha256"])},
        )

    return handler


def _reference_pdf_handler(context: GraphContext) -> NodeResult:
    source_path = _require_source_argument(context)
    toc_path = _validated_file_from_artifact(
        context.require(ART_TOC),
        name=ART_TOC,
    )
    output = _publication_target_path(context, ART_REFERENCE_PDF)
    legacy.build_bookmarked_pdf(
        source_path,
        output,
        legacy.load_toc(toc_path),
    )
    _register_managed_publication(context, ART_REFERENCE_PDF, output)
    artifact = _file_artifact(output)
    return NodeResult(
        outputs={ART_REFERENCE_PDF: artifact},
        fingerprints={ART_REFERENCE_PDF: str(artifact["sha256"])},
    )


def _verify_handler(
    selected_artifacts: frozenset[str],
    *,
    use_fallback_title: bool = False,
    page_artifact: str | None = None,
    report_artifact: str = ART_REPORT,
) -> Any:
    flags = {
        ART_KB: "--no-kb",
        ART_EPUB: "--no-epub",
        ART_DOCX: "--no-docx",
        ART_REFERENCE_PDF: "--no-bookmarked-pdf",
    }

    def handler(context: GraphContext) -> NodeResult:
        declared_inputs = set(context.values)
        if ART_SOURCE in declared_inputs:
            _require_source_argument(context)
        if page_artifact is not None and page_artifact in declared_inputs:
            _materialize_pages_artifact(context, page_artifact)
        if ART_TOC in declared_inputs:
            _materialize_toc_artifact(context)
        if ART_READER_CHAPTERS in declared_inputs:
            _publish_reader_bundle_to_canonical(context)
        _publish_artifacts_to_canonical(
            context,
            selected_artifacts & declared_inputs,
        )
        add = tuple(
            option
            for artifact, option in flags.items()
            if artifact not in selected_artifacts
        )
        remove = tuple(
            (option, False)
            for artifact, option in flags.items()
            if artifact in selected_artifacts
        )
        args = _parsed_args(context)
        publication_profile = (
            "word" if report_artifact == ART_WORD_REPORT else "full"
        )
        argv = _phase_argv(
            context,
            "verify",
            add=(
                *add,
                "--verification-profile",
                publication_profile,
            ),
            remove=(*remove, ("--verification-profile", True)),
        )
        if context.config.get("source_mode") == "text-pdf":
            argv = _remove_option(
                argv,
                "--required-ocr-model-prefix",
                takes_value=True,
            )
            argv.extend(
                [
                    "--required-ocr-model-prefix",
                    extract_textbook_layer.TEXT_LAYER_MODEL,
                ]
            )
        if use_fallback_title and not args.title:
            argv.extend(["--title", _book_title(context)])
        try:
            exit_code = _call_legacy_main(argv)
        except SystemExit as exc:
            try:
                exit_code = int(exc.code)
            except (TypeError, ValueError):
                exit_code = 1
        if exit_code:
            raise LegacyStageError("verify", exit_code)
        report_path = (
            Path(args.report).expanduser().resolve()
            if args.report
            else context.output_dir
            / "audit"
            / (
                "chapter-report.json"
                if args.chapter_id
                else (
                    "word-release-report.json"
                    if publication_profile == "word"
                    else "release-report.json"
                )
            )
        )
        artifact = _file_artifact(report_path)
        return NodeResult(
            outputs={report_artifact: artifact},
            fingerprints={report_artifact: str(artifact["sha256"])},
        )

    return handler


def _status_handler(context: GraphContext) -> NodeResult:
    legacy.load_env_file(Path(legacy.__file__).with_name(".env"))
    args = _parsed_args(context)
    profile_config = load_pipeline_profiles(args.config) if args.config else None
    ocr_profile = (
        profile_config.for_stage("ocr", args.ocr_profile)
        if profile_config is not None
        else None
    )
    toc_profile = (
        profile_config.for_stage("toc", args.toc_profile)
        if profile_config is not None
        else None
    )
    proofread_profile = (
        profile_config.for_stage("proofread", args.proofread_profile)
        if profile_config is not None
        else None
    )
    translation_profile = (
        profile_config.for_stage("translation", args.translation_profile)
        if profile_config is not None
        else None
    )
    expected_translation_identity = legacy.resolve_expected_translation_identity(
        args,
        toc_profile=toc_profile,
        translation_profile=translation_profile,
    )
    expected_proofread_identity = legacy.resolve_proofread_identity(
        args,
        glm_api_base=legacy.resolve_toc_api_base(args, toc_profile=toc_profile),
        profile=proofread_profile,
    )
    if _source_mode(context) == "text-pdf":
        expected_ocr_model_prefix = extract_textbook_layer.TEXT_LAYER_MODEL
        expected_ocr_model_exact = None
    else:
        expected_ocr_model_prefix = legacy.resolve_expected_ocr_model_prefix(
            args,
            ocr_profile,
        )
        expected_ocr_model_exact = legacy.resolve_expected_ocr_model_exact(
            args,
            ocr_profile,
        )
    status = legacy.output_status(
        context.output_dir,
        expected_translation_identity=expected_translation_identity,
        expected_proofread_identity=expected_proofread_identity,
        expected_ocr_model_prefix=expected_ocr_model_prefix,
        expected_ocr_model_exact=expected_ocr_model_exact,
    )
    status.update(semantic_status_for_args(context.output_dir, args))
    return NodeResult(outputs={ART_STATUS: status})


def _source_cache_validator(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    saved = outputs.get(ART_SOURCE)
    if not isinstance(saved, dict) or not saved.get("path"):
        return False
    path = Path(str(saved["path"]))
    binding_path = context.output_dir / ".pipeline_graph" / "source.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    source_mode = _source_mode(context)
    source_adapter = _source_adapter(source_mode)
    return (
        path.is_file()
        and saved.get("sha256") == _sha256_file(path)
        and saved.get("source_mode") == source_mode
        and saved.get("adapter") == source_adapter
        and isinstance(binding, dict)
        and binding.get("sha256") == saved.get("sha256")
        and binding.get("source_mode") == source_mode
        and binding.get("adapter") == source_adapter
    )


def _node(
    name: str,
    handler: Any,
    *,
    requires: Iterable[str],
    provides: Iterable[str],
    version: str,
    fingerprint: Any = None,
    cache: bool = True,
    cache_validator: Any = None,
    description: str = "",
) -> NodeSpec:
    artifact_requires = frozenset(requires)
    return NodeSpec(
        name=name,
        handler=handler,
        # ``pipeline.argv`` remains an explicit control dependency, but only
        # artifact edges participate automatically in the cache key.  Each
        # node's declared fingerprint selects its own semantic CLI settings;
        # workers, delays, unrelated profiles, and raw keys cannot invalidate
        # the entire graph.
        requires=frozenset({"pipeline.argv", *artifact_requires}),
        fingerprint_requires=artifact_requires,
        provides=frozenset(provides),
        version=version,
        fingerprint=fingerprint,
        # The graph owns one output-directory lock.  OCR/proofread/translate
        # keep their existing stage locks; declaring those again would self-lock.
        resources=frozenset({"output_dir"}),
        cache=cache,
        cache_validator=cache_validator,
        description=description,
    )


def _selected_publishers(args: Any, disabled: frozenset[str]) -> frozenset[str]:
    selected: set[str] = set()
    if not args.no_kb:
        selected.add(NODE_KB)
    if not args.no_epub:
        selected.add(NODE_EPUB)
    if not args.no_docx:
        selected.add(NODE_DOCX)
    if not args.no_bookmarked_pdf:
        selected.add(NODE_REFERENCE_PDF)
    return frozenset(selected - set(disabled))


def _requested_publication_artifacts(args: Any) -> frozenset[str]:
    artifacts: set[str] = set()
    if not args.no_kb:
        artifacts.add(ART_KB)
    if not args.no_epub:
        artifacts.add(ART_EPUB)
    if not args.no_docx:
        artifacts.add(ART_DOCX)
    if not args.no_bookmarked_pdf:
        artifacts.add(ART_REFERENCE_PDF)
    return frozenset(artifacts)


def _secret_redaction_values(args: Any) -> tuple[str, ...]:
    values: set[str] = set()
    for field_name in ("api_key", "ocr_api_key", "translation_api_key"):
        value = getattr(args, field_name, None)
        if isinstance(value, str) and value:
            values.add(value)
    environment_names = {
        "GLM_API_KEY",
        "GLM_CODING_API_KEY",
        "GLM_TOC_API_KEY",
        "Z_AI_API_KEY",
        "DEEPSEEK_API_KEY",
    }
    for field_name in (
        "api_key_env",
        "ocr_api_key_env",
        "translation_api_key_env",
    ):
        name = getattr(args, field_name, None)
        if isinstance(name, str) and name:
            environment_names.add(name)
    if args.config:
        try:
            profiles = load_pipeline_profiles(args.config)
            for profile in profiles.profiles.values():
                environment_names.add(profile.credential_env)
        except (OSError, ValueError):
            # Normal profile validation will raise with its established error
            # at the stage boundary; redaction collection must not replace it.
            pass
    for name, value in os.environ.items():
        upper_name = name.upper()
        if (
            name in environment_names
            or "API_KEY" in upper_name
            or upper_name.endswith("_TOKEN")
            or upper_name.endswith("_SECRET")
        ) and value:
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def prepare_book_graph(
    pipeline_argv: Iterable[str],
    *,
    options: BookGraphOptions | None = None,
    recipe: Recipe | None = None,
    plugin_allowlist: Iterable[str] = (),
) -> PreparedBookGraph:
    """Create a phase-compatible DAG without executing model or file stages."""

    options = options or BookGraphOptions()
    if recipe is not None:
        recipe_core_enable = {
            name for name in recipe.enable if name.startswith("core.")
        }
        unknown_core = recipe_core_enable - KNOWN_NODE_NAMES
        if unknown_core:
            raise BookGraphConfigurationError(
                f"recipe enables unknown core nodes: {sorted(unknown_core)}"
            )
        invalid_disable = {
            name for name in recipe.disable if name not in KNOWN_NODE_NAMES
        }
        if invalid_disable:
            raise BookGraphConfigurationError(
                "recipe.disable accepts built-in core nodes only; external nodes "
                f"are opt-in: {sorted(invalid_disable)}"
            )
        toc_source = options.toc_source
        if NODE_TOC_OUTLINE in recipe_core_enable:
            toc_source = "outline"
        source_mode = options.source_mode
        if NODE_TEXT_EXTRACT in recipe_core_enable:
            if options.source_mode != "scanned-pdf":
                source_mode = options.source_mode
            else:
                source_mode = "text-pdf"
        options = replace(
            options,
            include_proofread=(
                options.include_proofread or NODE_PROOFREAD in recipe_core_enable
            ),
            toc_source=toc_source,
            source_mode=source_mode,
            disabled_nodes=frozenset(
                set(options.disabled_nodes) | set(recipe.disable)
            ),
            target_artifacts=(
                options.target_artifacts or frozenset(recipe.targets)
            ),
        )
    if (
        options.source_mode == "text-pdf"
        and NODE_TEXT_EXTRACT in options.disabled_nodes
    ):
        raise BookGraphConfigurationError(
            "source_mode='text-pdf' requires core.pages.text_extract"
        )
    if options.source_mode == "text-pdf" and NODE_OCR not in options.disabled_nodes:
        # Selection is explicit and mutually exclusive even when a Recipe did
        # not repeat the redundant disable entry.
        options = replace(
            options,
            disabled_nodes=frozenset({*options.disabled_nodes, NODE_OCR}),
        )
    # Match book_pipeline.main(): parser defaults may be supplied by the repo
    # .env, but resolved secrets themselves are never fingerprinted.
    legacy.load_env_file(Path(legacy.__file__).with_name(".env"))
    argv = list(pipeline_argv)
    args = legacy.build_parser().parse_args(argv)
    if args.ocr_cache_model and args.ocr_cache_model_prefix:
        raise BookGraphConfigurationError(
            "--ocr-cache-model and --ocr-cache-model-prefix are mutually exclusive"
        )
    output_dir = Path(args.output_dir).expanduser().resolve()
    context = GraphContext(
        output_dir,
        values={"pipeline.argv": argv},
        # Raw argv is private control data.  Built-in nodes explicitly exclude
        # it from automatic cache dependencies and fingerprint only the stage
        # settings they consume.
        fingerprints={
            "pipeline.argv": stable_fingerprint(_sanitized_control_argv(argv))
        },
        config={
            "adapter_version": GRAPH_ADAPTER_VERSION,
            "adopt_existing_output": options.adopt_existing_output,
            "source_mode": options.source_mode,
        },
        private_value_names=frozenset({"pipeline.argv"}),
        redaction_values=_secret_redaction_values(args),
        value_validators=_book_artifact_validators(),
    )
    graph = PipelineGraph()
    disabled = options.disabled_nodes

    def add(spec: NodeSpec) -> None:
        if spec.name not in disabled:
            graph.add(spec)

    phase = args.phase
    source_spec = _node(
        NODE_SOURCE,
        _source_handler,
        requires=(),
        provides=(ART_SOURCE,),
        version="1",
        fingerprint=_source_fingerprint,
        cache_validator=_source_cache_validator,
        description="Validate and fingerprint the source PDF.",
    )
    if options.source_mode == "text-pdf" and phase in {"ocr", "proofread"}:
        raise BookGraphConfigurationError(
            "source_mode='text-pdf' supports translate/toc/compile/all; "
            "it deliberately has no OCR or OCR-proofreading phase"
        )
    source_required = phase in {"all", "ocr", "toc", "compile"} or (
        options.source_mode == "text-pdf" and phase == "translate"
    ) or (
        phase == "verify"
        and not args.chapter_id
        and not args.no_bookmarked_pdf
    )
    # Any import paired with an explicit PDF must validate/bind that PDF before
    # the import is allowed to mutate page checkpoints.
    source_before_import = source_required or bool(
        args.import_ocr_dir and args.input
    )
    if source_before_import:
        add(source_spec)
    import_requirement: tuple[str, ...] = ()
    if args.import_ocr_dir:
        add(
            _node(
                NODE_PAGES_IMPORT,
                _import_pages_handler,
                requires=(ART_SOURCE,) if source_before_import else (),
                provides=(ART_PAGES_IMPORTED,),
                version="1",
                cache_validator=_import_source_is_current,
                description="Import legacy page checkpoints exactly once.",
            )
        )
        import_requirement = (ART_PAGES_IMPORTED,)
    pages_load_spec = _node(
        NODE_PAGES_LOAD,
        _load_pages_handler,
        requires=import_requirement,
        provides=(ART_PAGES_RAW,),
        version="1",
        cache=False,
        description="Load existing page JSON checkpoints.",
    )
    chapters_load_spec = _node(
        NODE_CHAPTERS_LOAD,
        _load_chapters_handler,
        requires=import_requirement,
        provides=(ART_CHAPTERS,),
        version="1",
        cache=False,
        description="Load an existing chapter manifest and Markdown folder.",
    )
    semantic_spec = _node(
        NODE_SEMANTIC,
        _semantic_handler(allow_missing_audit=phase in {"epub", "docx"}),
        requires=(ART_CHAPTERS,),
        provides=(ART_SEMANTIC_CHAPTERS,),
        version="2",
        cache_validator=_semantic_cache_is_current,
        description=(
            "Validate one-to-one Markdown footnotes and enforce the semantic "
            "reconstruction audit before publication cleanup."
        ),
    )
    sanitize_spec = _node(
        NODE_SANITIZE,
        _sanitize_handler,
        requires=(ART_SEMANTIC_CHAPTERS,),
        provides=(ART_READER_CHAPTERS,),
        version="3",
        fingerprint=_sanitize_fingerprint,
        cache_validator=_artifact_is_current(_reader_chapters_artifact),
        description="Apply idempotent reader-facing publication cleanup.",
    )

    selected_publishers = _selected_publishers(args, disabled)
    selected_publication_artifacts = frozenset(
        PUBLISHER_ARTIFACTS[name] for name in selected_publishers
    )
    target_artifacts: set[str] = set(options.target_artifacts)
    if {ART_REPORT, ART_WORD_REPORT}.issubset(target_artifacts):
        raise BookGraphConfigurationError(
            "choose either publication.report or publication.word_report, not both"
        )
    word_report_requested = ART_WORD_REPORT in target_artifacts
    verification_node = NODE_VERIFY_WORD if word_report_requested else NODE_VERIFY
    verification_artifact = (
        ART_WORD_REPORT if word_report_requested else ART_REPORT
    )
    verification_publications = (
        frozenset({ART_DOCX})
        if word_report_requested
        else selected_publication_artifacts
    )

    if phase == "status":
        add(
            _node(
                NODE_STATUS,
                _status_handler,
                requires=import_requirement,
                provides=(ART_STATUS,),
                version="1",
                cache=False,
                description="Read checkpoint and artifact status.",
            )
        )
        target_artifacts = target_artifacts or {ART_STATUS}
    elif phase == "verify":
        # Standalone verification intentionally does not declare publication
        # artifacts as graph dependencies: the verifier must report every
        # missing or malformed output rather than fail planning early.
        add(
            _node(
                verification_node,
                _verify_handler(
                    verification_publications,
                    report_artifact=verification_artifact,
                ),
                requires=(
                    *import_requirement,
                    *((ART_SOURCE,) if source_required else ()),
                ),
                provides=(verification_artifact,),
                version="1",
                cache=False,
                description=(
                    "Run the deterministic Word publication quality gate."
                    if word_report_requested
                    else "Run the deterministic full publication quality gate."
                ),
            )
        )
        target_artifacts = target_artifacts or {verification_artifact}
    elif phase in {"epub", "docx"}:
        add(chapters_load_spec)
        add(semantic_spec)
        add(sanitize_spec)
        if phase == "epub":
            add(
                _node(
                    NODE_EPUB,
                    _epub_handler(ART_READER_CHAPTERS),
                    requires=(ART_READER_CHAPTERS,),
                    provides=(ART_EPUB,),
                    version="2",
                    fingerprint=_publisher_fingerprint("epub"),
                    cache_validator=_single_file_is_current,
                    description="Build EPUB from chapter Markdown.",
                )
            )
            target_artifacts = target_artifacts or {ART_EPUB}
        else:
            add(
                _node(
                    NODE_DOCX,
                    _docx_handler(ART_READER_CHAPTERS),
                    requires=(ART_READER_CHAPTERS,),
                    provides=(ART_DOCX,),
                    version="3",
                    fingerprint=_publisher_fingerprint("docx"),
                    cache_validator=_single_file_is_current,
                    description="Build Word from chapter Markdown.",
                )
            )
            target_artifacts = target_artifacts or {ART_DOCX}
    elif phase in {"ocr", "proofread", "translate", "toc", "compile", "all"}:
        if options.source_mode == "text-pdf":
            if args.import_ocr_dir:
                raise BookGraphConfigurationError(
                    "source_mode='text-pdf' cannot be combined with --import-ocr-dir"
                )
            add(
                _node(
                    NODE_TEXT_EXTRACT,
                    _text_pdf_handler(options),
                    requires=(ART_SOURCE,),
                    provides=(ART_PAGES_RAW,),
                    version="1",
                    fingerprint=_text_pdf_fingerprint(options),
                    # Always enter the deterministic importer so it can compare
                    # the complete current text layer with existing PageRecords.
                    # Identical pages preserve fresh translation overlays.
                    cache=False,
                    description=(
                        "Extract a complete embedded PDF text layer into page "
                        "checkpoints without calling OCR."
                    ),
                )
            )
        elif phase in {"ocr", "all"}:
            add(
                _node(
                    NODE_OCR,
                    _ocr_page_handler,
                    requires=(ART_SOURCE, *import_requirement),
                    provides=(ART_PAGES_RAW,),
                    version="2",
                    # PageStore owns the precise per-page model/source cache.
                    # Always enter the stage so it can validate the exact OCR
                    # identity; fully fresh books return with pending=0 and no
                    # model call.
                    cache=False,
                    description="OCR PDF pages with exact model-bound checkpoints.",
                )
            )
        else:
            add(pages_load_spec)

        current_pages = ART_PAGES_RAW
        include_proofread = options.include_proofread or phase == "proofread"
        if options.source_mode == "text-pdf" and include_proofread:
            raise BookGraphConfigurationError(
                "source_mode='text-pdf' cannot enable OCR proofreading"
            )
        if include_proofread:
            add(
                _node(
                    NODE_PROOFREAD,
                    _text_model_page_handler(
                        "proofread",
                        current_pages,
                        ART_PAGES_PROOFREAD,
                    ),
                    requires=(current_pages,),
                    provides=(ART_PAGES_PROOFREAD,),
                    version="1",
                    cache=False,
                    description="Apply optional non-destructive OCR proofreading overlays.",
                )
            )
            current_pages = ART_PAGES_PROOFREAD

        include_translation = args.translate_non_chinese and phase in {
            "all",
            "translate",
            "compile",
        }
        if include_translation:
            add(
                _node(
                    NODE_TRANSLATE,
                    _text_model_page_handler(
                        "translate",
                        current_pages,
                        ART_PAGES_TRANSLATED,
                    ),
                    requires=(current_pages,),
                    provides=(ART_PAGES_TRANSLATED,),
                    version="1",
                    cache=False,
                    description="Translate non-Chinese page text with profile-bound identity.",
                )
            )
            current_pages = ART_PAGES_TRANSLATED
        elif phase == "translate":
            raise BookGraphConfigurationError(
                "--phase translate requires --translate-non-chinese"
            )

        if phase == "ocr":
            target_artifacts = target_artifacts or {ART_PAGES_RAW}
        elif phase == "proofread":
            target_artifacts = target_artifacts or {ART_PAGES_PROOFREAD}
        elif phase == "translate":
            target_artifacts = target_artifacts or {ART_PAGES_TRANSLATED}
        else:
            if phase in {"all", "toc"} or (
                phase == "compile" and options.toc_source == "outline"
            ):
                toc_name = (
                    NODE_TOC_OUTLINE
                    if options.toc_source == "outline"
                    else NODE_TOC_PIPELINE
                )
                toc_handler = (
                    _toc_outline_handler
                    if options.toc_source == "outline"
                    else _toc_pipeline_handler(current_pages)
                )
                toc_requirements = (
                    (ART_SOURCE, *import_requirement)
                    if options.toc_source == "outline"
                    else (ART_SOURCE, current_pages)
                )
                add(
                    _node(
                        toc_name,
                        toc_handler,
                        requires=toc_requirements,
                        provides=(ART_TOC,),
                        version="1",
                        fingerprint=(
                            None
                            if options.toc_source == "outline"
                            else _toc_fingerprint
                        ),
                        cache_validator=_single_file_is_current,
                        description=(
                            "Build mapped TOC from the PDF outline."
                            if options.toc_source == "outline"
                            else "Resolve and map TOC using manual JSON or the selected LLM."
                        ),
                    )
                )
            else:
                add(
                    _node(
                        NODE_TOC_LOAD,
                        _toc_file_handler,
                        requires=import_requirement,
                        provides=(ART_TOC,),
                        version="1",
                        cache=False,
                        description="Load an existing mapped TOC.",
                    )
                )

            if phase == "toc":
                target_artifacts = target_artifacts or {ART_TOC}
            else:
                add(
                    _node(
                        NODE_COMPILE,
                        _compile_handler(
                            current_pages,
                            required_page_model=(
                                extract_textbook_layer.TEXT_LAYER_MODEL
                                if options.source_mode == "text-pdf"
                                else None
                            ),
                        ),
                        requires=(ART_SOURCE, current_pages, ART_TOC),
                        provides=(ART_CHAPTERS,),
                        version="3",
                        fingerprint=_reviewed_fingerprint,
                        cache_validator=_compile_inputs_are_current(current_pages),
                        description="Compile page text and mapped TOC into chapter Markdown.",
                    )
                )
                add(semantic_spec)
                add(sanitize_spec)
                if NODE_KB in selected_publishers:
                    add(
                        _node(
                            NODE_KB,
                            _knowledge_base_handler,
                            requires=(ART_SOURCE, ART_READER_CHAPTERS),
                            provides=(ART_KB,),
                            version="2",
                            fingerprint=_publisher_fingerprint("knowledge-base"),
                            cache_validator=_knowledge_base_is_current,
                            description=(
                                "Publish RAG knowledge-base JSONL and retrieval metadata "
                                "from final chapter text."
                            ),
                        )
                    )
                if NODE_EPUB in selected_publishers:
                    add(
                        _node(
                            NODE_EPUB,
                            _epub_handler(ART_READER_CHAPTERS),
                            requires=(ART_READER_CHAPTERS,),
                            provides=(ART_EPUB,),
                            version="2",
                            fingerprint=_publisher_fingerprint("epub"),
                            cache_validator=_single_file_is_current,
                            description="Publish EPUB from final chapter text.",
                        )
                    )
                if NODE_DOCX in selected_publishers:
                    add(
                        _node(
                            NODE_DOCX,
                            _docx_handler(ART_READER_CHAPTERS),
                            requires=(ART_READER_CHAPTERS,),
                            provides=(ART_DOCX,),
                            version="3",
                            fingerprint=_publisher_fingerprint("docx"),
                            cache_validator=_single_file_is_current,
                            description="Publish Word from final chapter text.",
                        )
                    )
                if NODE_REFERENCE_PDF in selected_publishers:
                    add(
                        _node(
                            NODE_REFERENCE_PDF,
                            _reference_pdf_handler,
                            requires=(ART_SOURCE, ART_TOC, *import_requirement),
                            provides=(ART_REFERENCE_PDF,),
                            version="1",
                            fingerprint=_publisher_fingerprint("reference-pdf"),
                            cache_validator=_single_file_is_current,
                            description="Publish a visual reference PDF with bookmarks.",
                        )
                    )

                if not args.no_verify and verification_node not in disabled:
                    verify_requirements = {
                        ART_SOURCE,
                        ART_SEMANTIC_CHAPTERS,
                        ART_READER_CHAPTERS,
                    }
                    verify_requirements.update({current_pages, ART_TOC})
                    verify_requirements.update(verification_publications)
                    add(
                        _node(
                            verification_node,
                            _verify_handler(
                                verification_publications,
                                use_fallback_title=True,
                                page_artifact=current_pages,
                                report_artifact=verification_artifact,
                            ),
                            requires=verify_requirements,
                            provides=(verification_artifact,),
                            version="1",
                            cache=False,
                            description=(
                                "Run the deterministic Word publication quality gate."
                                if word_report_requested
                                else "Run the deterministic full publication quality gate."
                            ),
                        )
                    )
                    target_artifacts = target_artifacts or {
                        verification_artifact
                    }
                else:
                    target_artifacts = target_artifacts or {
                        PUBLISHER_ARTIFACTS[name] for name in selected_publishers
                    } or {ART_READER_CHAPTERS}
    else:
        raise BookGraphConfigurationError(f"unsupported phase: {phase}")

    if not target_artifacts:
        raise BookGraphConfigurationError("graph has no target artifacts")
    # Fail early with a readable dependency error after all configured removals.
    if recipe is not None:
        registry = NodeRegistry(graph.nodes)
        registry.load_plugins(
            recipe.required_plugins,
            allowlist=plugin_allowlist,
        )
        registered_names = {node.name for node in registry.nodes}
        missing_core = {
            name
            for name in recipe.enable
            if name.startswith("core.") and name not in registered_names
        }
        if missing_core:
            raise BookGraphConfigurationError(
                "recipe enables core nodes that are unavailable in this phase: "
                f"{sorted(missing_core)}"
            )
        for name in recipe.enable:
            if name.startswith("core."):
                continue
            graph.add(registry.get(name))

    if phase == "verify" and any(
        node.name == verification_node for node in graph.nodes
    ):
        provided = {
            artifact
            for node in graph.nodes
            for artifact in node.provides
        }
        staged_publications = frozenset(
            provided & set(verification_publications)
        )
        existing_verify = next(
            node for node in graph.nodes if node.name == verification_node
        )
        graph.replace(
            verification_node,
            _node(
                verification_node,
                _verify_handler(
                    verification_publications,
                    report_artifact=verification_artifact,
                ),
                requires={
                    *(set(existing_verify.requires) - {"pipeline.argv"}),
                    *staged_publications,
                },
                provides=(verification_artifact,),
                version="1",
                cache=False,
                description=(
                    "Run the deterministic Word publication quality gate."
                    if word_report_requested
                    else "Run the deterministic full publication quality gate."
                ),
            ),
        )

    if phase in {"all", "compile"} and any(
        node.name == verification_node for node in graph.nodes
    ):
        provided = {
            artifact
            for node in graph.nodes
            for artifact in node.provides
        }
        verified_artifacts = frozenset(
            provided & set(verification_publications)
        )
        graph.replace(
            verification_node,
            _node(
                verification_node,
                _verify_handler(
                    verified_artifacts,
                    use_fallback_title=True,
                    page_artifact=current_pages,
                    report_artifact=verification_artifact,
                ),
                requires={
                    ART_SEMANTIC_CHAPTERS,
                    ART_READER_CHAPTERS,
                    ART_SOURCE,
                    current_pages,
                    ART_TOC,
                    *verified_artifacts,
                },
                provides=(verification_artifact,),
                version="1",
                cache=False,
                description=(
                    "Run the deterministic Word publication quality gate."
                    if word_report_requested
                    else "Run the deterministic full publication quality gate."
                ),
            ),
        )

    unknown_forced = set(options.force_nodes) - {
        node.name for node in graph.nodes
    }
    if unknown_forced:
        raise BookGraphConfigurationError(
            "forced nodes are unavailable in the resolved graph: "
            f"{sorted(unknown_forced)}"
        )

    prepared = PreparedBookGraph(
        graph=graph,
        context=context,
        targets=frozenset(target_artifacts),
        options=options,
        require_existing_output=phase == "status",
    )
    prepared.plan()
    return prepared


def run_book_graph(
    pipeline_argv: Iterable[str],
    *,
    options: BookGraphOptions | None = None,
    recipe: Recipe | None = None,
    plugin_allowlist: Iterable[str] = (),
) -> GraphRunResult:
    """Prepare and execute a graph-backed legacy pipeline request."""

    return prepare_book_graph(
        pipeline_argv,
        options=options,
        recipe=recipe,
        plugin_allowlist=plugin_allowlist,
    ).execute()
