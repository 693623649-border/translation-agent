"""Convert 视差之见 page-bottom notes into closed Markdown footnotes.

The scanned PDF keeps its apparatus as page-bottom notes whose in-text
superscript markers were mostly lost by OCR (only ~69 survive as Unicode
superscripts across 640 pages).  The note *definitions*, however, are intact:
each physical page ends with a run of ``N text`` lines numbered downwards to 1.

This script finds that trailing run per page (descending consecutive labels,
skipping running heads, printed page numbers and bare-numeric noise), joins
each note's wrapped continuation lines, and rewrites the chapter draft: the
block is replaced by a ``[^n]`` marker at the page boundary where the note sat,
and the definitions are appended at the chapter end.  Because the original
in-text anchors are unrecoverable, the marker marks the page-break position of
the note rather than a guessed sentence; nothing is invented and no note text
is dropped.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WORK = Path(r"outputs/视差之见")
PAGES = WORK / "pages"
DRAFTS = WORK / ".pipeline_graph" / "chapter_drafts"

NOTE_START = re.compile(r"^(\d{1,3})\s*([A-Za-z\u4e00-\u9fff\"“《（(].{2,})$")
PURE_NUM = re.compile(r"^[\d\s.,，。、;；:：\-—]+$")
PAGE_NUM = re.compile(r"^\d{1,3}$")
RUNHEAD = re.compile(
    r"(视差之见|恒星视差|太阳视差|月球视差|辩证唯物主义兵临城下|主体，这个"
    r"|唯物主义神学|神圣狗屎|自由之回环|剩余价值|意识形态纽结|康德的选择"
    r"|社会链接|译者后记|难以承受|用以堆积)"
)


def page_note_block(lines: list[str]) -> list[tuple[int, int, str]] | None:
    """Return [(line_index, label, note_text)] for the page-bottom note run."""

    starts: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        match = NOTE_START.match(stripped)
        if not match:
            continue
        if PURE_NUM.match(stripped) or RUNHEAD.search(stripped):
            continue
        starts.append((index, int(match.group(1)), match.group(2)))
    if not starts:
        return None
    run = [starts[-1]]
    for previous in reversed(starts[:-1]):
        if previous[1] == run[0][1] - 1:
            run.insert(0, previous)
        else:
            break
    if run[0][1] != 1:
        return None

    # Attach wrapped continuation lines and stop before trailing furniture.
    notes: list[tuple[int, int, str]] = []
    for position, (index, label, text) in enumerate(run):
        end = run[position + 1][0] if position + 1 < len(run) else len(lines)
        parts = [text]
        for follow in range(index + 1, end):
            stripped = lines[follow].strip()
            if not stripped or PAGE_NUM.match(stripped):
                continue
            parts.append(stripped)
        notes.append((index, label, re.sub(r"\s+", " ", " ".join(parts)).strip()))
    return notes


def block_span(lines: list[str], notes: list[tuple[int, int, str]]) -> tuple[int, int]:
    """Verbatim [first, last] line indices covering the notes and continuations."""

    first = notes[0][0]
    # Last note's continuation lines: walk forward while lines are not page furniture.
    last = notes[-1][0]
    for index in range(notes[-1][0] + 1, len(lines)):
        stripped = lines[index].strip()
        if PAGE_NUM.match(stripped) or RUNHEAD.search(stripped):
            break
        last = index
    return first, last


def convert_chapter(
    draft_path: Path,
    manifest_item: dict,
    *,
    dry: bool,
    verbose: bool,
) -> tuple[str, int, int]:
    text = draft_path.read_text(encoding="utf-8")
    start_page = int(manifest_item["pdf_page"])
    end_page = int(manifest_item.get("end_pdf_page") or start_page)
    counter = 0
    definitions: list[str] = []
    converted = 0
    ambiguous = 0
    for page in range(start_page, end_page + 1):
        page_path = PAGES / f"page_{page:04d}.md"
        if not page_path.is_file():
            continue
        lines = page_path.read_text(encoding="utf-8").splitlines()
        notes = page_note_block(lines)
        if not notes:
            continue
        first, last = block_span(lines, notes)
        block_text = "\n".join(ln.rstrip() for ln in lines[first : last + 1])
        if text.count(block_text) != 1:
            ambiguous += 1
            if verbose:
                print(f"    page {page}: block not uniquely located (skipped)", file=sys.stderr)
            continue
        markers = []
        for _, _, note in notes:
            counter += 1
            markers.append(f"[^{counter}]")
            definitions.append(f"[^{counter}]: {note}")
        text = text.replace(block_text, "".join(markers), 1)
        converted += len(notes)
        if verbose:
            print(f"    page {page}: {len(notes)} note(s) -> {marker}")
    if definitions:
        # Pull page-boundary markers back onto the preceding text so the Word
        # renderer keeps them inline and the sentence split by the page break
        # rejoins instead of becoming a marker-only paragraph.
        text = re.sub(
            r"\n(?P<markers>(?:\[\^\d+\])+)\n+",
            lambda match: match.group("markers"),
            text,
        )
        # A marker immediately followed by ``(`` is read as a Markdown link
        # (``[text](url)``) and the marker gets swallowed; escaping the
        # parenthesis keeps both the marker and the literal glyph.
        text = re.sub(r"(\[\^\d+\])\(", r"\1\\(", text)
        # Bare ``*`` divider lines collapse into ``**`` once the DOCX builder
        # joins wrapped lines, which then opens an unclosed bold span.
        text = re.sub(r"(?m)^[ \t]*\*[ \t]*$\n?", "", text)
        # Footnote text reaches Word through ``markdown_inline_to_plain_text``,
        # which strips ``<...>`` as markup and would drop autolink URLs.
        definitions = [
            re.sub(
                r"<(https?://[^<>]+)>",
                lambda match: re.sub(r"\s+", "", match.group(1)),
                note,
            )
            for note in definitions
        ]
        text = text.rstrip() + "\n\n" + "\n\n".join(definitions) + "\n"
    if not dry and converted:
        draft_path.write_text(text, encoding="utf-8", newline="\n")
    return text, converted, ambiguous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--only", help="limit to one manifest filename")
    args = parser.parse_args(argv)

    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    total_notes = 0
    total_ambiguous = 0
    for item in manifest:
        if args.only and item["filename"] != args.only:
            continue
        draft = DRAFTS / item["filename"]
        if not draft.is_file():
            continue
        print(f"== {item['filename'][:42]}")
        _, converted, ambiguous = convert_chapter(
            draft, item, dry=args.dry, verbose=args.verbose
        )
        total_notes += converted
        total_ambiguous += ambiguous
        print(f"   notes converted: {converted} | unlocated blocks: {ambiguous}")
    print(f"TOTAL notes: {total_notes} | unlocated blocks: {total_ambiguous}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
