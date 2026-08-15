"""Deterministic release gate for EPUB-native publication runs.

The verifier deliberately has no model or network dependency.  It binds the
published EPUB to the imported source, semantic audits, canonical Markdown,
and the EPUB package graph before emitting a release report.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import tempfile
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit
import zipfile

from lxml import etree

from publication_semantics import (
    markdown_footnote_contract_sha256,
    parse_markdown_footnotes,
)


SCHEMA_VERSION = 1
VERIFIER_VERSION = "epub-publication-verifier-v2"
PUBLICATION_PROFILE = "epub"
VERIFIER_NODE = "core.publication.verify.epub"
DEFAULT_REPORT_NAME = "epub-release-report.json"

CHECK_IDS = (
    "source.identity",
    "semantic.reconstruction",
    "semantic.translation",
    "chapters.canonical",
    "epub.package",
    "epub.navigation",
    "epub.content",
    "artifact.identity",
)

_CONTAINER = "META-INF/container.xml"
_MIMETYPE = "application/epub+zip"
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_XML_NS = "http://www.w3.org/XML/1998/namespace"
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_SAFE_EXTERNAL_LINK_SCHEMES = frozenset({"http", "https", "mailto"})


class EpubPublicationVerificationError(ValueError):
    """Raised for an invalid verifier invocation, not a failed quality check."""


class _CheckFailure(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _files_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _json_file(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    _require_regular_file(path, label=label)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _CheckFailure("invalid_json", f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise _CheckFailure("invalid_json_root", f"{label} must contain a JSON object: {path}")
    return payload, _sha256_bytes(raw)


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _CheckFailure("path_inspection_failed", f"cannot inspect {path}: {exc}") from exc


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    entry = _lstat(path)
    if entry is None:
        raise _CheckFailure("missing_file", f"{label} is missing: {path}")
    if stat.S_ISLNK(entry.st_mode):
        raise _CheckFailure("symlink_rejected", f"{label} must not be a symlink: {path}")
    if not stat.S_ISREG(entry.st_mode):
        raise _CheckFailure("non_regular_file", f"{label} is not a regular file: {path}")
    return entry


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_manifest_filename(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _CheckFailure("manifest_filename_invalid", "chapter filename must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise _CheckFailure("manifest_filename_unsafe", f"unsafe chapter filename: {value!r}")
    if Path(value).suffix.casefold() != ".md":
        raise _CheckFailure("manifest_filename_invalid", f"chapter filename is not Markdown: {value!r}")
    return value


_LANGUAGE_LABELS = {
    "简体中文": "zh-CN",
    "简体": "zh-CN",
    "簡體中文": "zh-CN",
    "繁体中文": "zh-Hant",
    "繁體中文": "zh-Hant",
    "中文": "zh",
    "english": "en",
    "英文": "en",
    "英语": "en",
    "英語": "en",
    "japanese": "ja",
    "日文": "ja",
    "日语": "ja",
    "日語": "ja",
}


def normalize_epub_language(value: str) -> str:
    """Return a canonical, structurally valid BCP 47 EPUB language tag.

    Product-facing labels are deliberately mapped here rather than written into
    ``dc:language``/XHTML ``lang`` verbatim.  The validation is structural (it
    does not require a network-backed IANA registry) and therefore accepts
    ordinary two- or three-letter tags such as ``en``/``eng`` plus ``und``.
    """

    if not isinstance(value, str) or not value.strip():
        raise EpubPublicationVerificationError(
            "EPUB language must be a non-empty string"
        )
    stripped = value.strip()
    mapped = _LANGUAGE_LABELS.get(stripped.casefold(), stripped)
    parts = mapped.replace("_", "-").split("-")
    if (
        not parts
        or not re.fullmatch(r"[A-Za-z]{2,8}", parts[0])
        or any(not re.fullmatch(r"[A-Za-z0-9]{1,8}", part) for part in parts[1:])
    ):
        raise EpubPublicationVerificationError(
            f"EPUB language is not a structurally valid BCP 47 tag: {value!r}"
        )
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 4 and part.isalpha():
            normalized.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (
            len(part) == 3 and part.isdigit()
        ):
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _check_audit_status(payload: Mapping[str, Any], *, label: str) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise _CheckFailure(
            "audit_schema_unsupported",
            f"{label} requires schema_version={SCHEMA_VERSION}",
        )
    if payload.get("status") != "passed":
        raise _CheckFailure("audit_not_passed", f"{label} status is not passed")
    if payload.get("release_blocked") is not False:
        raise _CheckFailure("audit_release_blocked", f"{label} does not explicitly clear release_blocked")
    summary = payload.get("summary")
    if not isinstance(summary, Mapping) or summary.get("release_blocked") is not False:
        raise _CheckFailure("audit_summary_blocked", f"{label} summary is missing or blocked")
    if int(summary.get("blocking_issue_count", 0) or 0) != 0:
        raise _CheckFailure("audit_blocking_issues", f"{label} contains blocking issues")
    chapters = payload.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise _CheckFailure("audit_chapters_invalid", f"{label} chapters must be a non-empty array")
    for index, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, Mapping):
            raise _CheckFailure("audit_chapter_invalid", f"{label} chapter {index} is not an object")
        if chapter.get("release_blocked") is not False:
            raise _CheckFailure("audit_chapter_blocked", f"{label} chapter {index} is blocked")
        issues = chapter.get("issues")
        if not isinstance(issues, list):
            raise _CheckFailure("audit_chapter_invalid", f"{label} chapter {index} issues must be an array")
        if any(
            not isinstance(issue, Mapping) or issue.get("blocking", True) is not False
            for issue in issues
        ):
            raise _CheckFailure("audit_chapter_blocked", f"{label} chapter {index} has a blocking issue")


def _audit_by_filename(payload: Mapping[str, Any], *, label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    chapters = payload.get("chapters")
    assert isinstance(chapters, list)
    for chapter in chapters:
        assert isinstance(chapter, Mapping)
        filename = chapter.get("filename")
        if not isinstance(filename, str) or not filename or filename in result:
            raise _CheckFailure("audit_filenames_invalid", f"{label} chapter filenames are missing or duplicated")
        result[filename] = chapter
    return result


def _xml(data: bytes, *, label: str) -> etree._Element:
    if len(data) > _MAX_MEMBER_BYTES:
        raise _CheckFailure("xml_too_large", f"{label} exceeds the XML size limit")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        recover=False,
        huge_tree=False,
        remove_comments=False,
    )
    try:
        return etree.fromstring(data, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise _CheckFailure("xml_invalid", f"{label} is not well-formed XML: {exc}") from exc


def _safe_zip_name(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise _CheckFailure("archive_path_unsafe", f"unsafe EPUB member name: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _CheckFailure("archive_path_unsafe", f"unsafe EPUB member name: {value!r}")
    return path.as_posix()


def _zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _internal_member(base: str, href: str, *, allow_fragment: bool = True) -> tuple[str, str | None] | None:
    parsed = urlsplit(href)
    scheme = parsed.scheme.casefold()
    if scheme or parsed.netloc:
        if scheme in _SAFE_EXTERNAL_LINK_SCHEMES:
            return None
        raise _CheckFailure("unsafe_link_scheme", f"unsafe EPUB link scheme: {href!r}")
    if parsed.query:
        raise _CheckFailure("internal_link_query", f"internal EPUB links must not contain queries: {href!r}")
    raw_path = unquote(parsed.path)
    if "\\" in raw_path or "\x00" in raw_path:
        raise _CheckFailure("internal_link_unsafe", f"unsafe EPUB link: {href!r}")
    member = base if not raw_path else posixpath.normpath(
        posixpath.join(posixpath.dirname(base), raw_path)
    )
    member = _safe_zip_name(member)
    fragment = unquote(parsed.fragment) if allow_fragment and parsed.fragment else None
    return member, fragment


def _manifest_member(opf_member: str, href: str) -> str:
    resolved = _internal_member(opf_member, href, allow_fragment=False)
    if resolved is None:
        raise _CheckFailure("manifest_href_external", f"EPUB manifest href must be internal: {href!r}")
    member, _fragment = resolved
    if urlsplit(href).fragment:
        raise _CheckFailure("manifest_href_fragment", f"EPUB manifest href must not contain a fragment: {href!r}")
    return member


def _element_ids(root: etree._Element) -> set[str]:
    result: set[str] = set()
    for value in root.xpath("//@id"):
        text = str(value)
        if text in result:
            raise _CheckFailure("duplicate_xhtml_id", f"duplicate XHTML id: {text!r}")
        result.add(text)
    return result


def _normalized_text(root: etree._Element) -> str:
    return " ".join("".join(root.itertext()).split())


def _expected_markdown_body_text(
    markdown_text: str,
    *,
    publication_title: str,
    chapter_title: str,
    reviewed_override: bool,
) -> str:
    # Publication comparison must mirror the deterministic publisher input.
    # Comparing against raw canonical Markdown rejects valid title-page cleanup
    # (for example a generated H1 followed by the same visible source title).
    from book_pipeline import (
        strip_publication_metadata,
        strip_reviewed_publication_metadata,
    )

    publication_markdown = (
        strip_reviewed_publication_metadata(markdown_text)
        if reviewed_override
        else strip_publication_metadata(
            markdown_text,
            publication_title=publication_title,
            chapter_title=chapter_title,
        )
    )
    try:
        import markdown
    except ImportError as exc:  # pragma: no cover - installation preflight owns this
        raise _CheckFailure("markdown_unavailable", "Markdown is required for EPUB verification") from exc
    rendered = markdown.markdown(
        publication_markdown,
        extensions=["extra", "sane_lists", "footnotes"],
        output_format="xhtml",
    )
    root = _xml(f"<body xmlns='{_XHTML_NS}'>{rendered}</body>".encode("utf-8"), label="rendered canonical Markdown")
    return _normalized_text(root)


def _atomic_report(path: Path, payload: Mapping[str, Any], *, output_dir: Path) -> None:
    resolved_parent = path.parent.expanduser().resolve()
    if not _inside(resolved_parent, output_dir):
        raise EpubPublicationVerificationError("EPUB release report must remain inside output_dir")
    resolved_parent.mkdir(parents=True, exist_ok=True)
    if resolved_parent.is_symlink() or not resolved_parent.is_dir():
        raise EpubPublicationVerificationError("EPUB release report parent must be a regular directory")
    destination = resolved_parent / path.name
    entry = _lstat(destination)
    if entry is not None and (stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode)):
        raise EpubPublicationVerificationError("EPUB release report target is not a regular file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=resolved_parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


class _Verifier:
    def __init__(
        self,
        output_dir: Path,
        source_epub: Path,
        artifact_path: Path,
        *,
        target_language: str,
        require_translation: bool,
        expected_translation_sha256: str | None,
    ) -> None:
        self.output_dir = output_dir
        self.source_epub = source_epub
        self.artifact_path = artifact_path
        self.expected_language = normalize_epub_language(target_language)
        self.require_translation = require_translation
        self.expected_translation_sha256 = expected_translation_sha256
        self.checks: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}

    def check(self, check_id: str, operation: Callable[[], Mapping[str, Any] | None]) -> None:
        try:
            metrics = dict(operation() or {})
        except _CheckFailure as exc:
            self.checks.append(
                {
                    "id": check_id,
                    "status": "failed",
                    "summary": str(exc),
                    "metrics": {},
                    "issues": [{"code": exc.code, "message": str(exc)}],
                    "warnings": [],
                }
            )
        except Exception as exc:
            self.checks.append(
                {
                    "id": check_id,
                    "status": "failed",
                    "summary": f"{type(exc).__name__}: {exc}",
                    "metrics": {},
                    "issues": [{"code": "verification_error", "message": f"{type(exc).__name__}: {exc}"}],
                    "warnings": [],
                }
            )
        else:
            self.checks.append(
                {
                    "id": check_id,
                    "status": "passed",
                    "summary": "check passed",
                    "metrics": metrics,
                    "issues": [],
                    "warnings": [],
                }
            )

    def source_identity(self) -> Mapping[str, Any]:
        source_stat = _require_regular_file(self.source_epub, label="source EPUB")
        source_sha = _sha256_file(self.source_epub)
        publication_audit = self.output_dir / "audit" / "semantic-reconstruction.json"
        draft_audit = self.output_dir / ".pipeline_graph" / "draft-semantic-audit.json"
        # The Graph keeps the pre-sanitize audit separately.  Prefer that
        # immutable evidence when available; the publication audit may already
        # describe reader bytes.
        reconstruction_path = draft_audit if draft_audit.is_file() else publication_audit
        reconstruction, reconstruction_sha = _json_file(
            reconstruction_path, label="semantic reconstruction audit"
        )
        source = reconstruction.get("source")
        if not isinstance(source, Mapping):
            raise _CheckFailure("source_identity_missing", "reconstruction audit has no source identity")
        audited_path = source.get("path")
        audited_sha = source.get("sha256")
        if not isinstance(audited_path, str) or Path(audited_path).expanduser().resolve() != self.source_epub:
            raise _CheckFailure("source_path_mismatch", "reconstruction audit is bound to another source path")
        if audited_sha != source_sha:
            raise _CheckFailure("source_hash_mismatch", "source EPUB hash does not match reconstruction audit")
        self.context.update(
            reconstruction=reconstruction,
            reconstruction_path=reconstruction_path,
            reconstruction_sha256=reconstruction_sha,
            source_sha256=source_sha,
        )
        return {"size": source_stat.st_size, "sha256": source_sha}

    def reconstruction(self) -> Mapping[str, Any]:
        payload = self.context.get("reconstruction")
        if not isinstance(payload, Mapping):
            raise _CheckFailure("prerequisite_failed", "source identity check did not load reconstruction audit")
        _check_audit_status(payload, label="semantic reconstruction audit")
        audit = _audit_by_filename(payload, label="semantic reconstruction audit")
        source_dir = self.output_dir / "semantic" / "source_chapters"
        for filename, item in audit.items():
            safe_name = _safe_manifest_filename(filename)
            source_path = source_dir / safe_name
            _require_regular_file(source_path, label=f"immutable source chapter {safe_name}")
            markdown = source_path.read_text(encoding="utf-8")
            source_digest = item.get("source_markdown_sha256") or item.get("markdown_sha256")
            if source_digest != _sha256_bytes(markdown.encode("utf-8")):
                raise _CheckFailure("reconstruction_hash_mismatch", f"reconstruction hash is stale for {safe_name}")
            if (
                not item.get("source_markdown_sha256")
                and item.get("footnote_contract_sha256")
                != markdown_footnote_contract_sha256(markdown)
            ):
                raise _CheckFailure("reconstruction_footnote_hash_mismatch", f"reconstruction footnote hash is stale for {safe_name}")
        summary = payload["summary"]
        if summary.get("chapter_count") != len(audit):
            raise _CheckFailure("reconstruction_summary_stale", "reconstruction chapter count is stale")
        self.context["reconstruction_audit"] = audit
        return {"chapter_count": len(audit), "sha256": self.context["reconstruction_sha256"]}

    def translation(self) -> Mapping[str, Any]:
        path = self.output_dir / "audit" / "semantic-translation.json"
        if not path.exists() and not self.require_translation:
            self.context["active_audit"] = self.context.get("reconstruction_audit", {})
            self.context["translation_applied"] = False
            return {"required": False, "present": False}
        payload, digest = _json_file(path, label="semantic translation audit")
        _check_audit_status(payload, label="semantic translation audit")
        upstream = payload.get("upstream_reconstruction")
        if not isinstance(upstream, Mapping) or upstream.get("sha256") != self.context.get("reconstruction_sha256"):
            raise _CheckFailure("translation_upstream_mismatch", "translation audit is not bound to reconstruction bytes")
        if upstream.get("status") != "passed" or upstream.get("schema_version") != SCHEMA_VERSION:
            raise _CheckFailure("translation_upstream_invalid", "translation audit records an invalid reconstruction state")
        audit = _audit_by_filename(payload, label="semantic translation audit")
        reconstruction_audit = self.context.get("reconstruction_audit")
        if not isinstance(reconstruction_audit, Mapping) or tuple(audit) != tuple(reconstruction_audit):
            raise _CheckFailure("translation_chapters_mismatch", "translation and reconstruction chapter sets differ")
        summary = payload["summary"]
        if summary.get("chapter_count") != len(audit):
            raise _CheckFailure("translation_summary_stale", "translation chapter count is stale")
        translation_input = payload.get("translation_input")
        if not isinstance(translation_input, Mapping):
            raise _CheckFailure(
                "translation_input_missing",
                "translation audit has no translation input identity",
            )
        translation_input_sha256 = translation_input.get("sha256")
        translation_input_count = translation_input.get("unit_count")
        if not isinstance(translation_input_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", translation_input_sha256
        ):
            raise _CheckFailure(
                "translation_input_invalid",
                "translation audit has an invalid translation input SHA-256",
            )
        if type(translation_input_count) is not int or translation_input_count < 1:
            raise _CheckFailure(
                "translation_input_invalid",
                "translation audit has an invalid translation input unit count",
            )
        if summary.get("translation_unit_count") != translation_input_count:
            raise _CheckFailure(
                "translation_input_count_mismatch",
                "translation audit unit count differs from its input identity",
            )
        if (
            self.expected_translation_sha256 is not None
            and translation_input_sha256 != self.expected_translation_sha256
        ):
            raise _CheckFailure(
                "translation_input_hash_mismatch",
                "translation audit is bound to different translation input bytes",
            )
        self.context["translation"] = payload
        self.context["translation_sha256"] = digest
        self.context["translation_input_sha256"] = translation_input_sha256
        self.context["active_audit"] = audit
        self.context["translation_applied"] = True
        return {"required": self.require_translation, "present": True, "chapter_count": len(audit), "sha256": digest}

    def canonical_chapters(self) -> Mapping[str, Any]:
        manifest_path = self.output_dir / "chapters.json"
        _require_regular_file(manifest_path, label="canonical chapter manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _CheckFailure("manifest_invalid", "canonical chapter manifest is invalid") from exc
        if not isinstance(manifest, list) or not manifest:
            raise _CheckFailure("manifest_invalid", "canonical chapter manifest must be non-empty")
        active = self.context.get("active_audit")
        if not isinstance(active, Mapping):
            raise _CheckFailure("prerequisite_failed", "semantic audit checks did not select an active audit")
        reader_path = self.output_dir / ".pipeline_graph" / "reader-semantic-audit.json"
        reader_payload: dict[str, Any] | None = None
        if reader_path.is_file():
            reader_payload, _reader_sha = _json_file(
                reader_path, label="semantic reader audit"
            )
            _check_audit_status(reader_payload, label="semantic reader audit")
            reader = _audit_by_filename(reader_payload, label="semantic reader audit")
            if tuple(reader) != tuple(active):
                raise _CheckFailure(
                    "reader_audit_chapters_mismatch",
                    "reader and upstream semantic chapter sets differ",
                )
            for filename, item in reader.items():
                upstream = active[filename]
                assert isinstance(upstream, Mapping)
                if item.get("source_markdown_sha256") != upstream.get("markdown_sha256"):
                    raise _CheckFailure(
                        "reader_audit_upstream_mismatch",
                        f"reader audit is not bound to upstream chapter bytes for {filename}",
                    )
            active = reader
            self.context["active_audit"] = active
        seen_ids: set[str] = set()
        filenames: list[str] = []
        chapters: list[dict[str, Any]] = []
        total_footnotes = 0
        for expected_sequence, item in enumerate(manifest, start=1):
            if not isinstance(item, Mapping):
                raise _CheckFailure("manifest_invalid", f"manifest item {expected_sequence} is not an object")
            chapter_id = item.get("id")
            if not isinstance(chapter_id, str) or not chapter_id or chapter_id in seen_ids:
                raise _CheckFailure("manifest_ids_invalid", "manifest chapter ids are missing or duplicated")
            seen_ids.add(chapter_id)
            sequence = item.get("sequence")
            if type(sequence) is not int or sequence != expected_sequence:
                raise _CheckFailure("manifest_sequence_invalid", "manifest sequence is not contiguous")
            filename = _safe_manifest_filename(item.get("filename"))
            if filename in filenames:
                raise _CheckFailure("manifest_filenames_invalid", "manifest filenames are duplicated")
            filenames.append(filename)
            audit = active.get(filename)
            if not isinstance(audit, Mapping):
                raise _CheckFailure("audit_manifest_mismatch", f"active audit is missing {filename}")
            if audit.get("chapter_id") not in {None, chapter_id}:
                raise _CheckFailure("audit_manifest_mismatch", f"active audit chapter id is stale for {filename}")
            path = self.output_dir / "chapters" / filename
            _require_regular_file(path, label=f"canonical chapter {filename}")
            markdown = path.read_text(encoding="utf-8")
            inventory = parse_markdown_footnotes(markdown)
            if not inventory.valid:
                raise _CheckFailure(
                    "footnote_closure_failed",
                    f"canonical footnotes are not one-to-one closed for {filename}",
                )
            digest = _sha256_bytes(markdown.encode("utf-8"))
            if audit.get("markdown_sha256") != digest:
                raise _CheckFailure("canonical_hash_mismatch", f"active semantic audit hash is stale for {filename}")
            contract = markdown_footnote_contract_sha256(markdown)
            if audit.get("footnote_contract_sha256") != contract:
                raise _CheckFailure("canonical_footnote_hash_mismatch", f"active semantic footnote hash is stale for {filename}")
            if audit.get("footnote_count") != len(inventory.definitions):
                raise _CheckFailure("canonical_footnote_count_mismatch", f"active semantic footnote count is stale for {filename}")
            total_footnotes += len(inventory.definitions)
            chapters.append(
                {
                    "id": chapter_id,
                    "filename": filename,
                    "display_title": str(item.get("display_title") or item.get("title") or ""),
                    "reviewed_override": item.get("reviewed_override") is True,
                    "markdown": markdown,
                    "markdown_sha256": digest,
                    "footnotes": len(inventory.definitions),
                }
            )
        extras = sorted(
            path.name
            for path in (self.output_dir / "chapters").glob("*.md")
            if path.name not in set(filenames)
        )
        if extras:
            raise _CheckFailure("canonical_chapter_extras", f"unmanifested canonical chapters exist: {extras}")
        if tuple(active) != tuple(filenames):
            raise _CheckFailure("audit_manifest_mismatch", "active semantic audit order differs from manifest")
        if reader_payload is not None:
            if reader_payload.get("reader_manifest_sha256") != _sha256_file(manifest_path):
                raise _CheckFailure(
                    "reader_manifest_hash_mismatch",
                    "reader audit manifest hash is stale",
                )
            chapter_paths = [self.output_dir / "chapters" / filename for filename in filenames]
            if reader_payload.get("reader_chapters_sha256") != _files_digest(chapter_paths):
                raise _CheckFailure(
                    "reader_chapters_hash_mismatch",
                    "reader audit chapter bundle hash is stale",
                )
        chapter_paths = [
            self.output_dir / "chapters" / filename for filename in filenames
        ]
        self.context["canonical_manifest_sha256"] = _sha256_file(manifest_path)
        self.context["canonical_chapters_sha256"] = _files_digest(chapter_paths)
        self.context["manifest"] = manifest
        self.context["chapters"] = chapters
        return {"chapter_count": len(chapters), "footnote_count": total_footnotes}

    def package(self) -> Mapping[str, Any]:
        artifact_stat = _require_regular_file(self.artifact_path, label="published EPUB")
        before_sha = _sha256_file(self.artifact_path)
        try:
            archive = zipfile.ZipFile(self.artifact_path)
        except zipfile.BadZipFile as exc:
            raise _CheckFailure("epub_zip_invalid", "published EPUB is not a valid ZIP package") from exc
        with archive:
            infos = archive.infolist()
            names = [info.filename for info in infos if not info.is_dir()]
            if not infos or infos[0].filename != "mimetype":
                raise _CheckFailure("epub_mimetype_order", "EPUB mimetype must be the first member")
            if infos[0].file_size != len(_MIMETYPE):
                raise _CheckFailure("epub_mimetype_invalid", "EPUB mimetype member has an invalid size")
            if infos[0].compress_type != zipfile.ZIP_STORED or infos[0].extra:
                raise _CheckFailure("epub_mimetype_encoding", "EPUB mimetype must be stored without compression or extra fields")
            if archive.read(infos[0]) != _MIMETYPE.encode("ascii"):
                raise _CheckFailure("epub_mimetype_invalid", "EPUB mimetype member is invalid")
            if len(names) != len(set(names)):
                raise _CheckFailure("epub_duplicate_members", "EPUB contains duplicate member names")
            total = 0
            for info in infos:
                _safe_zip_name(info.filename.rstrip("/"))
                if _zip_symlink(info):
                    raise _CheckFailure("epub_symlink_member", f"EPUB member is a symbolic link: {info.filename}")
                if info.flag_bits & 0x1:
                    raise _CheckFailure("epub_encrypted_member", f"EPUB member is encrypted: {info.filename}")
                if info.file_size > _MAX_MEMBER_BYTES:
                    raise _CheckFailure("epub_member_too_large", f"EPUB member exceeds size limit: {info.filename}")
                total += info.file_size
            if total > _MAX_TOTAL_BYTES:
                raise _CheckFailure("epub_package_too_large", "EPUB uncompressed size exceeds limit")
            if _CONTAINER not in names:
                raise _CheckFailure("epub_container_missing", "EPUB container.xml is missing")
            bad_crc = archive.testzip()
            if bad_crc is not None:
                raise _CheckFailure("epub_crc_invalid", f"EPUB member has invalid CRC: {bad_crc}")
            container = _xml(archive.read(_CONTAINER), label="EPUB container.xml")
            rootfiles = container.xpath(
                "/c:container/c:rootfiles/c:rootfile", namespaces={"c": _CONTAINER_NS}
            )
            if len(rootfiles) != 1:
                raise _CheckFailure("epub_rootfile_invalid", "EPUB container must declare exactly one rootfile")
            opf_member = _safe_zip_name(str(rootfiles[0].get("full-path") or ""))
            if rootfiles[0].get("media-type") != "application/oebps-package+xml" or opf_member not in names:
                raise _CheckFailure("epub_rootfile_invalid", "EPUB rootfile declaration is invalid")
            opf = _xml(archive.read(opf_member), label="EPUB package document")
            if opf.tag != f"{{{_OPF_NS}}}package" or opf.get("version") != "3.0":
                raise _CheckFailure("epub_package_version", "EPUB package must be an EPUB 3 package")
            languages = [str(value).strip() for value in opf.xpath("/opf:package/opf:metadata/dc:language/text()", namespaces={"opf": _OPF_NS, "dc": _DC_NS})]
            try:
                normalized_languages = [
                    normalize_epub_language(value) for value in languages
                ]
            except EpubPublicationVerificationError as exc:
                raise _CheckFailure("epub_language_invalid", str(exc)) from exc
            if normalized_languages != [self.expected_language]:
                raise _CheckFailure("epub_language_mismatch", f"EPUB package language is {languages}, expected {self.expected_language!r}")
            titles = [str(value).strip() for value in opf.xpath("/opf:package/opf:metadata/dc:title/text()", namespaces={"opf": _OPF_NS, "dc": _DC_NS}) if str(value).strip()]
            identifiers = [str(value).strip() for value in opf.xpath("/opf:package/opf:metadata/dc:identifier/text()", namespaces={"opf": _OPF_NS, "dc": _DC_NS}) if str(value).strip()]
            if len(titles) != 1 or not identifiers:
                raise _CheckFailure("epub_metadata_invalid", "EPUB package title or identifier is missing")
            unique_identifier = str(opf.get("unique-identifier") or "").strip()
            bound_identifiers = opf.xpath(
                "/opf:package/opf:metadata/dc:identifier[@id=$identifier]",
                namespaces={"opf": _OPF_NS, "dc": _DC_NS},
                identifier=unique_identifier,
            )
            if not unique_identifier or len(bound_identifiers) != 1:
                raise _CheckFailure(
                    "epub_unique_identifier_invalid",
                    "EPUB package unique-identifier does not bind exactly one "
                    "dc:identifier",
                )
            modified = [
                str(value).strip()
                for value in opf.xpath(
                    "/opf:package/opf:metadata/opf:meta"
                    "[@property='dcterms:modified']/text()",
                    namespaces={"opf": _OPF_NS},
                )
            ]
            if len(modified) != 1 or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
                modified[0] if modified else "",
            ):
                raise _CheckFailure(
                    "epub_modified_invalid",
                    "EPUB package must contain one UTC dcterms:modified value",
                )
            manifest_items = opf.xpath("/opf:package/opf:manifest/opf:item", namespaces={"opf": _OPF_NS})
            if not manifest_items:
                raise _CheckFailure("epub_manifest_empty", "EPUB package manifest is empty")
            items: dict[str, dict[str, str]] = {}
            members: dict[str, str] = {}
            nav_ids: list[str] = []
            for element in manifest_items:
                item_id = str(element.get("id") or "")
                href = str(element.get("href") or "")
                media_type = str(element.get("media-type") or "")
                if not item_id or item_id in items or not href or not media_type:
                    raise _CheckFailure("epub_manifest_invalid", "EPUB manifest ids/hrefs/media types are missing or duplicated")
                member = _manifest_member(opf_member, href)
                if member in members or member not in names:
                    raise _CheckFailure("epub_resource_missing", f"EPUB manifest resource is missing or duplicated: {href!r}")
                properties = str(element.get("properties") or "").split()
                if "nav" in properties:
                    nav_ids.append(item_id)
                items[item_id] = {"member": member, "media_type": media_type, "href": href}
                members[member] = item_id
            if len(nav_ids) != 1 or items[nav_ids[0]]["media_type"] != "application/xhtml+xml":
                raise _CheckFailure("epub_nav_manifest_invalid", "EPUB manifest must declare one XHTML navigation document")
            spine_elements = opf.xpath("/opf:package/opf:spine/opf:itemref", namespaces={"opf": _OPF_NS})
            spine_ids = [str(element.get("idref") or "") for element in spine_elements]
            if not spine_ids or len(spine_ids) != len(set(spine_ids)):
                raise _CheckFailure("epub_spine_invalid", "EPUB spine is empty or contains duplicate references")
            for item_id in spine_ids:
                if item_id not in items or items[item_id]["media_type"] != "application/xhtml+xml":
                    raise _CheckFailure("epub_spine_invalid", f"EPUB spine references an invalid item: {item_id!r}")
            expected_resources = set(members)
            packaged_resources = {
                name
                for name in names
                if name not in {"mimetype", _CONTAINER, opf_member}
                and not name.startswith("META-INF/")
            }
            if packaged_resources != expected_resources:
                raise _CheckFailure(
                    "epub_resource_inventory_mismatch",
                    f"EPUB packaged resources differ from manifest: missing={sorted(expected_resources - packaged_resources)}, extra={sorted(packaged_resources - expected_resources)}",
                )
            self.context.update(
                archive_names=set(names),
                opf_member=opf_member,
                manifest_items=items,
                manifest_members=members,
                spine_members=[items[item_id]["member"] for item_id in spine_ids],
                nav_member=items[nav_ids[0]]["member"],
                package_title=titles[0],
                artifact_sha256_before=before_sha,
            )
        return {"member_count": len(names), "resource_count": len(self.context["manifest_members"]), "spine_count": len(self.context["spine_members"]), "size": artifact_stat.st_size}

    def navigation(self) -> Mapping[str, Any]:
        nav_member = self.context.get("nav_member")
        spine_members = self.context.get("spine_members")
        if not isinstance(nav_member, str) or not isinstance(spine_members, list):
            raise _CheckFailure("prerequisite_failed", "EPUB package check did not resolve navigation")
        with zipfile.ZipFile(self.artifact_path) as archive:
            nav = _xml(archive.read(nav_member), label="EPUB navigation document")
        if nav.tag != f"{{{_XHTML_NS}}}html":
            raise _CheckFailure("epub_nav_xhtml_invalid", "EPUB navigation document is not XHTML")
        nav_language = str(
            nav.get("lang") or nav.get(f"{{{_XML_NS}}}lang") or ""
        )
        try:
            normalized_nav_language = normalize_epub_language(nav_language)
        except EpubPublicationVerificationError as exc:
            raise _CheckFailure(
                "epub_nav_language_invalid",
                f"invalid EPUB navigation language: {exc}",
            ) from exc
        if normalized_nav_language != self.expected_language:
            raise _CheckFailure(
                "epub_nav_language_mismatch",
                "EPUB navigation language differs from the package language",
            )
        nav_nodes = nav.xpath("//x:nav[contains(concat(' ', normalize-space(@epub:type), ' '), ' toc ')]", namespaces={"x": _XHTML_NS, "epub": _EPUB_NS})
        if len(nav_nodes) != 1:
            raise _CheckFailure("epub_nav_toc_invalid", "EPUB navigation document must contain one toc nav")
        links = nav_nodes[0].xpath(".//x:a", namespaces={"x": _XHTML_NS})
        resolved: list[str] = []
        labels: list[str] = []
        seen: set[tuple[str, str | None]] = set()
        with zipfile.ZipFile(self.artifact_path) as archive:
            for link in links:
                href = str(link.get("href") or "")
                target = _internal_member(nav_member, href)
                if target is None:
                    raise _CheckFailure("epub_nav_link_external", f"navigation link must be internal: {href!r}")
                member, fragment = target
                if member not in self.context["archive_names"]:
                    raise _CheckFailure("epub_nav_link_missing", f"navigation link target is missing: {href!r}")
                key = (member, fragment)
                if key in seen:
                    raise _CheckFailure("epub_nav_link_duplicate", f"navigation link is duplicated: {href!r}")
                seen.add(key)
                if fragment:
                    ids = _element_ids(_xml(archive.read(member), label=f"navigation target {member}"))
                    if fragment not in ids:
                        raise _CheckFailure("epub_nav_fragment_missing", f"navigation fragment is missing: {href!r}")
                resolved.append(member)
                labels.append(" ".join("".join(link.itertext()).split()))
        if resolved != spine_members:
            raise _CheckFailure("epub_nav_spine_mismatch", "navigation order does not match EPUB spine")
        chapters = self.context.get("chapters")
        expected_labels = (
            [str(chapter["display_title"]) for chapter in chapters]
            if isinstance(chapters, list)
            else []
        )
        if labels != expected_labels:
            raise _CheckFailure(
                "epub_nav_labels_mismatch",
                "navigation labels do not match canonical chapter titles",
            )
        return {"link_count": len(resolved)}

    def content(self) -> Mapping[str, Any]:
        chapters = self.context.get("chapters")
        spine_members = self.context.get("spine_members")
        if not isinstance(chapters, list) or not isinstance(spine_members, list):
            raise _CheckFailure("prerequisite_failed", "chapter/package checks did not resolve content")
        expected_members = [
            posixpath.join(posixpath.dirname(str(self.context["opf_member"])), Path(chapter["filename"]).with_suffix(".xhtml").name)
            for chapter in chapters
        ]
        if spine_members != expected_members:
            raise _CheckFailure("epub_spine_chapter_mismatch", "EPUB spine does not match canonical chapter filenames")
        total_links = 0
        total_footnotes = 0
        with zipfile.ZipFile(self.artifact_path) as archive:
            parsed: dict[str, etree._Element] = {
                member: _xml(archive.read(member), label=f"EPUB content {member}")
                for member in spine_members
            }
            ids_by_member = {member: _element_ids(root) for member, root in parsed.items()}
            for chapter, member in zip(chapters, spine_members):
                root = parsed[member]
                if root.tag != f"{{{_XHTML_NS}}}html":
                    raise _CheckFailure("epub_content_xhtml_invalid", f"spine content is not XHTML: {member}")
                lang = str(root.get("lang") or root.get(f"{{{_XML_NS}}}lang") or "")
                try:
                    normalized_lang = normalize_epub_language(lang)
                except EpubPublicationVerificationError as exc:
                    raise _CheckFailure(
                        "epub_content_language_invalid",
                        f"invalid content language in {member}: {exc}",
                    ) from exc
                if normalized_lang != self.expected_language:
                    raise _CheckFailure("epub_content_language_mismatch", f"content language mismatch in {member}")
                titles = root.xpath("/x:html/x:head/x:title", namespaces={"x": _XHTML_NS})
                title = (
                    " ".join("".join(titles[0].itertext()).split())
                    if len(titles) == 1
                    else ""
                )
                if title != str(chapter["display_title"]):
                    raise _CheckFailure(
                        "epub_content_title_mismatch",
                        f"content title differs from canonical manifest in {member}",
                    )
                bodies = root.xpath("/x:html/x:body", namespaces={"x": _XHTML_NS})
                if len(bodies) != 1:
                    raise _CheckFailure("epub_content_body_invalid", f"content body is missing in {member}")
                actual_text = _normalized_text(bodies[0])
                expected_text = _expected_markdown_body_text(
                    str(chapter["markdown"]),
                    publication_title=str(self.context.get("package_title") or ""),
                    chapter_title=str(chapter["display_title"]),
                    reviewed_override=bool(chapter["reviewed_override"]),
                )
                if actual_text != expected_text:
                    raise _CheckFailure("epub_content_text_mismatch", f"EPUB text differs from canonical Markdown in {member}")
                canonical_footnotes = int(chapter["footnotes"])
                definition_ids = {value for value in ids_by_member[member] if value.startswith("fn:")}
                reference_links = root.xpath("//x:a[starts-with(@href, '#fn:')]", namespaces={"x": _XHTML_NS})
                if len(definition_ids) != canonical_footnotes or len(reference_links) != canonical_footnotes:
                    raise _CheckFailure("epub_footnote_count_mismatch", f"EPUB footnotes differ from canonical Markdown in {member}")
                reference_ids = [
                    unquote(urlsplit(str(link.get("href") or "")).fragment)
                    for link in reference_links
                ]
                if (
                    len(set(reference_ids)) != len(reference_ids)
                    or set(reference_ids) != definition_ids
                ):
                    raise _CheckFailure(
                        "epub_footnote_bijection_mismatch",
                        "EPUB footnote references and definitions are not one-to-one "
                        f"closed in {member}",
                    )
                total_footnotes += canonical_footnotes
                for element in root.xpath("//*[@href] | //*[@src]"):
                    attribute = "href" if element.get("href") is not None else "src"
                    href = str(element.get(attribute) or "")
                    target = _internal_member(member, href)
                    if target is None:
                        if attribute == "src":
                            raise _CheckFailure("epub_external_resource", f"EPUB resource must be packaged: {href!r}")
                        continue
                    target_member, fragment = target
                    if target_member not in self.context["archive_names"]:
                        raise _CheckFailure("epub_internal_link_missing", f"EPUB internal target is missing: {href!r}")
                    if fragment:
                        target_root = parsed.get(target_member)
                        if target_root is None:
                            target_root = _xml(archive.read(target_member), label=f"EPUB link target {target_member}")
                            parsed[target_member] = target_root
                            ids_by_member[target_member] = _element_ids(target_root)
                        if fragment not in ids_by_member[target_member]:
                            raise _CheckFailure("epub_internal_fragment_missing", f"EPUB fragment target is missing: {href!r}")
                    total_links += 1
        return {"chapter_count": len(chapters), "internal_link_count": total_links, "footnote_count": total_footnotes}

    def artifact_identity(self) -> Mapping[str, Any]:
        entry = _require_regular_file(self.artifact_path, label="published EPUB")
        digest = _sha256_file(self.artifact_path)
        if digest != self.context.get("artifact_sha256_before"):
            raise _CheckFailure("artifact_changed_during_verification", "published EPUB changed during verification")
        self.context["artifact_sha256"] = digest
        self.context["artifact_size"] = entry.st_size
        return {"sha256": digest, "size": entry.st_size}

    def run(self) -> None:
        operations = (
            ("source.identity", self.source_identity),
            ("semantic.reconstruction", self.reconstruction),
            ("semantic.translation", self.translation),
            ("chapters.canonical", self.canonical_chapters),
            ("epub.package", self.package),
            ("epub.navigation", self.navigation),
            ("epub.content", self.content),
            ("artifact.identity", self.artifact_identity),
        )
        for check_id, operation in operations:
            self.check(check_id, operation)


def _resolve_artifact(output_dir: Path, artifact_path: Path | None) -> Path:
    if artifact_path is None:
        candidates = sorted(
            path for path in output_dir.glob("*.epub") if path.is_file() and not path.is_symlink()
        )
        if len(candidates) != 1:
            raise EpubPublicationVerificationError(
                "EPUB verification requires exactly one root-level .epub artifact "
                f"when artifact_path is omitted; found {len(candidates)}"
            )
        return candidates[0].resolve()
    unresolved = artifact_path.expanduser()
    _require_regular_file(unresolved, label="published EPUB")
    resolved = unresolved.resolve()
    if not _inside(resolved, output_dir):
        raise EpubPublicationVerificationError("published EPUB must remain inside output_dir")
    if resolved.suffix.casefold() != ".epub":
        raise EpubPublicationVerificationError("published EPUB must use the .epub extension")
    return resolved


def _flatten_errors(checks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []
    for check in checks:
        for issue in check.get("issues", []):
            if isinstance(issue, Mapping):
                errors.append(
                    {
                        "check_id": check.get("id"),
                        "code": issue.get("code"),
                        "message": issue.get("message"),
                    }
                )
    return errors


def verify_epub_publication(
    output_dir: Path | str,
    *,
    source_epub: Path | str,
    artifact_path: Path | str | None = None,
    target_language: str = "简体中文",
    require_translation: bool = True,
    expected_translation_sha256: str | None = None,
    report_path: Path | str | None = None,
) -> dict[str, Any]:
    """Verify and report one EPUB-native publication without model calls.

    Quality failures are returned in the report.  Invocation errors such as an
    ambiguous artifact selection or an out-of-root report path raise
    :class:`EpubPublicationVerificationError` before publication state is
    changed.
    """

    if type(require_translation) is not bool:
        raise EpubPublicationVerificationError("require_translation must be a boolean")
    if expected_translation_sha256 is not None and (
        not isinstance(expected_translation_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_translation_sha256)
    ):
        raise EpubPublicationVerificationError(
            "expected_translation_sha256 must be a lowercase SHA-256 digest"
        )
    if not require_translation and expected_translation_sha256 is not None:
        raise EpubPublicationVerificationError(
            "expected_translation_sha256 requires require_translation=True"
        )
    if not isinstance(target_language, str) or not target_language.strip():
        raise EpubPublicationVerificationError("target_language must be a non-empty string")
    raw_output = Path(output_dir).expanduser()
    output_entry = _lstat(raw_output)
    if (
        output_entry is None
        or stat.S_ISLNK(output_entry.st_mode)
        or not stat.S_ISDIR(output_entry.st_mode)
    ):
        raise EpubPublicationVerificationError(
            f"output_dir is not a regular directory: {raw_output}"
        )
    output = raw_output.resolve()
    raw_source = Path(source_epub).expanduser()
    try:
        _require_regular_file(raw_source, label="source EPUB")
    except _CheckFailure as exc:
        raise EpubPublicationVerificationError(str(exc)) from exc
    source = raw_source.resolve()
    artifact = _resolve_artifact(
        output,
        Path(artifact_path) if artifact_path is not None else None,
    )
    if report_path is None:
        destination = output / "audit" / DEFAULT_REPORT_NAME
    else:
        requested_report = Path(report_path).expanduser()
        destination = (
            requested_report
            if requested_report.is_absolute()
            else output / requested_report
        )
    verifier = _Verifier(
        output,
        source,
        artifact,
        target_language=target_language,
        require_translation=require_translation,
        expected_translation_sha256=expected_translation_sha256,
    )
    verifier.run()
    errors = _flatten_errors(verifier.checks)
    release_ready = not errors and tuple(check["id"] for check in verifier.checks) == CHECK_IDS
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "verifier_node": VERIFIER_NODE,
        "status": "passed" if release_ready else "failed",
        "ok": release_ready,
        "release_ready": release_ready,
        "mode": "full",
        "profile": PUBLICATION_PROFILE,
        "publication_profile": PUBLICATION_PROFILE,
        "output_dir": str(output),
        "source_epub": {
            "path": str(source),
            "sha256": verifier.context.get("source_sha256"),
        },
        "artifact": {
            "target": "publication.epub",
            "name": artifact.name,
            "path": str(artifact),
            "sha256": verifier.context.get("artifact_sha256"),
            "size": verifier.context.get("artifact_size"),
            "media_type": _MIMETYPE,
        },
        "semantic": {
            "reconstruction_sha256": verifier.context.get("reconstruction_sha256"),
            "translation_sha256": verifier.context.get("translation_sha256"),
            "translation_input_sha256": verifier.context.get(
                "translation_input_sha256"
            ),
            "canonical_manifest_sha256": verifier.context.get(
                "canonical_manifest_sha256"
            ),
            "canonical_chapters_sha256": verifier.context.get(
                "canonical_chapters_sha256"
            ),
            "translation_required": require_translation,
            "translation_applied": verifier.context.get("translation_applied", False),
        },
        "expected_language": verifier.expected_language,
        "report_path": str(destination.expanduser().resolve()),
        "summary": {
            "passed": sum(check["status"] == "passed" for check in verifier.checks),
            "failed": sum(check["status"] == "failed" for check in verifier.checks),
            "check_count": len(verifier.checks),
            "error_count": len(errors),
        },
        "checks": verifier.checks,
        "errors": errors,
        "warnings": [],
    }
    _atomic_report(destination, report, output_dir=output)
    return report


__all__ = [
    "CHECK_IDS",
    "DEFAULT_REPORT_NAME",
    "EpubPublicationVerificationError",
    "PUBLICATION_PROFILE",
    "SCHEMA_VERSION",
    "VERIFIER_NODE",
    "VERIFIER_VERSION",
    "normalize_epub_language",
    "verify_epub_publication",
]
