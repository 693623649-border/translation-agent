"""Repair duplicate EPUB footnote anchors that should point at anonymous notes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
import zipfile

from lxml import etree


EPUB_NS = "http://www.idpf.org/2007/ops"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
XHTML_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}


class RepairError(ValueError):
    """Raised when the EPUB cannot be repaired deterministically."""


@dataclass(frozen=True)
class _PackageItem:
    member: str
    media_type: str


@dataclass(frozen=True)
class _Noteref:
    element: etree._Element
    doc: str
    old_id: str
    occurrence: int


@dataclass(frozen=True)
class _Repair:
    doc: str
    old_id: str
    new_id: str
    occurrence: int


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def _replace_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


def _safe_member(value: str) -> str:
    decoded = unquote(value).replace("\\", "/")
    path = PurePosixPath(decoded)
    if path.is_absolute() or not decoded or any(part in {"", ".", ".."} for part in path.parts):
        raise RepairError(f"unsafe EPUB member path: {value!r}")
    return path.as_posix()


def _resolve_member(base_member: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise RepairError(f"external EPUB href is unsupported: {href!r}")
    joined = urljoin(PurePosixPath(base_member).parent.as_posix() + "/", parsed.path)
    return _safe_member(joined)


def _read_member(archive: zipfile.ZipFile, member: str) -> bytes:
    safe = _safe_member(member)
    try:
        return archive.read(safe)
    except KeyError as exc:
        raise RepairError(f"EPUB member is missing: {safe}") from exc


def _parse_xml(raw: bytes, *, member: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise RepairError(f"invalid XML/XHTML in {member}: {exc}") from exc
    if root is None:
        raise RepairError(f"empty XML/XHTML document: {member}")
    return root


def _epub_type(element: etree._Element) -> set[str]:
    value = (
        element.get(f"{{{EPUB_NS}}}type")
        or element.get("epub:type")
        or element.get("role")
        or ""
    )
    return {item.strip().lower() for item in value.split() if item.strip()}


def _element_id(element: etree._Element) -> str:
    return str(element.get("id") or element.get(XML_ID) or "")


def _set_element_id(element: etree._Element, value: str) -> None:
    if element.get(XML_ID) is not None and element.get("id") is None:
        element.set(XML_ID, value)
    else:
        element.set("id", value)


def _is_note(element: etree._Element) -> bool:
    return bool({"footnote", "endnote", "doc-footnote", "doc-endnote"} & _epub_type(element))


def _is_noteref(element: etree._Element) -> bool:
    return bool({"noteref", "doc-noteref"} & _epub_type(element))


def _package_items(archive: zipfile.ZipFile) -> dict[str, _PackageItem]:
    container = _parse_xml(
        _read_member(archive, "META-INF/container.xml"),
        member="META-INF/container.xml",
    )
    rootfiles = container.xpath("//*[local-name()='rootfile']/@full-path")
    if len(rootfiles) != 1:
        raise RepairError("EPUB must declare exactly one package rootfile")
    opf_member = _safe_member(str(rootfiles[0]))
    package = _parse_xml(_read_member(archive, opf_member), member=opf_member)
    items: dict[str, _PackageItem] = {}
    for item in package.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
        href = str(item.get("href") or "").strip()
        media_type = str(item.get("media-type") or "").strip()
        if not href:
            continue
        member = _resolve_member(opf_member, href)
        items[member] = _PackageItem(member=member, media_type=media_type)
    return items


def _candidate_docs(archive: zipfile.ZipFile) -> list[str]:
    items = _package_items(archive)
    return [
        name
        for name in archive.namelist()
        if items.get(name, _PackageItem(name, "")).media_type in XHTML_MEDIA_TYPES
    ]


def _href_for_id(old_href: str, new_id: str) -> str:
    parsed = urlsplit(old_href)
    if parsed.path:
        return parsed._replace(fragment=new_id).geturl()
    return f"#{new_id}"


def _stable_new_id(old_id: str, occurrence: int) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.:-]+", "-", old_id).strip("-._:")
    if not stem or not re.match(r"^[A-Za-z_]", stem):
        stem = f"fn-{stem}" if stem else "fn"
    return f"{stem}__repair_{occurrence}"


def _copy_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    clone = zipfile.ZipInfo(info.filename, info.date_time)
    clone.comment = info.comment
    clone.extra = info.extra
    clone.internal_attr = info.internal_attr
    clone.external_attr = info.external_attr
    clone.create_system = info.create_system
    clone.compress_type = info.compress_type
    return clone


def _write_zip(
    source: Path,
    output: Path,
    replacements: dict[str, bytes],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(source, "r") as src, zipfile.ZipFile(temporary, "w") as dst:
            for index, info in enumerate(src.infolist()):
                data = replacements.get(info.filename)
                if data is None:
                    data = src.read(info)
                clone = _copy_info(info)
                if index == 0 and info.filename == "mimetype":
                    clone.compress_type = zipfile.ZIP_STORED
                dst.writestr(clone, data)
        _replace_file(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def _serialize(root: etree._Element, original: bytes) -> bytes:
    has_declaration = original.lstrip().startswith(b"<?xml")
    return etree.tostring(
        root,
        encoding="utf-8",
        xml_declaration=has_declaration,
        pretty_print=False,
    )


def _validate_output_path(input_path: Path, output_path: Path) -> None:
    if input_path.resolve() == output_path.resolve():
        raise RepairError("output must not overwrite input EPUB")


def repair_epub_footnote_anchors(
    input_path: Path,
    output_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    _validate_output_path(input_path, output_path)
    if not input_path.is_file() or input_path.suffix.lower() != ".epub":
        raise RepairError(f"input must be an existing EPUB: {input_path}")
    if not zipfile.is_zipfile(input_path):
        raise RepairError(f"input is not a valid EPUB ZIP: {input_path}")

    replacements: dict[str, bytes] = {}
    repairs: list[_Repair] = []
    doc_counts: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(input_path, "r") as archive:
            docs = _candidate_docs(archive)
            roots: dict[str, etree._Element] = {}
            raws: dict[str, bytes] = {}
            for doc in docs:
                raw = _read_member(archive, doc)
                raws[doc] = raw
                roots[doc] = _parse_xml(raw, member=doc)

            targets: dict[tuple[str, str], etree._Element] = {}
            all_ids_by_doc: dict[str, set[str]] = {}
            anonymous_notes_by_doc: dict[str, list[etree._Element]] = {}
            for doc, root in roots.items():
                ids: set[str] = set()
                anonymous: list[etree._Element] = []
                for element in root.iter():
                    element_id = _element_id(element)
                    if element_id:
                        if element_id in ids:
                            raise RepairError(f"duplicate id {element_id!r} in {doc}")
                        ids.add(element_id)
                        targets[(doc, element_id)] = element
                    elif _is_note(element):
                        anonymous.append(element)
                all_ids_by_doc[doc] = ids
                anonymous_notes_by_doc[doc] = anonymous

            for doc, root in roots.items():
                seen: dict[tuple[str, str], int] = {}
                extra_noterefs: list[_Noteref] = []
                for element in root.iter():
                    if not (isinstance(element.tag, str) and element.tag.rsplit("}", 1)[-1].lower() == "a"):
                        continue
                    if not _is_noteref(element):
                        continue
                    href = str(element.get("href") or "")
                    parsed = urlsplit(href)
                    fragment = unquote(parsed.fragment)
                    target_doc = _resolve_member(doc, parsed.path) if parsed.path else doc
                    if not fragment or (target_doc, fragment) not in targets:
                        raise RepairError(f"noteref target missing in {doc}: {href!r}")
                    key = (target_doc, fragment)
                    occurrence = seen.get(key, 0) + 1
                    seen[key] = occurrence
                    if target_doc == doc and occurrence > 1:
                        extra_noterefs.append(
                            _Noteref(
                                element=element,
                                doc=doc,
                                old_id=fragment,
                                occurrence=occurrence,
                            )
                        )

                anonymous_notes = anonymous_notes_by_doc.get(doc, [])
                before = {
                    "doc": doc,
                    "anonymous_footnotes": len(anonymous_notes),
                    "duplicate_noteref_extra_occurrences": len(extra_noterefs),
                }
                if len(anonymous_notes) != len(extra_noterefs):
                    raise RepairError(
                        "anonymous footnote count does not match duplicate noteref "
                        f"extra occurrences in {doc}: {len(anonymous_notes)} != {len(extra_noterefs)}"
                    )
                doc_counts.append({**before, "after_anonymous_footnotes": 0})

                used_ids = all_ids_by_doc[doc]
                for noteref, note in zip(extra_noterefs, anonymous_notes, strict=True):
                    new_id = _stable_new_id(noteref.old_id, noteref.occurrence)
                    if new_id in used_ids:
                        raise RepairError(f"generated id collision in {doc}: {new_id}")
                    used_ids.add(new_id)
                    _set_element_id(note, new_id)
                    old_href = str(noteref.element.get("href") or "")
                    noteref.element.set("href", _href_for_id(old_href, new_id))
                    repairs.append(
                        _Repair(
                            doc=doc,
                            old_id=noteref.old_id,
                            new_id=new_id,
                            occurrence=noteref.occurrence,
                        )
                    )

            for doc, root in roots.items():
                if any(repair.doc == doc for repair in repairs):
                    replacements[doc] = _serialize(root, raws[doc])

        report = {
            "status": "passed",
            "input": str(input_path),
            "output": str(output_path),
            "repair_count": len(repairs),
            "counts": doc_counts,
            "repairs": [repair.__dict__ for repair in repairs],
        }
        _write_zip(input_path, output_path, replacements)
        _atomic_write_json(report_path, report)
        return report
    except Exception as exc:
        output_path.unlink(missing_ok=True)
        report = {
            "status": "blocked",
            "input": str(input_path),
            "output": str(output_path),
            "error": str(exc),
            "repair_count": len(repairs),
            "counts": doc_counts,
            "repairs": [repair.__dict__ for repair in repairs],
        }
        _atomic_write_json(report_path, report)
        if isinstance(exc, RepairError):
            raise
        raise RepairError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair duplicate EPUB noteref anchors to anonymous footnote asides."
    )
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = repair_epub_footnote_anchors(
            Path(args.input),
            Path(args.output),
            Path(args.report),
        )
    except RepairError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
