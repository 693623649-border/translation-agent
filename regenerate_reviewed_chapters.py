"""Regenerate reader-facing chapters from continuous PDF text-layer prose.

The normal pipeline deliberately checkpoints and translates one physical PDF
page at a time.  That is safe and resumable, but a dissertation with page-foot
notes can place a half sentence, then the page's notes, then the other half on
the next page.  This utility reconstructs the logical chapter first, moves
notes to a chapter-end notes section, translates paragraph-marked units in
parallel, and writes validated ``reviewed_chapters/<toc-id>.md`` overrides.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import threading
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import fitz

from book_pipeline import (
    DeepSeekClient,
    clean_translation_text,
    load_env_file,
    normalize_target_script,
    write_json,
)
from pipeline_profiles import ModelProfile, load_pipeline_profiles


REVIEW_PROMPT_VERSION = "reviewed-continuous-academic-v2"
LONG_FOOTNOTE_SEPARATOR = re.compile(r"\n[ \t]{20,}\n")
NOTE_START = re.compile(r"^\s*(\d{1,4})(?:\s+|(?=[A-ZÀ-ÖØ-Þ]))(.*)$")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FIGURE = re.compile(
    r"^Figures?\s+(\d+(?:(?:\s*[-–—]\s*|\s+and\s+)\d+)?)\.?$",
    re.I,
)
MARKER = re.compile(r"⟦([PN]\d{4})⟧")
REFERENCE_TOKEN = re.compile(r"⟦R(\d{1,4})⟧")
TERMINAL = re.compile(r"[.!?][\]\[)）'’\"”]*\d{0,4}$")

# PyMuPDF's logical text order puts these top-of-page figure labels after the
# page prose.  The visual source was checked before adding this narrow map.
TOP_FIGURE_PAGES: dict[str, set[int]] = {
    "ch-6": {292, 302, 305},
}

GLOSSARY = """
TYPE-MOON=TYPE-MOON；Nasuverse=奈须宇宙（首次可保留英文）；otaku=御宅族；
otaku world-image=御宅世界影像；Soulful Body=灵性身体；History（大写）=“历史”；
all-times=“全时间”；co-existential=共存性；transtemporal=跨时间；
nomadology=游牧学；fabulation=寓言创制；assemblage=组装体；dividual=分体；
plane of immanence=内在性平面；movement-image=运动—影像；
action-image=动作—影像；affection-image=情感—影像；
perception-image=感知—影像；recollection-image=回忆—影像；
time-image=时间—影像；crystal-image=晶体—影像；crystalline=晶体式；
seed-image=种子—影像；anime-image=动画影像；anime machine=动画机器；
animetic=动画式；animetic interval=动画式间隙；affect=情动；
sensorium=感知体系；line of flight=逃逸线；deterritorialisation=解域化；
minor/minoritarian=少数/少数化；becoming-woman=生成—女人；
actual/virtual=现实态/潜在态；opsign=纯视像符号；sonsign=纯声像符号；
lectosign=读符；hyalosign=晶体符号；chronosign=时间符号；
noosign=思维符号；Joan of Arc effect=圣女贞德效应；
Nero effect=尼禄效应；hypermnesia=记忆亢进；reticular pessimism=网状悲观主义；
interface effect=界面效应；data-image=数据—影像；Reality Marble=固有结界；
Holy Grail War=圣杯战争；Heroic Spirit=英灵；Servant=从者；
Noble Phantasm=宝具；Gate of Babylon=王之财宝；Ea=乖离剑Ea；
Enuma Elish=天地乖离开辟之星；Ionioi Hetairoi=王之军势；
Unlimited Blade Works=无限剑制；Avalon=遥远的理想乡；
Arcueid Brunestud=爱尔奎特·布伦史塔德；Fuyuki=冬木；
Shirō=卫宫士郎；Sawaragi Noi=椹木野衣；Shiriagari Kotobuki=しりあがり寿；
Wada Aruko=ワダアルコ；Kurahana Chinatsu=仓花千夏；Hakuno=岸波白野；
Twice H. Pieceman=特维斯·H·皮斯曼；Shinji=间桐慎二；
Shaft=SHAFT（沙夫特）；Nadia: The Secret of Blue Water=《蓝宝石之谜》；
aspect-to-aspect transition=视点到视点转场。
potency=力量/效力；vibrant=鲜明的/绚丽的；worldview=世界观；wield=持有/运用；
articulated=清晰可辨的/清晰发出的；milieu=环境；hyalosign/hyalosigns=晶体符号；
antiquated=陈旧的；flora=植物群/植被；visceral=本能而强烈的；tactile=触觉的；haptic=触感的/触感；
gachapon=扭蛋；proliferation/proliferations=激增/扩张；enacting=实现/施行；
Gilles Deleuze=吉尔·德勒兹；Félix Guattari=费利克斯·瓜塔里；
Deleuze and Guattari=德勒兹与瓜塔里；Sabu Kohso=高祖岩三郎；
Galloway=加洛韦；Napier=纳皮尔；Rodowick=罗多维克；
protocological=协议式的/协议化的；City of Deceit=欺诈之城；
deep digitality=深层数字性；multiples（名词）=多重体。
fan fiction visual novel=同人视觉小说；becoming-visionary=生成—幻视者；
becoming-seer=生成—见者；gender-bending/gender-queered=性别反转/性别酷儿化；
plastic representation=造型表现；Romano-Celtic Britain=罗马—凯尔特不列颠；
Caliburn=石中剑卡利班；rhizome=根茎；rhizomatic/rhizomatics=根茎式/根茎主义；
temporal hiccup=时间顿挫。
""".strip()


def effective_glossary(unit: TranslationUnit) -> str:
    """Add narrowly scoped name guidance only to units that require it."""

    additions: list[str] = []
    if "Miyoshi and Harootunian" in unit.source:
        # DeepSeek otherwise repeatedly emitted a Unicode replacement glyph
        # for Miyoshi.  The full names disambiguate the two cited scholars.
        additions.append(
            "Miyoshi Masao（三好将夫）=三好将夫；Harry Harootunian=哈里·哈鲁图尼安。"
        )
    return "\n".join((GLOSSARY, *additions))


@dataclass
class SourceBlock:
    kind: str
    text: str
    pdf_page: int
    quote: bool = False
    marker: str = ""
    starts_paragraph: bool = False


@dataclass
class Note:
    number: int
    lines: list[str]

    @property
    def text(self) -> str:
        return reflow_lines(self.lines)


@dataclass(frozen=True)
class TranslationUnit:
    kind: str
    index: int
    markers: tuple[str, ...]
    source: str


def reflow_lines(lines: Iterable[str]) -> str:
    """Join PDF visual wraps while retaining compound-word hyphens."""

    paragraphs: list[str] = []
    current = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            if current:
                paragraphs.append(current.strip())
                current = ""
            continue
        if not current:
            current = line
        elif current.endswith("-"):
            current += line
        else:
            current += " " + line
    if current:
        paragraphs.append(current.strip())
    return "\n\n".join(paragraphs).strip()


def split_page_streams(text: str) -> tuple[str, str]:
    """Return body and footnote streams using the final long-space separator."""

    matches = list(LONG_FOOTNOTE_SEPARATOR.finditer(text))
    if not matches:
        return text.strip(), ""
    separator = matches[-1]
    body = text[: separator.start()].strip()
    note_lines: list[str] = []
    trailing_figures: list[str] = []
    for raw_line in text[separator.end() :].strip().splitlines():
        if FIGURE.fullmatch(raw_line.strip()):
            # Full-page figures can place their caption below the footnote
            # separator in logical extraction order.  It is publication
            # content, not part of the final note definition.
            trailing_figures.append(raw_line.strip())
        else:
            note_lines.append(raw_line)
    if trailing_figures:
        body = "\n\n".join((body, *trailing_figures)).strip()
    notes = "\n".join(note_lines).strip()
    return body, notes


def layout_line_key(text: str) -> str:
    """Normalise a PDF layout line for matching against checkpoint text."""

    return re.sub(r"\s+", " ", text).strip()


def page_layout_hints(
    document: fitz.Document,
    pdf_page: int,
) -> tuple[set[str], set[str]]:
    """Return paragraph-start and displayed-quotation lines from PDF layout.

    The plain-text checkpoint intentionally carries no coordinates.  In this
    thesis a normal body line begins near x=113 while the first line of a new
    paragraph begins near x=149.  Displayed quotations remain indented for all
    their lines, so an indentation transition (rather than indentation alone)
    marks their beginning.  A larger vertical gap catches the first normal
    line following a displayed quotation or figure.
    """

    if pdf_page < 1 or pdf_page > document.page_count:
        raise ValueError(
            f"PDF page {pdf_page} is outside source document ({document.page_count} pages)."
        )
    page = document[pdf_page - 1]
    lines: list[tuple[float, float, str]] = []
    for block in page.get_text("dict", sort=False).get("blocks", []):
        for line in block.get("lines", []):
            text = "".join(str(span.get("text") or "") for span in line.get("spans", []))
            key = layout_line_key(text)
            if key:
                x0, y0 = float(line["bbox"][0]), float(line["bbox"][1])
                lines.append((x0, y0, key))
    # Ignore the running page number and the footer area.  Otherwise the large
    # gap below a header would make every page-top continuation look like a
    # fresh paragraph.
    # Body text can begin at y≈61 while the running number is at y≈36.  Keep
    # that first body line: dropping it would make a continued indented quote
    # look like a new paragraph on the following page.
    lines = [item for item in lines if 55.0 <= item[1] < 700.0]
    if not lines:
        return set(), set()

    # Use the leftmost plausible prose/note edge.  A quote-heavy page can have
    # more indented than ordinary lines, so the statistical mode would
    # incorrectly treat the quotation margin as the baseline.  Running page
    # numbers and centred figure labels have already been excluded by the
    # vertical/content filters.
    left_edges = [round(x0) for x0, _y0, key in lines if x0 < page.rect.width * 0.45 and len(key) > 1]
    if not left_edges:
        return set(), set()
    baseline = min(left_edges)
    vertical_deltas = [
        current[1] - previous[1]
        for previous, current in zip(lines, lines[1:])
        if 5.0 < current[1] - previous[1] < 35.0
    ]
    normal_gap = statistics.median(vertical_deltas) if vertical_deltas else 18.0
    indent_threshold = baseline + 20.0
    gap_threshold = max(27.0, normal_gap * 1.45)

    starts: set[str] = set()
    start_indexes: list[int] = []
    previous: tuple[float, float, str] | None = None
    for index, current in enumerate(lines):
        x0, y0, key = current
        is_start = False
        if previous is None:
            if x0 > indent_threshold:
                is_start = True
        else:
            previous_x, previous_y, _previous_key = previous
            indentation_transition = (
                x0 > indent_threshold and previous_x <= indent_threshold
            )
            separated_block = y0 - previous_y > gap_threshold
            if indentation_transition or separated_block:
                is_start = True
        if is_start:
            starts.add(key)
            start_indexes.append(index)
        previous = current

    # A normal paragraph has only its first line indented; displayed
    # quotations keep every line at the wider left margin.  Grouping on the
    # same layout boundaries therefore distinguishes them without relying on
    # punctuation or language-specific quote characters.
    quote_starts: set[str] = set()
    boundaries = sorted(set((0, *start_indexes, len(lines))))
    for begin, finish in zip(boundaries, boundaries[1:]):
        group = lines[begin:finish]
        if (
            len(group) >= 2
            and group[0][2] in starts
            and all(item[0] > indent_threshold for item in group)
        ):
            quote_starts.add(group[0][2])
    return starts, quote_starts


def page_layout_paragraph_starts(document: fitz.Document, pdf_page: int) -> set[str]:
    """Compatibility helper returning only paragraph-start hints."""

    return page_layout_hints(document, pdf_page)[0]


def parse_body_blocks(
    text: str,
    pdf_page: int,
    paragraph_starts: set[str] | None = None,
    quote_starts: set[str] | None = None,
) -> list[SourceBlock]:
    """Split logical prose while keeping headings and figure labels structural.

    PyMuPDF commonly emits a figure label on the first or last visual line of
    an otherwise ordinary paragraph without a blank line around it.  Reflowing
    the whole blank-line block first would therefore turn ``Figures 19-22``
    into prose and defeat both the figure placeholder and the page-specific
    relocation rules.
    """

    blocks: list[SourceBlock] = []
    prose_lines: list[str] = []
    prose_starts_paragraph = False
    prose_is_quote = False

    def flush_prose() -> None:
        nonlocal prose_starts_paragraph, prose_is_quote
        if not prose_lines:
            return
        value = reflow_lines(prose_lines)
        prose_lines.clear()
        if value:
            blocks.append(
                SourceBlock(
                    "body",
                    value,
                    pdf_page,
                    quote=prose_is_quote,
                    starts_paragraph=prose_starts_paragraph,
                )
            )
        prose_starts_paragraph = False
        prose_is_quote = False

    layout_enabled = paragraph_starts is not None
    paragraph_starts = paragraph_starts or set()
    quote_starts = quote_starts or set()
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if not line:
            flush_prose()
            continue
        if HEADING.fullmatch(line):
            flush_prose()
            blocks.append(SourceBlock("heading", line, pdf_page))
            continue
        if FIGURE.fullmatch(line):
            flush_prose()
            blocks.append(SourceBlock("figure", line, pdf_page))
            continue
        begins_paragraph = layout_line_key(line) in paragraph_starts
        if begins_paragraph and prose_lines:
            flush_prose()
        if not prose_lines:
            prose_starts_paragraph = begins_paragraph
            prose_is_quote = layout_line_key(line) in quote_starts
        prose_lines.append(raw_line)
        # The source thesis encodes a logical paragraph break as two trailing
        # spaces on its final visual line.  Ordinary wrapped lines have zero
        # or one trailing space, so preserve that distinction before strip/
        # reflow removes it.
        # Trailing double spaces are only a fallback for checkpoints without
        # the original PDF.  In justified PDF text they can also occur on a
        # completely ordinary wrapped line (including the first line of a
        # cross-page quotation); layout coordinates are authoritative when
        # available.
        if not layout_enabled and raw_line.endswith("  "):
            flush_prose()
    flush_prose()
    return blocks


def should_join_across_page(previous: SourceBlock, following: SourceBlock) -> bool:
    if previous.kind != "body" or following.kind != "body":
        return False
    tail = previous.text.rstrip()
    if not tail or TERMINAL.search(tail):
        return False
    # A displayed quotation that runs onto the next page remains indented on
    # that page, which otherwise resembles a fresh paragraph start.  Join only
    # when both sides are layout-confirmed quotations and the first is
    # syntactically incomplete.
    if previous.quote and following.quote:
        return True
    if following.starts_paragraph:
        return False
    return True


def join_text(left: str, right: str) -> str:
    return left.rstrip() + ("" if left.rstrip().endswith("-") else " ") + right.lstrip()


def append_note_stream(
    note_text: str,
    notes: "OrderedDict[int, Note]",
    active_number: int | None,
) -> int | None:
    """Append one page's note stream, including a carried-over long note."""

    for raw in note_text.splitlines():
        line = raw.strip()
        if not line:
            if active_number is not None:
                notes[active_number].lines.append("")
            continue
        match = NOTE_START.match(line)
        number = int(match.group(1)) if match else None
        if match and (active_number is None or number == active_number + 1):
            active_number = number
            notes.setdefault(number, Note(number, []))
            remainder = match.group(2).strip()
            if remainder:
                notes[number].lines.append(remainder)
            continue
        if active_number is None:
            raise ValueError(
                "Footnote continuation appeared before the first numbered note: "
                f"{line[:100]!r}"
            )
        notes[active_number].lines.append(line)
    return active_number


