"""Shared, fail-closed application of semantic translation units.

Source adapters own extraction only.  This module owns the contract between
their immutable source units and reader-facing translated chapters.  An apply
operation validates the complete unit set, builds every output in a staging
tree, and rolls back already-replaced files if the commit cannot complete.

The reconstruction audit is deliberately append-only from this module's point
of view.  A successful translation creates ``semantic-translation.json`` and
binds it to the SHA-256 of the reconstruction audit; it never rewrites the
upstream evidence that authorized the operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Mapping, Sequence

from publication_semantics import (
    markdown_footnote_contract_sha256,
    parse_markdown_footnotes,
)
from semantic_ir import (
    BLOCK_KINDS,
    SEMANTIC_SCHEMA_VERSION,
    SemanticContractError,
    SourceLocator,
    TranslationUnit,
)


TRANSLATION_SET_SCHEMA_VERSION = SEMANTIC_SCHEMA_VERSION
# ``list`` is the only pre-IR kind still emitted by the EPUB adapter.  New
# adapters use canonical ``list_item``; both are accepted during migration.
SUPPORTED_UNIT_KINDS = BLOCK_KINDS | frozenset({"list"})
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_LINK = re.compile(
    r"!?\[(?P<label>[^\]\n]*)\]\((?P<target>[^)\s]+)(?:\s+[\"'][^)]*[\"'])?\)"
)
_FOOTNOTE_MARKER = re.compile(r"\[\^(?P<id>[^\]\s]+)\]")
_LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d+[.)])\s+", re.M)
_HEADING = re.compile(r"^(?P<marks>#{1,6})[ \t]+", re.M)


class SemanticApplyError(ValueError):
    """Raised when applying translations would violate a semantic contract."""


@dataclass(frozen=True)
class ValidatedTranslationUnit:
    unit_id: str
    chapter_id: str
    sequence: int
    kind: str
    source_sha256: str
    source_markdown: str
    translated_markdown: str


@dataclass(frozen=True)
class ValidatedTranslationSet:
    schema_version: int
    units: tuple[ValidatedTranslationUnit, ...]
    chapter_order: tuple[str, ...]

    def by_chapter(self) -> dict[str, tuple[ValidatedTranslationUnit, ...]]:
        result: dict[str, list[ValidatedTranslationUnit]] = {}
        for unit in self.units:
            result.setdefault(unit.chapter_id, []).append(unit)
        return {key: tuple(value) for key, value in result.items()}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SemanticApplyError(f"translation JSONL is missing or unreadable: {path}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SemanticApplyError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise SemanticApplyError(f"translation record must be an object: {path}:{line_number}")
        records.append(value)
    if not records:
        raise SemanticApplyError(f"translation JSONL contains no units: {path}")
    return records


def load_reconstruction_audit(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticApplyError("semantic reconstruction audit is missing or invalid") from exc
    if not isinstance(value, dict):
        raise SemanticApplyError("semantic reconstruction audit root must be an object")
    assert_reconstruction_allows_apply(value)
    return value, _sha256_bytes(raw)


def _blocking_issues(value: object) -> bool:
    return isinstance(value, list) and any(
        not isinstance(issue, dict) or bool(issue.get("blocking", True))
        for issue in value
    )


def assert_reconstruction_allows_apply(audit: Mapping[str, Any]) -> None:
    """Authorize apply only from an explicit, internally consistent pass."""

    if audit.get("schema_version") != SEMANTIC_SCHEMA_VERSION:
        raise SemanticApplyError("unsupported semantic reconstruction audit schema")
    status = str(audit.get("status") or "").strip().lower()
    if status == "blocked":
        raise SemanticApplyError("semantic reconstruction is blocked; translations cannot be applied")
    if status != "passed":
        raise SemanticApplyError("semantic reconstruction is not passed; translations cannot be applied")
    if audit.get("release_blocked") is not False:
        raise SemanticApplyError("semantic reconstruction is blocked; translations cannot be applied")
    summary = audit.get("summary")
    if not isinstance(summary, dict):
        raise SemanticApplyError("semantic reconstruction summary is missing")
    if summary.get("release_blocked") is not False:
        raise SemanticApplyError("semantic reconstruction summary is blocked; translations cannot be applied")
    try:
        blocking_issue_count = int(summary.get("blocking_issue_count") or 0)
    except (TypeError, ValueError) as exc:
        raise SemanticApplyError("semantic reconstruction blocking issue count is invalid") from exc
    if blocking_issue_count != 0:
        raise SemanticApplyError("semantic reconstruction reports blocking issues")
    if _blocking_issues(audit.get("issues")):
        raise SemanticApplyError("semantic reconstruction contains blocking root issues")
    chapters = audit.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise SemanticApplyError("semantic reconstruction chapter audit is missing")
    for chapter in chapters:
        if not isinstance(chapter, dict):
            raise SemanticApplyError("semantic reconstruction chapter audit is invalid")
        if chapter.get("release_blocked") is not False or _blocking_issues(chapter.get("issues")):
            raise SemanticApplyError(
                f"semantic reconstruction chapter is blocked: {chapter.get('chapter_id')!r}"
            )


def _unit_kind(unit: Mapping[str, Any]) -> str:
    kind = str(unit.get("kind") or "").strip()
    if kind not in SUPPORTED_UNIT_KINDS:
        raise SemanticApplyError(f"unsupported or missing semantic unit kind: {kind!r}")
    return kind


def _validate_source_unit_contract(
    source: Mapping[str, Any],
    *,
    unit_id: str,
    chapter_id: str,
    sequence: int,
    kind: str,
    source_markdown: str,
    source_sha256: str,
) -> None:
    """Use canonical IR validation when present, else accept v1 legacy locators."""

    raw_locators = source.get("locators")
    if raw_locators is not None:
        if not isinstance(raw_locators, Sequence) or isinstance(raw_locators, (str, bytes)):
            raise SemanticApplyError(f"semantic source locators are invalid: {unit_id}")
        try:
            locators = tuple(
                item if isinstance(item, SourceLocator) else SourceLocator.from_dict(item)
                for item in raw_locators
            )
            TranslationUnit(
                schema_version=TRANSLATION_SET_SCHEMA_VERSION,
                id=unit_id,
                chapter_id=chapter_id,
                sequence=sequence,
                kind=kind,
                source_markdown=source_markdown,
                source_sha256=source_sha256,
                locators=locators,
            )
        except (SemanticContractError, TypeError, ValueError) as exc:
            raise SemanticApplyError(f"canonical translation unit is invalid: {unit_id}: {exc}") from exc
        return
    if not str(source.get("source_href") or source.get("source_pages") or "").strip():
        raise SemanticApplyError(f"semantic source locator is missing: {unit_id}")


def _heading_signature(markdown: str) -> tuple[int, ...]:
    return tuple(len(match.group("marks")) for match in _HEADING.finditer(markdown))


def _list_signature(markdown: str) -> tuple[tuple[int, str], ...]:
    signature: list[tuple[int, str]] = []
    for match in _LIST_ITEM.finditer(markdown):
        indent = len(match.group("indent").expandtabs(4))
        marker = match.group("marker")
        signature.append((indent, "ordered" if marker[0].isdigit() else "unordered"))
    return tuple(signature)


def _table_signature(markdown: str) -> tuple[tuple[int, bool], ...]:
    rows: list[tuple[int, bool]] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = re.split(r"(?<!\\)\|", stripped)[1:-1]
        separator = bool(cells) and all(
            re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells
        )
        rows.append((len(cells), separator))
    return tuple(rows)


def _link_targets(markdown: str) -> tuple[str, ...]:
    return tuple(match.group("target") for match in _LINK.finditer(markdown))


def _footnote_signature(markdown: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    inventory = parse_markdown_footnotes(markdown)
    return inventory.references, tuple(note_id for note_id, _ in inventory.definitions)


def _plain_text(markdown: str) -> str:
    value = _LINK.sub(lambda match: f" {match.group('label')} ", markdown)
    value = _FOOTNOTE_MARKER.sub(" ", value)
    value = re.sub(r"https?://\S+|www\.\S+", " ", value)
    value = re.sub(r"[`*_#>|~\[\]{}()]", " ", value)
    return " ".join(value.split())


def _validate_target_language(
    source: str,
    translated: str,
    *,
    unit_id: str,
    target_language: str,
) -> None:
    source_plain = _plain_text(source)
    translated_plain = _plain_text(translated)
    source_latin = len(_LATIN.findall(source_plain))
    normalized_source = " ".join(source_plain.casefold().split())
    normalized_translation = " ".join(translated_plain.casefold().split())
    if source_latin >= 4 and normalized_source == normalized_translation:
        raise SemanticApplyError(f"translated unit is unchanged source text: {unit_id}")
    if source_latin < 8 or target_language not in {"简体中文", "中文", "zh-CN", "zh-Hans"}:
        return

    cjk_count = len(_CJK.findall(translated_plain))
    translated_latin = len(_LATIN.findall(translated_plain))
    if cjk_count == 0:
        raise SemanticApplyError(f"translated unit has no target-language coverage: {unit_id}")

    similarity = SequenceMatcher(
        None, normalized_source, normalized_translation, autojunk=False
    ).ratio()
    # A token Chinese prefix attached to an otherwise copied English unit must
    # not satisfy the language gate.  Citation-heavy units remain possible
    # when they contain meaningful Chinese connective prose or are resolved by
    # the future review-decision contract.
    if similarity >= 0.90:
        raise SemanticApplyError(f"translated unit is substantially unchanged source text: {unit_id}")
    if translated_latin >= 60 and cjk_count < max(3, translated_latin // 28) and similarity >= 0.55:
        raise SemanticApplyError(f"translated unit has abnormal English residual: {unit_id}")


def _validate_pair_structure(
    source: str,
    translated: str,
    *,
    unit_id: str,
    target_language: str,
    glossary: Mapping[str, str],
) -> None:
    if not translated.strip():
        raise SemanticApplyError(f"translated_markdown is empty: {unit_id}")
    source_plain_length = len(_plain_text(source))
    translated_plain_length = len(_plain_text(translated))
    if source_plain_length >= 20 and translated_plain_length < max(
        2, int(source_plain_length * 0.08)
    ):
        raise SemanticApplyError(f"translated unit is implausibly short: {unit_id}")
    if translated_plain_length > max(300, source_plain_length * 5):
        raise SemanticApplyError(f"translated unit is implausibly long: {unit_id}")
    if _heading_signature(source) != _heading_signature(translated):
        raise SemanticApplyError(f"translation changed heading levels: {unit_id}")
    if _list_signature(source) != _list_signature(translated):
        raise SemanticApplyError(f"translation changed list structure: {unit_id}")
    if _table_signature(source) != _table_signature(translated):
        raise SemanticApplyError(f"translation changed table structure: {unit_id}")
    if _link_targets(source) != _link_targets(translated):
        raise SemanticApplyError(f"translation changed link targets: {unit_id}")
    if _footnote_signature(source) != _footnote_signature(translated):
        raise SemanticApplyError(f"translation changed footnote structure: {unit_id}")
    _validate_target_language(
        source,
        translated,
        unit_id=unit_id,
        target_language=target_language,
    )
    source_folded = source.casefold()
    for source_term, target_term in glossary.items():
        if source_term.casefold() in source_folded and target_term not in translated:
            raise SemanticApplyError(
                f"translation does not honor glossary term {source_term!r}: {unit_id}"
            )


def validate_translation_pair(
    source_markdown: str,
    translated_markdown: str,
    *,
    unit_id: str,
    target_language: str = "简体中文",
    glossary: Mapping[str, str] | None = None,
) -> None:
    """Validate one model response using the same checks as final apply."""

    _validate_pair_structure(
        source_markdown,
        translated_markdown,
        unit_id=unit_id,
        target_language=target_language,
        glossary=glossary or {},
    )


def validate_translation_set(
    source_units: Sequence[Mapping[str, Any]],
    translated_units: Sequence[Mapping[str, Any]],
    *,
    target_language: str = "简体中文",
    glossary: Mapping[str, str] | None = None,
) -> ValidatedTranslationSet:
    """Validate a complete, ordered, source-hash-bound translation set."""

    if not source_units or not translated_units:
        raise SemanticApplyError("source and translated unit sets must not be empty")
    if len(source_units) != len(translated_units):
        raise SemanticApplyError("translation unit set does not match the imported source")
    glossary = glossary or {}
    if not all(
        isinstance(source, str)
        and isinstance(target, str)
        and source.strip()
        and target.strip()
        for source, target in glossary.items()
    ):
        raise SemanticApplyError("glossary must contain non-empty string pairs")

    source_ids: set[str] = set()
    translated_ids: set[str] = set()
    chapter_order: list[str] = []
    closed_chapters: set[str] = set()
    current_chapter = ""
    expected_sequence = 0
    validated: list[ValidatedTranslationUnit] = []

    for index, (source, translated) in enumerate(zip(source_units, translated_units), 1):
        if not isinstance(source, Mapping) or not isinstance(translated, Mapping):
            raise SemanticApplyError(f"semantic unit must be an object at position {index}")
        if source.get("schema_version") != TRANSLATION_SET_SCHEMA_VERSION:
            raise SemanticApplyError(f"unsupported source unit schema at position {index}")
        if translated.get("schema_version") != source.get("schema_version"):
            raise SemanticApplyError(f"translation unit schema mismatch at position {index}")

        unit_id = str(source.get("id") or "").strip()
        translated_id = str(translated.get("id") or "").strip()
        chapter_id = str(source.get("chapter_id") or "").strip()
        translated_chapter = str(translated.get("chapter_id") or "").strip()
        try:
            sequence = int(source.get("sequence"))
            translated_sequence = int(translated.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise SemanticApplyError(f"semantic unit sequence is invalid: {unit_id!r}") from exc
        if not unit_id or not chapter_id or sequence < 1:
            raise SemanticApplyError(f"semantic unit identity is incomplete at position {index}")
        if unit_id in source_ids or translated_id in translated_ids:
            raise SemanticApplyError(f"source or translated unit ids are duplicated: {unit_id!r}")
        if translated_id != unit_id or translated_chapter != chapter_id or translated_sequence != sequence:
            raise SemanticApplyError(f"translated unit identity/order mismatch: {unit_id!r}")
        source_ids.add(unit_id)
        translated_ids.add(translated_id)

        if chapter_id != current_chapter:
            if chapter_id in closed_chapters:
                raise SemanticApplyError(f"chapter units are not contiguous: {chapter_id}")
            if current_chapter:
                closed_chapters.add(current_chapter)
            current_chapter = chapter_id
            chapter_order.append(chapter_id)
            expected_sequence = 1
        else:
            expected_sequence += 1
        if sequence != expected_sequence:
            raise SemanticApplyError(
                f"chapter unit sequence is not contiguous: {chapter_id} expected={expected_sequence} actual={sequence}"
            )

        kind = _unit_kind(source)
        source_markdown = source.get("source_markdown")
        source_sha256 = source.get("source_sha256")
        if not isinstance(source_markdown, str) or not source_markdown.strip():
            raise SemanticApplyError(f"source_markdown is empty: {unit_id}")
        actual_sha256 = _sha256_bytes(source_markdown.encode("utf-8"))
        if source_sha256 != actual_sha256:
            raise SemanticApplyError(f"stored source unit hash is stale: {unit_id}")
        _validate_source_unit_contract(
            source,
            unit_id=unit_id,
            chapter_id=chapter_id,
            sequence=sequence,
            kind=kind,
            source_markdown=source_markdown,
            source_sha256=actual_sha256,
        )
        if translated.get("source_sha256") != actual_sha256:
            raise SemanticApplyError(f"translated unit is bound to different source bytes: {unit_id}")
        translated_markdown = translated.get("translated_markdown")
        if not isinstance(translated_markdown, str):
            raise SemanticApplyError(f"translated_markdown is missing: {unit_id}")
        translated_markdown = translated_markdown.strip()
        validate_translation_pair(
            source_markdown,
            translated_markdown,
            unit_id=unit_id,
            target_language=target_language,
            glossary=glossary,
        )
        validated.append(
            ValidatedTranslationUnit(
                unit_id=unit_id,
                chapter_id=chapter_id,
                sequence=sequence,
                kind=kind,
                source_sha256=actual_sha256,
                source_markdown=source_markdown,
                translated_markdown=translated_markdown,
            )
        )

    if source_ids != translated_ids:
        raise SemanticApplyError("translation unit set does not match the imported source")
    return ValidatedTranslationSet(
        schema_version=TRANSLATION_SET_SCHEMA_VERSION,
        units=tuple(validated),
        chapter_order=tuple(chapter_order),
    )


def _safe_filename(value: object) -> str:
    filename = str(value or "")
    path = Path(filename)
    if not filename or path.is_absolute() or path.name != filename or filename in {".", ".."}:
        raise SemanticApplyError(f"unsafe chapter filename: {filename!r}")
    return filename


def _heading_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return re.sub(r"[*_`]+", "", match.group(1)).strip() or fallback
    return fallback


def _write_staged_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _replace_file(source: Path, target: Path) -> None:
    """Single-file atomic replacement, isolated for fault-injection tests."""

    os.replace(source, target)


def _commit_staged_files(
    output_dir: Path,
    stage_dir: Path,
    relative_paths: Sequence[Path],
) -> None:
    rollback_dir = stage_dir / ".rollback"
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for relative in relative_paths:
            staged = stage_dir / relative
            target = output_dir / relative
            if not staged.is_file():
                raise SemanticApplyError(f"staged semantic output is missing: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                backup = rollback_dir / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
                backups[relative] = backup
            else:
                backups[relative] = None
            _replace_file(staged, target)
            committed.append(relative)
    except Exception as exc:
        rollback_errors: list[str] = []
        for relative in reversed(committed):
            target = output_dir / relative
            backup = backups[relative]
            try:
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    _replace_file(backup, target)
            except Exception as rollback_exc:  # pragma: no cover - catastrophic filesystem failure
                rollback_errors.append(f"{relative}: {rollback_exc}")
        detail = f"; rollback_errors={rollback_errors}" if rollback_errors else ""
        raise SemanticApplyError(f"semantic translation commit failed and was rolled back: {exc}{detail}") from exc


def apply_translation_transaction(
    output_dir: Path,
    translations_path: Path,
    *,
    generated_by: str,
    contract_mode: str,
    target_language: str = "简体中文",
    glossary: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate, stage and transactionally commit a complete translation."""

    output_dir = output_dir.expanduser().resolve()
    translations_path = translations_path.expanduser().resolve()
    reconstruction_path = output_dir / "audit" / "semantic-reconstruction.json"
    reconstruction, reconstruction_sha256 = load_reconstruction_audit(reconstruction_path)
    source_units_path = output_dir / "semantic" / "translation-units.jsonl"
    source_units = read_jsonl(source_units_path)
    source_units_sha256 = _sha256_bytes(source_units_path.read_bytes())
    translated_units = read_jsonl(translations_path)
    validation = validate_translation_set(
        source_units,
        translated_units,
        target_language=target_language,
        glossary=glossary,
    )
    by_chapter = validation.by_chapter()

    manifest_path = output_dir / "chapters.json"
    try:
        raw_manifest_bytes = manifest_path.read_bytes()
        raw_manifest = json.loads(raw_manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticApplyError("chapter manifest is missing or invalid") from exc
    if not isinstance(raw_manifest, list) or not raw_manifest:
        raise SemanticApplyError("chapter manifest must be a non-empty array")
    manifest = copy.deepcopy(raw_manifest)
    manifest_ids = [str(item.get("id") or "") for item in manifest if isinstance(item, dict)]
    if (
        len(manifest_ids) != len(manifest)
        or any(not item for item in manifest_ids)
        or len(set(manifest_ids)) != len(manifest_ids)
    ):
        raise SemanticApplyError("chapter manifest ids are invalid or duplicated")
    if tuple(manifest_ids) != validation.chapter_order:
        raise SemanticApplyError("translation chapter order does not match the manifest")

    stage_dir = Path(tempfile.mkdtemp(prefix=".semantic-apply-", dir=output_dir))
    relative_paths: list[Path] = []
    audit_chapters: list[dict[str, Any]] = []
    source_chapter_sha256: dict[Path, str] = {}
    try:
        for item in manifest:
            chapter_id = str(item["id"])
            filename = _safe_filename(item.get("filename"))
            units = by_chapter.get(chapter_id, ())
            if not units:
                raise SemanticApplyError(f"translation is missing chapter: {chapter_id}")
            translated_markdown = "\n\n".join(
                unit.translated_markdown for unit in units
            ).rstrip() + "\n"
            source_path = output_dir / "semantic" / "source_chapters" / filename
            try:
                source_markdown = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SemanticApplyError(f"source chapter is missing: {chapter_id}") from exc
            source_chapter_sha256[source_path] = _sha256_bytes(
                source_markdown.encode("utf-8")
            )

            # Unit concatenation must reproduce the immutable source chapter;
            # this proves neither the manifest nor source unit file is stale.
            unit_source = "\n\n".join(unit.source_markdown for unit in units).rstrip() + "\n"
            if unit_source != source_markdown:
                raise SemanticApplyError(f"source units do not reconstruct chapter: {chapter_id}")
            source_inventory = parse_markdown_footnotes(source_markdown)
            translated_inventory = parse_markdown_footnotes(translated_markdown)
            if (
                source_inventory.references != translated_inventory.references
                or tuple(note_id for note_id, _ in source_inventory.definitions)
                != tuple(note_id for note_id, _ in translated_inventory.definitions)
                or not translated_inventory.valid
            ):
                raise SemanticApplyError(
                    f"translation changed the chapter footnote reference-definition relation: {chapter_id}"
                )

            title = _heading_title(translated_markdown, str(item.get("display_title") or ""))
            item["title"] = title
            item["display_title"] = title
            item["translation_applied"] = True
            relative = Path("chapters") / filename
            _write_staged_text(stage_dir / relative, translated_markdown)
            relative_paths.append(relative)
            audit_chapters.append(
                {
                    "chapter_id": chapter_id,
                    "filename": filename,
                    "source_markdown_sha256": _sha256_bytes(source_markdown.encode("utf-8")),
                    "translated_markdown_sha256": _sha256_bytes(translated_markdown.encode("utf-8")),
                    "markdown_sha256": _sha256_bytes(translated_markdown.encode("utf-8")),
                    "footnote_contract_sha256": markdown_footnote_contract_sha256(translated_markdown),
                    "footnote_count": len(translated_inventory.definitions),
                    "translation_unit_count": len(units),
                    "issues": [],
                    "release_blocked": False,
                }
            )

        report = {
            "schema_version": TRANSLATION_SET_SCHEMA_VERSION,
            "status": "passed",
            "release_blocked": False,
            "generated_by": generated_by,
            "contract_mode": contract_mode,
            "upstream_reconstruction": {
                "path": "audit/semantic-reconstruction.json",
                "sha256": reconstruction_sha256,
                "schema_version": reconstruction.get("schema_version"),
                "status": reconstruction.get("status"),
            },
            "review_resolution": {
                "schema_version": SEMANTIC_SCHEMA_VERSION,
                "decisions": [],
            },
            "summary": {
                "chapter_count": len(audit_chapters),
                "footnote_count": sum(item["footnote_count"] for item in audit_chapters),
                "translation_unit_count": sum(item["translation_unit_count"] for item in audit_chapters),
                "issue_count": 0,
                "blocking_issue_count": 0,
                "release_blocked": False,
            },
            "chapters": audit_chapters,
        }
        _write_staged_text(
            stage_dir / "chapters.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        _write_staged_text(
            stage_dir / "audit" / "semantic-translation.json",
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
        # The release audit is the commit marker and is intentionally last.
        relative_paths.extend(
            [Path("chapters.json"), Path("audit") / "semantic-translation.json"]
        )

        # Refuse to commit against an audit changed while staging.
        if _sha256_bytes(reconstruction_path.read_bytes()) != reconstruction_sha256:
            raise SemanticApplyError("semantic reconstruction audit changed during translation apply")
        if _sha256_bytes(source_units_path.read_bytes()) != source_units_sha256:
            raise SemanticApplyError("semantic translation units changed during translation apply")
        if _sha256_bytes(manifest_path.read_bytes()) != _sha256_bytes(raw_manifest_bytes):
            raise SemanticApplyError("chapter manifest changed during translation apply")
        for source_path, expected_sha256 in source_chapter_sha256.items():
            if _sha256_bytes(source_path.read_bytes()) != expected_sha256:
                raise SemanticApplyError(
                    f"source chapter changed during translation apply: {source_path.name}"
                )
        _commit_staged_files(output_dir, stage_dir, relative_paths)
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)

    return {
        **report["summary"],
        "status": "passed",
        "manifest": str(manifest_path),
        "translation_audit": str(output_dir / "audit" / "semantic-translation.json"),
    }


__all__ = [
    "SemanticApplyError",
    "TRANSLATION_SET_SCHEMA_VERSION",
    "ValidatedTranslationSet",
    "ValidatedTranslationUnit",
    "apply_translation_transaction",
    "assert_reconstruction_allows_apply",
    "load_reconstruction_audit",
    "read_jsonl",
    "validate_translation_pair",
    "validate_translation_set",
]
