"""Convert 恶之花/巴黎的忧郁 endnote references into Markdown footnotes.

Source shape per chapter (钱春绮译本 EPUB):
  body refs  ``<sup>[\\[N\\]](partNNNN.html#note_N)</sup>``
  defs       ``[\\[N\\]](partNNNN.html#noteBack_N) text``
  headings may carry a stray directory anchor ``<sup>[\\[*\\]]</sup>`` which
  is dropped (the ``#b-…`` target is an EPUB bookmark, not a note).

Reader-edition pruning follows the project default: definitions >= 150
characters are removed together with all their markers, and every decision
is recorded in ``audit/reader-edition-notes.json`` with refreshed digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(
    r"outputs/恶之花 巴黎的忧郁（法国象征派诗歌先驱波德莱尔扛鼎之作；"
    r"中国翻译工作者协会理事、“资深翻译家”荣誉称号得主钱春绮译本） "
    r"(外国文学名著... (z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

REF_MD = re.compile(r"<sup>\[\\*\[(\d{1,3})\\*\]\]\([^)]*#note_\d+\)</sup>")
STAR_ANCHOR = re.compile(r"<sup>\[\\*\[\*\\*\]\]\([^)]*\)</sup>")
DEF_LINE = re.compile(r"^\[\\*\[(\d{1,3})\\*\]\]\([^)]*#noteBack_\d+\)\s?(.*)$")


def _norm_title(value: str) -> str:
    value = re.sub(r"^\d+\s*", "", value)
    return re.sub(r"[\s，。,.：:（）()]", "", value)


def transform(text: str, *, chapter_title: str = "") -> tuple[str, dict]:
    lines = text.splitlines()
    defs: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []
    body_end = len(lines)
    for index, line in enumerate(lines):
        match = DEF_LINE.match(line)
        if match:
            if current is not None:
                defs[current] = " ".join(buf).strip()
            current = int(match.group(1))
            buf = [match.group(2).strip()]
            body_end = min(body_end, index)
        elif current is not None:
            if line.strip():
                buf.append(line.strip())
    if current is not None:
        defs[current] = " ".join(buf).strip()
    # Cross-references inside note definitions are plain text in a Word
    # footnote: drop the markdown link syntax and unescape the brackets.
    def clean_note(value: str) -> str:
        value = value.replace("\\[", "[").replace("\\]", "]")
        value = re.sub(
            r"\[([^\[\]]+)\]\(part\d+\.html#[^)]*\)", r"\1", value
        )
        value = re.sub(
            r"\[([^\[\]]+\[[^\[\]]+\])\]\(part\d+\.html#[^)]*\)", r"\1", value
        )
        return value

    defs = {label: clean_note(value) for label, value in defs.items()}
    body = "\n".join(lines[:body_end])
    body = re.sub(r"(?m)^---\s*$", "", body)
    body = STAR_ANCHOR.sub("", body)
    body = re.sub(r"\[([^\]]+)\]\(part\d+\.html#[^)]*\)", r"\1", body)

    kept: dict[int, str] = {}
    dropped_long: list[int] = []
    dropped_unanchored: list[int] = []
    dup_markers_removed: list[int] = []
    pieces: list[str] = []
    cursor = 0
    for match in REF_MD.finditer(body):
        label = int(match.group(1))
        note = defs.get(label)
        pieces.append(body[cursor:match.start()])
        cursor = match.end()
        if note is None:
            continue
        if len(note) >= LONG_NOTE_CHARS:
            if label not in dropped_long:
                dropped_long.append(label)
            continue
        if label not in kept:
            kept[label] = note
            pieces.append(f"[^{label}]")
            continue
        dup_markers_removed.append(label)
    pieces.append(body[cursor:])
    new_body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()

    # The poem's first line often repeats the chapter title; the Word
    # publisher's running-title cleanup drops it, so remove it at source and
    # move any footnote marker it carries onto the next verse line.
    if chapter_title:
        title_norm = _norm_title(chapter_title)
        body_lines = new_body.splitlines()
        for index, line in enumerate(body_lines):
            if line.lstrip().startswith("#"):
                continue
            markers = re.findall(r"\[\^\d+\]", line)
            stripped_norm = _norm_title(re.sub(r"\[\^\d+\]", "", line))
            matches_title = bool(
                stripped_norm
                and (
                    stripped_norm == title_norm
                    or (
                        len(stripped_norm) >= 6
                        and title_norm.endswith(stripped_norm)
                        and len(stripped_norm) >= len(title_norm) - 4
                    )
                )
            )
            if matches_title:
                nxt = index + 1
                while nxt < len(body_lines) and not body_lines[nxt].strip():
                    nxt += 1
                if markers and nxt < len(body_lines) and not body_lines[nxt].strip().startswith("#"):
                    body_lines[nxt] = body_lines[nxt].rstrip() + "".join(markers)
                body_lines[index] = ""
        new_body = re.sub(r"\n{3,}", "\n\n", "\n".join(body_lines)).strip()

    for label, note in defs.items():
        if label in kept:
            continue
        if len(note) >= LONG_NOTE_CHARS:
            if label not in dropped_long:
                dropped_long.append(label)
            continue
        dropped_unanchored.append(label)

    if kept:
        tail = "\n\n".join(f"[^{label}]: {kept[label]}" for label in sorted(kept))
        markdown = f"{new_body}\n\n{tail}\n"
    else:
        markdown = f"{new_body}\n"
    return markdown, {
        "defined": len(defs),
        "kept": len(kept),
        "dropped_long": dropped_long,
        "dropped_unanchored": dropped_unanchored,
        "dup_markers_removed": dup_markers_removed,
    }


def main() -> int:
    from publication_semantics import (
        markdown_footnote_contract_sha256,
        parse_markdown_footnotes,
    )

    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    audit = json.loads(
        (WORK / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8")
    )
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}
    report = []
    total: Counter = Counter()
    for item in manifest:
        path = CHAPTERS / item["filename"]
        text = path.read_text(encoding="utf-8")
        markdown, stats = transform(
            text,
            chapter_title=str(item.get("display_title") or ""),
        )
        inventory = parse_markdown_footnotes(markdown)
        violations = [
            name
            for name, values in (
                ("duplicate_definitions", inventory.duplicate_definitions),
                ("missing_definitions", inventory.missing_definitions),
                ("unused_definitions", inventory.unused_definitions),
                ("duplicate_references", inventory.duplicate_references),
            )
            if values
        ]
        if violations:
            print(f"{item['filename']}: CLOSED-SET VIOLATION {violations}", file=sys.stderr)
            return 1
        path.write_text(markdown, encoding="utf-8")
        entry = audit_by_id.get(item["id"])
        if entry is not None:
            entry["markdown_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            entry["footnote_contract_sha256"] = markdown_footnote_contract_sha256(markdown)
            entry["footnote_count"] = len(inventory.definitions)
        total["defined"] += stats["defined"]
        total["kept"] += stats["kept"]
        total["dropped_long"] += len(stats["dropped_long"])
        total["dropped_unanchored"] += len(stats["dropped_unanchored"])
        total["dup_markers"] += len(stats["dup_markers_removed"])
        report.append({"id": item["id"], "filename": item["filename"], **stats})
    audit["summary"]["footnote_count"] = sum(
        entry.get("footnote_count", 0) for entry in audit["chapters"]
    )
    (WORK / "audit" / "semantic-reconstruction.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (WORK / "audit" / "reader-edition-notes.json").write_text(
        json.dumps(
            {
                "policy": "reader-edition",
                "long_note_minimum_characters": LONG_NOTE_CHARS,
                "chapters": report,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print("totals:", dict(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
