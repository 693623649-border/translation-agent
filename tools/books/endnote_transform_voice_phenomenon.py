"""Convert 声音与现象 (汉译世界学术名著/商务 2010, 杜小真 译) chapter-end
translator notes into closed Markdown footnotes.

Body markers keep the ``[<sup>(N)</sup>](file.html#chN)`` shape while the
definitions survived import as trailing backlink lines
``[(N)](file.html#chN-back)　note text`` inside the same chapter markdown
(the duokan/sigil variant where the importer kept the def block).  This script
pairs markers and definitions by their printed number N per chapter, turns
short notes (<150 chars) into ``[^N]`` footnotes, drops long notes with their
markers (reader-edition policy), removes the original def lines, cleans image
cover chapters and title-page noise, strips the 目 录 chapter links, renames
the epigraph chapter, and refreshes the semantic audit digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(
    r"outputs/声音与现象 (汉译世界学术名著丛书) (雅克·德里达) (z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

REF_SUP = re.compile(r"\[<sup>\((\d{1,3})\)</sup>\]\([^)]+\)")
DEF_LINE = re.compile(r"^\[\((\d{1,3})\)\]\([^)]*\)[ \u3000]*(.*)$")
IMG_ONLY = re.compile(r"^#\s*!\[[^\]]*\]\([^)]*\)\s*!?\[?")
TOC_LINK = re.compile(r"^\[([^\]]+)\]\([^)]*\)\s*$")


def extract_defs(text: str) -> tuple[list[str], dict[int, str]]:
    """Split body from the trailing def block; return (body_lines, defs)."""

    lines = text.splitlines()
    defs: dict[int, str] = {}
    body_end = len(lines)
    pending: int | None = None
    buf: list[str] = []
    for index, line in enumerate(lines):
        match = DEF_LINE.match(line)
        if match:
            if pending is not None:
                defs[pending] = " ".join(buf).strip(" \u3000")
            pending = int(match.group(1))
            buf = [match.group(2).strip(" \u3000")]
            body_end = min(body_end, index)
        elif pending is not None:
            if line.strip():
                buf.append(line.strip(" \u3000"))
            else:
                defs[pending] = " ".join(buf).strip(" \u3000")
                pending = None
                buf = []
    if pending is not None:
        defs[pending] = " ".join(buf).strip(" \u3000")
    return lines[:body_end], defs


def transform(text: str) -> tuple[str, dict]:
    body_lines, defs = extract_defs(text)
    body = "\n".join(body_lines)
    kept: dict[int, str] = {}
    dropped_long: list[int] = []
    dropped_missing: list[int] = []
    pieces: list[str] = []
    cursor = 0
    for match in REF_SUP.finditer(body):
        label = int(match.group(1))
        line_start = body.rfind("\n", 0, match.start()) + 1
        if body[line_start : match.start()].startswith("#"):
            pieces.append(body[cursor : match.start()])
            cursor = match.end()
            continue
        pieces.append(body[cursor : match.start()])
        cursor = match.end()
        note = defs.get(label)
        if note is None:
            dropped_missing.append(label)
            continue
        if len(note) >= LONG_NOTE_CHARS:
            dropped_long.append(label)
            continue
        if label not in kept:
            kept[label] = note
            pieces.append(f"[^{label}]")
    pieces.append(body[cursor:])
    new_body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()
    if kept:
        definitions = "\n\n".join(
            f"[^{label}]: {kept[label]}" for label in sorted(kept)
        )
        markdown = f"{new_body}\n\n{definitions}\n"
    else:
        markdown = f"{new_body}\n"
    return markdown, {
        "defined": len(defs),
        "kept": sorted(kept),
        "dropped_long": dropped_long,
        "dropped_missing": dropped_missing,
        "heading_marker_drops": len(
            [m for m in REF_SUP.finditer(body) if body[body.rfind('\n', 0, m.start()) + 1 : m.start()].startswith('#')]
        ),
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

    renames = {
        "epub-0006": "题词",
        "epub-0004": "原版书名页",
    }
    dropped: list[str] = []
    kept_items: list[dict] = []
    total: Counter[str] = Counter()
    for item in manifest:
        path = CHAPTERS / item["filename"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if IMG_ONLY.match(text):
            dropped.append(item["id"])
            path.unlink()
            total["image_chapters_dropped"] += 1
            continue
        new_title = renames.get(item["id"])
        if new_title:
            text = re.sub(r"(?m)^# .*$", f"# {new_title}", text, count=1)
            item["display_title"] = new_title
            item["title"] = new_title
        elif item["display_title"] == "目 录":
            lines = []
            for line in text.splitlines():
                match = TOC_LINK.match(line.strip())
                if match:
                    lines.append(match.group(1).strip())
                else:
                    lines.append(line)
            text = "\n".join(lines) + "\n"
        refs = list(REF_SUP.finditer(text)) or [
            m for m in DEF_LINE.finditer(text)
        ]
        if refs:
            markdown, stats = transform(text)
        else:
            markdown, stats = text, {}
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
        path.write_text(markdown, encoding="utf-8")
        entry = audit_by_id.get(item["id"])
        if entry is not None:
            entry["markdown_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            entry["footnote_contract_sha256"] = markdown_footnote_contract_sha256(markdown)
            entry["footnote_count"] = len(inventory.definitions)
        for key in ("kept", "dropped_long", "dropped_missing"):
            total[key] += len(stats.get(key, []) or [])
        kept_items.append(item)
        print(
            f"{item['filename'][:36]:38s} refs={len(refs):3d} kept={len(stats.get('kept', [])):3d} "
            f"long={len(stats.get('dropped_long', [])):3d}"
        )

    kept_ids = {item["id"] for item in kept_items}
    audit["chapters"] = [
        entry for entry in audit["chapters"] if entry.get("chapter_id") in kept_ids
    ]
    audit["summary"]["chapter_count"] = len(audit["chapters"])
    audit["summary"]["footnote_count"] = sum(
        entry.get("footnote_count", 0) for entry in audit["chapters"]
    )
    (WORK / "chapters.json").write_text(
        json.dumps(kept_items, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (WORK / "audit" / "semantic-reconstruction.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("totals:", dict(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
