"""Build reviewed reader-edition chapters for 镜与灯 (endnote-book repair).

The printed book uses chapter-endnotes.  Page-local legacy-note
reconstruction can never prove their anchors, and OCR dropped whole runs of
inline superscripts.  Per the project's reader-edition default this script:

1. keeps short (<= 150 chars) notes that have at least one inline ``[n]``
   anchor, as closed Markdown footnotes (``[^n]``), first anchor only;
2. removes long notes together with every one of their inline markers;
3. removes short notes whose anchors were destroyed by OCR (documented);
4. records every decision in ``reviewed_chapters/reader-edition-audit.json``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

BASE = Path(
    r"outputs/镜与灯：浪漫主义文论及批评传统（修订译本） ([美]艾布拉姆斯) "
    r"(z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = BASE / "chapters"
REVIEWED = BASE / "reviewed_chapters"
LONG_NOTE_CHARS = 150

DEF_LINE = re.compile(r"^\[(\d{1,3})\]\s*(.*)$")


def parse_notes(note_lines: list[str]) -> dict[int, str]:
    defs: dict[int, list[str]] = {}
    current: int | None = None
    for line in note_lines:
        match = DEF_LINE.match(line)
        if match:
            current = int(match.group(1))
            defs[current] = [match.group(2).strip()]
        elif current is not None and line.strip():
            defs[current].append(line.strip())
    return {label: " ".join(parts).strip() for label, parts in defs.items()}


def build_reviewed(chapter_id: str, filename: str) -> dict:
    source = (CHAPTERS / filename).read_text(encoding="utf-8")
    lines = source.splitlines()
    heading = lines[0]
    notes_idx = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"注\s*释", line.strip())),
        None,
    )
    if notes_idx is None:
        return {"chapter_id": chapter_id, "status": "no-notes-section"}

    body = "\n".join(lines[1:notes_idx]).strip()
    defs = parse_notes(lines[notes_idx + 1:])

    kept: dict[int, str] = {}
    dropped_long: list[int] = []
    dropped_unanchored: list[int] = []
    removed_duplicate_markers: list[int] = []
    seen_labels: set[int] = set()
    kept_pieces: list[str] = []
    cursor = 0
    for match in re.finditer(r"\[(\d{1,3})\]", body):
        position, end, label = match.start(), match.end(), int(match.group(1))
        text = defs.get(label)
        kept_pieces.append(body[cursor:position])
        cursor = end
        if label not in defs:
            kept_pieces.append(f"[{label}]")
            continue
        if len(text) >= LONG_NOTE_CHARS:
            if label not in dropped_long:
                dropped_long.append(label)
            continue
        if label not in kept:
            kept[label] = text
            seen_labels.add(label)
            kept_pieces.append(f"[^{label}]")
            continue
        removed_duplicate_markers.append(label)
    kept_pieces.append(body[cursor:])
    new_body = "".join(kept_pieces)

    for label in defs:
        text = defs[label]
        if label in kept:
            continue
        if len(text) >= LONG_NOTE_CHARS:
            if label not in dropped_long:
                dropped_long.append(label)
            continue
        dropped_unanchored.append(label)

    definitions = "\n\n".join(f"[^{label}]: {defs[label]}" for label in sorted(kept))
    markdown = (
        f"{heading}\n\n"
        + re.sub(r"\n{3,}", "\n\n", new_body).strip()
        + ("\n\n" + definitions if definitions else "")
        + "\n"
    )
    REVIEWED.mkdir(exist_ok=True)
    (REVIEWED / f"{chapter_id}.md").write_text(markdown, encoding="utf-8")
    return {
        "chapter_id": chapter_id,
        "filename": filename,
        "status": "reviewed",
        "defined_notes": len(defs),
        "kept_footnotes": len(kept),
        "dropped_long_notes": dropped_long,
        "dropped_unanchored_short_notes": dropped_unanchored,
        "duplicate_anchor_markers_removed": [
            label for label, count in Counter(removed_duplicate_markers).items()
        ],
    }


def main() -> None:
    manifest = json.loads((BASE / "chapters.json").read_text(encoding="utf-8"))
    report = [build_reviewed(item["id"], item["filename"]) for item in manifest]
    payload = {
        "policy": "reader-edition",
        "long_note_minimum_characters": LONG_NOTE_CHARS,
        "chapters": report,
    }
    (REVIEWED / "reader-edition-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    totals = Counter()
    for entry in report:
        if entry.get("status") != "reviewed":
            print(entry["chapter_id"], entry["status"])
            continue
        totals["defined"] += entry["defined_notes"]
        totals["kept"] += entry["kept_footnotes"]
        totals["dropped_long"] += len(entry["dropped_long_notes"])
        totals["dropped_unanchored"] += len(entry["dropped_unanchored_short_notes"])
        totals["dup_markers_removed"] += len(entry["duplicate_anchor_markers_removed"])
        print(
            f"{entry['chapter_id']} defined={entry['defined_notes']:3d} "
            f"kept={entry['kept_footnotes']:3d} "
            f"long={len(entry['dropped_long_notes']):3d} "
            f"unanchored={len(entry['dropped_unanchored_short_notes']):2d} "
            f"dup={len(entry['duplicate_anchor_markers_removed'])}"
        )
    print("totals:", dict(totals))


if __name__ == "__main__":
    main()
