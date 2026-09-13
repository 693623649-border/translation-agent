"""Build a standalone RAG knowledge base from one pasted-article text file.

Splits the article on its own headings (``摘要``/``一、…``/``余论：…``), chunks
each section at paragraph boundaries with the standard ≤4000-char contract, and
emits the five-field ``knowledge_base.jsonl`` plus the ``knowledge_base.meta.jsonl``
routing sidecar used by the hybrid retriever.

Usage:
  python tools/books/txt_article_kb.py <article.txt> \
      --output-dir "outputs/<书名>" --title "<题名>" [--author 作者] [--language zh]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

import book_pipeline as legacy

ABSTRACT = re.compile(r"^(?:摘\s*要|内容提要)\s*$")
NUMBERED = re.compile(r"^[一二三四五六七八九十]+(?:、|\s{2,})\S")
EPILOGUE = re.compile(r"^余论[：:]|^(?:结论|结语)[\s：:]")


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip())
    return re.sub(r"-{2,}", "-", text).strip("-")[:48] or "sec"


def parse_sections(text: str) -> list[tuple[str, str]]:
    """Return [(section_title, body)] in document order."""

    lines = text.splitlines()
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if ABSTRACT.match(stripped):
            headings.append((index, "摘要"))
        elif NUMBERED.match(stripped) or EPILOGUE.match(stripped):
            headings.append((index, stripped))
    if not headings:
        return [("全文", text.strip())]
    # Provenance lines before the first heading (e.g. the journal source line)
    # belong with the first section rather than being dropped.
    preamble = "\n".join(lines[: headings[0][0]]).strip()
    sections: list[tuple[str, str]] = []
    for position, (index, title) in enumerate(headings):
        start = index + 1 if title != "摘要" else index
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        if title == "摘要":
            body = "\n".join(lines[index:end]).strip()
            if preamble:
                body = f"{preamble}\n\n{body}"
        elif body:
            body = f"{title}\n{body}"
        if body:
            sections.append((title, body))
    return sections


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("article", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default="")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args(argv)

    sections = parse_sections(args.article.read_text(encoding="utf-8"))
    rows: list[dict] = []
    sidecar: list[dict] = []
    book_id = f"01_{_slug(args.title)}"
    order = 0
    for section_title, body in sections:
        for chunk_index, chunk in enumerate(legacy.split_text(body, 4000), start=1):
            order += 1
            chapter_id = f"{book_id}:{_slug(section_title)}"
            row_id = hashlib.sha1(
                f"{chapter_id}\n{chunk_index}\n{chunk[:200]}".encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "id": row_id,
                    "title": f"[{args.title}] {section_title}",
                    "chapter_id": chapter_id,
                    "chapter_order": order,
                    "content": chunk,
                }
            )
            sidecar.append(
                {
                    "id": row_id,
                    "book_id": book_id,
                    "book_title": args.title,
                    "author": args.author,
                    "language": args.language,
                }
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
    print(f"sections: {len(sections)} | chunks: {len(rows)} -> {out}")
    for title, body in sections:
        print(f"  {title[:40]:42s} {len(body):6d} chars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
