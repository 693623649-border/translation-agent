"""Extract a born-digital (text-layer) PDF into book_pipeline page records.

For text-based PDFs (e.g. wkhtmltopdf output) the embedded text layer is exact,
so we skip vision OCR and write page_XXXX.json / .md directly in the pipeline's
PageRecord format. Also detects chapter headings from the printed 目次 and writes
a manual toc.json (pdf_page set directly, printed_page = pdf_page, offset 0) so
the downstream compile phase can slice chapters without an LLM TOC call.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import fitz

# 目次 titles in printed order (page 4 of the PDF).
TITLES = [
    ("角川文庫版のための序", "frontmatter"),
    ("全著作集のための序", "frontmatter"),
    ("序", "frontmatter"),
    ("禁制論", "chapter"),
    ("憑人論", "chapter"),
    ("巫覡論", "chapter"),
    ("巫女論", "chapter"),
    ("他界論", "chapter"),
    ("祭儀論", "chapter"),
    ("母制論", "chapter"),
    ("対幻想論", "chapter"),
    ("罪責論", "chapter"),
    ("規範論", "chapter"),
    ("起源論", "chapter"),
    ("後記", "other"),
]


def clean(s: str) -> str:
    return s.strip().lstrip("　 \t　").strip()


def main(pdf: str, out: str) -> None:
    outdir = Path(out)
    pages_dir = outdir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf)
    page_texts: list[str] = []
    for i in range(doc.page_count):
        page_texts.append(doc[i].get_text())
    doc.close()

    # Write page records.
    for i, text in enumerate(page_texts):
        pdf_page = i + 1
        rec = {
            "pdf_page": pdf_page,
            "text": text.strip(),
            "language": "ja",
            "translated_text": "",
            "notes": "text-layer",
            "ocr_model": "text-layer",
        }
        (pages_dir / f"page_{pdf_page:04d}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (pages_dir / f"page_{pdf_page:04d}.md").write_text(text.strip() + "\n", encoding="utf-8")

    # Detect chapter starts: first page (after 目次) whose text begins with the title.
    # Match longest titles first so "序" does not shadow "全著作集のための序".
    toc_page = next((i + 1 for i, t in enumerate(page_texts) if "目次" in t[:6]), 1)
    found: dict[str, int] = {}
    sorted_titles = sorted({t for t, _ in TITLES}, key=len, reverse=True)
    for i, text in enumerate(page_texts):
        if i + 1 <= toc_page:
            continue
        head = clean(text)
        for title in sorted_titles:
            if title in found:
                continue
            if head.startswith(title):
                found[title] = i + 1
                break

    # Build entries in printed order.
    entries = []
    for pos, (title, kind) in enumerate(TITLES, start=1):
        pg = found.get(title)
        if pg is None:
            print(f"[warn] title not located: {title}", file=sys.stderr)
            continue
        entries.append(
            {
                "id": f"toc-{pos:04d}",
                "index": "",
                "title": title,
                "level": 1,
                "kind": kind,
                "printed_page": pg,  # = pdf_page; with --page-offset 0 this pins pdf_page
                "pdf_page": pg,
                "end_pdf_page": None,
            }
        )

    toc = {
        "schema_version": 1,
        "toc_pdf_pages": [toc_page],
        "page_offset": 0,
        "offset_evidence": [],
        "entries": entries,
    }
    (outdir / "toc.json").write_text(json.dumps(toc, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[done] pages={len(page_texts)} toc_page={toc_page} entries={len(entries)}")
    for e in entries:
        print(f"  {e['kind']:11} pdf={e['pdf_page']:>3} :: {e['title']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
