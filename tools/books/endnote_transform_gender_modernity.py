"""Merge 现代性的性别 spine files into logical chapters with true footnotes.

The EPUB splits every chapter across several spine files: section text files
carry ``<sup>(N)</sup>`` note references, and the section file that closes a
chapter also carries that chapter's endnote definitions
(``[(N)](partNNNN.html#chN-back)\\xa0text``).  One manifest item per spine
file cannot express cross-file footnotes, so this script rewrites the import
output to one manifest item per logical chapter:

- concatenates each group's files, demoting the later files' ``H1`` to ``H2``
  and dropping their duplicated anchor headings;
- converts first-occurrence ``(N)`` references into ``[^N]`` and the matching
  definitions into ``[^N]:`` Markdown footnotes (reader-edition pruning:
  definitions >= 150 chars are removed together with all their markers,
  unanchored short definitions are dropped);
- drops the content-free 目录/cover scaffolds the docx builder cannot use;
- rebuilds ``chapters.json`` and refreshes the semantic audit digests, and
  records every pruning decision in ``audit/reader-edition-notes.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

WORK = Path(
    r"outputs/现代性的性别 = The Gender of Modernity ( etc.) "
    r"(z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

# logical chapter -> spine-file basenames in order (from chapters.json ids)
GROUPS = [
    ("front", ["epub-0001"]),  # titlepage — dropped later (cover scaffold)
    ("front", ["epub-0002"]),  # 书名页
    ("front", ["epub-0003"]),  # 版权页
    ("front", ["epub-0004"]),  # 目录 — empty, dropped later
    ("front", ["epub-0005"]),  # 致谢
    ("导论 现代的神话", ["epub-0006"]),
    ("第一章 现代性和女性主义", ["epub-0007", "epub-0008", "epub-0009", "epub-0010"]),
    ("第二章 论怀旧：史前女人", ["epub-0011", "epub-0012", "epub-0013", "epub-0014", "epub-0015"]),
    ("第三章 想象的快感：消费的情色和审美", ["epub-0016", "epub-0017", "epub-0018", "epub-0019", "epub-0020", "epub-0021"]),
    ("第四章 面具下的男性气概：女性化创作", ["epub-0022", "epub-0023", "epub-0024", "epub-0025", "epub-0026", "epub-0027"]),
    ("第五章 爱情、上帝和东方：解读大众化的崇高", ["epub-0028", "epub-0029", "epub-0030", "epub-0031", "epub-0032", "epub-0033"]),
    ("第六章 新视野：关于进化和革命的女性主义话语", ["epub-0034", "epub-0035", "epub-0036", "epub-0037", "epub-0038"]),
    ("第七章 性变态的艺术：女性受虐狂和男性赛博格", ["epub-0039", "epub-0040", "epub-0041", "epub-0042", "epub-0043"]),
    ("后记 重写现代", ["epub-0044"]),
    ("译名对照表", ["epub-0045"]),
]
DROP_FRONT_IDS = {"epub-0001", "epub-0004"}

REF_SUP = re.compile(r"<sup>\((\d{1,3})\)</sup>")
DEF_LINE = re.compile(r"^\[\((\d{1,3})\)\]\([^)]*\)\s?(.*)$")
DUPLICATE_H2 = re.compile(r"^## (.+)$")


def slugify(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", text).strip("_")[:60]


def merge_group(files: list[Path]) -> str:
    parts: list[str] = []
    for index, path in enumerate(files):
        lines = path.read_text(encoding="utf-8").splitlines()
        if index:
            lines = [
                ("## " + line[2:].lstrip() if line.startswith("# ") else line)
                for line in lines
            ]
        # drop an H2 that only repeats the (possibly demoted) H1 next to it
        cleaned: list[str] = []
        skip_next_blank = False
        for position, line in enumerate(lines):
            if skip_next_blank:
                if not line.strip():
                    continue
                skip_next_blank = False
            match = DUPLICATE_H2.match(line)
            if match:
                previous_heading = next(
                    (entry for entry in reversed(cleaned) if entry.startswith("# ")),
                    "",
                )
                if previous_heading.endswith(match.group(1)):
                    skip_next_blank = True
                    continue
            cleaned.append(line)
        parts.append("\n".join(cleaned).strip())
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(parts)).strip() + "\n"


def transform(text: str) -> tuple[str, dict]:
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
            buf = [match.group(2).strip().replace("\xa0", " ")]
            body_end = min(body_end, index)
        elif current is not None:
            if line.strip():
                buf.append(line.strip().replace("\xa0", " "))
    if current is not None:
        defs[current] = " ".join(buf).strip()
    body = "\n".join(lines[:body_end])
    body = re.sub(r"(?m)^---\s*$", "", body)

    kept: dict[int, str] = {}
    dropped_long: list[int] = []
    dropped_unanchored: list[int] = []
    dup_markers: list[int] = []
    pieces: list[str] = []
    cursor = 0
    for match in REF_SUP.finditer(body):
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
        dup_markers.append(label)
    pieces.append(body[cursor:])
    new_body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()

    for label, note in defs.items():
        if label in kept or len(note) >= LONG_NOTE_CHARS:
            continue
        dropped_unanchored.append(label)

    if kept:
        tail = "\n\n".join(f"[^{label}]: {kept[label]}" for label in sorted(kept))
        return f"{new_body}\n\n{tail}\n", {
            "defined": len(defs),
            "kept": len(kept),
            "dropped_long": dropped_long,
            "dropped_unanchored": dropped_unanchored,
            "dup_markers_removed": dup_markers,
        }
    return f"{new_body}\n", {
        "defined": len(defs),
        "kept": 0,
        "dropped_long": dropped_long,
        "dropped_unanchored": dropped_unanchored,
        "dup_markers_removed": dup_markers,
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
    by_id = {item["id"]: item for item in manifest}
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}

    new_manifest: list[dict] = []
    new_audit: list[dict] = []
    report: list[dict] = []
    totals: Counter = Counter()
    sequence = 0
    for title, ids in GROUPS:
        files = [CHAPTERS / by_id[item_id]["filename"] for item_id in ids]
        if title == "front":
            for item_id in ids:
                if item_id in DROP_FRONT_IDS:
                    continue
                source = by_id[item_id]
                sequence += 1
                new_manifest.append({**source, "sequence": sequence})
                new_audit.append(audit_by_id[item_id])
            continue

        merged = merge_group(files)
        markdown, stats = transform(merged)
        lead = by_id[ids[0]]
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
            print(f"{title}: CLOSED-SET VIOLATION {violations}", file=sys.stderr)
            return 1
        sequence += 1
        filename = f"{sequence:03d}_{slugify(title)}.md"
        (CHAPTERS / filename).write_text(markdown, encoding="utf-8")
        new_item = {
            **lead,
            "id": lead["id"],
            "sequence": sequence,
            "title": title,
            "display_title": title,
            "filename": filename,
        }
        new_manifest.append(new_item)
        entry = dict(audit_by_id[ids[0]])
        entry["chapter_id"] = lead["id"]
        entry["filename"] = filename
        entry["markdown_sha256"] = hashlib.sha256(
            (CHAPTERS / filename).read_bytes()
        ).hexdigest()
        entry["footnote_contract_sha256"] = markdown_footnote_contract_sha256(markdown)
        entry["footnote_count"] = len(inventory.definitions)
        new_audit.append(entry)
        for old_id in ids[1:]:
            old_file = CHAPTERS / by_id[old_id]["filename"]
            if old_file.exists():
                old_file.unlink()
        totals["defined"] += stats["defined"]
        totals["kept"] += stats["kept"]
        totals["dropped_long"] += len(stats["dropped_long"])
        totals["dropped_unanchored"] += len(stats["dropped_unanchored"])
        totals["dup_markers"] += len(stats["dup_markers_removed"])
        report.append({"chapter": title, "merged_ids": ids, **stats})
        print(
            f"{title[:24]:26s} files={len(ids)} defined={stats['defined']:3d} "
            f"kept={stats['kept']:3d} long={len(stats['dropped_long']):3d} "
            f"unanchored={len(stats['dropped_unanchored'])}"
        )

    (WORK / "chapters.json").write_text(
        json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    audit["chapters"] = new_audit
    audit["summary"]["chapter_count"] = len(new_audit)
    audit["summary"]["footnote_count"] = sum(
        entry.get("footnote_count", 0) for entry in new_audit
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
    print("new chapter count:", len(new_manifest), "| totals:", dict(totals))
    return 0


if __name__ == "__main__":
    sys.exit(main())
