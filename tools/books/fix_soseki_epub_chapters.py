"""Repair the 夏目漱石 (江藤淳, 講談社文庫) EPUB import.

The source EPUB packs each part's chapters into single XHTML files with one
anchor per chapter; the semantic importer kept whole files, so 第一部's eight
chapters landed in one 59k-char markdown blob, 第二部's first seven in another,
and 第八+九章 in a third.  This script splits those blobs at their ``### 第…章``
markers, renames every chapter with its part prefix (both parts number from
第一章), drops cover/notice/divider chapters with empty bodies, de-links the
目次, rebuilds ``chapters.json`` and the semantic audit with fresh digests, so
publication and the Word gate see a clean one-to-one manifest.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

WORK = Path(
    r"outputs/夏目漱石 (江藤淳) (z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"

DROP_IDS = {
    "epub-0001",  # cover image
    "epub-0002",  # 縦書き recommendation notice
    "epub-0003",  # 書名頁
    "epub-0006",  # blank/ad page
    "epub-0007",  # 献辞 (image-only dedication)
    "epub-0008",  # 第一部 divider
    "epub-0010",  # 第二部 divider
    "epub-0016",  # 電子書籍化 notice
    "epub-0017",  # 外部リンク notice
}

PART1 = [
    "第一章　漱石神話と「則天去私」",
    "第二章　文明開化と文明批評",
    "第三章　「無」と「夢」──漱石の低音部",
    "第四章　神経衰弱と「文学論」",
    "第五章　漱石の深淵",
    "第六章　「猫」は何故面白いか？",
    "第七章　職業作家漱石の誕生",
    "第八章　神の不在と文明批評的典型",
]
PART2 = [
    "第一章　作家と批評",
    "第二章　倫理と超倫理──修善寺大患をめぐって",
    "第三章　「門」──罪からの遁走",
    "第四章　「行人」──「我執」と「自己抹殺」",
    "第五章　「行人」の孤独と東洋的自然観",
    "第六章　「心」──所謂「漱石の微笑」",
    "第七章「道草」──日常生活と思想",
    "第八章　「明暗」──近代小説の誕生",
    "第九章　「明暗」それに続くもの",
]

SPLITS = {
    # manifest id -> (part label, chapter titles, source manifest id template)
    "epub-0009": ("第一部", PART1),
    "epub-0011": ("第二部", PART2[:7]),
    "epub-0012": ("第二部", PART2[7:]),
}


def _safe_filename(title: str) -> str:
    title = unicodedata.normalize("NFKC", title)
    title = re.sub(r'[\\/:*?"<>|\s]+', "_", title.strip())
    return re.sub(r"_+", "_", title).strip("_")[:60] or "untitled"


def split_blob(text: str, count: int) -> list[str]:
    """Split one merged chapter into `count` parts at ### markers."""

    matches = list(re.finditer(r"(?m)^### .+$", text))
    assert len(matches) == count, f"expected {count} ### markers, found {len(matches)}"
    parts = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        parts.append(body)
    return parts


def main() -> int:
    from publication_semantics import markdown_footnote_contract_sha256

    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    audit_path = WORK / "audit" / "semantic-reconstruction.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}
    manifest_by_id = {item["id"]: item for item in manifest}

    new_manifest: list[dict] = []
    new_audit: list[dict] = []
    counter = 0

    def emit(item: dict, filename: str, markdown: str, parent_audit: dict) -> None:
        nonlocal counter
        counter += 1
        entry_manifest = {
            "id": f"epub-{counter:04d}",
            "sequence": counter,
            "level": 1,
            "title": item["display_title"],
            "display_title": item["display_title"],
            "filename": filename,
            "source_format": "epub",
            "source_href": parent_audit.get("source_href"),
            "source_item_id": parent_audit.get("source_item_id"),
            "source_sha256": parent_audit.get("source_sha256"),
            "reviewed_override": False,
            "semantic_footnote_count": 0,
            "semantic_issue_count": 0,
        }
        path = CHAPTERS / filename
        path.write_text(markdown, encoding="utf-8", newline="\n")
        entry_audit = {
            "chapter_id": entry_manifest["id"],
            "filename": filename,
            "reviewed_override": False,
            "footnote_count": 0,
            "markdown_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "footnote_contract_sha256": markdown_footnote_contract_sha256(markdown),
            "pages": [],
            "issues": [],
            "release_blocked": False,
            "source_sha256": parent_audit.get("source_sha256"),
            "source_href": parent_audit.get("source_href"),
            "source_markdown_sha256": parent_audit.get("source_markdown_sha256"),
            "translation_unit_count": 0,
            "continuation_merged_count": 0,
        }
        new_manifest.append(entry_manifest)
        new_audit.append(entry_audit)

    for item in manifest:
        if item["id"] in DROP_IDS:
            path = CHAPTERS / item["filename"]
            if path.is_file():
                path.unlink()
            continue
        item = dict(item)
        parent_audit = audit_by_id.get(item["id"], {})
        if item["id"] in SPLITS:
            part, titles = SPLITS[item["id"]]
            text = (CHAPTERS / item["filename"]).read_text(encoding="utf-8")
            parts = split_blob(text, len(titles))
            (CHAPTERS / item["filename"]).unlink()
            for title, body in zip(titles, parts):
                display = f"{part}　{title}"
                markdown = f"# {display}\n\n{body}\n"
                emit(
                    {**item, "display_title": display},
                    f"{counter + 1:03d}_{_safe_filename(display)}.md",
                    markdown,
                    parent_audit,
                )
            continue
        # Kept chapters: normalize the display title/filename, de-link 目次.
        text = (CHAPTERS / item["filename"]).read_text(encoding="utf-8")
        if item["display_title"] == "目次":
            text = re.sub(
                r"(?m)^\[([^\]]+)\]\([^)]*\)\s*$", r"\1", text
            )
            item["display_title"] = "目次"
        emit(item, item["filename"], text, parent_audit)

    # Renumber filenames to match the new sequence.
    for entry, audit_entry in zip(new_manifest, new_audit):
        old_name = entry["filename"]
        if re.match(r"^\d{3}_", old_name) and not old_name.startswith(
            f"{entry['sequence']:03d}_"
        ):
            new_name = f"{entry['sequence']:03d}_" + old_name.split("_", 1)[1]
            (CHAPTERS / old_name).rename(CHAPTERS / new_name)
            entry["filename"] = new_name
            audit_entry["filename"] = new_name

    audit["chapters"] = new_audit
    audit["summary"]["chapter_count"] = len(new_audit)
    audit["summary"]["footnote_count"] = 0
    audit["summary"]["issue_count"] = 0
    audit["summary"]["blocking_issue_count"] = 0
    audit["summary"]["release_blocked"] = False
    (WORK / "chapters.json").write_text(
        json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"chapters: {len(new_manifest)}")
    for entry in new_manifest:
        print("  ", entry["sequence"], entry["display_title"][:44])
    return 0


if __name__ == "__main__":
    sys.exit(main())