def display_title(entry: dict[str, Any]) -> str:
    return " ".join(
        value.strip() for value in (str(entry.get("index") or ""), str(entry["title"]))
        if value.strip()
    )


def chapter_range(
    entries: Sequence[dict[str, Any]], chapter_id: str, last_page: int
) -> tuple[dict[str, Any], int, int]:
    position = next((i for i, item in enumerate(entries) if item["id"] == chapter_id), None)
    if position is None:
        raise ValueError(f"Unknown TOC entry: {chapter_id}")
    entry = entries[position]
    start = int(entry["pdf_page"])
    level = int(entry["level"])
    end = last_page
    for following in entries[position + 1 :]:
        if int(following["level"]) <= level:
            end = int(following["pdf_page"]) - 1
            break
    return entry, start, end


def load_page_text(output_dir: Path, pdf_page: int) -> str:
    path = output_dir / "pages" / f"page_{pdf_page:04d}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("pdf_page") or 0) != pdf_page:
        raise ValueError(f"Page checkpoint mismatch: {path}")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise ValueError(f"Empty source text: {path}")
    return text


def reconstruct_chapter(
    output_dir: Path,
    chapter_id: str,
    entry: dict[str, Any],
    start: int,
    end: int,
    paragraph_starts_by_page: dict[int, set[str]] | None = None,
    quote_starts_by_page: dict[int, set[str]] | None = None,
) -> tuple[list[SourceBlock], "OrderedDict[int, Note]"]:
    blocks: list[SourceBlock] = []
    notes: "OrderedDict[int, Note]" = OrderedDict()
    active_note: int | None = None
    for pdf_page in range(start, end + 1):
        body, note_stream = split_page_streams(load_page_text(output_dir, pdf_page))
        page_blocks = parse_body_blocks(
            body,
            pdf_page,
            (paragraph_starts_by_page or {}).get(pdf_page),
            (quote_starts_by_page or {}).get(pdf_page),
        )
        if pdf_page in TOP_FIGURE_PAGES.get(chapter_id, set()):
            figures = [item for item in page_blocks if item.kind == "figure"]
            page_blocks = figures + [item for item in page_blocks if item.kind != "figure"]
        if blocks and page_blocks and should_join_across_page(blocks[-1], page_blocks[0]):
            blocks[-1].text = join_text(blocks[-1].text, page_blocks[0].text)
            page_blocks = page_blocks[1:]
        blocks.extend(page_blocks)
        if note_stream:
            active_note = append_note_stream(note_stream, notes, active_note)

    # The reviewed file supplies its own validated H1.  Retain only H2+ source
    # headings, whose Chinese replacements were already human-reviewed.
    blocks = [
        item
        for item in blocks
        if not (item.kind == "heading" and item.text.startswith("# "))
    ]
    previous_body: SourceBlock | None = None
    has_layout_hints = paragraph_starts_by_page is not None
    for item in blocks:
        if item.kind == "body":
            punctuation_quote = bool(
                previous_body is not None
                and previous_body.text.rstrip().endswith((":", "："))
                and (item.starts_paragraph or not has_layout_hints)
            )
            item.quote = item.quote or punctuation_quote
            previous_body = item
        elif item.kind == "heading":
            previous_body = None

    numbers = list(notes)
    if not numbers or numbers != list(range(numbers[0], numbers[-1] + 1)):
        raise ValueError(
            f"{chapter_id} footnote definitions are not continuous: "
            f"{numbers[:3]} ... {numbers[-3:] if numbers else []}"
        )
    return blocks, notes


