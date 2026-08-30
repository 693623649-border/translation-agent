"""Deterministically materialize true Word footnotes from stable markers.

``python-docx`` does not expose a public API for creating true footnotes.  This
module therefore performs the small OOXML patch after the high-level document
has been saved.  Authors put ``[[FN:<stable-id>]]`` in normal runs and pass an
ordered collection of ``(stable_id, note_text)`` pairs to
:func:`patch_docx_footnotes`.

The patch is deliberately strict: every supplied note must have exactly one
marker and every marker must have exactly one supplied note.  This prevents a
publication from silently shipping orphan references or definitions.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence, Tuple, Union

from os import PathLike

from lxml import etree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKGREL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
XML_NS = "http://www.w3.org/XML/1998/namespace"

REL_TYPE_FOOTNOTES = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
)


def replace_with_retry(
    source: PathLike,
    target: PathLike,
    *,
    attempts: int = 5,
    delay: float = 2.0,
) -> None:
    """Atomically rename ``source`` over ``target`` with bounded retries.

    A lingering Word renderer can keep the previous publication open on
    Windows, so the first replace attempt fails with PermissionError.
    Retrying for a bounded window absorbs that transient lock and surfaces
    a clear error when it persists.
    """

    source_path = Path(source)
    target_path = Path(target)
    for attempt in range(1, attempts + 1):
        try:
            os.replace(str(source_path), str(target_path))
            return
        except PermissionError:
            if attempt == attempts:
                raise
            time.sleep(delay)
CONTENT_TYPE_FOOTNOTES = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
)

DOCUMENT_PART = "word/document.xml"
DOCUMENT_RELS_PART = "word/_rels/document.xml.rels"
FOOTNOTES_PART = "word/footnotes.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"

_W = "{%s}" % W_NS
_REL = "{%s}" % PKGREL_NS
_CT = "{%s}" % CT_NS
_MARKER_RE = re.compile(r"\[\[FN:([^\]\r\n]+)\]\]")

PathLike = Union[str, os.PathLike]
OrderedNotes = Union[Mapping[str, str], Iterable[Tuple[str, str]]]


class FootnotePatchError(ValueError):
    """Raised when marker or OOXML invariants would be violated."""


@dataclass(frozen=True)
class FootnoteInventory:
    """Structural inventory for true footnotes in a DOCX package."""

    has_part: bool
    reference_ids: Tuple[int, ...]
    definition_ids: Tuple[int, ...]
    separators: Tuple[Tuple[int, Union[str, None]], ...]
    relationship_ids: Tuple[str, ...]
    content_type_parts: Tuple[str, ...]
    problems: Tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class FootnotePatchResult:
    """Result of a completed, structurally audited footnote patch."""

    output_path: Path
    stable_to_word_id: Mapping[str, int]
    inventory: FootnoteInventory


def _xml(data: bytes, part_name: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
    try:
        return etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise FootnotePatchError("invalid XML in %s: %s" % (part_name, exc)) from exc


def _xml_bytes(root: etree._Element) -> bytes:
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
    )


def _attribute_int(
    element: etree._Element,
    attribute: str,
    problem_prefix: str,
    problems: list,
) -> Union[int, None]:
    value = element.get(attribute)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        problems.append("%s:%r" % (problem_prefix, value))
        return None


def inspect_docx_footnotes(docx_path: PathLike) -> FootnoteInventory:
    """Return a deterministic inventory and consistency problems for a DOCX.

    A package without any footnote artifacts is considered valid.  Once any
    footnote artifact is present, the part, relationship, content-type
    override, both typed separators, and a one-to-one set of positive
    reference/definition IDs are all required.
    """

    path = Path(docx_path)
    problems = []
    reference_ids = []
    definition_ids = []
    separators = []
    relationship_ids = []
    content_type_parts = []

    with zipfile.ZipFile(path, "r") as archive:
        names = set(archive.namelist())
        for required in (DOCUMENT_PART, DOCUMENT_RELS_PART, CONTENT_TYPES_PART):
            if required not in names:
                raise FootnotePatchError("DOCX is missing required part: %s" % required)

        document = _xml(archive.read(DOCUMENT_PART), DOCUMENT_PART)
        for reference in document.xpath(".//w:footnoteReference", namespaces={"w": W_NS}):
            note_id = _attribute_int(
                reference,
                _W + "id",
                "invalid_reference_id",
                problems,
            )
            if note_id is not None:
                reference_ids.append(note_id)

        has_part = FOOTNOTES_PART in names
        if has_part:
            footnotes = _xml(archive.read(FOOTNOTES_PART), FOOTNOTES_PART)
            if footnotes.tag != _W + "footnotes":
                problems.append("invalid_footnotes_root")
            for note in footnotes.findall(_W + "footnote"):
                note_id = _attribute_int(
                    note,
                    _W + "id",
                    "invalid_definition_id",
                    problems,
                )
                if note_id is None:
                    continue
                if note_id > 0:
                    definition_ids.append(note_id)
                elif note_id in (-1, 0):
                    separators.append((note_id, note.get(_W + "type")))
                else:
                    problems.append("unsupported_reserved_id:%d" % note_id)

        relationships = _xml(archive.read(DOCUMENT_RELS_PART), DOCUMENT_RELS_PART)
        for relationship in relationships.findall(_REL + "Relationship"):
            if relationship.get("Type") == REL_TYPE_FOOTNOTES:
                relationship_ids.append(relationship.get("Id") or "")
                if relationship.get("Target") != "footnotes.xml":
                    problems.append("invalid_footnotes_relationship_target")
                if relationship.get("TargetMode") is not None:
                    problems.append("external_footnotes_relationship")

        content_types = _xml(archive.read(CONTENT_TYPES_PART), CONTENT_TYPES_PART)
        for override in content_types.findall(_CT + "Override"):
            if override.get("PartName") == "/word/footnotes.xml":
                content_type_parts.append(override.get("PartName") or "")
                if override.get("ContentType") != CONTENT_TYPE_FOOTNOTES:
                    problems.append("invalid_footnotes_content_type")

    artifacts_present = bool(
        has_part
        or reference_ids
        or relationship_ids
        or content_type_parts
        or any(problem.startswith("invalid_reference_id") for problem in problems)
    )
    if artifacts_present:
        if not has_part:
            problems.append("missing_footnotes_part")
        if len(relationship_ids) != 1:
            problems.append("footnotes_relationship_count:%d" % len(relationship_ids))
        if len(content_type_parts) != 1:
            problems.append("footnotes_content_type_count:%d" % len(content_type_parts))

        separator_counts = Counter(note_id for note_id, _ in separators)
        if separator_counts[-1] != 1:
            problems.append("separator_count:%d" % separator_counts[-1])
        if separator_counts[0] != 1:
            problems.append(
                "continuation_separator_count:%d" % separator_counts[0]
            )
        separator_types = {note_id: note_type for note_id, note_type in separators}
        if separator_counts[-1] == 1 and separator_types.get(-1) != "separator":
            problems.append("invalid_separator_type")
        if (
            separator_counts[0] == 1
            and separator_types.get(0) != "continuationSeparator"
        ):
            problems.append("invalid_continuation_separator_type")

        reference_counts = Counter(reference_ids)
        definition_counts = Counter(definition_ids)
        if any(note_id <= 0 for note_id in reference_ids):
            problems.append("nonpositive_reference_id")
        if any(count != 1 for count in reference_counts.values()):
            problems.append("duplicate_reference_id")
        if any(count != 1 for count in definition_counts.values()):
            problems.append("duplicate_definition_id")
        if reference_counts != definition_counts:
            problems.append("reference_definition_mismatch")

    return FootnoteInventory(
        has_part=has_part,
        reference_ids=tuple(reference_ids),
        definition_ids=tuple(definition_ids),
        separators=tuple(separators),
        relationship_ids=tuple(relationship_ids),
        content_type_parts=tuple(content_type_parts),
        problems=tuple(dict.fromkeys(problems)),
    )


def _normalize_notes(notes: OrderedNotes) -> Sequence[Tuple[str, str]]:
    raw_items = notes.items() if isinstance(notes, Mapping) else notes
    normalized = []
    seen = set()
    try:
        iterator = iter(raw_items)
    except TypeError as exc:
        raise FootnotePatchError("notes must be a mapping or iterable of pairs") from exc

    for item in iterator:
        try:
            stable_id, text = item
        except (TypeError, ValueError) as exc:
            raise FootnotePatchError("each footnote must be a (stable_id, text) pair") from exc
        if not isinstance(stable_id, str) or not stable_id:
            raise FootnotePatchError("footnote stable IDs must be non-empty strings")
        if "]" in stable_id or "\r" in stable_id or "\n" in stable_id:
            raise FootnotePatchError("invalid footnote stable ID: %r" % stable_id)
        if stable_id in seen:
            raise FootnotePatchError("duplicate footnote stable ID: %s" % stable_id)
        if not isinstance(text, str):
            raise FootnotePatchError("footnote text for %s must be a string" % stable_id)
        if not text.strip():
            raise FootnotePatchError("footnote text for %s must not be blank" % stable_id)
        seen.add(stable_id)
        normalized.append((stable_id, text))

    if not normalized:
        raise FootnotePatchError("at least one footnote is required")
    return tuple(normalized)


def _simple_run_text(run: etree._Element) -> Union[str, None]:
    if run.tag != _W + "r":
        return None
    allowed = {_W + "rPr", _W + "t"}
    if any(child.tag not in allowed for child in run):
        return None
    text_nodes = run.findall(_W + "t")
    if not text_nodes:
        return None
    return "".join(node.text or "" for node in text_nodes)


def _run_groups(root: etree._Element):
    """Yield contiguous, simple direct-run groups from every run container."""

    for parent in list(root.iter()):
        group = []
        for child in list(parent):
            if _simple_run_text(child) is not None:
                group.append(child)
            else:
                if group:
                    yield parent, tuple(group)
                    group = []
        if group:
            yield parent, tuple(group)


def _paragraph_marker_ids(root: etree._Element) -> list:
    marker_ids = []
    for paragraph in root.xpath(".//w:p", namespaces={"w": W_NS}):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces={"w": W_NS}))
        marker_ids.extend(match.group(1) for match in _MARKER_RE.finditer(text))
    return marker_ids


def _processable_marker_ids(root: etree._Element) -> list:
    marker_ids = []
    for _, runs in _run_groups(root):
        text = "".join(_simple_run_text(run) or "" for run in runs)
        marker_ids.extend(match.group(1) for match in _MARKER_RE.finditer(text))
    return marker_ids


def _clone_text_run(run: etree._Element, text: str) -> etree._Element:
    cloned = deepcopy(run)
    for child in list(cloned):
        if child.tag != _W + "rPr":
            cloned.remove(child)
    node = etree.SubElement(cloned, _W + "t")
    node.text = text
    if text[:1].isspace() or text[-1:].isspace():
        node.set("{%s}space" % XML_NS, "preserve")
    return cloned


def _reference_run(note_id: int) -> etree._Element:
    run = etree.Element(_W + "r")
    properties = etree.SubElement(run, _W + "rPr")
    style = etree.SubElement(properties, _W + "rStyle")
    style.set(_W + "val", "FootnoteReference")
    reference = etree.SubElement(run, _W + "footnoteReference")
    reference.set(_W + "id", str(note_id))
    return run


def _text_runs_for_range(
    runs: Sequence[etree._Element],
    texts: Sequence[str],
    start: int,
    end: int,
) -> list:
    output = []
    run_start = 0
    for run, text in zip(runs, texts):
        run_end = run_start + len(text)
        overlap_start = max(start, run_start)
        overlap_end = min(end, run_end)
        if overlap_start < overlap_end:
            fragment = text[overlap_start - run_start : overlap_end - run_start]
            if fragment:
                output.append(_clone_text_run(run, fragment))
        run_start = run_end
    return output


def _replace_markers(root: etree._Element, word_ids: Mapping[str, int]) -> None:
    for parent, runs in list(_run_groups(root)):
        texts = tuple(_simple_run_text(run) or "" for run in runs)
        combined = "".join(texts)
        matches = list(_MARKER_RE.finditer(combined))
        if not matches:
            continue

        replacements = []
        cursor = 0
        for match in matches:
            replacements.extend(
                _text_runs_for_range(runs, texts, cursor, match.start())
            )
            replacements.append(_reference_run(word_ids[match.group(1)]))
            cursor = match.end()
        replacements.extend(_text_runs_for_range(runs, texts, cursor, len(combined)))

        insertion_index = parent.index(runs[0])
        for run in runs:
            parent.remove(run)
        for offset, replacement in enumerate(replacements):
            parent.insert(insertion_index + offset, replacement)


def _new_separator(note_id: int, note_type: str) -> etree._Element:
    note = etree.Element(_W + "footnote")
    note.set(_W + "id", str(note_id))
    note.set(_W + "type", note_type)
    paragraph = etree.SubElement(note, _W + "p")
    run = etree.SubElement(paragraph, _W + "r")
    etree.SubElement(run, _W + note_type)
    return note


def _normalize_separators(footnotes: etree._Element) -> None:
    for note in list(footnotes.findall(_W + "footnote")):
        raw_id = note.get(_W + "id")
        if raw_id in ("-1", "0"):
            footnotes.remove(note)
    footnotes.insert(0, _new_separator(0, "continuationSeparator"))
    footnotes.insert(0, _new_separator(-1, "separator"))


def _footnote_paragraph(text: str, include_reference: bool) -> etree._Element:
    paragraph = etree.Element(_W + "p")
    properties = etree.SubElement(paragraph, _W + "pPr")
    style = etree.SubElement(properties, _W + "pStyle")
    style.set(_W + "val", "FootnoteText")
    if include_reference:
        reference_run = etree.SubElement(paragraph, _W + "r")
        reference_properties = etree.SubElement(reference_run, _W + "rPr")
        reference_style = etree.SubElement(reference_properties, _W + "rStyle")
        reference_style.set(_W + "val", "FootnoteReference")
        etree.SubElement(reference_run, _W + "footnoteRef")
    if text or include_reference:
        run = etree.SubElement(paragraph, _W + "r")
        node = etree.SubElement(run, _W + "t")
        node.text = (" " if include_reference else "") + text
        if include_reference or text[:1].isspace() or text[-1:].isspace():
            node.set("{%s}space" % XML_NS, "preserve")
    return paragraph


def _append_definition(
    footnotes: etree._Element,
    note_id: int,
    text: str,
) -> None:
    note = etree.SubElement(footnotes, _W + "footnote")
    note.set(_W + "id", str(note_id))
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        note.append(_footnote_paragraph(line, include_reference=index == 0))


def _ensure_relationship(relationships: etree._Element) -> None:
    note_relationships = [
        relationship
        for relationship in relationships.findall(_REL + "Relationship")
        if relationship.get("Type") == REL_TYPE_FOOTNOTES
    ]
    if len(note_relationships) > 1:
        raise FootnotePatchError("multiple footnotes relationships are not supported")
    if note_relationships:
        relationship = note_relationships[0]
        relationship.set("Target", "footnotes.xml")
        relationship.attrib.pop("TargetMode", None)
        return

    used_ids = {
        relationship.get("Id")
        for relationship in relationships.findall(_REL + "Relationship")
    }
    relationship_id = "rIdFootnotes"
    suffix = 1
    while relationship_id in used_ids:
        relationship_id = "rIdFootnotes%d" % suffix
        suffix += 1
    relationship = etree.SubElement(relationships, _REL + "Relationship")
    relationship.set("Id", relationship_id)
    relationship.set("Type", REL_TYPE_FOOTNOTES)
    relationship.set("Target", "footnotes.xml")


def _ensure_content_type(content_types: etree._Element) -> None:
    overrides = [
        override
        for override in content_types.findall(_CT + "Override")
        if override.get("PartName") == "/word/footnotes.xml"
    ]
    if len(overrides) > 1:
        raise FootnotePatchError("multiple footnotes content-type overrides are not supported")
    if overrides:
        overrides[0].set("ContentType", CONTENT_TYPE_FOOTNOTES)
        return
    override = etree.SubElement(content_types, _CT + "Override")
    override.set("PartName", "/word/footnotes.xml")
    override.set("ContentType", CONTENT_TYPE_FOOTNOTES)


def _existing_positive_ids(footnotes: etree._Element) -> list:
    ids = []
    for note in footnotes.findall(_W + "footnote"):
        raw_id = note.get(_W + "id")
        try:
            note_id = int(raw_id) if raw_id is not None else None
        except ValueError as exc:
            raise FootnotePatchError("invalid existing footnote ID: %r" % raw_id) from exc
        if note_id is not None and note_id > 0:
            ids.append(note_id)
    if len(ids) != len(set(ids)):
        raise FootnotePatchError("existing footnote definition IDs are not unique")
    return ids


def _write_package_atomic(
    source_path: Path,
    output_path: Path,
    replacements: Mapping[str, bytes],
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".%s." % output_path.name,
        suffix=".tmp",
        dir=str(output_path.parent),
        delete=False,
    )
    temporary_path = Path(handle.name)
    handle.close()
    try:
        with zipfile.ZipFile(source_path, "r") as source, zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as destination:
            destination.comment = source.comment
            source_names = set()
            for info in source.infolist():
                if info.filename in source_names:
                    raise FootnotePatchError(
                        "DOCX contains duplicate ZIP member: %s" % info.filename
                    )
                source_names.add(info.filename)
                data = replacements.get(info.filename, source.read(info.filename))
                destination.writestr(info, data)
            for name, data in replacements.items():
                if name in source_names:
                    continue
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o600 << 16
                destination.writestr(info, data)

        # Windows implements ``os.fsync`` with ``_commit``, which rejects a
        # read-only descriptor even though POSIX accepts one.  Reopen the
        # completed package read/write so the durability barrier is portable.
        with temporary_path.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(output_path))
        return output_path
    except Exception:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def patch_docx_footnotes(
    input_docx: PathLike,
    output_docx: PathLike,
    notes: OrderedNotes,
) -> FootnotePatchResult:
    """Replace stable markers with true footnotes and atomically write a DOCX.

    Word IDs are allocated in the order supplied by ``notes``.  Existing valid
    footnotes are retained and new IDs start after the largest existing
    positive ID.  Markers may span adjacent, plain text runs; surrounding run
    properties and text order are retained.
    """

    input_path = Path(input_docx)
    output_path = Path(output_docx)
    ordered_notes = _normalize_notes(notes)

    if not input_path.is_file():
        raise FileNotFoundError(input_path)

    with zipfile.ZipFile(input_path, "r") as archive:
        names = set(archive.namelist())
        for required in (DOCUMENT_PART, DOCUMENT_RELS_PART, CONTENT_TYPES_PART):
            if required not in names:
                raise FootnotePatchError("DOCX is missing required part: %s" % required)

        document = _xml(archive.read(DOCUMENT_PART), DOCUMENT_PART)
        relationships = _xml(archive.read(DOCUMENT_RELS_PART), DOCUMENT_RELS_PART)
        content_types = _xml(archive.read(CONTENT_TYPES_PART), CONTENT_TYPES_PART)
        if FOOTNOTES_PART in names:
            footnotes = _xml(archive.read(FOOTNOTES_PART), FOOTNOTES_PART)
            if footnotes.tag != _W + "footnotes":
                raise FootnotePatchError("invalid footnotes root element")
        else:
            footnotes = etree.Element(
                _W + "footnotes",
                nsmap={"w": W_NS, "r": R_NS},
            )

        expected_ids = [stable_id for stable_id, _ in ordered_notes]
        paragraph_ids = _paragraph_marker_ids(document)
        processable_ids = _processable_marker_ids(document)
        if Counter(paragraph_ids) != Counter(processable_ids):
            raise FootnotePatchError(
                "a footnote marker crosses a complex run or container boundary"
            )
        unknown_ids = sorted(set(paragraph_ids).difference(expected_ids))
        if unknown_ids:
            raise FootnotePatchError(
                "markers have no footnote definitions: %s" % ", ".join(unknown_ids)
            )
        marker_counts = Counter(paragraph_ids)
        marker_errors = [
            "%s=%d" % (stable_id, marker_counts[stable_id])
            for stable_id in expected_ids
            if marker_counts[stable_id] != 1
        ]
        if marker_errors:
            raise FootnotePatchError(
                "each footnote needs exactly one marker: %s" % ", ".join(marker_errors)
            )

        existing_ids = _existing_positive_ids(footnotes)
        existing_references = []
        for reference in document.xpath(
            ".//w:footnoteReference", namespaces={"w": W_NS}
        ):
            raw_id = reference.get(_W + "id")
            try:
                note_id = int(raw_id) if raw_id is not None else None
            except ValueError as exc:
                raise FootnotePatchError(
                    "invalid existing footnote reference ID: %r" % raw_id
                ) from exc
            if note_id is None or note_id <= 0:
                raise FootnotePatchError(
                    "existing footnote references must use positive IDs"
                )
            existing_references.append(note_id)
        if Counter(existing_ids) != Counter(existing_references):
            raise FootnotePatchError(
                "existing footnote references and definitions are not one-to-one"
            )

        next_id = max(existing_ids, default=0) + 1
        stable_to_word_id = {
            stable_id: next_id + index
            for index, stable_id in enumerate(expected_ids)
        }
        _replace_markers(document, stable_to_word_id)
        _normalize_separators(footnotes)
        for stable_id, text in ordered_notes:
            _append_definition(footnotes, stable_to_word_id[stable_id], text)
        _ensure_relationship(relationships)
        _ensure_content_type(content_types)

        replacements = {
            DOCUMENT_PART: _xml_bytes(document),
            DOCUMENT_RELS_PART: _xml_bytes(relationships),
            CONTENT_TYPES_PART: _xml_bytes(content_types),
            FOOTNOTES_PART: _xml_bytes(footnotes),
        }

    # First write next to the destination; validation happens before the final
    # destination is considered complete.  If post-write validation fails, an
    # existing destination must remain untouched, so stage a second temporary
    # path and replace the destination only after inspection.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging_handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".%s.audit." % output_path.name,
        suffix=".docx",
        dir=str(output_path.parent),
        delete=False,
    )
    staging_path = Path(staging_handle.name)
    staging_handle.close()
    try:
        _write_package_atomic(input_path, staging_path, replacements)
        inventory = inspect_docx_footnotes(staging_path)
        if not inventory.valid:
            raise FootnotePatchError(
                "patched DOCX failed footnote audit: %s"
                % ", ".join(inventory.problems)
            )
        replace_with_retry(staging_path, output_path)
    except Exception:
        try:
            staging_path.unlink()
        except FileNotFoundError:
            pass
        raise

    return FootnotePatchResult(
        output_path=output_path,
        stable_to_word_id=MappingProxyType(dict(stable_to_word_id)),
        inventory=inventory,
    )


__all__ = [
    "FootnoteInventory",
    "FootnotePatchError",
    "FootnotePatchResult",
    "inspect_docx_footnotes",
    "patch_docx_footnotes",
]
