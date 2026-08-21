"""Import born-digital EPUB books into the publication semantic layer.

The PDF pipeline reconstructs continuous chapters from page checkpoints.  An
EPUB already has a reading order, so this importer uses the package spine as
the source boundary and emits the same ``chapters.json`` + chapter Markdown
contract consumed by the existing EPUB/DOCX publishers.

No model is called here.  Translation is exchanged as hash-bound JSONL units:
``import`` writes ``semantic/translation-units.jsonl`` and
``apply-translations`` accepts the same records with ``translated_markdown``.
This keeps source extraction deterministic and lets any translation provider
operate outside the structural reconstruction step.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import unquote, urljoin, urlsplit
import zipfile

from lxml import etree

from publication_semantics import (
    markdown_footnote_contract_sha256,
    parse_markdown_footnotes,
    prune_long_markdown_footnotes,
    prune_standalone_page_markers,
    semantic_audit_summary,
)
from semantic_apply import SemanticApplyError, apply_translation_transaction


EPUB_NS = "http://www.idpf.org/2007/ops"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
SCHEMA_VERSION = 1
IMPORTER_VERSION = "epub-semantic-v1"


class EpubSemanticError(ValueError):
    """Raised when an EPUB cannot prove a safe semantic reconstruction."""


@dataclass(frozen=True)
class EpubMetadata:
    title: str
    author: str
    language: str
    identifier: str


@dataclass(frozen=True)
class _SpineDocument:
    item_id: str
    href: str
    media_type: str
    properties: frozenset[str]
    root: etree._Element
    raw_sha256: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


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


def _safe_member(value: str) -> str:
    decoded = unquote(value).replace("\\", "/")
    path = PurePosixPath(decoded)
    if path.is_absolute() or not decoded or any(part in {"", ".", ".."} for part in path.parts):
        raise EpubSemanticError(f"unsafe EPUB member path: {value!r}")
    return path.as_posix()


def _resolve_member(base_member: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise EpubSemanticError(f"external EPUB package href is unsupported: {href!r}")
    joined = urljoin(PurePosixPath(base_member).parent.as_posix() + "/", parsed.path)
    return _safe_member(joined)


def _read_member(archive: zipfile.ZipFile, member: str) -> bytes:
    safe = _safe_member(member)
    try:
        info = archive.getinfo(safe)
    except KeyError as exc:
        raise EpubSemanticError(f"EPUB member is missing: {safe}") from exc
    if info.file_size > 64 * 1024 * 1024:
        raise EpubSemanticError(f"EPUB member is unexpectedly large: {safe}")
    return archive.read(info)


def _parse_xml(raw: bytes, *, member: str) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise EpubSemanticError(f"invalid XML/XHTML in {member}: {exc}") from exc
    if root is None:
        raise EpubSemanticError(f"empty XML/XHTML document: {member}")
    return root


def _local_name(element: etree._Element) -> str:
    if not isinstance(element.tag, str):
        return ""
    return etree.QName(element).localname.lower()


def _text_value(element: etree._Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


def _epub_type(element: etree._Element) -> set[str]:
    value = (
        element.get(f"{{{EPUB_NS}}}type")
        or element.get("epub:type")
        or element.get("role")
        or ""
    )
    return {item.strip().lower() for item in value.split() if item.strip()}


def _package_documents(
    source: Path,
) -> tuple[
    EpubMetadata,
    list[_SpineDocument],
    dict[str, etree._Element],
    dict[str, str],
    str,
]:
    if not source.is_file() or source.suffix.lower() != ".epub":
        raise EpubSemanticError(f"input must be an existing EPUB: {source}")
    if not zipfile.is_zipfile(source):
        raise EpubSemanticError(f"input is not a valid EPUB ZIP: {source}")
    with zipfile.ZipFile(source) as archive:
        encrypted = "META-INF/encryption.xml" in archive.namelist()
        if encrypted:
            encryption = _read_member(archive, "META-INF/encryption.xml")
            if b"EncryptedData" in encryption:
                raise EpubSemanticError("encrypted/DRM EPUB content is unsupported")
        container = _parse_xml(
            _read_member(archive, "META-INF/container.xml"),
            member="META-INF/container.xml",
        )
        rootfiles = container.xpath(
            "//*[local-name()='rootfile']/@full-path",
            namespaces={"c": CONTAINER_NS},
        )
        if len(rootfiles) != 1:
            raise EpubSemanticError("EPUB must declare exactly one package rootfile")
        opf_member = _safe_member(str(rootfiles[0]))
        package = _parse_xml(_read_member(archive, opf_member), member=opf_member)

        def metadata_value(local: str) -> str:
            values = package.xpath(
                f"//*[local-name()='metadata']/*[local-name()='{local}']"
            )
            return _text_value(values[0]) if values else ""

        metadata = EpubMetadata(
            title=metadata_value("title") or source.stem,
            author=metadata_value("creator"),
            language=metadata_value("language") or "und",
            identifier=metadata_value("identifier"),
        )
        manifest: dict[str, dict[str, str]] = {}
        for item in package.xpath("//*[local-name()='manifest']/*[local-name()='item']"):
            item_id = str(item.get("id") or "").strip()
            href = str(item.get("href") or "").strip()
            if not item_id or not href or item_id in manifest:
                raise EpubSemanticError("EPUB manifest contains missing or duplicate ids/hrefs")
            manifest[item_id] = {
                "href": _resolve_member(opf_member, href),
                "media_type": str(item.get("media-type") or ""),
                "properties": str(item.get("properties") or ""),
            }
        spine_refs = [
            str(item.get("idref") or "").strip()
            for item in package.xpath("//*[local-name()='spine']/*[local-name()='itemref']")
            if str(item.get("linear") or "yes").lower() != "no"
        ]
        if not spine_refs:
            raise EpubSemanticError("EPUB spine is empty")

        documents: list[_SpineDocument] = []
        all_roots: dict[str, etree._Element] = {}
        for item_id, item in manifest.items():
            if item["media_type"] not in {"application/xhtml+xml", "text/html"}:
                continue
            raw = _read_member(archive, item["href"])
            all_roots[item["href"]] = _parse_xml(raw, member=item["href"])
        title_hints: dict[str, str] = {}
        spine_nodes = package.xpath("//*[local-name()='spine']")
        ncx_id = str(spine_nodes[0].get("toc") or "") if spine_nodes else ""
        if ncx_id and ncx_id in manifest:
            ncx_item = manifest[ncx_id]
            ncx_root = _parse_xml(
                _read_member(archive, ncx_item["href"]),
                member=ncx_item["href"],
            )
            for point in ncx_root.xpath("//*[local-name()='navPoint']"):
                content = point.xpath("./*[local-name()='content']/@src")
                labels = point.xpath(
                    "./*[local-name()='navLabel']/*[local-name()='text']"
                )
                if content and labels:
                    href = _resolve_member(ncx_item["href"], str(content[0]))
                    label = _text_value(labels[0])
                    if label:
                        title_hints.setdefault(href, label)
        for item_id, item in manifest.items():
            if "nav" not in item["properties"].split() or item["href"] not in all_roots:
                continue
            for anchor in all_roots[item["href"]].xpath(
                "//*[local-name()='nav']//*[local-name()='a'][@href]"
            ):
                href = _resolve_member(item["href"], str(anchor.get("href")))
                label = _text_value(anchor)
                if label:
                    title_hints.setdefault(href, label)
        for item_id in spine_refs:
            if item_id not in manifest:
                raise EpubSemanticError(f"spine references missing manifest id: {item_id}")
            item = manifest[item_id]
            if item["href"] not in all_roots:
                raise EpubSemanticError(
                    f"spine item is not XHTML/HTML: {item['href']} ({item['media_type']})"
                )
            raw = _read_member(archive, item["href"])
            documents.append(
                _SpineDocument(
                    item_id=item_id,
                    href=item["href"],
                    media_type=item["media_type"],
                    properties=frozenset(item["properties"].split()),
                    root=all_roots[item["href"]],
                    raw_sha256=_sha256_bytes(raw),
                )
            )
    return metadata, documents, all_roots, title_hints, opf_member


def _element_id(element: etree._Element) -> str:
    return str(element.get("id") or element.get("{http://www.w3.org/XML/1998/namespace}id") or "")


def _is_note(element: etree._Element) -> bool:
    kinds = _epub_type(element)
    return bool({"footnote", "endnote", "doc-footnote", "doc-endnote"} & kinds)


def _is_noteref(element: etree._Element) -> bool:
    kinds = _epub_type(element)
    return bool({"noteref", "doc-noteref"} & kinds)


def _escape_markdown(value: str) -> str:
    return re.sub(r"([\\`*_[\]])", r"\\\1", value)


class _XhtmlRenderer:
    def __init__(self, documents: Mapping[str, etree._Element]):
        self.documents = documents
        self.note_roots: set[etree._Element] = set()
        self.targets: dict[tuple[str, str], etree._Element] = {}
        for href, root in documents.items():
            for element in root.iter():
                fragment = _element_id(element)
                if fragment:
                    self.targets[(href, fragment)] = element
                if _is_note(element):
                    self.note_roots.add(element)
        self.legacy_reference_targets: dict[
            etree._Element,
            tuple[str, str],
        ] = {}
        self.legacy_suppressed_links: set[etree._Element] = set()
        for href, root in documents.items():
            self._register_legacy_split_bracket_notes(href, root)
        self.reference_counts: dict[tuple[str, str], int] = {}
        self.definitions: list[tuple[str, str]] = []
        self.issues: list[dict[str, Any]] = []
        self.pending_issues: dict[str, list[dict[str, Any]]] = {}
        self.note_continuations: dict[etree._Element, list[etree._Element]] = {}
        self.element_hrefs: dict[etree._Element, str] = {}
        self.render_metrics: dict[str, dict[str, int]] = {}
        for href, root in documents.items():
            for parent in root.iter():
                last_identified_note: etree._Element | None = None
                for child in parent:
                    if not _is_note(child):
                        last_identified_note = None
                        continue
                    if _element_id(child):
                        last_identified_note = child
                    elif last_identified_note is not None:
                        self.note_continuations.setdefault(last_identified_note, []).append(child)
                    else:
                        self.pending_issues.setdefault(href, []).append(
                            {
                                "code": "epub_orphan_note_continuation",
                                "message": "匿名 EPUB 脚注续段之前没有带 ID 的脚注定义。",
                                "source_page": href,
                                "note_label": None,
                                "blocking": True,
                                "evidence": {
                                    "preview": _text_value(child)[:200],
                                },
                            }
                        )
            for element in root.iter():
                self.element_hrefs[element] = href

    @staticmethod
    def _legacy_note_target_id(fragment: str) -> tuple[str, str] | None:
        match = re.fullmatch(r"(?P<prefix>.+)_note_(?P<label>\d+)", fragment)
        if match:
            return (
                f"{match.group('prefix')}_noteBack_{match.group('label')}",
                match.group("label"),
            )
        match = re.fullmatch(r"(?P<prefix>.+)-(?P<label>\d+)-ref", fragment)
        if match:
            return (
                f"{match.group('prefix')}-{match.group('label')}-back",
                match.group("label"),
            )
        return None

    @staticmethod
    def _nearest_block(element: etree._Element) -> etree._Element:
        current = element
        while current.getparent() is not None:
            current = current.getparent()
            if _local_name(current) in {"p", "li", "aside", "div", "section"}:
                return current
        return element

    @staticmethod
    def _self_links(
        container: etree._Element,
        fragment: str,
    ) -> list[etree._Element]:
        links: list[etree._Element] = []
        for element in container.iter():
            if _local_name(element) != "a":
                continue
            parsed = urlsplit(str(element.get("href") or ""))
            if not parsed.path and unquote(parsed.fragment) == fragment:
                links.append(element)
        return links

    def _register_legacy_split_bracket_notes(
        self,
        href: str,
        root: etree._Element,
    ) -> None:
        """Recover self-linked ``[``/``1``/``]`` footnotes from old EPUBs.

        Some Calibre-era books encode a reference and its definition as two
        self-linked bracket triplets instead of reciprocal noteref/footnote
        links.  Treat the pair as a semantic footnote only when both IDs and
        both complete bracket labels are present; otherwise preserve the raw
        links for fail-visible review.
        """

        for element in root.iter():
            if _local_name(element) != "a":
                continue
            fragment = _element_id(element)
            target_spec = self._legacy_note_target_id(fragment)
            if not fragment or target_spec is None:
                continue
            target_fragment, label = target_spec
            target = self.targets.get((href, target_fragment))
            if target is None:
                continue
            reference_block = self._nearest_block(element)
            reference_links = self._self_links(reference_block, fragment)
            definition_links = self._self_links(target, target_fragment)
            reference_label = "".join(_text_value(link) for link in reference_links)
            definition_label = "".join(_text_value(link) for link in definition_links)
            expected_label = f"[{label}]"
            if (
                reference_label.replace(" ", "") != expected_label
                or definition_label.replace(" ", "") != expected_label
            ):
                continue
            self.legacy_reference_targets[element] = (href, target_fragment)
            self.legacy_suppressed_links.update(reference_links)
            self.legacy_suppressed_links.discard(element)
            self.legacy_suppressed_links.update(definition_links)
            self.note_roots.add(target)
            self.note_roots.add(self._nearest_block(target))

    def _note_reference(self, element: etree._Element, current_href: str) -> str:
        href = str(element.get("href") or "")
        legacy_target = self.legacy_reference_targets.get(element)
        if legacy_target is not None:
            target_href, fragment = legacy_target
            href = f"#{fragment}"
        else:
            parsed = urlsplit(href)
            fragment = unquote(parsed.fragment)
            try:
                target_href = _resolve_member(current_href, parsed.path) if parsed.path else current_href
            except EpubSemanticError:
                target_href = ""
        target = self.targets.get((target_href, fragment)) if fragment else None
        if target is None:
            self.issues.append(
                {
                    "code": "epub_footnote_target_missing",
                    "message": "EPUB 脚注引用无法解析到定义。",
                    "source_page": current_href,
                    "note_label": fragment or None,
                    "blocking": True,
                    "evidence": {"href": href},
                }
            )
            return self._inline_children(element, current_href)
        key = (target_href, fragment)
        occurrence = self.reference_counts.get(key, 0) + 1
        self.reference_counts[key] = occurrence
        digest = hashlib.sha256(f"{target_href}#{fragment}".encode("utf-8")).hexdigest()[:12]
        note_id = f"epub-{digest}-r{occurrence}"
        note_text = self._note_text(target, target_href)
        continuation_texts = [
            self._note_text(continuation, target_href)
            for continuation in self.note_continuations.get(target, [])
        ]
        note_text = "\n\n".join(
            value for value in [note_text, *continuation_texts] if value
        )
        if not note_text:
            self.issues.append(
                {
                    "code": "epub_footnote_definition_empty",
                    "message": "EPUB 脚注定义为空。",
                    "source_page": target_href,
                    "note_label": fragment,
                    "blocking": True,
                    "evidence": {"href": href},
                }
            )
            return self._inline_children(element, current_href)
        self.definitions.append((note_id, note_text))
        return f"[^{note_id}]"

    def _note_text(self, element: etree._Element, current_href: str) -> str:
        pieces: list[str] = []
        if element.text:
            pieces.append(element.text)
        for child in element:
            if _local_name(child) == "a" and (
                "backlink" in _epub_type(child)
                or str(child.get("href") or "").startswith("#")
                and _text_value(child) in {"↩", "↑", "back", "Back"}
            ):
                if child.tail:
                    pieces.append(child.tail)
                continue
            pieces.append(self._inline(child, current_href))
            if child.tail:
                pieces.append(child.tail)
        return " ".join("".join(pieces).split()).strip()

    def _inline_children(self, element: etree._Element, current_href: str) -> str:
        pieces = [element.text or ""]
        for child in element:
            pieces.append(self._inline(child, current_href))
            pieces.append(child.tail or "")
        return "".join(pieces)

    def _inline(self, element: etree._Element, current_href: str) -> str:
        tag = _local_name(element)
        if tag in {"script", "style"}:
            return ""
        if element in self.legacy_suppressed_links:
            return ""
        if tag == "a" and (
            _is_noteref(element) or element in self.legacy_reference_targets
        ):
            return self._note_reference(element, current_href)
        inner = self._inline_children(element, current_href)
        if tag in {"em", "i"} and inner.strip():
            return f"*{inner.strip()}*"
        if tag in {"strong", "b"} and inner.strip():
            return f"**{inner.strip()}**"
        if tag == "code" and inner.strip():
            return f"`{inner.strip()}`"
        if tag == "br":
            return "  \n"
        if tag == "img":
            alt = _escape_markdown(str(element.get("alt") or ""))
            src = str(element.get("src") or "")
            return f"![{alt}]({src})" if src else ""
        if tag == "a":
            href = str(element.get("href") or "")
            label = inner.strip()
            parsed = urlsplit(href)
            # Printed-page/index locators are reader labels, not semantic
            # cross-document links.  Keeping their href would leak source EPUB
            # coordinates into a newly published book.
            if (
                parsed.fragment.lower().startswith("page_")
                or re.fullmatch(r"[ivxlcdm]+|\d+(?:[-–]\d+)?", label, re.I)
                and parsed.path
            ):
                return _escape_markdown(label)
            escaped_label = _escape_markdown(label)
            return (
                f"[{escaped_label}]({href})"
                if href and escaped_label
                else escaped_label
            )
        if (
            tag == "sup"
            and "calibre14" in str(element.get("class") or "").split()
            and re.fullmatch(r"\d{1,4}", inner.strip())
            and not element.xpath(".//*[local-name()='a']")
        ):
            # This Calibre class is used by the source book for printed-page
            # digits embedded in the text flow.  Restrict the cleanup to the
            # class and to non-linked numbers so mathematical superscripts and
            # semantic footnote references remain intact.
            return ""
        if tag == "sup" and inner.strip():
            return f"<sup>{html.escape(inner.strip())}</sup>"
        if tag == "sub" and inner.strip():
            return f"<sub>{html.escape(inner.strip())}</sub>"
        return inner

    def _blocks(self, element: etree._Element, current_href: str) -> Iterator[str]:
        if element in self.note_roots:
            return
        tag = _local_name(element)
        if tag in {"script", "style", "head"}:
            return
        if tag in {f"h{level}" for level in range(1, 7)}:
            level = int(tag[1])
            value = " ".join(self._inline_children(element, current_href).split())
            if value:
                yield f"{'#' * level} {value}"
            return
        if tag in {"p", "dt", "dd", "figcaption"}:
            if (
                tag == "p"
                and "calibre7" in str(element.get("class") or "").split()
                and element.get("id") is None
                and re.fullmatch(r"\d{1,4}", _text_value(element))
                and not element.xpath(".//*[local-name()='a']")
            ):
                # Standalone printed page numbers in the repaired source are
                # paragraphs of this exact structural form.  A class-scoped
                # predicate avoids deleting legitimate numbered prose.
                return
            value = self._inline_children(element, current_href).strip()
            if value:
                yield value
            return
        if tag == "blockquote":
            nested = list(self._child_blocks(element, current_href))
            if nested:
                yield "\n> \n".join("> " + line.replace("\n", "\n> ") for line in nested)
            return
        if tag in {"ul", "ol"}:
            ordered = tag == "ol"
            items = [child for child in element if _local_name(child) == "li"]
            for index, item in enumerate(items, start=1):
                if item in self.note_roots:
                    continue
                value = " ".join(self._inline_children(item, current_href).split())
                if value:
                    yield f"{index}. {value}" if ordered else f"- {value}"
            return
        if tag == "table":
            rows: list[list[str]] = []
            for row in element.xpath(".//*[local-name()='tr']"):
                cells = [
                    " ".join(self._inline_children(cell, current_href).split())
                    for cell in row
                    if _local_name(cell) in {"th", "td"}
                ]
                if cells:
                    rows.append(cells)
            if rows:
                width = max(len(row) for row in rows)
                padded = [row + [""] * (width - len(row)) for row in rows]
                yield "| " + " | ".join(padded[0]) + " |"
                yield "| " + " | ".join(["---"] * width) + " |"
                for row in padded[1:]:
                    yield "| " + " | ".join(row) + " |"
            return
        if tag == "img":
            value = self._inline(element, current_href)
            if value:
                yield value
            return
        if tag == "pre":
            value = "".join(element.itertext()).rstrip()
            if value:
                yield f"```\n{value}\n```"
            return
        if tag == "hr":
            yield "---"
            return
        yield from self._child_blocks(element, current_href)

    def _child_blocks(self, element: etree._Element, current_href: str) -> Iterator[str]:
        if element.text and element.text.strip() and _local_name(element) in {"body", "div", "section", "article", "main"}:
            yield " ".join(element.text.split())
        for child in element:
            yield from self._blocks(child, current_href)
            if child.tail and child.tail.strip():
                yield " ".join(child.tail.split())

    def render(self, document: _SpineDocument) -> tuple[str, list[tuple[str, str]], list[dict[str, Any]]]:
        before_definitions = len(self.definitions)
        before_issues = len(self.issues)
        self.issues.extend(self.pending_issues.get(document.href, []))
        body_nodes = document.root.xpath("//*[local-name()='body']")
        root = body_nodes[0] if body_nodes else document.root
        blocks = [block.strip() for block in self._blocks(root, document.href) if block.strip()]
        definitions = self.definitions[before_definitions:]
        issues = self.issues[before_issues:]
        self.render_metrics[document.href] = {
            "continuation_merged_count": sum(
                len(continuations)
                for note, continuations in self.note_continuations.items()
                if self.element_hrefs.get(note) == document.href
            )
        }
        return "\n\n".join(blocks).strip(), definitions, issues


def _heading_title(markdown: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if match:
            return re.sub(r"[*_`]+", "", match.group(1)).strip() or fallback
    return fallback


def _document_title(markdown: str, *, navigation: str, fallback: str) -> str:
    for line in markdown.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return re.sub(r"[*_`]+", "", match.group(1)).strip() or fallback
    if navigation.strip():
        return navigation.strip()
    heading = _heading_title(markdown, "")
    if heading:
        return heading
    first_block = next(
        (" ".join(block.split()) for block in re.split(r"\n{2,}", markdown) if block.strip()),
        "",
    )
    if first_block and len(first_block) <= 120:
        return re.sub(r"[*_`]+", "", first_block).strip() or fallback
    return fallback


def _slug(value: str) -> str:
    slug = re.sub(r"[^\w\-一-龥]+", "_", value, flags=re.UNICODE).strip("_")
    return slug[:80] or "chapter"


def _translation_units(chapter_id: str, markdown: str, source_href: str) -> list[dict[str, Any]]:
    raw_blocks = [block.rstrip() for block in re.split(r"\n{2,}", markdown.strip()) if block.strip()]
    blocks: list[str] = []
    footnote_index: int | None = None
    for raw_block in raw_blocks:
        if re.match(r"^\[\^[^]]+\]:", raw_block.lstrip()):
            blocks.append(raw_block.lstrip())
            footnote_index = len(blocks) - 1
            continue
        if footnote_index is not None and re.match(r"^(?: {2,}|\t)\S", raw_block):
            blocks[footnote_index] += "\n\n" + raw_block
            continue
        blocks.append(raw_block.strip())
        footnote_index = None
    units: list[dict[str, Any]] = []
    for index, block in enumerate(blocks, start=1):
        source_sha256 = _sha256_bytes(block.encode("utf-8"))
        unit_digest = hashlib.sha256(
            f"{source_href}\0{index}\0{source_sha256}".encode("utf-8")
        ).hexdigest()[:16]
        kind = (
            "heading" if re.match(r"^#{1,6}\s", block)
            else "footnote_definition" if re.match(r"^\[\^[^]]+\]:", block)
            else "list" if re.match(r"^(?:[-*+] |\d+\. )", block)
            else "paragraph"
        )
        units.append(
            {
                "schema_version": SCHEMA_VERSION,
                "id": f"{chapter_id}-u{index:04d}-{unit_digest}",
                "chapter_id": chapter_id,
                "sequence": index,
                "kind": kind,
                "source_href": source_href,
                "source_sha256": source_sha256,
                "source_markdown": block,
            }
        )
    return units


def import_epub(source: Path, output_dir: Path) -> dict[str, Any]:
    """Import an EPUB spine into chapter Markdown and translation units."""

    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    metadata, documents, roots, title_hints, opf_member = _package_documents(source)
    renderer = _XhtmlRenderer(roots)
    chapter_dir = output_dir / "chapters"
    source_dir = output_dir / "semantic" / "source_chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)
    source_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    audit_chapters: list[dict[str, Any]] = []
    units: list[dict[str, Any]] = []
    expected_files: set[str] = set()
    for sequence, document in enumerate(documents, start=1):
        body, definitions, issues = renderer.render(document)
        fallback = PurePosixPath(document.href).stem
        title = _document_title(
            body,
            navigation=title_hints.get(document.href, ""),
            fallback=fallback,
        )
        if not re.match(r"^#\s+", body):
            body = f"# {title}\n\n{body}".strip()
        if definitions:
            body += "\n\n" + "\n\n".join(
                f"[^{note_id}]: " + text.replace("\n\n", "\n\n    ")
                for note_id, text in definitions
            )
        markdown = body.rstrip() + "\n"
        inventory = parse_markdown_footnotes(markdown)
        for code, values in (
            ("semantic_markdown_duplicate_definitions", inventory.duplicate_definitions),
            ("semantic_markdown_missing_definitions", inventory.missing_definitions),
            ("semantic_markdown_unused_definitions", inventory.unused_definitions),
            ("semantic_markdown_duplicate_references", inventory.duplicate_references),
        ):
            if values:
                issues.append(
                    {
                        "code": code,
                        "message": "EPUB 章节脚注未形成一对一闭环。",
                        "source_page": document.href,
                        "note_label": None,
                        "blocking": True,
                        "evidence": {"values": list(values)},
                    }
                )
        chapter_id = f"epub-{sequence:04d}"
        filename = f"{sequence:03d}_{_slug(title)}.md"
        expected_files.add(filename)
        _atomic_write_text(chapter_dir / filename, markdown)
        _atomic_write_text(source_dir / filename, markdown)
        chapter_units = _translation_units(chapter_id, markdown, document.href)
        units.extend(chapter_units)
        manifest.append(
            {
                "id": chapter_id,
                "sequence": sequence,
                "level": 1,
                "title": title,
                "display_title": title,
                "filename": filename,
                "source_format": "epub",
                "source_href": document.href,
                "source_item_id": document.item_id,
                "source_sha256": document.raw_sha256,
                "reviewed_override": False,
                "semantic_footnote_count": len(inventory.definitions),
                "semantic_issue_count": len(issues),
            }
        )
        audit_chapters.append(
            {
                "chapter_id": chapter_id,
                "filename": filename,
                "source_href": document.href,
                "source_sha256": document.raw_sha256,
                "markdown_sha256": _sha256_bytes(markdown.encode("utf-8")),
                "footnote_contract_sha256": markdown_footnote_contract_sha256(markdown),
                "footnote_count": len(inventory.definitions),
                "translation_unit_count": len(chapter_units),
                "continuation_merged_count": renderer.render_metrics.get(
                    document.href, {}
                ).get("continuation_merged_count", 0),
                "issues": issues,
                "release_blocked": any(bool(issue.get("blocking", True)) for issue in issues),
            }
        )

    for directory in (chapter_dir, source_dir):
        for stale in directory.glob("*.md"):
            if stale.name not in expected_files:
                stale.unlink()
    summary = semantic_audit_summary(audit_chapters)
    summary["continuation_merged_count"] = sum(
        int(item["continuation_merged_count"]) for item in audit_chapters
    )
    audit = {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked" if summary["release_blocked"] else "passed",
        "release_blocked": summary["release_blocked"],
        "generated_by": "core.source.epub+core.reconstruct.semantic",
        "contract_mode": "epub-spine-markdown-footnotes",
        "importer_version": IMPORTER_VERSION,
        "source": {
            "path": str(source),
            "sha256": _sha256_bytes(source.read_bytes()),
            "package_document": opf_member,
            "metadata": metadata.__dict__,
        },
        "summary": summary,
        "chapters": audit_chapters,
    }
    _atomic_write_json(output_dir / "chapters.json", manifest)
    _atomic_write_json(output_dir / "audit" / "semantic-reconstruction.json", audit)
    units_path = output_dir / "semantic" / "translation-units.jsonl"
    _atomic_write_text(
        units_path,
        "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
    )
    return {
        "status": audit["status"],
        "release_blocked": audit["release_blocked"],
        "chapter_count": len(manifest),
        "footnote_count": summary["footnote_count"],
        "translation_unit_count": len(units),
        "title": metadata.title,
        "author": metadata.author,
        "language": metadata.language,
        "manifest": str((output_dir / "chapters.json").resolve()),
        "chapters": str(chapter_dir.resolve()),
        "translation_units": str(units_path.resolve()),
        "audit": str((output_dir / "audit" / "semantic-reconstruction.json").resolve()),
    }


def apply_translations(
    output_dir: Path,
    translations_path: Path,
    *,
    target_language: str = "简体中文",
    glossary: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Apply a complete EPUB translation without mutating source evidence."""

    try:
        return apply_translation_transaction(
            output_dir,
            translations_path,
            generated_by="core.source.epub+core.pages.translate+core.reconstruct.semantic",
            contract_mode="epub-spine-translated-markdown-footnotes",
            target_language=target_language,
            glossary=glossary,
        )
    except SemanticApplyError as exc:
        raise EpubSemanticError(str(exc)) from exc