def protect_references(blocks: list[SourceBlock], notes: "OrderedDict[int, Note]") -> None:
    for number in notes:
        pattern = re.compile(rf"(?<!\d){number}(?!\d)")
        matches = sum(len(pattern.findall(item.text)) for item in blocks if item.kind == "body")
        if matches != 1:
            raise ValueError(
                f"Footnote {number} must have exactly one body reference; found {matches}."
            )
        for item in blocks:
            if item.kind == "body":
                item.text = pattern.sub(f"⟦R{number}⟧", item.text)


def assign_markers(blocks: list[SourceBlock], notes: "OrderedDict[int, Note]") -> None:
    body_index = 0
    for item in blocks:
        if item.kind == "body":
            body_index += 1
            item.marker = f"P{body_index:04d}"
    for number, note in notes.items():
        # Note markers use their stable original number, which is unique in a
        # dissertation and makes audit/checkpoint files easy to inspect.
        if number > 9999:
            raise ValueError(f"Footnote number is too large: {number}")


def make_units(
    blocks: list[SourceBlock],
    notes: "OrderedDict[int, Note]",
    max_chars: int,
) -> list[TranslationUnit]:
    units: list[TranslationUnit] = []

    def batch(kind: str, items: list[tuple[str, str]]) -> None:
        current: list[tuple[str, str]] = []
        size = 0

        def flush() -> None:
            nonlocal current, size
            if not current:
                return
            index = sum(unit.kind == kind for unit in units) + 1
            source = "\n\n".join(f"⟦{marker}⟧ {text}" for marker, text in current)
            units.append(
                TranslationUnit(kind, index, tuple(marker for marker, _ in current), source)
            )
            current, size = [], 0

        for marker, text in items:
            item_size = len(marker) + len(text) + 6
            if current and size + item_size > max_chars:
                flush()
            if item_size > max_chars:
                raise ValueError(
                    f"A single {kind} paragraph exceeds --max-chars: {marker} ({item_size})."
                )
            current.append((marker, text))
            size += item_size
        flush()

    batch("body", [(item.marker, item.text) for item in blocks if item.kind == "body"])
    batch("note", [(f"N{number:04d}", note.text) for number, note in notes.items()])
    return units


