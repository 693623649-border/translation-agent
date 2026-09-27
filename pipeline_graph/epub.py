"""First-class EPUB DAG built on the shared graph runtime.

Born-digital EPUB input already has a package spine, so this graph does not
pretend that it passed through the page/OCR pipeline.  It binds the exact EPUB
bytes, reconstructs immutable semantic source chapters, optionally translates
and applies hash-bound units, materializes one canonical reader bundle, and
publishes only the requested formats.

The module intentionally has no dotenv integration.  A live translation
request must be injected by the caller together with a non-secret identity
fingerprint.  Product entry points remain responsible for resolving profiles
and credentials according to their own trust boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any
import unicodedata
from pathlib import PurePosixPath
import zipfile

import book_pipeline as legacy
from epub_publication_verifier import (
    VERIFIER_VERSION,
    normalize_epub_language,
    verify_epub_publication,
)
from epub_semantic_import import IMPORTER_VERSION, apply_translations, import_epub
from semantic_ir import SemanticContractError, TranslationUnit
from semantic_review import REVIEW_POLICY_VERSION, SemanticReviewError
from semantic_review_policy import validate_semantic_review
from semantic_translation_runner import (
    PROMPT_CONTRACT_SHA256,
    RUNNER_VERSION,
    translate_units,
)

from .core import (
    GraphContext,
    GraphExecutor,
    GraphRunResult,
    NodeResult,
    NodeSpec,
    PipelineGraph,
    stable_fingerprint,
)


EPUB_GRAPH_ADAPTER_VERSION = "epub-graph-v1"
MAX_EPUB_ENTRIES = 10_000
MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_EPUB_COMPRESSION_RATIO = 200.0

NODE_SOURCE = "core.source.epub.inspect"
NODE_IMPORT = "core.reconstruct.epub_semantic"
NODE_TRANSLATIONS_IMPORT = "core.semantic.translations.inspect"
NODE_TRANSLATE = "core.semantic.translate"
NODE_REVIEW = "core.semantic.review"
NODE_APPLY = "core.semantic.apply"
NODE_READER = "core.semantic.materialize_reader"
NODE_EPUB = "core.publish.epub"
NODE_DOCX = "core.publish.docx"
NODE_VERIFY_EPUB = "core.publication.verify.epub"

ART_SOURCE = "source.epub"
ART_SEMANTIC_CHAPTERS = "chapters.semantic"
ART_TRANSLATION_UNITS = "semantic.translation_units"
ART_TRANSLATIONS = "semantic.translations"
ART_REVIEW = "semantic.review"
ART_READER_CHAPTERS = "chapters.reader"
ART_EPUB = "publication.epub"
ART_DOCX = "publication.docx"
ART_EPUB_REPORT = "publication.epub_report"

KNOWN_TARGET_ARTIFACTS = frozenset(
    {
        ART_SOURCE,
        ART_SEMANTIC_CHAPTERS,
        ART_TRANSLATION_UNITS,
        ART_TRANSLATIONS,
        ART_REVIEW,
        ART_READER_CHAPTERS,
        ART_EPUB,
        ART_DOCX,
        ART_EPUB_REPORT,
    }
)


class EpubGraphConfigurationError(ValueError):
    """Raised before execution when an EPUB graph contract is invalid."""


class EpubSourceChangedError(RuntimeError):
    """Raised when a prepared source/input no longer has the bound bytes."""


class EpubSemanticBlockedError(RuntimeError):
    """Raised when a blocked semantic reconstruction reaches publication."""


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    sha256: str
    size: int
    entry_count: int | None = None
    uncompressed_size: int | None = None

    @classmethod
    def capture(cls, value: Path | str, *, suffix: str | None = None) -> "_FileSnapshot":
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        _assert_regular_file(candidate)
        path = candidate.resolve()
        if suffix is not None and path.suffix.lower() != suffix:
            raise EpubGraphConfigurationError(
                f"input must have the {suffix} extension: {path}"
            )
        before = path.stat()
        sha256 = _sha256_file(path)
        inspection = _inspect_epub_archive(path) if suffix == ".epub" else None
        after = path.stat()
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EpubSourceChangedError(
                f"input changed while its graph identity was prepared: {path}"
            )
        return cls(
            path=path,
            sha256=sha256,
            size=after.st_size,
            entry_count=(inspection[0] if inspection is not None else None),
            uncompressed_size=(inspection[1] if inspection is not None else None),
        )

    def assert_current(self, *, label: str) -> None:
        try:
            _assert_regular_file(self.path)
            before = self.path.stat()
            current_sha256 = _sha256_file(self.path)
            inspection = (
                _inspect_epub_archive(self.path)
                if self.entry_count is not None or self.uncompressed_size is not None
                else None
            )
            after = self.path.stat()
        except (OSError, EpubGraphConfigurationError) as exc:
            raise EpubSourceChangedError(
                f"prepared {label} is missing or unreadable: {self.path}"
            ) from exc
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise EpubSourceChangedError(
                f"prepared {label} changed while it was inspected: {self.path}"
            )
        if after.st_size != self.size or current_sha256 != self.sha256:
            raise EpubSourceChangedError(
                f"prepared {label} changed after graph construction: {self.path}"
            )
        if self.entry_count is not None or self.uncompressed_size is not None:
            assert inspection is not None
            entry_count, uncompressed_size = inspection
            if (
                entry_count != self.entry_count
                or uncompressed_size != self.uncompressed_size
            ):
                raise EpubSourceChangedError(
                    f"prepared {label} ZIP identity changed after graph construction: {self.path}"
                )


@dataclass(frozen=True)
class EpubGraphOptions:
    """Explicit, non-secret controls for a born-digital EPUB graph.

    ``translation_mode`` has three deliberately separate states:

    * ``none`` publishes the reconstructed source-language reader bundle;
    * ``run`` calls ``translation_request`` through ``translate_units``;
    * ``apply`` inspects and applies an externally supplied JSONL file.

    No credential or dotenv path belongs in this contract.  Callers that use
    ``run`` inject a request closure and identify it with
    ``translation_request_fingerprint`` so graph cache invalidation does not
    depend on secret bytes or on a callable's unstable repr.
    """

    translation_mode: str = "none"
    target_artifacts: frozenset[str] = field(
        default_factory=lambda: frozenset({ART_EPUB, ART_DOCX})
    )
    target_language: str = "简体中文"
    glossary: Mapping[str, str] = field(default_factory=dict)
    translations_path: Path | str | None = None
    translation_request: Callable[[str], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    translation_request_fingerprint: str | None = None
    translation_provider: str = "deepseek"
    translation_base_url: str = ""
    translation_model: str = "deepseek-v4-flash"
    translation_prompt_profile: str = RUNNER_VERSION
    translation_thinking: str = "disabled"
    translation_temperature: float = 0.0
    translation_max_chars: int = 9000
    translation_concurrency: int = 16
    translation_retries: int = 3
    translation_cache_dir: Path | str | None = None
    book_title: str | None = None
    author: str | None = None
    publication_language: str | None = None
    force_nodes: frozenset[str] = field(default_factory=frozenset)
    force_all: bool = False

    def __post_init__(self) -> None:
        if self.translation_mode not in {"none", "run", "apply"}:
            raise EpubGraphConfigurationError(
                "translation_mode must be 'none', 'run', or 'apply'"
            )
        targets = _string_set(self.target_artifacts, label="target_artifacts")
        unknown_targets = targets - KNOWN_TARGET_ARTIFACTS
        if unknown_targets:
            raise EpubGraphConfigurationError(
                f"unknown EPUB target artifacts: {sorted(unknown_targets)}"
            )
        if not targets:
            raise EpubGraphConfigurationError("target_artifacts must not be empty")
        force_nodes = _string_set(self.force_nodes, label="force_nodes")
        if type(self.force_all) is not bool:
            raise EpubGraphConfigurationError("force_all must be a boolean")
        if not isinstance(self.target_language, str) or not self.target_language.strip():
            raise EpubGraphConfigurationError("target_language must not be empty")
        try:
            normalize_epub_language(self.target_language)
            if self.publication_language is not None:
                normalize_epub_language(self.publication_language)
        except ValueError as exc:
            raise EpubGraphConfigurationError(str(exc)) from exc
        if not isinstance(self.glossary, Mapping) or not all(
            isinstance(source, str)
            and isinstance(target, str)
            and source.strip()
            and target.strip()
            for source, target in self.glossary.items()
        ):
            raise EpubGraphConfigurationError(
                "glossary must contain non-empty string pairs"
            )
        if self.translation_mode == "run":
            if not callable(self.translation_request):
                raise EpubGraphConfigurationError(
                    "translation_mode='run' requires translation_request"
                )
            if not isinstance(self.translation_request_fingerprint, str) or not (
                self.translation_request_fingerprint.strip()
            ):
                raise EpubGraphConfigurationError(
                    "translation_mode='run' requires a non-secret "
                    "translation_request_fingerprint"
                )
            if self.translations_path is not None:
                raise EpubGraphConfigurationError(
                    "translations_path is only valid for translation_mode='apply'"
                )
        elif self.translation_mode == "apply":
            if self.translations_path is None:
                raise EpubGraphConfigurationError(
                    "translation_mode='apply' requires translations_path"
                )
            if (
                self.translation_request is not None
                or self.translation_request_fingerprint is not None
            ):
                raise EpubGraphConfigurationError(
                    "translation request settings are only valid for "
                    "translation_mode='run'"
                )
        elif any(
            value is not None
            for value in (
                self.translations_path,
                self.translation_request,
                self.translation_request_fingerprint,
            )
        ):
            raise EpubGraphConfigurationError(
                "translation inputs require translation_mode='run' or 'apply'"
            )
        for label, value in (
            ("translation_provider", self.translation_provider),
            ("translation_model", self.translation_model),
            ("translation_prompt_profile", self.translation_prompt_profile),
        ):
            if not isinstance(value, str) or not value.strip():
                raise EpubGraphConfigurationError(f"{label} must not be empty")
        if not isinstance(self.translation_base_url, str):
            raise EpubGraphConfigurationError("translation_base_url must be a string")
        for label, value in (
            ("translations_path", self.translations_path),
            ("translation_cache_dir", self.translation_cache_dir),
        ):
            if value is not None and not isinstance(value, (str, Path)):
                raise EpubGraphConfigurationError(
                    f"{label} must be a path string, Path, or None"
                )
        if self.translation_thinking not in {"enabled", "disabled", "omit"}:
            raise EpubGraphConfigurationError(
                "translation_thinking must be enabled, disabled, or omit"
            )
        if type(self.translation_temperature) not in {int, float} or not (
            0.0 <= float(self.translation_temperature) <= 2.0
        ):
            raise EpubGraphConfigurationError(
                "translation_temperature must be between 0 and 2"
            )
        for label, value in (
            ("translation_max_chars", self.translation_max_chars),
            ("translation_concurrency", self.translation_concurrency),
            ("translation_retries", self.translation_retries),
        ):
            if type(value) is not int or value < 1:
                raise EpubGraphConfigurationError(f"{label} must be a positive integer")
        for label, value in (
            ("book_title", self.book_title),
            ("author", self.author),
            ("publication_language", self.publication_language),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise EpubGraphConfigurationError(f"{label} must be a non-empty string")
        object.__setattr__(self, "target_artifacts", targets)
        object.__setattr__(self, "force_nodes", force_nodes)
        object.__setattr__(
            self,
            "glossary",
            MappingProxyType(
                {
                    str(source).strip(): str(target).strip()
                    for source, target in self.glossary.items()
                }
            ),
        )


@dataclass(frozen=True)
class PreparedEpubGraph:
    """Prepared EPUB graph with exact source/input snapshots."""

    graph: PipelineGraph
    context: GraphContext
    targets: frozenset[str]
    options: EpubGraphOptions
    source_snapshot: _FileSnapshot
    translations_snapshot: _FileSnapshot | None = None

    def plan(self) -> tuple[NodeSpec, ...]:
        return self.graph.plan(available=self.context.values, targets=self.targets)

    def execute(self) -> GraphRunResult:
        self.source_snapshot.assert_current(label="EPUB source")
        if self.translations_snapshot is not None:
            self.translations_snapshot.assert_current(label="translation input")
        force: bool | Iterable[str]
        force = True if self.options.force_all else self.options.force_nodes
        return GraphExecutor(self.graph).execute(
            self.context,
            targets=self.targets,
            force=force,
        )


def _string_set(values: Iterable[str], *, label: str) -> frozenset[str]:
    result = frozenset(values)
    if not all(isinstance(value, str) and value.strip() for value in result):
        raise EpubGraphConfigurationError(
            f"{label} must contain non-empty strings"
        )
    return result


def _assert_regular_file(path: Path) -> None:
    try:
        entry = path.lstat()
    except OSError as exc:
        raise EpubGraphConfigurationError(f"file is missing or unreadable: {path}") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise EpubGraphConfigurationError(
            f"path must be a non-symlink regular file: {path}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_zip_member(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
    ):
        raise EpubGraphConfigurationError("EPUB ZIP contains an empty or invalid member")
    normalized = unicodedata.normalize("NFC", value)
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or (path.parts and path.parts[0].endswith(":"))
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise EpubGraphConfigurationError(
            f"EPUB ZIP contains an unsafe member path: {value!r}"
        )
    return path.as_posix().rstrip("/")


def _inspect_epub_archive(path: Path) -> tuple[int, int]:
    """Fail closed on ambiguous, encrypted, or bomb-like EPUB packages."""

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_EPUB_ENTRIES:
                raise EpubGraphConfigurationError(
                    f"EPUB ZIP entry count is outside the safe budget: {len(infos)}"
                )
            seen: set[str] = set()
            total = 0
            encryption_member: str | None = None
            for info in infos:
                normalized = _normalized_zip_member(info.filename)
                if normalized in seen:
                    raise EpubGraphConfigurationError(
                        f"EPUB ZIP contains duplicate normalized member: {normalized!r}"
                    )
                seen.add(normalized)
                member_mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(member_mode):
                    raise EpubGraphConfigurationError(
                        f"EPUB ZIP symlink member is unsupported: {normalized!r}"
                    )
                if info.flag_bits & 0x1:
                    raise EpubGraphConfigurationError(
                        f"encrypted EPUB ZIP member is unsupported: {normalized!r}"
                    )
                if info.file_size < 0 or info.file_size > MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES:
                    raise EpubGraphConfigurationError(
                        f"EPUB ZIP member exceeds the uncompressed budget: {normalized!r}"
                    )
                total += info.file_size
                if total > MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES:
                    raise EpubGraphConfigurationError(
                        "EPUB ZIP exceeds the aggregate uncompressed byte budget"
                    )
                if info.file_size > 1024 * 1024:
                    ratio = info.file_size / max(1, info.compress_size)
                    if ratio > MAX_EPUB_COMPRESSION_RATIO:
                        raise EpubGraphConfigurationError(
                            f"EPUB ZIP member has a suspicious compression ratio: {normalized!r}"
                        )
                if normalized.casefold() == "meta-inf/encryption.xml":
                    encryption_member = info.filename
            if encryption_member is not None:
                raw = archive.read(encryption_member)
                if b"EncryptedData" in raw or b"encrypteddata" in raw.lower():
                    raise EpubGraphConfigurationError(
                        "encrypted/DRM EPUB content is unsupported"
                    )
            return len(infos), total
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubGraphConfigurationError(f"input is not a valid EPUB ZIP: {path}") from exc


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.parent.is_symlink():
        raise EpubGraphConfigurationError(
            f"refusing symlinked EPUB graph output: {target}"
        )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_handle, os.fdopen(
            descriptor, "wb"
        ) as output_handle:
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_translation_cache_dir(
    output_dir: Path,
    configured: Path | str | None,
) -> Path:
    candidate = (
        output_dir / ".translation-cache"
        if configured is None
        else Path(configured).expanduser()
    )
    if not candidate.is_absolute():
        candidate = output_dir / candidate
    # Normalize lexical dot components without resolving symlinks.
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(output_dir)
    except ValueError as exc:
        raise EpubGraphConfigurationError(
            "translation cache must remain inside the managed output directory"
        ) from exc
    current = output_dir
    for part in relative.parts:
        current = current / part
        try:
            entry = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise EpubGraphConfigurationError(
                f"cannot inspect translation cache path: {current}"
            ) from exc
        if stat.S_ISLNK(entry.st_mode):
            raise EpubGraphConfigurationError(
                f"translation cache path must not contain a symlink: {current}"
            )
        if not stat.S_ISDIR(entry.st_mode):
            raise EpubGraphConfigurationError(
                f"translation cache path component is not a directory: {current}"
            )
    return candidate


def _file_artifact(path: Path) -> dict[str, Any]:
    _assert_regular_file(path)
    return {
        "path": str(path.resolve()),
        "sha256": _sha256_file(path),
        "size": path.stat().st_size,
    }


def _semantic_review_artifact(output_dir: Path) -> dict[str, Any]:
    try:
        artifact = validate_semantic_review(output_dir)
    except SemanticReviewError as exc:
        raise EpubSemanticBlockedError(
            f"semantic review evidence is missing, stale, or invalid: {exc}"
        ) from exc
    resolution = artifact.resolution
    file_artifact = _file_artifact(artifact.audit_path)
    if file_artifact["sha256"] != artifact.audit_sha256:
        raise EpubSemanticBlockedError(
            "semantic review audit changed while its graph artifact was built"
        )
    return {
        **file_artifact,
        "status": resolution.status,
        "release_blocked": resolution.release_blocked,
        "reconstruction_sha256": resolution.reconstruction_sha256,
        "decision_log_sha256": resolution.decision_log_sha256,
        "issue_set_sha256": resolution.issue_set_sha256,
        "policy_version": resolution.review_policy_version,
        "policy_fingerprint": resolution.review_policy_fingerprint,
    }


def _semantic_review_artifact_is_current(
    context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        current = _semantic_review_artifact(context.output_dir)
    except (OSError, ValueError, SemanticReviewError, EpubSemanticBlockedError):
        return False
    return all(value.get(key) == current.get(key) for key in current)


def _translation_units_artifact(path: Path) -> dict[str, Any]:
    artifact = _file_artifact(path)
    records = _read_canonical_translation_units(path)
    artifact.update({"schema_version": 1, "unit_count": len(records)})
    return artifact


def _file_artifact_is_current(
    _context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        candidate = Path(str(value["path"])).expanduser()
        _assert_regular_file(candidate)
        path = candidate.resolve()
        return (
            value.get("sha256") == _sha256_file(path)
            and value.get("size") == path.stat().st_size
        )
    except (KeyError, OSError, ValueError, EpubGraphConfigurationError):
        return False


def _read_canonical_translation_units(path: Path) -> tuple[TranslationUnit, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EpubGraphConfigurationError(
            f"semantic translation units are unreadable: {path}"
        ) from exc
    units: list[TranslationUnit] = []
    seen_ids: set[str] = set()
    closed_chapters: set[str] = set()
    current_chapter = ""
    expected_sequence = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            unit = TranslationUnit.from_dict(payload)
        except (json.JSONDecodeError, SemanticContractError, TypeError, ValueError) as exc:
            raise EpubGraphConfigurationError(
                f"invalid canonical translation unit at {path}:{line_number}: {exc}"
            ) from exc
        if unit.id in seen_ids:
            raise EpubGraphConfigurationError(
                f"duplicate canonical translation unit id: {unit.id!r}"
            )
        seen_ids.add(unit.id)
        if unit.chapter_id != current_chapter:
            if unit.chapter_id in closed_chapters:
                raise EpubGraphConfigurationError(
                    f"translation units are not chapter-contiguous: {unit.chapter_id!r}"
                )
            if current_chapter:
                closed_chapters.add(current_chapter)
            current_chapter = unit.chapter_id
            expected_sequence = 1
        else:
            expected_sequence += 1
        if unit.sequence != expected_sequence:
            raise EpubGraphConfigurationError(
                "translation unit sequence is not contiguous: "
                f"{unit.chapter_id!r} expected={expected_sequence} "
                f"actual={unit.sequence}"
            )
        units.append(unit)
    if not units:
        raise EpubGraphConfigurationError(
            f"semantic translation units are empty: {path}"
        )
    return tuple(units)


def _translation_units_artifact_is_current(
    context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    if not _file_artifact_is_current(context, value):
        return False
    try:
        path = Path(str(value["path"])).expanduser()
        units = _read_canonical_translation_units(path)
        return bool(
            value.get("schema_version") == 1
            and value.get("unit_count") == len(units)
        )
    except (KeyError, OSError, ValueError, EpubGraphConfigurationError):
        return False


def _translation_units_match_bundle(
    semantic: Mapping[str, Any],
    units_artifact: Mapping[str, Any],
) -> bool:
    try:
        manifest_path = Path(str(semantic["manifest"])).expanduser()
        chapter_dir = Path(str(semantic["chapter_dir"])).expanduser()
        units_path = Path(str(units_artifact["path"])).expanduser()
        manifest = _safe_manifest(manifest_path)
        units = _read_canonical_translation_units(units_path)
        by_chapter: dict[str, list[TranslationUnit]] = {}
        for unit in units:
            by_chapter.setdefault(unit.chapter_id, []).append(unit)
        manifest_ids = [str(item.get("id") or "") for item in manifest]
        if list(by_chapter) != manifest_ids:
            return False
        for item in manifest:
            chapter_id = str(item["id"])
            source_href = str(item.get("source_href") or "")
            filename = str(item["filename"])
            chapter_units = by_chapter[chapter_id]
            if not source_href:
                return False
            for unit in chapter_units:
                if len(unit.locators) != 1:
                    return False
                locator = unit.locators[0]
                if (
                    locator.adapter != "epub"
                    or locator.source != source_href
                    or locator.href != source_href
                    or locator.block_index != unit.sequence - 1
                ):
                    return False
            reconstructed = "\n\n".join(
                unit.source_markdown for unit in chapter_units
            ).rstrip() + "\n"
            if reconstructed != (chapter_dir / filename).read_text(encoding="utf-8"):
                return False
        return True
    except (
        KeyError,
        OSError,
        UnicodeError,
        ValueError,
        EpubGraphConfigurationError,
    ):
        return False


def _semantic_import_outputs_are_current(
    context: GraphContext,
    outputs: Mapping[str, Any],
) -> bool:
    semantic = outputs.get(ART_SEMANTIC_CHAPTERS)
    units = outputs.get(ART_TRANSLATION_UNITS)
    return bool(
        set(outputs) == {ART_SEMANTIC_CHAPTERS, ART_TRANSLATION_UNITS}
        and isinstance(semantic, Mapping)
        and isinstance(units, Mapping)
        and _bundle_is_current(context, semantic)
        and _translation_units_artifact_is_current(context, units)
        and _translation_units_match_bundle(semantic, units)
    )


def _epub_source_artifact_is_current(
    context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    if not _file_artifact_is_current(context, value):
        return False
    try:
        path = Path(str(value["path"])).expanduser().resolve()
        entry_count, uncompressed_size = _inspect_epub_archive(path)
        return bool(
            value.get("adapter") == EPUB_GRAPH_ADAPTER_VERSION
            and value.get("adapter_identity") == EPUB_GRAPH_ADAPTER_VERSION
            and value.get("entry_count") == entry_count
            and value.get("uncompressed_size") == uncompressed_size
        )
    except (KeyError, OSError, ValueError, EpubGraphConfigurationError):
        return False


def _single_output_is_current(
    artifact_name: str,
    validator: Callable[[GraphContext, Mapping[str, Any]], bool],
) -> Callable[[GraphContext, Mapping[str, Any]], bool]:
    def validate(context: GraphContext, outputs: Mapping[str, Any]) -> bool:
        return bool(
            isinstance(outputs, Mapping)
            and set(outputs) == {artifact_name}
            and isinstance(outputs.get(artifact_name), Mapping)
            and validator(context, outputs[artifact_name])
        )

    return validate


def _safe_manifest(manifest_path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EpubGraphConfigurationError(
            f"invalid EPUB semantic manifest: {manifest_path}"
        ) from exc
    if not isinstance(payload, list) or not payload:
        raise EpubGraphConfigurationError("EPUB semantic manifest must be non-empty")
    filenames: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise EpubGraphConfigurationError("EPUB semantic manifest entry is invalid")
        filename = str(item.get("filename") or "")
        candidate = Path(filename)
        if (
            not filename
            or candidate.is_absolute()
            or candidate.name != filename
            or filename in filenames
        ):
            raise EpubGraphConfigurationError(
                f"unsafe or duplicate EPUB chapter filename: {filename!r}"
            )
        filenames.add(filename)
    return payload


def _bundle_digest(
    manifest_path: Path,
    chapter_dir: Path,
    audit_paths: Iterable[Path],
) -> str:
    _assert_regular_file(manifest_path)
    manifest = _safe_manifest(manifest_path)
    if chapter_dir.is_symlink() or not chapter_dir.is_dir():
        raise EpubGraphConfigurationError(
            f"EPUB semantic chapter directory is missing or unsafe: {chapter_dir}"
        )
    files = [manifest_path]
    files.extend(chapter_dir / str(item["filename"]) for item in manifest)
    files.extend(audit_paths)
    digest = hashlib.sha256()
    for path in files:
        _assert_regular_file(path)
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _bundle_artifact(
    *,
    manifest_path: Path,
    chapter_dir: Path,
    semantic_audit: Path,
    review_audit: Path | None,
    translation_audit: Path | None,
    title: str,
    author: str,
    language: str,
    release_blocked: bool,
    translation_applied: bool,
    translation_input_sha256: str | None = None,
) -> dict[str, Any]:
    audits = [semantic_audit]
    if review_audit is not None:
        audits.append(review_audit)
    if translation_audit is not None:
        audits.append(translation_audit)
    content_sha256 = _bundle_digest(manifest_path, chapter_dir, audits)
    bundle_sha256 = (
        hashlib.sha256(
            f"{content_sha256}\0{translation_input_sha256}".encode("utf-8")
        ).hexdigest()
        if translation_input_sha256 is not None
        else content_sha256
    )
    return {
        "manifest": str(manifest_path.resolve()),
        "chapter_dir": str(chapter_dir.resolve()),
        "semantic_audit": str(semantic_audit.resolve()),
        "review_audit": (
            str(review_audit.resolve()) if review_audit is not None else None
        ),
        "translation_audit": (
            str(translation_audit.resolve()) if translation_audit is not None else None
        ),
        "content_sha256": content_sha256,
        "translation_input_sha256": translation_input_sha256,
        "sha256": bundle_sha256,
        "title": title,
        "author": author,
        "language": language,
        "release_blocked": release_blocked,
        "translation_applied": translation_applied,
    }


def _bundle_is_current(
    _context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    try:
        manifest = Path(str(value["manifest"])).expanduser()
        chapters = Path(str(value["chapter_dir"])).expanduser()
        semantic_audit = Path(str(value["semantic_audit"])).expanduser()
        review_value = value.get("review_audit")
        translation_value = value.get("translation_audit")
        audits = [semantic_audit]
        if review_value:
            audits.append(Path(str(review_value)).expanduser())
        if translation_value:
            audits.append(Path(str(translation_value)).expanduser())
        content_sha256 = _bundle_digest(manifest, chapters, audits)
        translation_input_sha256 = value.get("translation_input_sha256")
        expected_sha256 = (
            hashlib.sha256(
                f"{content_sha256}\0{translation_input_sha256}".encode("utf-8")
            ).hexdigest()
            if translation_input_sha256 is not None
            else content_sha256
        )
        return bool(
            value.get("content_sha256") == content_sha256
            and value.get("sha256") == expected_sha256
        )
    except (KeyError, OSError, ValueError, EpubGraphConfigurationError):
        return False


def _source_reader_is_current(
    context: GraphContext,
    value: Mapping[str, Any],
) -> bool:
    translation_audit = context.output_dir / "audit" / "semantic-translation.json"
    translation_state_exists = (
        translation_audit.exists() or translation_audit.is_symlink()
    )
    return _bundle_is_current(context, value) and not translation_state_exists


def _source_handler(snapshot: _FileSnapshot) -> Callable[[GraphContext], NodeResult]:
    def handler(_context: GraphContext) -> NodeResult:
        snapshot.assert_current(label="EPUB source")
        artifact = {
            **_file_artifact(snapshot.path),
            "adapter": EPUB_GRAPH_ADAPTER_VERSION,
            "adapter_identity": EPUB_GRAPH_ADAPTER_VERSION,
            "entry_count": snapshot.entry_count,
            "uncompressed_size": snapshot.uncompressed_size,
        }
        return NodeResult(
            outputs={ART_SOURCE: artifact},
            fingerprints={ART_SOURCE: snapshot.sha256},
        )

    return handler


def _remove_generated_translation_state(output_dir: Path) -> None:
    for path in (
        output_dir / "audit" / "semantic-translation.json",
        output_dir / "semantic" / "translations.jsonl",
    ):
        if path.is_symlink():
            raise EpubGraphConfigurationError(
                f"refusing symlinked stale EPUB translation artifact: {path}"
            )
        if path.exists():
            _assert_regular_file(path)
            path.unlink()


def _import_handler(context: GraphContext) -> NodeResult:
    source_artifact = context.require(ART_SOURCE)
    if not _epub_source_artifact_is_current(context, source_artifact):
        raise EpubSourceChangedError("source.epub no longer matches its artifact")
    source = Path(str(source_artifact["path"])).resolve()
    result = import_epub(source, context.output_dir)
    if not _epub_source_artifact_is_current(context, source_artifact):
        raise EpubSourceChangedError(
            "EPUB source changed while semantic import was running"
        )
    semantic_audit = context.output_dir / "audit" / "semantic-reconstruction.json"
    try:
        audit_payload = json.loads(semantic_audit.read_text(encoding="utf-8"))
        audited_source = audit_payload["source"]
        audited_path = Path(str(audited_source["path"])).expanduser().resolve()
        audited_sha256 = str(audited_source["sha256"])
    except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise EpubSourceChangedError(
            "EPUB semantic reconstruction audit has no valid source binding"
        ) from exc
    if (
        audited_path != source
        or audited_sha256 != str(source_artifact["sha256"])
    ):
        raise EpubSourceChangedError(
            "EPUB semantic reconstruction audit does not bind source.epub"
        )
    _remove_generated_translation_state(context.output_dir)
    source_manifest = context.output_dir / "semantic" / "source-manifest.json"
    _atomic_copy(context.output_dir / "chapters.json", source_manifest)
    bundle = _bundle_artifact(
        manifest_path=source_manifest,
        chapter_dir=context.output_dir / "semantic" / "source_chapters",
        semantic_audit=semantic_audit,
        review_audit=None,
        translation_audit=None,
        title=str(result.get("title") or source.stem),
        author=str(result.get("author") or ""),
        language=str(result.get("language") or "und"),
        release_blocked=bool(result.get("release_blocked")),
        translation_applied=False,
    )
    units = _translation_units_artifact(
        context.output_dir / "semantic" / "translation-units.jsonl"
    )
    if not _translation_units_match_bundle(bundle, units):
        raise EpubGraphConfigurationError(
            "EPUB translation units do not reconstruct their semantic chapters"
        )
    return NodeResult(
        outputs={
            ART_SEMANTIC_CHAPTERS: bundle,
            ART_TRANSLATION_UNITS: units,
        },
        fingerprints={
            ART_SEMANTIC_CHAPTERS: str(bundle["sha256"]),
            ART_TRANSLATION_UNITS: str(units["sha256"]),
        },
        metadata={
            "status": str(result.get("status") or ""),
            "chapter_count": int(result.get("chapter_count") or 0),
            "translation_unit_count": int(result.get("translation_unit_count") or 0),
            "release_blocked": bool(result.get("release_blocked")),
        },
    )


def _review_handler(context: GraphContext) -> NodeResult:
    semantic = context.require(ART_SEMANTIC_CHAPTERS)
    if not _bundle_is_current(context, semantic):
        raise EpubSourceChangedError(
            "chapters.semantic no longer matches its artifact"
        )
    semantic_audit = Path(str(semantic["semantic_audit"])).resolve()
    reconstruction_sha256 = _sha256_file(semantic_audit)
    try:
        resolved = validate_semantic_review(
            context.output_dir,
            expected_reconstruction_sha256=reconstruction_sha256,
        )
    except SemanticReviewError as exc:
        raise EpubSemanticBlockedError(
            f"semantic review evidence is missing, stale, or invalid: {exc}"
        ) from exc
    artifact = _semantic_review_artifact(context.output_dir)
    return NodeResult(
        outputs={ART_REVIEW: artifact},
        fingerprints={ART_REVIEW: str(artifact["sha256"])},
        metadata={
            "status": resolved.resolution.status,
            "release_blocked": resolved.resolution.release_blocked,
            "issue_count": len(resolved.resolution.issues),
        },
    )


def _translations_inspect_handler(
    snapshot: _FileSnapshot,
) -> Callable[[GraphContext], NodeResult]:
    def handler(_context: GraphContext) -> NodeResult:
        snapshot.assert_current(label="translation input")
        artifact = _file_artifact(snapshot.path)
        return NodeResult(
            outputs={ART_TRANSLATIONS: artifact},
            fingerprints={ART_TRANSLATIONS: snapshot.sha256},
        )

    return handler


def _translation_fingerprint(options: EpubGraphOptions) -> dict[str, Any]:
    return {
        "runner": RUNNER_VERSION,
        "prompt_contract_sha256": PROMPT_CONTRACT_SHA256,
        "request": options.translation_request_fingerprint,
        "provider": options.translation_provider,
        "base_url": options.translation_base_url.rstrip("/"),
        "model": options.translation_model,
        "prompt_profile": options.translation_prompt_profile,
        "thinking": options.translation_thinking,
        "temperature": float(options.translation_temperature),
        "target_language": options.target_language,
        "glossary": dict(options.glossary),
        "max_chars": options.translation_max_chars,
    }


def _translate_handler(
    options: EpubGraphOptions,
    prepared_cache_dir: Path,
) -> Callable[[GraphContext], NodeResult]:
    request = options.translation_request
    assert request is not None

    def handler(context: GraphContext) -> NodeResult:
        units_artifact = context.require(ART_TRANSLATION_UNITS)
        if not _translation_units_artifact_is_current(context, units_artifact):
            raise EpubSourceChangedError(
                "semantic.translation_units no longer matches its artifact"
            )
        units_path = Path(str(units_artifact["path"])).resolve()
        output_path = context.output_dir / "semantic" / "translations.jsonl"
        cache_dir = _secure_translation_cache_dir(
            context.output_dir,
            prepared_cache_dir,
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = _secure_translation_cache_dir(context.output_dir, cache_dir)
        result = translate_units(
            units_path,
            output_path,
            target_language=options.target_language,
            glossary=dict(options.glossary),
            model=options.translation_model,
            provider=options.translation_provider,
            base_url=options.translation_base_url,
            prompt_profile=options.translation_prompt_profile,
            thinking=options.translation_thinking,
            temperature=float(options.translation_temperature),
            cache_dir=cache_dir,
            request=request,
            max_chars=options.translation_max_chars,
            concurrency=options.translation_concurrency,
            retries=options.translation_retries,
        )
        if result.get("status") != "passed" or result.get("mode") != "run":
            raise RuntimeError("semantic translation runner did not produce translations")
        artifact = _file_artifact(output_path)
        return NodeResult(
            outputs={ART_TRANSLATIONS: artifact},
            fingerprints={ART_TRANSLATIONS: str(artifact["sha256"])},
            metadata={
                "unit_count": int(result.get("unit_count") or 0),
                "batch_count": int(result.get("batch_count") or 0),
                "cache_hits": int(result.get("cache_hits") or 0),
            },
        )

    return handler


def _assert_publishable(
    bundle: Mapping[str, Any],
    review: Mapping[str, Any] | None = None,
) -> None:
    if review is not None:
        if (
            review.get("status") != "passed"
            or review.get("release_blocked") is not False
        ):
            raise EpubSemanticBlockedError(
                "EPUB semantic review is unresolved or blocked"
            )
        semantic_audit = Path(str(bundle["semantic_audit"])).resolve()
        if review.get("reconstruction_sha256") != _sha256_file(semantic_audit):
            raise EpubSemanticBlockedError(
                "EPUB semantic review is bound to different reconstruction bytes"
            )
        return
    if bundle.get("release_blocked") is not False:
        raise EpubSemanticBlockedError(
            "EPUB semantic reconstruction is blocked; review its audit"
        )


def _materialize_bundle(
    output_dir: Path,
    bundle: Mapping[str, Any],
    *,
    clear_translation_audit: bool,
) -> None:
    if not _bundle_is_current(GraphContext(output_dir), bundle):
        raise EpubSourceChangedError("semantic chapter bundle no longer matches its digest")
    source_manifest = Path(str(bundle["manifest"])).resolve()
    source_dir = Path(str(bundle["chapter_dir"])).resolve()
    manifest = _safe_manifest(source_manifest)
    target_dir = output_dir / "chapters"
    if target_dir.is_symlink():
        raise EpubGraphConfigurationError(
            f"refusing symlinked reader chapter directory: {target_dir}"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    expected: set[str] = set()
    for item in manifest:
        filename = str(item["filename"])
        expected.add(filename)
        _atomic_copy(source_dir / filename, target_dir / filename)
    for stale in target_dir.iterdir():
        if stale.name in expected:
            continue
        if stale.is_symlink() or not stale.is_file():
            raise EpubGraphConfigurationError(
                f"unsafe stale reader artifact: {stale}"
            )
        if stale.suffix.lower() == ".md":
            stale.unlink()
    _atomic_copy(source_manifest, output_dir / "chapters.json")
    if clear_translation_audit:
        audit = output_dir / "audit" / "semantic-translation.json"
        if audit.is_symlink():
            raise EpubGraphConfigurationError(
                f"refusing symlinked semantic translation audit: {audit}"
            )
        if audit.exists():
            _assert_regular_file(audit)
            audit.unlink()


def _source_reader_handler(context: GraphContext) -> NodeResult:
    semantic = context.require(ART_SEMANTIC_CHAPTERS)
    review = context.require(ART_REVIEW)
    _assert_publishable(semantic, review)
    _materialize_bundle(
        context.output_dir,
        semantic,
        clear_translation_audit=True,
    )
    reader = _bundle_artifact(
        manifest_path=context.output_dir / "chapters.json",
        chapter_dir=context.output_dir / "chapters",
        semantic_audit=Path(str(semantic["semantic_audit"])).resolve(),
        review_audit=Path(str(review["path"])).resolve(),
        translation_audit=None,
        title=str(semantic["title"]),
        author=str(semantic["author"]),
        language=str(semantic["language"]),
        release_blocked=False,
        translation_applied=False,
    )
    return NodeResult(
        outputs={ART_READER_CHAPTERS: reader},
        fingerprints={ART_READER_CHAPTERS: str(reader["sha256"])},
        metadata={"translation_applied": False},
    )


def _apply_handler(
    options: EpubGraphOptions,
) -> Callable[[GraphContext], NodeResult]:
    def handler(context: GraphContext) -> NodeResult:
        semantic = context.require(ART_SEMANTIC_CHAPTERS)
        translations = context.require(ART_TRANSLATIONS)
        review = context.require(ART_REVIEW)
        _assert_publishable(semantic, review)
        if not _file_artifact_is_current(context, translations):
            raise EpubSourceChangedError(
                "semantic.translations no longer matches its artifact"
            )
        # Always start from the immutable imported reader bytes.  This prevents
        # a prior translated canonical bundle from influencing a later apply.
        _materialize_bundle(
            context.output_dir,
            semantic,
            clear_translation_audit=True,
        )
        result = apply_translations(
            context.output_dir,
            Path(str(translations["path"])).resolve(),
            target_language=options.target_language,
            glossary=dict(options.glossary),
        )
        if result.get("release_blocked") is True or result.get("status") != "passed":
            raise EpubSemanticBlockedError(
                "translated EPUB reader bundle failed semantic validation"
            )
        if not _file_artifact_is_current(context, translations):
            raise EpubSourceChangedError(
                "semantic.translations changed while translations were applied"
            )
        translation_input_sha256 = str(translations["sha256"])
        if result.get("translation_input_sha256") != translation_input_sha256:
            raise EpubSourceChangedError(
                "semantic translation audit does not bind semantic.translations"
            )
        translation_audit = context.output_dir / "audit" / "semantic-translation.json"
        try:
            translation_audit_payload = json.loads(
                translation_audit.read_text(encoding="utf-8")
            )
            audited_translation_sha256 = str(
                translation_audit_payload["translation_input"]["sha256"]
            )
            reviewed = translation_audit_payload["review_resolution"]
            effective_bundle = translation_audit_payload[
                "effective_semantic_bundle"
            ]
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise EpubSourceChangedError(
                "semantic translation audit has incomplete provenance"
            ) from exc
        if audited_translation_sha256 != translation_input_sha256:
            raise EpubSourceChangedError(
                "semantic translation audit binds different translation bytes"
            )
        review_audit_identity = reviewed.get("audit") if isinstance(reviewed, Mapping) else None
        review_upstream = reviewed.get("upstream_reconstruction") if isinstance(reviewed, Mapping) else None
        review_log = reviewed.get("decision_log") if isinstance(reviewed, Mapping) else None
        review_policy = reviewed.get("review_policy") if isinstance(reviewed, Mapping) else None
        review_issue_set = reviewed.get("issue_set") if isinstance(reviewed, Mapping) else None
        if (
            not isinstance(review_audit_identity, Mapping)
            or review_audit_identity.get("sha256") != review.get("sha256")
            or not isinstance(review_upstream, Mapping)
            or review_upstream.get("sha256") != review.get("reconstruction_sha256")
            or not isinstance(review_log, Mapping)
            or review_log.get("sha256") != review.get("decision_log_sha256")
            or not isinstance(review_policy, Mapping)
            or review_policy.get("fingerprint") != review.get("policy_fingerprint")
            or not isinstance(review_issue_set, Mapping)
            or review_issue_set.get("sha256") != review.get("issue_set_sha256")
        ):
            raise EpubSourceChangedError(
                "semantic translation audit binds different review evidence"
            )
        if (
            not isinstance(effective_bundle, Mapping)
            or effective_bundle.get("status") != "passed"
            or effective_bundle.get("review_audit_sha256")
            != review.get("sha256")
            or effective_bundle.get("decision_log_sha256")
            != review.get("decision_log_sha256")
            or effective_bundle.get("review_policy_fingerprint")
            != review.get("policy_fingerprint")
            or effective_bundle.get("issue_set_sha256")
            != review.get("issue_set_sha256")
        ):
            raise EpubSourceChangedError(
                "semantic translation audit has a stale effective semantic bundle"
            )
        reader = _bundle_artifact(
            manifest_path=context.output_dir / "chapters.json",
            chapter_dir=context.output_dir / "chapters",
            semantic_audit=Path(str(semantic["semantic_audit"])).resolve(),
            review_audit=Path(str(review["path"])).resolve(),
            translation_audit=translation_audit,
            title=str(semantic["title"]),
            author=str(semantic["author"]),
            language=str(semantic["language"]),
            release_blocked=False,
            translation_applied=True,
            translation_input_sha256=translation_input_sha256,
        )
        return NodeResult(
            outputs={ART_READER_CHAPTERS: reader},
            fingerprints={ART_READER_CHAPTERS: str(reader["sha256"])},
            metadata={
                "translation_applied": True,
                "chapter_count": int(result.get("chapter_count") or 0),
            },
        )

    return handler


def _publication_metadata(
    bundle: Mapping[str, Any],
    options: EpubGraphOptions,
) -> tuple[str, str, str]:
    title = options.book_title or str(bundle.get("title") or "Untitled")
    author = options.author if options.author is not None else str(bundle.get("author") or "")
    source_language = str(bundle.get("language") or "und")
    language = options.publication_language or (
        "zh-CN"
        if bundle.get("translation_applied") and options.target_language == "简体中文"
        else (options.target_language if bundle.get("translation_applied") else source_language)
    )
    return title, author, normalize_epub_language(language)


def _publication_path(output_dir: Path, title: str, suffix: str) -> Path:
    filename = f"{legacy.slugify(title)}{suffix}"
    candidate = output_dir / filename
    if candidate.is_symlink():
        raise EpubGraphConfigurationError(
            f"refusing symlinked EPUB publication target: {candidate}"
        )
    target = candidate.resolve()
    try:
        target.relative_to(output_dir)
    except ValueError as exc:
        raise EpubGraphConfigurationError(
            f"EPUB publication path escapes output directory: {target}"
        ) from exc
    return target


def _publisher_handler(
    artifact_name: str,
    options: EpubGraphOptions,
) -> Callable[[GraphContext], NodeResult]:
    def handler(context: GraphContext) -> NodeResult:
        bundle = context.require(ART_READER_CHAPTERS)
        _assert_publishable(bundle)
        if not _bundle_is_current(context, bundle):
            raise EpubSourceChangedError("reader chapter bundle no longer matches its digest")
        manifest_path = Path(str(bundle["manifest"])).resolve()
        chapter_dir = Path(str(bundle["chapter_dir"])).resolve()
        manifest = _safe_manifest(manifest_path)
        title, author, language = _publication_metadata(bundle, options)
        if artifact_name == ART_EPUB:
            path = _publication_path(context.output_dir, title, ".epub")
            legacy.build_epub(
                path,
                chapter_dir,
                manifest,
                book_title=title,
                language=language,
            )
        elif artifact_name == ART_DOCX:
            path = _publication_path(context.output_dir, title, ".docx")
            legacy.build_docx(
                path,
                chapter_dir,
                manifest,
                book_title=title,
                author=author or None,
            )
        else:  # pragma: no cover - construction prevents this.
            raise AssertionError(artifact_name)
        artifact = _file_artifact(path)
        return NodeResult(
            outputs={artifact_name: artifact},
            fingerprints={artifact_name: str(artifact["sha256"])},
            metadata={"title": title, "language": language},
        )

    return handler


def register_epub_verifier(graph: PipelineGraph, node: NodeSpec) -> PipelineGraph:
    """Register a native EPUB release gate with a strict public contract.

    The verifier implementation is intentionally replaceable.  It must bind
    source identity, the exact reader bundle, and the emitted EPUB rather than
    validating an arbitrary file discovered by glob.
    """

    required = {ART_SOURCE, ART_REVIEW, ART_READER_CHAPTERS, ART_EPUB}
    if node.name != NODE_VERIFY_EPUB:
        raise EpubGraphConfigurationError(
            f"EPUB verifier node must be named {NODE_VERIFY_EPUB!r}"
        )
    if set(node.provides) != {ART_EPUB_REPORT}:
        raise EpubGraphConfigurationError(
            f"EPUB verifier must provide only {ART_EPUB_REPORT!r}"
        )
    if not required.issubset(node.requires):
        raise EpubGraphConfigurationError(
            "EPUB verifier must require source.epub, semantic.review, "
            "chapters.reader, and publication.epub"
        )
    graph.add(node)
    return graph


def _epub_verify_handler(
    options: EpubGraphOptions,
) -> Callable[[GraphContext], NodeResult]:
    def handler(context: GraphContext) -> NodeResult:
        source = context.require(ART_SOURCE)
        review = context.require(ART_REVIEW)
        reader = context.require(ART_READER_CHAPTERS)
        publication = context.require(ART_EPUB)
        translations = (
            context.require(ART_TRANSLATIONS)
            if options.translation_mode != "none"
            else None
        )
        if not _epub_source_artifact_is_current(context, source):
            raise EpubSourceChangedError("source.epub no longer matches its artifact")
        if not _semantic_review_artifact_is_current(context, review):
            raise EpubSourceChangedError(
                "semantic.review no longer matches its artifact"
            )
        if review.get("status") != "passed" or review.get("release_blocked") is not False:
            raise EpubSemanticBlockedError(
                "semantic review is not release-ready"
            )
        if not _bundle_is_current(context, reader):
            raise EpubSourceChangedError(
                "chapters.reader no longer matches its artifact"
            )
        if not _file_artifact_is_current(context, publication):
            raise EpubSourceChangedError(
                "publication.epub no longer matches its artifact"
            )
        if translations is not None:
            if not _file_artifact_is_current(context, translations):
                raise EpubSourceChangedError(
                    "semantic.translations no longer matches its artifact"
                )
            if reader.get("translation_input_sha256") != translations.get("sha256"):
                raise EpubSourceChangedError(
                    "chapters.reader is not bound to semantic.translations"
                )
        _title, _author, publication_language = _publication_metadata(reader, options)
        report_path = context.output_dir / "audit" / "epub-release-report.json"
        report = verify_epub_publication(
            context.output_dir,
            source_epub=Path(str(source["path"])).resolve(),
            artifact_path=Path(str(publication["path"])).resolve(),
            target_language=publication_language,
            require_translation=options.translation_mode != "none",
            expected_translation_sha256=(
                str(translations["sha256"])
                if translations is not None
                else None
            ),
            report_path=report_path,
        )
        if report.get("release_ready") is not True:
            raise EpubSemanticBlockedError(
                "EPUB native publication verification failed; review "
                f"{report_path}"
            )
        artifact = _file_artifact(report_path)
        return NodeResult(
            outputs={ART_EPUB_REPORT: artifact},
            fingerprints={ART_EPUB_REPORT: str(artifact["sha256"])},
            metadata={
                "release_ready": True,
                "publication_profile": "epub",
                "mode": "full",
            },
        )

    return handler


def epub_verifier_node(options: EpubGraphOptions) -> NodeSpec:
    """Build the deterministic, non-cacheable native EPUB release gate."""

    requirements = {ART_SOURCE, ART_REVIEW, ART_READER_CHAPTERS, ART_EPUB}
    if options.translation_mode != "none":
        requirements.add(ART_TRANSLATIONS)
    return NodeSpec(
        name=NODE_VERIFY_EPUB,
        handler=_epub_verify_handler(options),
        requires=frozenset(requirements),
        provides=frozenset({ART_EPUB_REPORT}),
        version="1",
        fingerprint=stable_fingerprint(
            {
                "verifier": VERIFIER_VERSION,
                "translation_required": options.translation_mode != "none",
                "target_language": options.target_language,
                "publication_language": options.publication_language,
            }
        ),
        cache=False,
        description="Run the deterministic EPUB-native full publication gate.",
    )


def prepare_epub_graph(
    source: Path | str,
    output_dir: Path | str,
    *,
    options: EpubGraphOptions | None = None,
    verifier_node: NodeSpec | None = None,
    redaction_values: Iterable[str] = (),
) -> PreparedEpubGraph:
    """Construct an EPUB graph without executing or loading environment files."""

    options = options or EpubGraphOptions()
    output = Path(output_dir).expanduser().resolve()
    source_snapshot = _FileSnapshot.capture(source, suffix=".epub")
    try:
        source_snapshot.path.relative_to(output)
    except ValueError:
        pass
    else:
        raise EpubGraphConfigurationError(
            "EPUB source must remain outside the managed output directory"
        )
    translations_snapshot = (
        _FileSnapshot.capture(options.translations_path)
        if options.translation_mode == "apply"
        and options.translations_path is not None
        else None
    )
    if translations_snapshot is not None:
        try:
            translations_snapshot.path.relative_to(output)
        except ValueError:
            pass
        else:
            raise EpubGraphConfigurationError(
                "external translations must remain outside the managed output directory"
            )
    translation_cache_dir = (
        _secure_translation_cache_dir(output, options.translation_cache_dir)
        if options.translation_mode == "run"
        else None
    )
    graph = PipelineGraph()
    graph.add(
        NodeSpec(
            name=NODE_SOURCE,
            handler=_source_handler(source_snapshot),
            provides=frozenset({ART_SOURCE}),
            version="1",
            fingerprint=stable_fingerprint({
                "adapter": EPUB_GRAPH_ADAPTER_VERSION,
                "adapter_identity": EPUB_GRAPH_ADAPTER_VERSION,
                "path": str(source_snapshot.path),
                "sha256": source_snapshot.sha256,
                "size": source_snapshot.size,
                "entry_count": source_snapshot.entry_count,
                "uncompressed_size": source_snapshot.uncompressed_size,
            }),
            cache_validator=_single_output_is_current(
                ART_SOURCE, _epub_source_artifact_is_current
            ),
            description="Bind the exact non-symlink EPUB source bytes.",
        )
    )
    graph.add(
        NodeSpec(
            name=NODE_IMPORT,
            handler=_import_handler,
            requires=frozenset({ART_SOURCE}),
            provides=frozenset({ART_SEMANTIC_CHAPTERS, ART_TRANSLATION_UNITS}),
            version="1",
            fingerprint=stable_fingerprint({
                "adapter": EPUB_GRAPH_ADAPTER_VERSION,
                "importer": IMPORTER_VERSION,
            }),
            cache_validator=_semantic_import_outputs_are_current,
            description="Import the EPUB spine into immutable semantic chapters and units.",
        )
    )
    graph.add(
        NodeSpec(
            name=NODE_REVIEW,
            handler=_review_handler,
            requires=frozenset({ART_SEMANTIC_CHAPTERS}),
            provides=frozenset({ART_REVIEW}),
            version="1",
            fingerprint=stable_fingerprint(
                {
                    "policy": REVIEW_POLICY_VERSION,
                    "contract": "raw-reconstruction+append-only-decisions",
                }
            ),
            cache_validator=_single_output_is_current(
                ART_REVIEW, _semantic_review_artifact_is_current
            ),
            description=(
                "Resolve the complete reconstruction issue set through the "
                "hash-bound append-only review policy."
            ),
        )
    )

    if options.translation_mode == "run":
        graph.add(
            NodeSpec(
                name=NODE_TRANSLATE,
                handler=_translate_handler(options, translation_cache_dir),
                requires=frozenset({ART_TRANSLATION_UNITS}),
                provides=frozenset({ART_TRANSLATIONS}),
                version="1",
                fingerprint=stable_fingerprint(_translation_fingerprint(options)),
                cache_validator=_single_output_is_current(
                    ART_TRANSLATIONS, _file_artifact_is_current
                ),
                description=(
                    "Translate hash-bound semantic units through an explicit "
                    "request callback."
                ),
            )
        )
    elif options.translation_mode == "apply":
        assert translations_snapshot is not None
        graph.add(
            NodeSpec(
                name=NODE_TRANSLATIONS_IMPORT,
                handler=_translations_inspect_handler(translations_snapshot),
                provides=frozenset({ART_TRANSLATIONS}),
                version="1",
                fingerprint=stable_fingerprint({
                    "path": str(translations_snapshot.path),
                    "sha256": translations_snapshot.sha256,
                    "size": translations_snapshot.size,
                }),
                cache_validator=_single_output_is_current(
                    ART_TRANSLATIONS, _file_artifact_is_current
                ),
                description="Bind an externally supplied translation JSONL file.",
            )
        )

    if options.translation_mode == "none":
        graph.add(
            NodeSpec(
                name=NODE_READER,
                handler=_source_reader_handler,
                requires=frozenset({ART_SEMANTIC_CHAPTERS, ART_REVIEW}),
                provides=frozenset({ART_READER_CHAPTERS}),
                version="2",
                fingerprint=stable_fingerprint(
                    {"mode": "source", "adapter": EPUB_GRAPH_ADAPTER_VERSION}
                ),
                cache_validator=_single_output_is_current(
                    ART_READER_CHAPTERS, _source_reader_is_current
                ),
                description="Materialize and validate the source-language reader bundle.",
            )
        )
    else:
        graph.add(
            NodeSpec(
                name=NODE_APPLY,
                handler=_apply_handler(options),
                requires=frozenset(
                    {ART_SEMANTIC_CHAPTERS, ART_TRANSLATIONS, ART_REVIEW}
                ),
                provides=frozenset({ART_READER_CHAPTERS}),
                version="2",
                fingerprint=stable_fingerprint({
                    "target_language": options.target_language,
                    "glossary": dict(options.glossary),
                    "contract": "epub-spine-translated-markdown-footnotes",
                }),
                cache_validator=_single_output_is_current(
                    ART_READER_CHAPTERS, _bundle_is_current
                ),
                description="Apply a complete translation set to the canonical reader bundle.",
            )
        )

    publication_fingerprint = {
        "adapter": EPUB_GRAPH_ADAPTER_VERSION,
        "title": options.book_title,
        "author": options.author,
        "language": options.publication_language,
        "target_language": options.target_language,
    }
    graph.add(
        NodeSpec(
            name=NODE_EPUB,
            handler=_publisher_handler(ART_EPUB, options),
            requires=frozenset({ART_READER_CHAPTERS}),
            provides=frozenset({ART_EPUB}),
            version="1",
            fingerprint=stable_fingerprint(
                {**publication_fingerprint, "publisher": "epub-v2"}
            ),
            cache_validator=_single_output_is_current(
                ART_EPUB, _file_artifact_is_current
            ),
            description="Publish EPUB from the exact materialized reader bundle.",
        )
    )
    graph.add(
        NodeSpec(
            name=NODE_DOCX,
            handler=_publisher_handler(ART_DOCX, options),
            requires=frozenset({ART_READER_CHAPTERS}),
            provides=frozenset({ART_DOCX}),
            version="1",
            fingerprint=stable_fingerprint(
                {**publication_fingerprint, "publisher": "docx-v1"}
            ),
            cache_validator=_single_output_is_current(
                ART_DOCX, _file_artifact_is_current
            ),
            description="Publish Word from the exact materialized reader bundle.",
        )
    )
    if verifier_node is not None:
        if (
            options.translation_mode != "none"
            and ART_TRANSLATIONS not in verifier_node.requires
        ):
            raise EpubGraphConfigurationError(
                "translated EPUB verifier must require semantic.translations"
            )
        register_epub_verifier(graph, verifier_node)
    elif ART_EPUB_REPORT in options.target_artifacts:
        register_epub_verifier(graph, epub_verifier_node(options))

    node_names = {node.name for node in graph.nodes}
    unknown_forced = options.force_nodes - node_names
    if unknown_forced:
        raise EpubGraphConfigurationError(
            f"unknown forced EPUB graph nodes: {sorted(unknown_forced)}"
        )
    context = GraphContext(
        output,
        config={
            "source_mode": "epub",
            "adapter_version": EPUB_GRAPH_ADAPTER_VERSION,
            "translation_mode": options.translation_mode,
            "target_language": options.target_language,
        },
        redaction_values=tuple(redaction_values),
        value_validators={
            ART_SOURCE: _epub_source_artifact_is_current,
            ART_SEMANTIC_CHAPTERS: _bundle_is_current,
            ART_TRANSLATION_UNITS: _translation_units_artifact_is_current,
            ART_TRANSLATIONS: _file_artifact_is_current,
            ART_REVIEW: _semantic_review_artifact_is_current,
            ART_READER_CHAPTERS: _bundle_is_current,
            ART_EPUB: _file_artifact_is_current,
            ART_DOCX: _file_artifact_is_current,
            ART_EPUB_REPORT: _file_artifact_is_current,
        },
    )
    # Planning here catches invalid mode/target combinations before any output
    # directory or graph state is created.
    graph.plan(available=context.values, targets=options.target_artifacts)
    return PreparedEpubGraph(
        graph=graph,
        context=context,
        targets=options.target_artifacts,
        options=options,
        source_snapshot=source_snapshot,
        translations_snapshot=translations_snapshot,
    )


def run_epub_graph(
    source: Path | str,
    output_dir: Path | str,
    *,
    options: EpubGraphOptions | None = None,
    verifier_node: NodeSpec | None = None,
    redaction_values: Iterable[str] = (),
) -> GraphRunResult:
    """Prepare and execute the first-class born-digital EPUB graph."""

    return prepare_epub_graph(
        source,
        output_dir,
        options=options,
        verifier_node=verifier_node,
        redaction_values=redaction_values,
    ).execute()


__all__ = [
    "ART_DOCX",
    "ART_EPUB",
    "ART_EPUB_REPORT",
    "ART_READER_CHAPTERS",
    "ART_SEMANTIC_CHAPTERS",
    "ART_SOURCE",
    "ART_TRANSLATIONS",
    "ART_TRANSLATION_UNITS",
    "EPUB_GRAPH_ADAPTER_VERSION",
    "MAX_EPUB_COMPRESSION_RATIO",
    "MAX_EPUB_ENTRIES",
    "MAX_EPUB_MEMBER_UNCOMPRESSED_BYTES",
    "MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES",
    "EpubGraphConfigurationError",
    "EpubGraphOptions",
    "EpubSemanticBlockedError",
    "EpubSourceChangedError",
    "NODE_APPLY",
    "NODE_DOCX",
    "NODE_EPUB",
    "NODE_IMPORT",
    "NODE_READER",
    "NODE_SOURCE",
    "NODE_TRANSLATE",
    "NODE_TRANSLATIONS_IMPORT",
    "NODE_VERIFY_EPUB",
    "PreparedEpubGraph",
    "epub_verifier_node",
    "prepare_epub_graph",
    "register_epub_verifier",
    "run_epub_graph",
]
