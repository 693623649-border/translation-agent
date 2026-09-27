"""Build a multi-work RAG knowledge base from a z-library collection EPUB.

Splits a multi-volume/multi-work EPUB (鲁迅全集, 王小波作品大全集, ...) into
individual works using the EPUB TOC nesting, then emits the standard five-field
knowledge_base.jsonl plus a knowledge_base.meta.jsonl routing sidecar (one row
per chunk, keyed by chunk id).  Long pieces are split at paragraph boundaries
into <=--max-chars chunks, matching the aggregate-KB chunking contract
(知识库_日本思想政治: p50 ~936, max 4000).

Usage:
  python tools/books/epub_collection_kb.py <book.epub> --output-dir outputs/知识库_鲁迅全集 \
      --author 鲁迅 --language zh [--collection-title 鲁迅全集]
"""

from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import re
import sys
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
OPF = "{http://www.idpf.org/2007/opf}"

VOLUME_RE = re.compile(r"鲁迅全集\s*[•·]\s*第[一二三四五六七八九十]+卷")
FOOTNOTE_RE = re.compile(r"^\[\d+\]")
SECTION_TITLE_RE = re.compile(
    r"^[一二三四五六七八九十百零]{1,4}(\s*[\u4e00-\u9fffA-Za-z0-9]{0,8})?$"
)

_CHUNK_TARGET = 4000
_SPLIT_OVERLAP = 0  # Phase-2 re-chunking may add overlap; contract keeps max 4000.


