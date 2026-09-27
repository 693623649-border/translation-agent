"""Rebuild outputs/视差之见/toc.json from the PDF's own bookmark tree.

The scanned PDF ships a complete, accurate bookmark outline (68 entries) while
the LLM TOC pass truncated titles (道成肉身是喜 / 自由之囚 / 诱言，诱惑 …) and
dropped most page anchors.  This script treats the bookmarks as authoritative:
printed_page = pdf_page - PAGE_OFFSET (verified against running heads), parts
(一 恒星视差 / 二 太阳视差 / 三 月球视差) become kind="part" so chapter
granularity skips their title-less container pages, 插曲1/插曲2 are chapters,
and the （N） subsections become kind="section".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

WORK = Path(r"outputs/视差之见")
PAGE_OFFSET = 9

# (level_in_book, title, pdf_page, kind)
# level 1 = part, 2 = chapter/插曲, 3 = section
ENTRIES: list[tuple[int, str, int, str]] = [
    (2, "引论：辩证唯物主义兵临城下", 10, "chapter"),
    (1, "一  恒星视差：存有论差异之陷阱", 34, "part"),
    (2, "1  主体，这个“在内心行过割礼的犹太人”", 34, "chapter"),
    (3, "（1）发痒的客体", 34, "section"),
    (3, "（2）康德式视差", 40, "section"),
    (3, "（3）从（康德的）二律背反精神中脱颖而出的（黑格尔的）具体普遍性", 55, "section"),
    (3, "（4）主人能指及其兴衰", 69, "section"),
    (3, "（5）愿微风轻吹", 78, "section"),
    (3, "（6）政治经济学批判之视差", 94, "section"),
    (3, "（7）“……这唯一的客体，太虚因之而荣幸”", 109, "section"),
    (2, "2  用以堆积唯物主义神学的砌块", 125, "chapter"),
    (3, "（1）少年遇到淑女", 125, "section"),
    (3, "（2）作为黑格尔派哲学家的克尔凯郭尔", 136, "section"),
    (3, "（3）挫败", 146, "section"),
    (3, "（4）纯粹牺牲这个陷阱", 155, "section"),
    (3, "（5）做个康德派哲学家，还真是不容易", 163, "section"),
    (3, "（6）道成肉身是喜剧", 185, "section"),
    (3, "（7）作为政治范畴的“奥德拉岱克”", 197, "section"),
    (3, "（8）活得太久！", 210, "section"),
    (2, "插曲1  康德的选择，或，亨利·詹姆斯的唯物主义", 218, "chapter"),
    (1, "二  太阳视差：难以承受的非我之轻", 252, "part"),
    (2, "3  难以承受的神圣狗屎之重", 252, "chapter"),
    (3, "（1）被太阳烤焦", 252, "section"),
    (3, "（2）拣起你的洞来！", 276, "section"),
    (3, "（3）哥白尼、达尔文、弗洛伊德……还有很多别的人", 280, "section"),
    (3, "（4）走向新的表象科学", 291, "section"),
    (3, "（5）对祛魅的抗拒", 299, "section"),
    (3, "（6）上帝四处游荡之时", 312, "section"),
    (3, "（7）去崇高的后意识形态客体", 320, "section"),
    (3, "（8）危险？什么危险？", 331, "section"),
    (2, "4  自由之回环", 339, "chapter"),
    (3, "（1）“设定预设”", 339, "section"),
    (3, "（2）认知主义者黑格尔？", 351, "section"),
    (3, "（3）虚假的不透明", 359, "section"),
    (3, "（4）情绪在撒谎，或，达马西奥错在哪里", 371, "section"),
    (3, "（5）黑格尔、马克思、丹尼特", 387, "section"),
    (3, "（6）从物理到设计？", 396, "section"),
    (3, "（7）无意识的自由行为", 402, "section"),
    (3, "（8）诱惑的语言，语言的诱惑", 411, "section"),
    (2, "插曲2  社会链接中的小客体，或，排犹主义的僵局", 418, "chapter"),
    (1, "三  月球视差：走向减法政治", 446, "part"),
    (2, "5  从剩余价值到剩余权力", 446, "chapter"),
    (3, "（1）存有性的漂泊，存有论的真理", 446, "section"),
    (3, "（2）听之任之？不，谢谢！", 455, "section"),
    (3, "（3）走向斯大林主义音乐片理论", 471, "section"),
    (3, "（4）生物政治视差", 478, "section"),
    (3, "（5）四种话语的史实性", 482, "section"),
    (3, "（6）作为一个政治范畴的原乐", 498, "section"),
    (3, "（7）我们活在世上吗？", 510, "section"),
    (2, "6  淫荡的意识形态纽结，以及如何解开它", 529, "chapter"),
    (3, "（1）学术游荡，或，权力与抵抗的视差", 529, "section"),
    (3, "（2）人权与非人之权", 538, "section"),
    (3, "（3）被框定的暴力", 547, "section"),
    (3, "（4）鸡的浑然不知", 553, "section"),
    (3, "（5）谁害怕原教旨主义这个大坏蛋？", 566, "section"),
    (3, "（6）飞越彩虹联盟！", 573, "section"),
    (3, "（7）作为意识形态理论家的罗伯特·舒曼", 581, "section"),
    (3, "（8）欢迎来到美国亚文化这个实在界", 583, "section"),
    (3, "（9）鸡蛋、煎蛋卷和巴特尔比的微笑", 596, "section"),
    (1, "译者后记", 612, "other"),
]


def main() -> int:
    path = WORK / "toc.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = []
    for index, (level, title, pdf_page, kind) in enumerate(ENTRIES, start=1):
        entries.append(
            {
                "id": f"toc-{index:04d}",
                "index": "",
                "title": title,
                "level": level,
                "kind": kind,
                "printed_page": pdf_page - PAGE_OFFSET,
                "pdf_page": pdf_page,
                "end_pdf_page": None,
            }
        )
    payload["entries"] = entries
    payload["page_offset"] = PAGE_OFFSET
    payload["toc_pdf_pages"] = [7, 8, 9]
    payload["offset_evidence"] = [
        {
            "entry_id": entry["id"],
            "title": entry["title"],
            "printed_page": entry["printed_page"],
            "pdf_page": entry["pdf_page"],
            "offset": PAGE_OFFSET,
            "score": 1.0,
            "printed_pages_per_pdf_page": 1,
        }
        for entry in entries
        if entry["kind"] == "chapter"
    ]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"entries: {len(entries)} (chapters: {sum(1 for e in entries if e['kind'] in ('chapter', 'other'))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