def translation_prompt(unit: TranslationUnit) -> tuple[str, str]:
    kind_instruction = (
        "这些是连续的学术正文段落。保持每个段落独立，不合并、删减或调换顺序。"
        if unit.kind == "body"
        else "这些是章末学术注释。完整翻译解释性文字，并保留作者、书名、出版信息、URL和页码。"
    )
    glossary = effective_glossary(unit)
    prompt = f"""
请将下列已经从英文 PDF 原稿重建并消除物理分页的文本完整翻译为流畅、严谨的简体中文。

要求：
1. {kind_instruction}
2. 每个形如 ⟦P0001⟧ 或 ⟦N0663⟧ 的段落标记必须原样保留一次，放在对应译文开头；不得新增标记。
3. 形如 ⟦R663⟧ 的注释引用是受保护标记，必须原样、原位保留；不得翻译或删除。
4. 不总结、不压缩、不扩写；完整保留论证、限定语、引文、例子及文献信息。
5. 原稿已合并跨页断句。按英文语法修复明显的断词和 OCR 拼写错误，不得把半句话另起一段。
6. 采用自然的中文学术表达，避免逐词硬译。所有专名和理论术语必须遵循下列统一表：
{glossary}
7. Fate、TYPE-MOON 等已有正式译名的作品、人物和设定优先使用通行中文译名；无法确认时保留原文，不要猜造汉字名。
8. 只输出带原标记的译文，不附加说明、标题、代码围栏或质量报告。
9. 不得在中文句中遗留 potency、vibrant、haptic、flora、proliferations、enacting 等普通英文词。作品名、机构名、人物罗马字和必要的首次术语对照除外。
10. 若某段在语法上引出下一段展示引文，应把引导段译成自然完整的中文，并在需要时以中文冒号收束；不得留下突兀的半句话。
11. 英文引文中用于恢复首字母的编辑方括号（如 [r]ecollection、[w]hen）不要机械移入中文；直接译成完整中文词语。

原文：
{unit.source}
""".strip()
    system = "你是精通德勒兹哲学、动画研究与TYPE-MOON作品的资深学术译者和中文编辑。"
    return prompt, system


