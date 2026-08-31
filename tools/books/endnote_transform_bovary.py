"""Convert 包法利夫人 chapter-end translator notes into true Markdown footnotes.

Source shape per chapter (namesake 名著名译 EPUB):
  body refs    ``[<sup>\\[N\\]</sup>](partNNNN.xhtml#mN)``
  chapter tail ``---`` then ``[\\[N\\]](partNNNN.xhtml#wN) text`` definitions

Reader-edition policy (project default): notes whose normalized text is at
least ``LONG_NOTE_CHARS`` characters are removed together with every one of
their body markers; short notes become closed ``[^N]`` Markdown footnotes
(first anchor only).  Every decision is recorded in the audit JSON, and the
semantic audit digests are refreshed so the verifier stays in sync.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(
    r"outputs/包法利夫人（名著名译丛书） (福楼拜) (z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

REF_MD = re.compile(r"\[<sup>\\?\[(\d{1,3})\\?\]</sup>\]\([^)]+\)")
DEF_LINE = re.compile(r"^\[\\?\[(\d{1,3})\\?\]\]\([^)]*\)\s?(.*)$")


def transform(text: str) -> tuple[str, dict]:
    lines = text.splitlines()
    body_end = len(lines)
    defs: dict[int, str] = {}
    current: int | None = None
    buf: list[str] = []
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
    body = "\n".join(lines[:body_end])
    body = re.sub(r"(?m)^---\s*$", "", body)

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

    for label, note in defs.items():
        if label in kept:
            continue
        if len(note) >= LONG_NOTE_CHARS:
            if label not in dropped_long:
                dropped_long.append(label)
            continue
        dropped_unanchored.append(label)

    if kept:
        definitions = "\n\n".join(f"[^{label}]: {kept[label]}" for label in sorted(kept))
        markdown = f"{new_body}\n\n{definitions}\n"
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
    total = Counter()
    for item in manifest:
        path = CHAPTERS / item["filename"]
        text = path.read_text(encoding="utf-8")
        if not REF_MD.search(text) and not DEF_LINE.search(text):
            total["untouched"] += 1
            continue
        markdown, stats = transform(text)
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
        total["defined"] += stats["defined"]
        total["kept"] += stats["kept"]
        total["dropped_long"] += len(stats["dropped_long"])
        total["dropped_unanchored"] += len(stats["dropped_unanchored"])
        total["dup_markers"] += len(stats["dup_markers_removed"])
        report.append({"id": item["id"], "filename": item["filename"], **stats})
        print(
            f"{item['filename'][:30]:32s} defined={stats['defined']:3d} "
            f"kept={stats['kept']:3d} long={len(stats['dropped_long']):3d} "
            f"unanchored={len(stats['dropped_unanchored'])}"
        )
    audit["summary"]["footnote_count"] = sum(
        entry.get("footnote_count", 0) for entry in audit["chapters"]
    )
    (WORK / "audit" / "semantic-reconstruction.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (WORK / "audit" / "reader-edition-notes.json").write_text(
        json.dumps(
            {"policy": "reader-edition", "long_note_minimum_characters": LONG_NOTE_CHARS,
             "chapters": report},
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print("totals:", dict(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
