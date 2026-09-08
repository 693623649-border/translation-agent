"""Convert 马克思的幽灵 (何一 译, 人大社 2016) chapter-end notes into closed
Markdown footnotes.

The scanned PDF keeps numbered references ``[N]`` inline in the body text and
collects the definitions (``[N] text`` lines) on the final pages of each
chapter, so the page-level semantic audit can never pair them.  This script
reassembles each chapter from its physical OCR pages: per page, everything
from the first leading ``[N]`` line to the page end is treated as the notes
block (running heads and bare page numbers are discarded), the rest is body.
Body references (including OCR-mangled ones missing the closing bracket)
become ``[^N]`` markers; short notes (<150 chars) become closed ``[^N]``
definitions, long notes are dropped with their markers (reader-edition
policy), and the chapter file is rewritten so the semantic node can validate
the one-to-one Markdown footnote contract.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(r"outputs/马克思的幽灵")
PAGES = WORK / "pages"
DRAFTS = WORK / ".pipeline_graph" / "chapter_drafts"
LONG_NOTE_CHARS = 150

DEF_REF = re.compile(r"^\[\d{1,3}(?:\])?")
DEF_REF_PARSE = re.compile(r"^\[(\d{1,3})(?:\])?[ \t\u3000]*(.*)$")
BODY_REF = re.compile(r"\[(\d{1,3})(?:\])?(?=[^\d])")
BARE_PAGE = re.compile(r"^\d{1,3}$")
RUNNING_HEAD = {"马克思的幽灵", "Spectres de Marx"}


def split_page(lines: list[str]) -> tuple[list[str], list[str]]:
    """Return (body lines, note lines) for one physical page."""

    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and lines[0].strip() in RUNNING_HEAD:
        lines.pop(0)
    if lines and lines[0].strip() in RUNNING_HEAD:
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    first_lead = next(
        (i for i, ln in enumerate(lines) if DEF_REF.match(ln.strip())), None
    )
    if first_lead is None:
        return lines, []
    return lines[:first_lead], lines[first_lead:]


def parse_notes(note_lines: list[str]) -> dict[int, str]:
    """Turn note-region lines into {label: text}."""

    defs: dict[int, list[str]] = {}
    current: int | None = None
    for ln in note_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if BARE_PAGE.match(stripped) or stripped in RUNNING_HEAD:
            continue
        match = DEF_REF_PARSE.match(stripped)
        if match and (match.group(2) or True):
            current = int(match.group(1))
            defs[current] = [match.group(2).strip(" \u3000")]
            continue
        if current is not None:
            defs[current].append(stripped)
    notes: dict[int, str] = {}
    for label, parts in defs.items():
        text = re.sub(r"\s+", " ", " ".join(parts)).strip(" \u3000")
        if text:
            notes[label] = text
    return notes


def assemble_chapter(item: dict) -> tuple[str, dict[int, str]]:
    start = int(item["pdf_page"])
    end = int(item.get("end_pdf_page") or start)
    body_lines: list[str] = []
    note_lines: list[str] = []
    for page in range(start, end + 1):
        path = PAGES / f"page_{page:04d}.md"
        if not path.is_file():
            continue
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        body, notes = split_page(raw_lines)
        body_lines.extend(body)
        note_lines.extend(notes)
    body = "\n".join(body_lines)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body, parse_notes(note_lines)


def transform(text: str, notes: dict[int, str]) -> tuple[str, dict]:
    pieces: list[str] = []
    cursor = 0
    kept: dict[int, str] = {}
    dropped_long: list[int] = []
    dropped_missing: list[int] = []
    for match in BODY_REF.finditer(text):
        label = int(match.group(1))
        line_start = text.rfind("\n", 0, match.start()) + 1
        if text[line_start : match.start()].startswith("#"):
            pieces.append(text[cursor : match.start()])
            cursor = match.end()
            continue
        pieces.append(text[cursor : match.start()])
        cursor = match.end()
        note = notes.get(label)
        if note is None:
            dropped_missing.append(label)
            continue
        if len(note) >= LONG_NOTE_CHARS:
            dropped_long.append(label)
            continue
        if label not in kept:
            kept[label] = note
            pieces.append(f"[^{label}]")
    pieces.append(text[cursor:])
    new_body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    if kept:
        definitions = "\n\n".join(f"[^{k}]: {kept[k]}" for k in sorted(kept))
        markdown = f"{new_body}\n\n{definitions}\n"
    else:
        markdown = f"{new_body}\n"
    return markdown, {
        "defined": len(notes),
        "kept": sorted(kept),
        "dropped_long": dropped_long,
        "dropped_missing": dropped_missing,
    }


def main() -> int:
    from publication_semantics import (
        markdown_footnote_contract_sha256,
        parse_markdown_footnotes,
    )

    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    audit_path = WORK / "audit" / "semantic-reconstruction.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}

    total: Counter[str] = Counter()
    for item in manifest:
        title = str(item.get("display_title") or item.get("title") or "")
        body, notes = assemble_chapter(item)
        if not body.strip():
            print(f"{item['filename']}: EMPTY BODY", file=sys.stderr)
            return 1
        markdown, stats = transform(body, notes)
        inventory = parse_markdown_footnotes(markdown)
        problems = [
            name
            for name, values in (
                ("duplicate_definitions", inventory.duplicate_definitions),
                ("missing_definitions", inventory.missing_definitions),
                ("unused_definitions", inventory.unused_definitions),
                ("duplicate_references", inventory.duplicate_references),
            )
            if values
        ]
        if problems:
            print(f"{item['filename']}: CLOSED-SET VIOLATION {problems}", file=sys.stderr)
            return 1
        path = DRAFTS / item["filename"]
        path.write_text(f"# {title}\n\n{markdown}", encoding="utf-8", newline="\n")
        entry = audit_by_id.get(item["id"])
        if entry is not None:
            entry["markdown_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            entry["footnote_contract_sha256"] = markdown_footnote_contract_sha256(
                markdown
            )
            entry["footnote_count"] = len(inventory.definitions)
        for key in ("kept", "dropped_long", "dropped_missing"):
            total[key] += len(stats[key])
        print(
            f"{item['filename'][:34]:36s} body={len(markdown):6d} "
            f"defined={stats['defined']:3d} kept={len(stats['kept']):3d} "
            f"long={len(stats['dropped_long']):3d} missing={len(stats['dropped_missing']):3d}"
        )
    audit["summary"]["footnote_count"] = sum(
        entry.get("footnote_count", 0) for entry in audit["chapters"]
    )
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("totals:", dict(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
