"""Publish one standalone Word document per work from a collection EPUB.

Companion to epub_collection_kb.py: reuses the same TOC-nesting split
(volume -> work -> piece) and the publication-grade ``build_docx`` renderer so
every work (坟, 呐喊, 黄金时代, ...) lands as its own styled .docx with the
work title on the cover and each piece as a Heading 1 chapter.

Usage:
  python tools/books/epub_collection_docx.py <book.epub> \
      --output-dir "outputs/鲁迅全集" --author 鲁迅
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

import epub_collection_kb as splitter
import book_pipeline as legacy


def _safe_filename(title: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())
    return re.sub(r"_+", "_", title).strip("_")[:60] or "未命名"


def publish_work(
    work: splitter.Work,
    output_path: Path,
    *,
    author: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="epub_split_docx_") as tmp:
        chapter_dir = Path(tmp)
        manifest: list[dict[str, object]] = []
        for sequence, piece in enumerate(work.pieces, start=1):
            text = (piece.text or "").strip()
            if not text:
                continue
            piece_title = piece.title or work.title
            filename = f"{sequence:03d}_{_safe_filename(piece_title)}.md"
            (chapter_dir / filename).write_text(
                f"# {piece_title}\n\n{text}\n",
                encoding="utf-8",
                newline="\n",
            )
            manifest.append(
                {
                    "id": f"piece-{sequence:04d}",
                    "index": "",
                    "title": piece_title,
                    "level": 1,
                    "kind": "chapter",
                    "sequence": sequence,
                    "display_title": piece_title,
                    "filename": filename,
                    "boundary_mode": "non-overlap",
                    "reviewed_override": False,
                }
            )
        if not manifest:
            raise SystemExit(f"work {work.title!r} has no text to publish")
        legacy.build_docx(
            output_path,
            chapter_dir,
            manifest,
            book_title=work.title,
            author=author,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("epub", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--author", required=True)
    args = parser.parse_args(argv)

    works, spine_files = splitter.parse_collection(args.epub)
    splitter.extract_work_texts(args.epub, works, spine_files)
    works = [w for w in works if w.text_length > 0]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.output_dir / "works_index.json"
    index: list[dict[str, object]] = []
    for work_index, work in enumerate(works, start=1):
        filename = f"{work_index:02d}_{_safe_filename(work.title)}.docx"
        output_path = args.output_dir / filename
        publish_work(work, output_path, author=args.author)
        index.append(
            {
                "book_id": f"{work_index:02d}_{work.title}",
                "docx": filename,
                "pieces": len(work.pieces),
                "chars": work.text_length,
            }
        )
        print(f"[docx] {filename}: {len(work.pieces)} pieces, {work.text_length} chars")

    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"published {len(index)} works -> {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
