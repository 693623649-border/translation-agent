"""Build the 视差之见 RAG knowledge base from the published chapter Markdown.

Strips the Markdown footnote apparatus (definitions and ``[^n]`` markers) so the
corpus holds reader-facing body text, and restores the parenthesis escapes the
Markdown-link workaround left in the body.  Then attaches the Zhipu
embedding-3 index.
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

WORK = Path("outputs/视差之见")
SOURCE = Path(
    "book/视差之见 (斯拉沃热·齐泽克, Slavoj Žižek) (z-library.sk, 1lib.sk, z-lib.sk).pdf"
)
DEF_LINE = re.compile(r"^\[\^[^\]]*\]:\s*(.*)$")
REF = re.compile(r"\[\^[^\]]*\]")
ESCAPED_PAREN = re.compile(r"\\\(")


def main() -> int:
    for line in (Path(".env").read_text(encoding="utf-8", errors="replace")).splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    chapter_dir = WORK / "chapters"
    rows: list[dict] = []
    for item in manifest:
        markdown = (chapter_dir / item["filename"]).read_text(encoding="utf-8")
        body_lines = [
            DEF_LINE.sub(lambda match: "", line).rstrip()
            for line in markdown.splitlines()[1:]
        ]
        body = REF.sub("", "\n".join(body_lines))
        body = ESCAPED_PAREN.sub("(", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        for chunk_index, chunk in enumerate(legacy.split_text(body, 4000), start=1):
            row_id = hashlib.sha1(
                f"{SOURCE.name}:{item['id']}:compiled:{chunk_index}".encode("utf-8")
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
    target = WORK / "knowledge_base.jsonl"
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    apparatus = sum(1 for row in rows if "[^" in row["content"] or "\\(" in row["content"])
    total = sum(
        len((chapter_dir / item["filename"]).read_text(encoding="utf-8"))
        for item in manifest
    )
    print(f"rows: {len(rows)} | apparatus rows: {apparatus}")
    print(f"coverage vs md: {100 * sum(len(r['content']) for r in rows) / total:.1f}%")
    metadata = rag.maybe_build_zhipu_embedding_index(target, requested=True)
    print(f"embedding: {metadata.provider_name} {metadata.model} {metadata.chunk_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
