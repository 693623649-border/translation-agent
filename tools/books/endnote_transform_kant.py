"""Convert the Kant set's flattened section footnotes into Markdown footnotes.

Imported shape per chapter (one spine part per chapter):
  body ref   ``<sup>[\\[N\\]](partNNNN.xhtml#dNeN)</sup>``   (dNeN = definition)
  defs       ``[\\[N\\]](partNNNN.xhtml#sdNeN) text``          (sdNeN = back-ref)
  margins    ``<sup>〔N〕</sup>``                              (original-edition
             marginal numerals — kept as plain text)

Labels repeat per section, so the anchor id (``dNeN``) is the footnote
identity.  Reader-edition pruning: definitions >= 150 chars are removed with
all their markers; referenceless definitions are dropped; everything is
recorded in ``audit/reader-edition-notes.json`` with refreshed digests.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(".tmp/kant-set/work")
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

REF_SUP = re.compile(
    r"<sup>\[\\?\[(\d{1,3})\\?\]\]\((part\d+\.xhtml)#(d1e\d+)\)</sup>"
)
MARGIN_SUP = re.compile(r"<sup>（?(〔\d{1,3}〕)）?</sup>")
MARGIN_SUP2 = re.compile(r"<sup>(〔\d{1,3}〕)</sup>")
DEF_LINE = re.compile(
    r"^\[\\?\[(\d{1,3})\\?\]\]\((part\d+\.xhtml)#s(d1e\d+)\)\s?(.*)$"
)


def transform(text: str) -> tuple[str, dict]:
    lines = text.splitlines()
    defs: dict[str, str] = {}
    keep_lines: list[str] = []
    drop_block = False
    for line in lines:
        match = DEF_LINE.match(line)
        if match:
            key = match.group(3)
            if key in defs:
                defs[key] += " " + match.group(4).strip()
            else:
                defs[key] = match.group(4).strip()
            drop_block = True
            continue
        if line.strip() == "---":
            continue
        drop_block = False
        keep_lines.append(line)
    text = "\n".join(keep_lines)

    kept: dict[str, str] = {}
    dropped_long: list[str] = []
    dropped_unanchored: list[str] = []
    unanchored_refs = 0

    def replace_ref(match: re.Match) -> str:
        nonlocal unanchored_refs
        key = match.group(3)
        note = defs.get(key)
        if note is None:
            unanchored_refs += 1
            return f"[{match.group(1)}]"
        if len(note) >= LONG_NOTE_CHARS:
            if key not in dropped_long:
                dropped_long.append(key)
            return ""
        kept[key] = note
        return f"[^{key}]"

    text = REF_SUP.sub(replace_ref, text)
    text = MARGIN_SUP2.sub(r"\1", text)

    for key, note in defs.items():
        if key in kept:
            continue
        if len(note) >= LONG_NOTE_CHARS:
            dropped_long.append(key)
        else:
            dropped_unanchored.append(key)

    if kept:
        tail = "\n\n".join(f"[^{key}]: {kept[key]}" for key in sorted(kept))
        text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n\n" + tail + "\n"
    else:
        text = re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
    return text, {
        "defined": len(defs),
        "kept": len(kept),
        "dropped_long": dropped_long,
        "dropped_unanchored": dropped_unanchored,
        "unanchored_refs": unanchored_refs,
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
    totals: Counter = Counter()
    report = []
    for item in manifest:
        path = CHAPTERS / item["filename"]
        text = path.read_text(encoding="utf-8")
        if not REF_SUP.search(text) and not DEF_LINE.search(text):
            continue
        markdown, stats = transform(text)
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
            print(
                f"{item['filename']}: CLOSED-SET VIOLATION {violations}",
                file=sys.stderr,
            )
            return 1
        path.write_text(markdown, encoding="utf-8")
        entry = audit_by_id.get(item["id"])
        if entry is not None:
            entry["markdown_sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            entry["footnote_contract_sha256"] = (
                markdown_footnote_contract_sha256(markdown)
            )
            entry["footnote_count"] = len(inventory.definitions)
        totals["defined"] += stats["defined"]
        totals["kept"] += stats["kept"]
        totals["dropped_long"] += len(stats["dropped_long"])
        totals["dropped_unanchored"] += len(stats["dropped_unanchored"])
        totals["unanchored_refs"] += stats["unanchored_refs"]
        report.append({"filename": item["filename"], **stats})
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
        )
        + "\n",
        encoding="utf-8",
    )
    print("totals:", dict(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