class _TextExtractor(html.parser.HTMLParser):
    """Extract readable text while recording offsets of ``id`` attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip = 0
        self._length = 0
        self.anchor_offsets: dict[str, int] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip += 1
        for name, value in attrs:
            if name == "id" and value and self._skip == 0:
                self.anchor_offsets.setdefault(value, self._length)
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "br", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "li", "tr"):
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)
            self._length += len(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        paragraphs = [re.sub(r"\s+", " ", p).strip() for p in raw.split("\n")]
        return "\n\n".join(p for p in paragraphs if p)


@dataclass
class Piece:
    title: str
    file: str
    anchor: str | None = None
    text: str = ""


@dataclass
class Work:
    title: str
    volume: str | None
    pieces: list[Piece] = field(default_factory=list)

    @property
    def text_length(self) -> int:
        return sum(len(p.text) for p in self.pieces)


def _nav_label(navpoint: ET.Element) -> str:
    label = navpoint.find(NCX + "navLabel/" + NCX + "text")
    return (label.text or "").strip() if label is not None else ""


def _nav_src(navpoint: ET.Element) -> str | None:
    content = navpoint.find(NCX + "content")
    if content is None or not content.get("src"):
        return None
    return content.get("src")


def _clean_work_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title).strip()
    return re.sub(r"\s+", "", title)[:40] or "未命名"


def parse_collection(epub_path: Path) -> tuple[list[Work], list[str]]:
    """Return (works in TOC order, spine file order)."""

    with zipfile.ZipFile(epub_path) as zf:
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        opf_href = container.find(
            ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
        ).get("full-path")
        opf_dir = "" if "/" not in opf_href else opf_href.rsplit("/", 1)[0] + "/"
        opf = ET.fromstring(zf.read(opf_href))

        manifest = {
            item.get("id"): item.get("href")
            for item in opf.iter(OPF + "item")
        }
        spine = [
            manifest[ref.get("idref")]
            for ref in opf.find(OPF + "spine").findall(OPF + "itemref")
            if ref.get("idref") in manifest
        ]
        spine_files = [opf_dir + href for href in spine]

        ncx_href = None
        for item in opf.iter(OPF + "item"):
            if (item.get("media-type") or "") == "application/x-dtbncx+xml":
                ncx_href = item.get("href")
                break
        if ncx_href is None:
            raise SystemExit("EPUB has no toc.ncx; cannot split by TOC")
        ncx = ET.fromstring(zf.read(opf_dir + ncx_href))

    nav_map = ncx.find(NCX + "navMap")
    top = list(nav_map.findall(NCX + "navPoint"))

    def descendants(navpoint: ET.Element) -> list[ET.Element]:
        out = []
        for child in navpoint.findall(NCX + "navPoint"):
            out.append(child)
            out.extend(descendants(child))
        return out

    works: list[Work] = []
    volume_mode = any(VOLUME_RE.search(_nav_label(np)) for np in top)

    def opf_file(src: str) -> tuple[str, str | None]:
        file, _, anchor = src.partition("#")
        return (opf_dir + file) if file else "", anchor or None

    if volume_mode:
        previous_work: Work | None = None
        for volume in top:
            if not VOLUME_RE.search(_nav_label(volume)):
                continue
            volume_title = _nav_label(volume)
            for work_np in volume.findall(NCX + "navPoint"):
                label = _nav_label(work_np)
                # Sloppy TOCs put numbered continuation files (splits of a
                # year-group essay run) at volume level; they belong to the
                # preceding work rather than being works of their own.
                if re.fullmatch(r"\d{1,3}", label) and previous_work is not None:
                    src = _nav_src(work_np)
                    if src:
                        file, anchor = opf_file(src)
                        previous_work.pieces.append(
                            Piece(title=label, file=file, anchor=anchor)
                        )
                    continue
                work = Work(
                    title=_clean_work_title(label),
                    volume=volume_title,
                )
                children = work_np.findall(NCX + "navPoint")
                if children:
                    for piece_np in children:
                        src = _nav_src(piece_np)
                        if not src:
                            continue
                        file, anchor = opf_file(src)
                        work.pieces.append(
                            Piece(title=_nav_label(piece_np), file=file, anchor=anchor)
                        )
                else:
                    src = _nav_src(work_np) or ""
                    if not src:
                        continue
                    file, anchor = opf_file(src)
                    if file:
                        work.pieces.append(
                            Piece(title=work.title, file=file, anchor=anchor)
                        )
                if work.pieces:
                    works.append(work)
                    previous_work = work
    else:
        for work_np in top:
            work = Work(title=_clean_work_title(_nav_label(work_np)), volume=None)
            for piece_np in descendants(work_np):
                src = _nav_src(piece_np)
                if not src:
                    continue
                file, anchor = opf_file(src)
                work.pieces.append(
                    Piece(title=_nav_label(piece_np), file=file, anchor=anchor)
                )
            if not work.pieces:
                src = _nav_src(work_np) or ""
                if src:
                    file, anchor = opf_file(src)
                    if file:
                        work.pieces.append(
                            Piece(title=work.title, file=file, anchor=anchor)
                        )
            if work.pieces:
                works.append(work)

    return works, spine_files


def _strip_unknown_header(text: str) -> str:
    """Sigil-generated files open with a placeholder '未知' heading."""

    return re.sub(r"^未知\s*\n\n*", "", text).strip()


def extract_work_texts(
    epub_path: Path,
    works: list[Work],
    spine_files: list[str],
) -> None:
    """Fill Piece.text, splitting multi-anchor files and attaching stray files."""

    with zipfile.ZipFile(epub_path) as zf:
        prefix = _common_prefix(works)
        cache: dict[str, tuple[str, dict[str, int]]] = {}

        def load(file: str) -> tuple[str, dict[str, int]]:
            if file not in cache:
                try:
                    raw = zf.read(file).decode("utf-8", "replace")
                except KeyError:
                    raw = ""
                parser = _TextExtractor()
                parser.feed(raw)
                cache[file] = (parser.text(), parser.anchor_offsets)
            return cache[file]

        claimed: set[str] = set()
        for work in works:
            seen_files: dict[str, list[Piece]] = {}
            for piece in work.pieces:
                seen_files.setdefault(piece.file, []).append(piece)
            for file, pieces in seen_files.items():
                text, anchors = load(file)
                anchored = [p for p in pieces if p.anchor and p.anchor in anchors]
                if len(anchored) >= 2:
                    ordered = sorted(anchored, key=lambda p: anchors[p.anchor])
                    ranges: list[tuple[int, int, Piece]] = []
                    for i, piece in enumerate(ordered):
                        start = anchors[piece.anchor]
                        end = (
                            anchors[ordered[i + 1].anchor]
                            if i + 1 < len(ordered)
                            else len(text)
                        )
                        ranges.append((start, end, piece))
                    for start, end, piece in ranges:
                        piece.text = _strip_unknown_header(text[start:end])
                    covered = [(r[0], r[1]) for r in ranges]
                    leftovers: list[str] = []
                    cursor = 0
                    for start, end in covered:
                        if start > cursor:
                            leftovers.append(text[cursor:start])
                        cursor = max(cursor, end)
                    if cursor < len(text):
                        leftovers.append(text[cursor:])
                    remainder = "\n\n".join(leftovers).strip()
                    for piece in pieces:
                        if piece not in ordered:
                            piece.text = _strip_unknown_header(remainder)
                elif len(anchored) == 1 and len(pieces) > 1:
                    # One anchored section inside a file another piece claims
                    # whole (year-group divider + 后记): split at the anchor.
                    piece = anchored[0]
                    start = anchors[piece.anchor]
                    piece.text = _strip_unknown_header(text[start:])
                    head = text[:start].strip()
                    for other in pieces:
                        if other is not piece:
                            other.text = _strip_unknown_header(head) or text
                else:
                    for p in pieces:
                        p.text = _strip_unknown_header(text)
                claimed.add(file)

        # Attach unreferenced spine files to the nearest preceding claimed work.
        file_to_work: dict[str, Work] = {}
        for work in works:
            for piece in work.pieces:
                file_to_work.setdefault(piece.file, work)

        current_work: Work | None = None
        for file in spine_files:
            if file in file_to_work:
                current_work = file_to_work[file]
                continue
            if current_work is None:
                continue
            try:
                text, _ = load(file)
            except KeyError:
                continue
            text = re.sub(r"^未知\s*\n\n*", "", text).strip()
            if len(text) < 30:
                continue
            if FOOTNOTE_RE.match(text):
                target = current_work.pieces[-1]
                target.text = (target.text + "\n\n" + text).strip()
                continue
            first_para, _, rest = text.partition("\n\n")
            title = first_para.strip()
            if len(title) <= 15 and (SECTION_TITLE_RE.match(title) or not title.endswith(("。", "！", "？", "，"))):
                current_work.pieces.append(
                    Piece(title=title or "（未编目）", file=file, text=_strip_unknown_header(rest) or text)
                )
            else:
                current_work.pieces.append(
                    Piece(title="（未编目）", file=file, text=text)
                )


def _common_prefix(works: list[Work]) -> str:
    files = [piece.file for work in works for piece in work.pieces]
    if not files:
        return ""
    head = files[0].split("/")
    prefix: list[str] = []
    for i, part in enumerate(head[:-1]):
        if all(len(f.split("/")) > i + 1 and f.split("/")[i] == part for f in files):
            prefix.append(part)
        else:
            break
    return "/".join(prefix) + "/" if prefix else ""


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip())
    return re.sub(r"-{2,}", "-", text).strip("-")[:48] or "p"


def chunk_piece(title: str, text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text] if text.strip() else []
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    buffer: list[str] = []
    size = 0
    for para in paragraphs:
        if size + len(para) + 2 > max_chars and buffer:
            chunks.append("\n\n".join(buffer))
            buffer, size = [], 0
        if len(para) > max_chars:
            if buffer:
                chunks.append("\n\n".join(buffer))
                buffer, size = [], 0
            for i in range(0, len(para), max_chars):
                chunks.append(para[i : i + max_chars])
            continue
        buffer.append(para)
        size += len(para) + 2
    if buffer:
        chunks.append("\n\n".join(buffer))
    return [c for c in chunks if c.strip()]


def build_rows(
    works: list[Work],
    *,
    author: str,
    language: str,
    max_chars: int,
) -> tuple[list[dict], list[dict]]:
    rows: list[dict] = []
    sidecar: list[dict] = []
    order = 0
    for work_index, work in enumerate(works, start=1):
        book_id = f"{work_index:02d}_{work.title}"
        for piece in work.pieces:
            piece_title = piece.title or work.title
            chunks = chunk_piece(piece_title, piece.text, max_chars)
            for chunk_index, content in enumerate(chunks, start=1):
                if len(content.strip()) < 12:
                    continue
                order += 1
                chapter_id = f"{book_id}:{_slug(piece_title)}"
                row_id = hashlib.sha1(
                    f"{chapter_id}\n{chunk_index}\n{content[:200]}".encode("utf-8")
                ).hexdigest()
                suffix = f"·{chunk_index}" if len(chunks) > 1 else ""
                rows.append(
                    {
                        "id": row_id,
                        "title": f"[{work.title}] {piece_title}{suffix}",
                        "chapter_id": chapter_id,
                        "chapter_order": order,
                        "content": content,
                    }
                )
                sidecar.append(
                    {
                        "id": row_id,
                        "book_id": book_id,
                        "book_title": work.title,
                        "author": author,
                        "language": language,
                    }
                )
    return rows, sidecar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--max-chars", type=int, default=_CHUNK_TARGET)
    parser.add_argument(
        "--sources", action="store_true", help="Write per-work provenance JSONL"
    )
    args = parser.parse_args(argv)

    works, spine_files = parse_collection(args.epub)
    extract_work_texts(args.epub, works, spine_files)

    works = [w for w in works if w.text_length > 0]
    rows, sidecar = build_rows(
        works, author=args.author, language=args.language, max_chars=args.max_chars
    )

    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    with (out / "knowledge_base.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (out / "knowledge_base.meta.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as fh:
        for row in sidecar:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.sources:
        sources = out / "sources"
        sources.mkdir(exist_ok=True)
        by_book: dict[str, list[dict]] = {}
        for row, meta in zip(rows, sidecar):
            by_book.setdefault(meta["book_id"], []).append(row)
        for book_id, book_rows in by_book.items():
            with (sources / f"{book_id}.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ) as fh:
                for row in book_rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"works: {len(works)}")
    for work in works:
        print(f"  {work.title}: {len(work.pieces)} pieces, {work.text_length} chars")
    print(f"chunks: {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
