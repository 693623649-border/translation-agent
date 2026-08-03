"""2019boydphd 章节注释重组（v2）：

把"正文段 + 页脚注定义段"交错格式重排为标准结构：
  正文（〔n〕行内引用，引用标记与章末定义一一对应）+ 章末 `## 注释` 尾注。

处理规则：
1. 定义行 = 行首裸数字 或 **数字**（全书连续编号，排除 1000-2100 年份）。
   定义文本若含 "第X页。" 且其后紧跟正文引述动词（写道/指出/声称/强调/说），
   则引述部分切回正文（定义行原位），定义主体入章末。
2. 行内引用 = 标点后紧跟的裸数字（不跨空行），数字必须属于本章定义集合；
   数字后可接中文/字母/括号/标点/行尾；书名号后无空格的数字同样识别。
   已存在的 〔n〕 引用原样保留。
3. 定义续行 = 紧跟定义行、以标点开头、长度 <= 80 的行（并入定义）。
4. 正文段拼接：注释抽走后，前段不以句末标点结尾（忽略尾部〔n〕）、
   或后段以逗号类标点开头则合并。
5. 输出 = 标题/引用块/合并正文段 + `## 注释` + `**n**　定义`（编号升序）。

dry-run（--dry）只统计不写盘。
"""

import re
import sys
from pathlib import Path

OUT = Path("outputs/2019boydphd")
REVIEWED = OUT / "reviewed_chapters"

CHAPTERS = [
    ("intro", "intro.md"),
    ("ch-1", "ch-1.md"),
    ("ch-2", "ch-2.md"),
    ("ch-3", "ch-3.md"),
    ("ch-4", "ch-4.md"),
    ("ch-5", "ch-5.md"),
    ("ch-6", "ch-6.md"),
    ("conclusion", None),  # 读 chapters/017, 写 reviewed_chapters/conclusion.md
]

SENTENCE_END = "。！？!?…"
DEF_RE = re.compile(r"^\s*(?:\*\*(\d{1,4})\*\*|(\d{1,4}))[ \t　]+(?=\S)")
INLINE_AFTER_PUNCT = re.compile(
    r"([。．！？!?；;，,）)》」』”’])([ \t]*)(\d{1,4})"
    r"(?=[ \t]*[一-鿿A-Za-z(（《\"“，、。；;！？）)》」』”’\n]|$)"
)
INLINE_AFTER_BRACKET = re.compile(
    r"([》」』」])(\d{1,3})(?=[和与及]|[一-鿿])"
)
INLINE_AFTER_HAN = re.compile(
    r"(?<!第)([一-鿿])([ \t]*)(\d{1,2})(?=[，、。；）)]|，)"
)
# 孤立 URL 行 → 按内容关键词归位到对应定义编号（thesis 导入时的错位续行）
URL_ANCHORS = [
    ("networkologies", "24"),
    ("cultureandcommunication.org/galloway", "26"),
    ("shaviro.com/Blog", "28"),
    ("youtube.com/watch?v=p_00g", "31"),
    ("publicseminar.org/2015/09/otaku-philosophy", "32"),
]
QUOTE_RE = re.compile(r"^>\s?")
HEADING_RE = re.compile(r"^#{1,6}\s+\S")
NOTE_HEADING_RE = re.compile(r"^#{2,6}\s*(?:注释|脚注|尾注|注)\s*$")
# 定义文本中"第X页。"之后出现正文引述动词 → 切分点
PAGE_CITE = re.compile(r"第[^。]{0,25}页[。.]")
BODY_VERB = re.compile(r"(?:写道|指出|声称|强调|认为|说)[：:]")
NOTE_MARK_RE = re.compile(r"〔(\d+)〕")


def is_year(value: str) -> bool:
    return value.isdigit() and 1000 <= int(value) <= 2100


def split_definition_prefix(line: str) -> tuple[str, str] | None:
    m = DEF_RE.match(line)
    if not m:
        return None
    number = m.group(1) or m.group(2)
    if is_year(number):
        return None
    return number, line[m.end():].strip()