def parse_unit_output(unit: TranslationUnit, response: str) -> dict[str, str]:
    cleaned = normalize_target_script(
        clean_translation_text(response),
        "简体中文",
    ).strip()
    if "�" in cleaned:
        raise ValueError("Translation contains Unicode replacement characters.")
    matches = list(MARKER.finditer(cleaned))
    found = tuple(match.group(1) for match in matches)
    if found != unit.markers:
        raise ValueError(f"Marker mismatch: expected={unit.markers}, found={found}")
    prefix = cleaned[: matches[0].start()].strip() if matches else cleaned
    if prefix:
        raise ValueError(f"Unexpected text before first marker: {prefix[:100]!r}")
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(cleaned)
        value = cleaned[match.end() : end].strip()
        if not value:
            raise ValueError(f"Empty translated paragraph: {match.group(1)}")
        values[match.group(1)] = value
    source_refs = Counter(REFERENCE_TOKEN.findall(unit.source))
    output_refs = Counter(REFERENCE_TOKEN.findall(cleaned))
    if source_refs != output_refs:
        raise ValueError(f"Reference marker mismatch: {source_refs} != {output_refs}")
    source_chars = max(1, len(MARKER.sub("", unit.source)))
    output_chars = len(MARKER.sub("", cleaned))
    if output_chars / source_chars < 0.18:
        raise ValueError(
            f"Suspiciously short translation: {output_chars}/{source_chars} characters."
        )
    return values


