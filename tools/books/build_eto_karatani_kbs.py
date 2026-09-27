"""Build the RAG knowledge bases for the three scanned 江藤淳/柄谷行人 PDFs.

Reads the sanitized reader chapters from each book's graph state, strips the
Markdown footnote apparatus, chunks at the standard 4000-char contract, writes
the five-field corpus plus a language=ja routing sidecar, and attaches the
Zhipu embedding-3 index.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

import book_pipeline as legacy
import rag_knowledge_base as rag

DEF_LINE = re.compile(r"^\[\^[^\]]*\]:\s*(.*)$")
REF = re.compile(r"\[\^[^\]]*\]")

BOOKS = [
    (
        "outputs/成熟と喪失",
        "成熟と喪失―「母」の崩壊",
        "江藤淳",
        "book/江藤淳/成熟と喪失゛母″の崩壊 (江藤淳) (z-library.sk, 1lib.sk, z-lib.sk).pdf",
    ),
    (
        "outputs/江藤淳と少女",
        "江藤淳と少女―フェミニズム的戦後",
        "大塚英志",
        "book/江藤淳/江藤淳と少女フェミニズム的戦後―サブカルチャー文学論序章 (Eiji Otsuka) (z-library.sk, 1lib.sk, z-lib.sk).pdf",
    ),
    (
        "outputs/漱石論集成",
        "漱石論集成",
        "柄谷行人",
        "book/柄谷行人/漱石論集成 (柄谷行人) (z-library.sk, 1lib.sk, z-lib.sk).pdf",
    ),
]


def main() -> int:
    for line in Path(".env").read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    for out_name, book_title, author, source_name in BOOKS:
        out = Path(out_name)
        state = json.loads(
            (out / ".pipeline_graph" / "state.json").read_text(encoding="utf-8")
        )
        art = state["nodes"]["core.publication.sanitize"]["outputs"]["chapters.reader"]
        chapter_dir = Path(art["chapter_dir"])
        manifest = json.loads(Path(art["manifest"]).read_text(encoding="utf-8"))
        source = Path(source_name)
        book_id = "01_" + book_title.split("―")[0]
        rows: list[dict] = []
        sidecar: list[dict] = []
        for item in manifest:
            md = (chapter_dir / item["filename"]).read_text(encoding="utf-8")
            body_lines = [
                DEF_LINE.sub(lambda m: "", ln).rstrip() for ln in md.splitlines()[1:]
            ]
            body = REF.sub("", "\n".join(body_lines))
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            for chunk_index, chunk in enumerate(
                legacy.split_text(body, 4000), start=1
            ):
                row_id = hashlib.sha1(
                    f"{source.name}:{item['id']}:compiled:{chunk_index}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                rows.append(
                    {
                        "id": row_id,
                        "title": str(item["display_title"]),
                        "chapter_id": str(item["id"]),
                        "chapter_order": int(item["sequence"]),
                        "content": chunk,
                    }
                )
                sidecar.append(
                    {
                        "id": row_id,
                        "book_id": book_id,
                        "book_title": book_title,
                        "author": author,
                        "language": "ja",
                    }
                )
        target = out / "knowledge_base.jsonl"
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (out / "knowledge_base.meta.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as fh:
            for row in sidecar:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        metadata = rag.maybe_build_zhipu_embedding_index(target, requested=True)
        print(
            f"{book_title}: rows={len(rows)} embedding={metadata.provider_name}"
            f"/{metadata.model}/{metadata.chunk_count}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
