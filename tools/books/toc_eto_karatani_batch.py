"""Write final TOC payloads for the three scanned 江藤淳/柄谷行人 PDFs.

- 成熟と喪失: no printed TOC — the book is one continuous essay plus あとがき.
- 漱石論集成: rebuilt from the printed vertical TOC (numbers verified against
  GLM anchors and page-top evidence; offset +3).
- 江藤淳と少女: GLM TOC was good, but container chapters are demoted to parts
  and their long sections promoted to chapters for retrieval granularity.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def entry(eid, title, level, kind, pdf_page=None):
    return {
        "id": eid,
        "index": "",
        "title": title,
        "level": level,
        "kind": kind,
        "printed_page": (pdf_page - 3) if pdf_page else None,
        "pdf_page": pdf_page,
        "end_pdf_page": None,
    }


def payload(entries, offset=3):
    return {
        "schema_version": 1,
        "toc_pdf_pages": [],
        "page_offset": offset,
        "printed_pages_per_pdf_page": 1,
        "offset_evidence": [
            {
                "entry_id": e["id"],
                "title": e["title"],
                "printed_page": e["printed_page"],
                "pdf_page": e["pdf_page"],
                "offset": offset,
                "score": 1.0,
                "printed_pages_per_pdf_page": 1,
            }
            for e in entries
            if e["pdf_page"] is not None
        ],
        "entries": entries,
    }


def seijuku() -> dict:
    entries = [
        entry("toc-0001", "成熟と喪失―「母」の崩壊", 1, "chapter", 4),
        entry("toc-0002", "あとがき", 1, "other", 254),
    ]
    return payload(entries)


def joujou_shoujo() -> dict:
    entries = [
        entry("toc-0001", "序章　犬猫に根差した思想", 1, "chapter", 8),
        entry("toc-0002", "サブカルチャ文学論・江藤淳編", 1, "part", 16),
        entry("toc-0003", "ツルリとしたものと妻の崩壊", 2, "chapter", 17),
        entry("toc-0004", "「母を崩壊させない小説」を探した少年のために", 2, "chapter", 51),
        entry("toc-0005", "江藤淳と来歴否認の人々", 1, "part", 63),
        entry("toc-0006", "三島由紀夫とサブカルチャとしての日本", 2, "chapter", 106),
        entry("toc-0007", "手塚治虫と非リアリズム的「日本語の可能性」", 2, "chapter", 127),
        entry("toc-0008", "江藤淳と来歴否認の人々", 2, "chapter", 143),
        entry("toc-0009", "柳田國男と「家への忸怩」", 2, "chapter", 164),
        entry("toc-0010", "村上春樹と村上龍の「私」語りをめぐって", 2, "chapter", 182),
        entry("toc-0011", "歴史と私の立ち位置", 1, "chapter", 198),
        entry("toc-0012", "あとがき", 1, "other", 219),
    ]
    return payload(entries)


def shaseki() -> dict:
    entries = [
        entry("toc-0001", "Ⅰ　漱石試論Ⅰ", 1, "part", 10),
        entry("toc-0002", "意識と自然", 2, "chapter", 12),
        entry("toc-0003", "内側から見た生", 2, "chapter", 73),
        entry("toc-0004", "階級について", 2, "chapter", 101),
        entry("toc-0005", "文学について", 2, "chapter", 127),
        entry("toc-0006", "風景の発見", 2, "chapter", 155),
        entry("toc-0007", "Ⅱ　漱石試論Ⅱ", 1, "part", 198),
        entry("toc-0008", "漱石とジンル", 2, "chapter", 200),
        entry("toc-0009", "漱石の「文…」", 2, "chapter", 236),
        entry("toc-0010", "作品解説", 1, "part", 264),
        entry("toc-0011", "『門』", 2, "chapter", 266),
        entry("toc-0012", "『草枕』", 2, "chapter", 273),
        entry("toc-0013", "『それから』", 2, "chapter", 280),
        entry("toc-0014", "『三四郎』", 2, "chapter", 287),
        entry("toc-0015", "『明暗』", 2, "chapter", 294),
        entry("toc-0016", "断片", 2, "chapter", 301),
        entry("toc-0017", "Ⅲ　講演その他", 1, "part", 304),
        entry("toc-0018", "『虞美人草』", 2, "chapter", 308),
        entry("toc-0019", "『彼岸過迄』", 2, "chapter", 318),
        entry("toc-0020", "漱石の多様性", 2, "chapter", 336),
        entry("toc-0021", "漱石と構造", 2, "chapter", 353),
        entry("toc-0022", "淋しい「昭和の精神」", 2, "chapter", 360),
        entry("toc-0023", "漱石の「文…」（講演）", 2, "chapter", 365),
        entry("toc-0024", "エクリチュル", 2, "chapter", 370),
        entry("toc-0025", "あとがき", 1, "other", 416),
        entry("toc-0026", "【柄谷行人著書一覧】", 1, "other", 419),
        entry("toc-0027", "【初出一覧】", 1, "other", 420),
    ]
    return payload(entries)


def main() -> int:
    jobs = [
        ("outputs/成熟と喪失", seijuku()),
        ("outputs/江藤淳と少女", joujou_shoujo()),
        ("outputs/漱石論集成", shaseki()),
    ]
    for directory, payload_data in jobs:
        path = Path(directory) / "toc.json"
        path.write_text(
            json.dumps(payload_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(path, "entries:", len(payload_data["entries"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