def split_body_tail(definition_text: str) -> tuple[str, str | None]:
    """定义文本中若含"第X页。"后紧跟人名+引述动词（如"拉马尔写道："）
    → 切出正文部分（引述句属于正文流，不属脚注书目）。"""
    for m in PAGE_CITE.finditer(definition_text):
        tail = definition_text[m.end():]
        if tail and re.match(
            r"[一-鿿A-Za-z·．\- ]{1,8}?(?:写道|指出|声称|强调|认为|说)[：:]",
            tail,
        ):
            return definition_text[:m.end()].rstrip(), tail.strip()
    return definition_text, None


def reflow(markdown: str) -> tuple[str, dict]:
    lines = markdown.split("\n")
    items: list[tuple[str, str]] = []
    body_buf: list[str] = []
    quote_buf: list[str] = []
    definitions: list[tuple[str, str]] = []
    def_anchors: list[tuple[str, int]] = []  # (number, items_index_at_definition)
    current_def: tuple[str, list[str]] | None = None

    def flush_body() -> None:
        if body_buf:
            items.append(("body", "\n".join(body_buf)))
            body_buf.clear()

    def flush_quote() -> None:
        if quote_buf:
            items.append(("quote", "\n".join(quote_buf)))
            quote_buf.clear()

    def flush_def() -> None:
        nonlocal current_def
        if current_def is not None:
            number, parts = current_def
            text = "".join(parts).strip()
            if text:
                head, tail = split_body_tail(text)
                definitions.append((number, head))
                if tail:
                    # 定义行中混入的正文引述：留在正文流原位
                    items.append(("body", tail))
            current_def = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # 空行不关闭定义：定义主行以残句结尾时，隔空行的标点续行仍属定义
            continue
        if HEADING_RE.match(line):
            flush_def(); flush_body(); flush_quote()
            items.append(("heading", line))
            continue
        if QUOTE_RE.match(line):
            flush_def(); flush_body()
            quote_buf.append(line)
            continue
        prefix = split_definition_prefix(line)
        if prefix is not None:
            flush_def(); flush_body(); flush_quote()
            def_anchors.append((prefix[0], len(items)))
            current_def = [prefix[0], [prefix[1]]]
            continue
        if current_def is not None:
            if (
                stripped[0] in "，。；：、　\"“"
                and len(stripped) <= 60
                and "〔" not in stripped
            ):
                current_def[1].append(stripped)
                continue
            flush_def()
        body_buf.append(line)
    flush_def(); flush_body(); flush_quote()

    def_numbers = {n for n, _ in definitions}
    new_refs: list[str] = []
    existing_refs: list[str] = []

    # 孤立 URL 行归位：thesis 导入时脚注 URL 续行错位散落在正文，
    # 按 URL 内容关键词挂回对应定义（剥离行尾引用标记，保留在正文）。
    def_numbers = {n for n, _ in definitions}
    def_number_map = {n: t for n, t in definitions}

    url_kept: list[str] = []
    for idx in range(len(items) - 1, -1, -1):
        kind, payload = items[idx]
        if kind != "body":
            continue
        first_line = payload.lstrip().split("\n", 1)[0]
        if not re.match(r"^https?://", first_line.strip()):
            continue
        # 剥离行尾 〔n〕，追加到该行之前最近的正文段/引用块
        refs = re.findall(r"〔(\d+)〕", payload)
        url_text = re.sub(r"〔\d+〕\s*$", "", payload).strip()
        if refs:
            for j in range(idx - 1, -1, -1):
                if items[j][0] in {"body", "quote"}:
                    items[j] = (
                        items[j][0],
                        items[j][1].rstrip() + "".join(f"〔{r}〕" for r in refs),
                    )
                    break
        target = None
        for feature, number in URL_ANCHORS:
            if feature in url_text:
                target = number
                break
        if target is not None and target in def_number_map:
            def_number_map[target] = def_number_map[target].rstrip() + " " + url_text
            url_kept.append(url_text[:40])
        else:
            url_kept.append(f"UNRESOLVED:{url_text[:40]}")
        del items[idx]
    if url_kept:
        unresolved = [u for u in url_kept if u.startswith("UNRESOLVED")]
        if unresolved:
            raise AssertionError(f"孤立 URL 无法归位: {unresolved}")
    if url_kept:
        definitions[:] = [(n, def_number_map[n]) for n, _ in definitions]

    def mark_inline(text: str) -> str:
        def replace(m: re.Match) -> str:
            number = m.group(3)
            if number in def_numbers:
                new_refs.append(number)
                return f"{m.group(1)}〔{number}〕"
            return m.group(0)
        text = INLINE_AFTER_PUNCT.sub(replace, text)
        text = INLINE_AFTER_HAN.sub(replace, text)
        return text

    for idx, (kind, payload) in enumerate(items):
        if kind == "body":
            items[idx] = (kind, mark_inline(payload))

    # 正文中丢失的引用标记：追加到定义行之前最近的正文段/引用块末尾
    # （原论文页脚注所属的位置；保证引用 ↔ 定义双向闭环）
    # 注意：文件里已存在的 〔n〕 必须计入 seen，否则已标记的引用会被重复追加。
    existing_before: set[str] = set()
    for kind, payload in items:
        if kind in {"body", "quote"}:
            existing_before.update(NOTE_MARK_RE.findall(payload))
    seen_refs: set[str] = set(new_refs) | existing_before
    for number, anchor in def_anchors:
        if number in seen_refs:
            continue
        for idx in range(anchor - 1, -1, -1):
            kind, payload = items[idx]
            if kind not in {"body", "quote"}:
                continue
            items[idx] = (kind, payload.rstrip() + f"〔{number}〕")
            seen_refs.add(number)
            break

    new_items: list[tuple[str, str]] = [
        (kind, payload)
        for kind, payload in items
        if not (kind == "heading" and NOTE_HEADING_RE.match(payload))
    ]

    final_items = new_items

    # 正文段拼接
    merged: list[str] = []

    def sentence_ended(text: str) -> bool:
        text = text.rstrip()
        m = NOTE_MARK_RE.search(text)
        if m:
            text = text[: m.start()].rstrip()
        return text.endswith(tuple(SENTENCE_END))

    for kind, payload in final_items:
        if kind != "body":
            continue
        text = payload.strip()
        if not text:
            continue
        if merged:
            prev = merged[-1]
            if (
                not sentence_ended(prev)
                or text.lstrip().startswith(("、", "，", "；", ":", "：", "—", "－"))
            ):
                merged[-1] = prev.rstrip() + "\n" + text
                continue
        merged.append(text)

    # 统计正文区已有 〔n〕（ch-5/6 已标记化的引用；引用块内的引用同样有效）
    body_text = "\n".join(
        payload for kind, payload in final_items if kind in {"body", "quote"}
    )
    existing_refs = list(NOTE_MARK_RE.findall(body_text))

    # 输出
    out: list[str] = []
    for kind, payload in final_items:
        if kind == "heading":
            out.append(payload)
        elif kind == "quote":
            out.append(payload)
        elif kind == "body":
            out.append("")
            out.append(payload)
    final: list[str] = []
    for line in out:
        if line.strip() == "" and final and final[-1].strip() == "":
            continue
        final.append(line)

    if definitions:
        final.append("")
        final.append("## 注释")
        for number, text in sorted(definitions, key=lambda x: int(x[0])):
            final.append("")
            final.append(f"**{number}**　{text}")
    final.append("")

    def_ids = sorted(def_numbers, key=int)
    ref_ids = sorted(set(new_refs) | set(existing_refs), key=int)
    stats = {
        "definitions": len(definitions),
        "def_ids": def_ids,
        "ref_ids": ref_ids,
        "missing_refs": [n for n in def_ids if n not in ref_ids],
        "orphan_refs": [n for n in ref_ids if n not in def_ids],
    }
    return "\n".join(final), stats


def main() -> None:
    dry = "--dry" in sys.argv
    for chapter_id, filename in CHAPTERS:
        source = REVIEWED / filename if filename else None
        if source is None or not source.exists():
            source = OUT / "chapters" / "017_结论_作为任意世界的御宅世界影像.md"
        md = source.read_text(encoding="utf-8").lstrip("﻿")
        result, stats = reflow(md)
        print(
            f"{chapter_id:12s} 定义={stats['definitions']:3d} "
            f"引用(唯一)={len(stats['ref_ids']):3d} "
            f"缺引用={stats['missing_refs'][:10]} "
            f"无定义引用={stats['orphan_refs'][:5]}"
        )
        if dry:
            continue
        target = REVIEWED / (filename or "conclusion.md")
        target.write_text(result, encoding="utf-8")
        print(f"  -> 写入 {target}")


if __name__ == "__main__":
    main()