def unit_cache_path(work_dir: Path, unit: TranslationUnit) -> Path:
    return work_dir / f"{unit.kind}_{unit.index:03d}.json"


def unit_digest(unit: TranslationUnit, profile: ModelProfile) -> str:
    payload = "\n".join(
        (
            REVIEW_PROMPT_VERSION,
            profile.provider,
            profile.adapter,
            profile.base_url.rstrip("/"),
            profile.model,
            profile.thinking,
            "简体中文",
            effective_glossary(unit),
            unit.kind,
            unit.source,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_cached_unit(
    path: Path, unit: TranslationUnit, profile: ModelProfile
) -> dict[str, str] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("source_sha256") != unit_digest(unit, profile):
        return None
    values = payload.get("translations")
    if not isinstance(values, dict) or tuple(values) != unit.markers:
        return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
        return None
    # A truncated or manually damaged cache must never bypass the same marker,
    # reference, Unicode, and minimum-length checks applied to live output.
    reconstructed = "\n\n".join(
        f"⟦{marker}⟧ {values[marker]}" for marker in unit.markers
    )
    try:
        return parse_unit_output(unit, reconstructed)
    except ValueError:
        return None


def translate_unit(
    client: DeepSeekClient,
    profile: ModelProfile,
    unit: TranslationUnit,
    cache_path: Path,
    *,
    force: bool,
) -> dict[str, str]:
    if not force:
        cached = load_cached_unit(cache_path, unit, profile)
        if cached is not None:
            return cached
    prompt, system = translation_prompt(unit)
    failures: list[str] = []
    for attempt in range(1, 4):
        try:
            response = client.chat_text(
                prompt,
                system=system,
                max_tokens=min(32768, max(4096, len(unit.source) * 4)),
            )
            values = parse_unit_output(unit, response)
            write_json(
                cache_path,
                {
                    "schema_version": 1,
                    "prompt_version": REVIEW_PROMPT_VERSION,
                    "provider": profile.provider,
                    "model": profile.model,
                    "source_sha256": unit_digest(unit, profile),
                    "kind": unit.kind,
                    "index": unit.index,
                    "markers": list(unit.markers),
                    "translations": values,
                },
            )
            return values
        except Exception as exc:  # semantic failures are safe to retry
            failures.append(f"attempt {attempt}: {exc}")
    raise RuntimeError(
        f"Translation unit {unit.kind}-{unit.index} failed validation: "
        + " | ".join(failures)
    )


def restore_references(text: str) -> str:
    return REFERENCE_TOKEN.sub(lambda match: f"〔{match.group(1)}〕", text)


def figure_placeholder(source: str) -> str:
    match = FIGURE.fullmatch(source)
    label = match.group(1).replace("-", "–") if match else source
    return f"> **图{label}**：原稿插图，详见带目录 PDF 参考版。"


def render_reviewed_chapter(
    title: str,
    blocks: list[SourceBlock],
    notes: "OrderedDict[int, Note]",
    translations: dict[str, str],
) -> str:
    output = [f"# {title}", ""]
    for item in blocks:
        if item.kind == "heading":
            output.extend([item.text, ""])
        elif item.kind == "figure":
            output.extend([figure_placeholder(item.text), ""])
        else:
            value = restore_references(translations[item.marker]).strip()
            if item.quote:
                value = "\n".join("> " + line if line.strip() else ">" for line in value.splitlines())
            output.extend([value, ""])
    output.extend(["---", "", "### 注释", ""])
    for number in notes:
        marker = f"N{number:04d}"
        output.extend([f"**{number}**　{restore_references(translations[marker]).strip()}", ""])
    return "\n".join(output).rstrip() + "\n"


def write_reconstruction_audit(
    path: Path,
    *,
    chapter_id: str,
    title: str,
    start: int,
    end: int,
    blocks: list[SourceBlock],
    notes: "OrderedDict[int, Note]",
    units: list[TranslationUnit],
    reviewed: str,
    profile: ModelProfile,
) -> None:
    heading_count = sum(item.kind == "heading" for item in blocks)
    body_count = sum(item.kind == "body" for item in blocks)
    figure_count = sum(item.kind == "figure" for item in blocks)
    expected_refs = {f"〔{number}〕" for number in notes}
    missing_refs = sorted(ref for ref in expected_refs if reviewed.count(ref) != 1)
    write_json(
        path,
        {
            "schema_version": 1,
            "chapter_id": chapter_id,
            "title": title,
            "pdf_range_internal_audit_only": [start, end],
            "provider": profile.provider,
            "model": profile.model,
            "prompt_version": REVIEW_PROMPT_VERSION,
            "source_pages": end - start + 1,
            "body_paragraphs": body_count,
            "headings": heading_count,
            "figures": figure_count,
            "footnotes": len(notes),
            "first_footnote": next(iter(notes), None),
            "last_footnote": next(reversed(notes), None) if notes else None,
            "translation_units": len(units),
            "missing_or_duplicate_reference_markers": missing_refs,
            "unicode_replacement_characters": reviewed.count("�"),
            "publication_page_markers": sum(
                reviewed.count(marker)
                for marker in ("source-pdf", "pdf-pages", "PDF_PAGE", "pagebreak")
            ),
        },
    )
    if missing_refs or "�" in reviewed:
        raise ValueError(f"Reviewed chapter audit failed: {path}")


def profile_client(config: Path, profile_name: str) -> tuple[ModelProfile, DeepSeekClient]:
    profiles = load_pipeline_profiles(config)
    profile = profiles.for_stage("translation", profile_name)
    if profile is None or profile.provider.lower() != "deepseek":
        raise ValueError("Reviewed regeneration currently requires a DeepSeek text profile.")
    secret = profile.resolve_credential().get_secret_value()
    return profile, DeepSeekClient(
        api_key=secret,
        api_base=profile.base_url,
        text_model=profile.model,
        timeout=profile.timeout,
        thinking=profile.thinking,
        adapter_name=profile.adapter,
    )


def parse_chapter_ids(raw_values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for raw in raw_values:
        for value in raw.split(","):
            chapter_id = value.strip()
            if chapter_id and chapter_id not in result:
                result.append(chapter_id)
    if not result:
        raise ValueError("At least one --chapter-id is required.")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Regenerate selected reviewed chapters from continuous page text."
    )
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument(
        "--source-pdf",
        type=Path,
        help=(
            "Original PDF used to recover paragraph indentation and displayed-quote "
            "boundaries that are absent from plain-text page checkpoints."
        ),
    )
    parser.add_argument("--chapter-id", action="append", required=True)
    parser.add_argument("--config", type=Path, default=Path("pipeline.example.toml"))
    parser.add_argument("--translation-profile", default="deepseek_flash")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--max-chars", type=int, default=9000)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.concurrency < 1 or args.max_chars < 1000:
        raise ValueError("--concurrency must be positive and --max-chars at least 1000.")
    output_dir = args.output_dir.expanduser().resolve()
    source_pdf = args.source_pdf.expanduser().resolve() if args.source_pdf else None
    if source_pdf is not None and not source_pdf.is_file():
        raise ValueError(f"Source PDF not found: {source_pdf}")
    load_env_file(Path(".env"))
    toc = json.loads((output_dir / "toc.json").read_text(encoding="utf-8"))
    entries = toc["entries"]
    page_files = sorted((output_dir / "pages").glob("page_*.json"))
    if not page_files:
        raise ValueError(f"No page checkpoints found in {output_dir / 'pages'}")
    last_page = max(int(path.stem.split("_")[-1]) for path in page_files)
    profile, client = profile_client(args.config, args.translation_profile)
    chapter_ids = parse_chapter_ids(args.chapter_id)

    requested_ranges: dict[str, tuple[dict[str, Any], int, int]] = {
        chapter_id: chapter_range(entries, chapter_id, last_page)
        for chapter_id in chapter_ids
    }
    layout_starts: dict[int, set[str]] = {}
    layout_quotes: dict[int, set[str]] = {}
    if source_pdf is not None:
        requested_pages = sorted(
            {
                page
                for _entry, start, end in requested_ranges.values()
                for page in range(start, end + 1)
            }
        )
        with fitz.open(source_pdf) as document:
            if document.page_count < last_page:
                raise ValueError(
                    f"Source PDF has {document.page_count} pages, but checkpoints reach {last_page}."
                )
            for page in requested_pages:
                starts, quotes = page_layout_hints(document, page)
                layout_starts[page] = starts
                layout_quotes[page] = quotes
        print(
            f"[layout] source={source_pdf} pages={len(requested_pages)} "
            f"paragraph-start-lines={sum(map(len, layout_starts.values()))} "
            f"displayed-quotes={sum(map(len, layout_quotes.values()))}"
        )

    chapter_data: dict[
        str,
        tuple[dict[str, Any], int, int, list[SourceBlock], OrderedDict[int, Note], list[TranslationUnit]],
    ] = {}
    all_jobs: list[tuple[str, TranslationUnit, Path]] = []
    for chapter_id in chapter_ids:
        entry, start, end = requested_ranges[chapter_id]
        blocks, notes = reconstruct_chapter(
            output_dir,
            chapter_id,
            entry,
            start,
            end,
            layout_starts if source_pdf is not None else None,
            layout_quotes if source_pdf is not None else None,
        )
        protect_references(blocks, notes)
        assign_markers(blocks, notes)
        units = make_units(blocks, notes, args.max_chars)
        work_dir = output_dir / "reviewed_work" / chapter_id
        work_dir.mkdir(parents=True, exist_ok=True)
        source_markdown = [f"# {display_title(entry)}", ""]
        for item in blocks:
            source_markdown.extend([item.text, ""])
        source_markdown.extend(["---", "", "### Notes", ""])
        for number, note in notes.items():
            source_markdown.extend([f"{number}. {note.text}", ""])
        (work_dir / "reconstructed_source.md").write_text(
            "\n".join(source_markdown).rstrip() + "\n", encoding="utf-8"
        )
        chapter_data[chapter_id] = (entry, start, end, blocks, notes, units)
        for unit in units:
            all_jobs.append((chapter_id, unit, unit_cache_path(work_dir, unit)))
        print(
            f"[reconstruct] chapter={chapter_id} pages={start}-{end} "
            f"body={sum(x.kind == 'body' for x in blocks)} notes={len(notes)} units={len(units)}"
        )

    results: dict[str, dict[str, str]] = {chapter_id: {} for chapter_id in chapter_ids}
    lock = threading.Lock()

    def run(job: tuple[str, TranslationUnit, Path]) -> tuple[str, TranslationUnit, dict[str, str]]:
        chapter_id, unit, cache_path = job
        values = translate_unit(
            client, profile, unit, cache_path, force=args.force
        )
        return chapter_id, unit, values

    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {executor.submit(run, job): job for job in all_jobs}
        completed = 0
        for future in as_completed(futures):
            chapter_id, unit, _cache = futures[future]
            try:
                result_chapter, result_unit, values = future.result()
                with lock:
                    results[result_chapter].update(values)
                completed += 1
                print(
                    f"[review-translate] chapter={result_chapter} "
                    f"unit={result_unit.kind}-{result_unit.index} "
                    f"completed={completed}/{len(all_jobs)}"
                )
            except Exception as exc:
                failures.append(f"{chapter_id}:{unit.kind}-{unit.index}: {exc}")
    if failures:
        raise RuntimeError("Reviewed translation failed:\n" + "\n".join(failures))

    reviewed_dir = output_dir / "reviewed_chapters"
    reviewed_dir.mkdir(parents=True, exist_ok=True)
    for chapter_id in chapter_ids:
        entry, start, end, blocks, notes, units = chapter_data[chapter_id]
        expected_markers = {
            item.marker for item in blocks if item.kind == "body"
        } | {f"N{number:04d}" for number in notes}
        if set(results[chapter_id]) != expected_markers:
            raise ValueError(
                f"{chapter_id} translated marker coverage mismatch: "
                f"missing={sorted(expected_markers - set(results[chapter_id]))}"
            )
        title = display_title(entry)
        reviewed = render_reviewed_chapter(
            title, blocks, notes, results[chapter_id]
        )
        target = reviewed_dir / f"{chapter_id}.md"
        write_reconstruction_audit(
            output_dir / "reviewed_work" / chapter_id / "audit.json",
            chapter_id=chapter_id,
            title=title,
            start=start,
            end=end,
            blocks=blocks,
            notes=notes,
            units=units,
            reviewed=reviewed,
            profile=profile,
        )
        # Validate the complete publication before replacing a previously
        # reviewed chapter.  A failed audit must leave the last good override
        # untouched.
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(reviewed, encoding="utf-8")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"[done] reviewed chapter={chapter_id} path={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