def prune_long_footnotes(
    output_dir: Path,
    *,
    minimum_characters: int = 150,
    remove_standalone_page_markers: bool = False,
) -> dict[str, Any]:
    """Create a reader edition by suppressing long semantic footnotes.

    The immutable EPUB evidence under ``semantic/source_chapters`` is left
    untouched.  Current publication chapters, their manifest, and the
    semantic audit are updated together so the normal verifier can prove the
    new reference-definition contract before DOCX publication.
    """

    if minimum_characters < 1:
        raise EpubSemanticError("minimum footnote length must be positive")

    output_dir = output_dir.expanduser().resolve()
    manifest_path = output_dir / "chapters.json"
    audit_path = output_dir / "audit" / "semantic-reconstruction.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EpubSemanticError("semantic publication manifest or audit is invalid") from exc
    if not isinstance(manifest, list) or not manifest:
        raise EpubSemanticError("chapter manifest must be a non-empty array")
    if not isinstance(audit, dict) or not isinstance(audit.get("chapters"), list):
        raise EpubSemanticError("semantic reconstruction audit must contain chapters")

    audit_by_id: dict[str, dict[str, Any]] = {}
    for item in audit["chapters"]:
        if not isinstance(item, dict) or not str(item.get("chapter_id") or ""):
            raise EpubSemanticError("semantic reconstruction audit has an invalid chapter")
        chapter_id = str(item["chapter_id"])
        if chapter_id in audit_by_id:
            raise EpubSemanticError("semantic reconstruction audit has duplicate chapters")
        audit_by_id[chapter_id] = item

    pending_markdown: dict[Path, str] = {}
    chapter_reports: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for manifest_item in manifest:
        if not isinstance(manifest_item, dict):
            raise EpubSemanticError("chapter manifest contains a non-object entry")
        chapter_id = str(manifest_item.get("id") or "")
        filename = str(manifest_item.get("filename") or "")
        if (
            not chapter_id
            or chapter_id in seen_ids
            or not filename
            or Path(filename).name != filename
            or Path(filename).suffix.lower() != ".md"
        ):
            raise EpubSemanticError("chapter manifest identity or filename is invalid")
        seen_ids.add(chapter_id)
        audited = audit_by_id.get(chapter_id)
        if audited is None:
            raise EpubSemanticError(f"semantic audit is missing chapter: {chapter_id}")

        chapter_path = output_dir / "chapters" / filename
        try:
            markdown = chapter_path.read_text(encoding="utf-8")
            note_result = prune_long_markdown_footnotes(
                markdown,
                minimum_characters=minimum_characters,
            )
            page_result = (
                prune_standalone_page_markers(note_result.markdown)
                if remove_standalone_page_markers
                else None
            )
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            raise EpubSemanticError(
                f"cannot prune long footnotes in chapter {chapter_id}: {exc}"
            ) from exc

        publication_markdown = (
            page_result.markdown if page_result is not None else note_result.markdown
        )
        removed_page_markers = page_result.removed if page_result is not None else ()
        pending_markdown[chapter_path] = publication_markdown
        manifest_item["semantic_footnote_count"] = note_result.remaining_count
        if note_result.removed:
            manifest_item["suppressed_long_footnote_count"] = len(note_result.removed)
            manifest_item["suppressed_long_footnote_minimum_characters"] = (
                minimum_characters
            )
        if removed_page_markers:
            manifest_item["suppressed_source_page_marker_count"] = len(
                removed_page_markers
            )
        digest = _sha256_bytes(publication_markdown.encode("utf-8"))
        audited["markdown_sha256"] = digest
        if "translated_markdown_sha256" in audited:
            audited["translated_markdown_sha256"] = digest
        audited["footnote_contract_sha256"] = markdown_footnote_contract_sha256(
            publication_markdown
        )
        audited["footnote_count"] = note_result.remaining_count
        if note_result.removed:
            transformations = audited.setdefault("transformations", [])
            if not isinstance(transformations, list):
                raise EpubSemanticError(
                    f"semantic audit transformations are invalid: {chapter_id}"
                )
            transformations.append(
                {
                    "kind": "suppress-long-footnotes",
                    "minimum_characters": minimum_characters,
                    "removed": [
                        {"id": note_id, "characters": characters}
                        for note_id, characters in note_result.removed
                    ],
                }
            )
        if removed_page_markers:
            transformations = audited.setdefault("transformations", [])
            if not isinstance(transformations, list):
                raise EpubSemanticError(
                    f"semantic audit transformations are invalid: {chapter_id}"
                )
            transformations.append(
                {
                    "kind": "suppress-source-page-markers",
                    "removed": list(removed_page_markers),
                }
            )
        chapter_reports.append(
            {
                "chapter_id": chapter_id,
                "filename": filename,
                "removed_count": len(note_result.removed),
                "removed": [
                    {"id": note_id, "characters": characters}
                    for note_id, characters in note_result.removed
                ],
                "remaining_count": note_result.remaining_count,
                "removed_page_marker_count": len(removed_page_markers),
                "removed_page_markers": list(removed_page_markers),
            }
        )

    total_removed = sum(item["removed_count"] for item in chapter_reports)
    total_remaining = sum(item["remaining_count"] for item in chapter_reports)
    total_page_markers = sum(
        item["removed_page_marker_count"] for item in chapter_reports
    )
    if not total_removed and not total_page_markers:
        return {
            "status": "passed",
            "release_blocked": bool(audit.get("release_blocked", False)),
            "minimum_characters": minimum_characters,
            "removed_count": 0,
            "remaining_count": total_remaining,
            "removed_page_marker_count": 0,
            "changed": False,
        }

    summary = audit.setdefault("summary", {})
    if not isinstance(summary, dict):
        raise EpubSemanticError("semantic reconstruction summary is invalid")
    summary["footnote_count"] = total_remaining
    if total_removed:
        summary["suppressed_long_footnote_count"] = int(
            summary.get("suppressed_long_footnote_count") or 0
        ) + total_removed
    if total_page_markers:
        summary["suppressed_source_page_marker_count"] = int(
            summary.get("suppressed_source_page_marker_count") or 0
        ) + total_page_markers
    generated_steps = []
    if total_removed:
        generated_steps.append("core.publication.prune-long-footnotes")
    if total_page_markers:
        generated_steps.append("core.publication.prune-page-markers")
    audit["generated_by"] = "+".join(
        [str(audit.get("generated_by") or "semantic"), *generated_steps]
    )
    transformations = audit.setdefault("transformations", [])
    if not isinstance(transformations, list):
        raise EpubSemanticError("semantic reconstruction transformations are invalid")
    transformations.append(
        {
            "kind": "reader-edition-pruning",
            "minimum_characters": minimum_characters,
            "removed_count": total_removed,
            "remaining_count": total_remaining,
            "removed_page_marker_count": total_page_markers,
        }
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "passed",
        "release_blocked": bool(audit.get("release_blocked", False)),
        "generated_by": "+".join(generated_steps),
        "minimum_characters": minimum_characters,
        "removed_count": total_removed,
        "remaining_count": total_remaining,
        "removed_page_marker_count": total_page_markers,
        "chapters": chapter_reports,
    }

    for path, markdown in pending_markdown.items():
        _atomic_write_text(path, markdown)
    _atomic_write_json(manifest_path, manifest)
    _atomic_write_json(audit_path, audit)
    _atomic_write_json(output_dir / "audit" / "reader-edition-pruning.json", report)
    return {**report, "changed": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import EPUB into translation-agent semantic chapters.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="extract EPUB spine and semantic footnotes")
    importer.add_argument("source")
    importer.add_argument("-o", "--output-dir", required=True)
    apply = subparsers.add_parser("apply-translations", help="validate and apply translated JSONL units")
    apply.add_argument("-o", "--output-dir", required=True)
    apply.add_argument("translations")
    prune = subparsers.add_parser(
        "prune-long-footnotes",
        help="remove long semantic footnotes for a reader-edition publication",
    )
    prune.add_argument("-o", "--output-dir", required=True)
    prune.add_argument(
        "--minimum-characters",
        type=int,
        default=150,
        help="remove definitions with at least this many normalized characters",
    )
    prune.add_argument(
        "--remove-standalone-page-markers",
        action="store_true",
        help="also remove high-confidence monotonic standalone legacy EPUB page labels",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "import":
            result = import_epub(Path(args.source), Path(args.output_dir))
        elif args.command == "apply-translations":
            result = apply_translations(Path(args.output_dir), Path(args.translations))
        else:
            result = prune_long_footnotes(
                Path(args.output_dir),
                minimum_characters=args.minimum_characters,
                remove_standalone_page_markers=args.remove_standalone_page_markers,
            )
    except (OSError, zipfile.BadZipFile, EpubSemanticError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("release_blocked", False) else 1


if __name__ == "__main__":
    raise SystemExit(main())
