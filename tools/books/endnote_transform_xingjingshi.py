"""Convert 性经验史 (佘碧平 译) per-file endnote apparatus into Markdown footnotes.

This z-library EPUB (sigil/duokan 风格) puts body markers as
``[<sup>(N)</sup>](partXXXX.xhtml#jz_xx_yyyy)`` inside the chapter markdown and
leaves the note definitions (``<p class="fnContent-1">`` blocks keyed by the
same anchors) behind in the EPUB XHTML — the semantic importer does not carry
them over.  This script harvests the definitions from the source EPUB,
rewrites short notes (<150 chars) into closed ``[^N]`` footnotes, drops long
notes together with their markers (reader-edition policy), removes footnote
markup embedded in H1 lines, disambiguates repeated display titles by volume
position, drops the printed 目 录 chapter, and refreshes the semantic audit
digests so the Word verification gate stays in sync.
"""

from __future__ import annotations

import hashlib
import html.parser
import json
import re
import sys
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

WORK = Path(
    r"outputs/性经验史 = Histoire de la Sexualité ([法] 米歇尔 · 福柯 (Michel Foucault) 著  佘碧平 译) (z-library.sk, 1lib.sk, z-lib.sk)"
)
EPUB = Path(
    r"book/福柯/性经验史 = Histoire de la Sexualité ([法] 米歇尔 · 福柯 (Michel Foucault) 著  佘碧平 译) (z-library.sk, 1lib.sk, z-lib.sk).epub"
)
CHAPTERS = WORK / "chapters"
LONG_NOTE_CHARS = 150

REF_MD = re.compile(
    r"\[<sup>\((\d{1,3})\)</sup>\]\(([^)#]+\.xhtml)#([A-Za-z0-9_]+)\)"
)
# Original duokan def blocks: ``[(N)](partXXXX.xhtml#id)　note text`` lines the
# importer keeps as body text; they duplicate the footnotes added below.
DEF_BACKLINK = re.compile(r"^\[\(\d{1,3}\)\]\([^)]*\)")
FN_P = re.compile(
    r'<p[^>]*class="fnContent[^"]*"[^>]*>\s*<a[^>]*id="([A-Za-z0-9_]+)"[^>]*>.*?</a>(.*?)</p>',
    re.DOTALL,
)
TAG = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    text = html.unescape(TAG.sub("", text))
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \u3000")


def load_defs(epub: Path, files: set[str]) -> dict[str, dict[str, str]]:
    """Map xhtml file -> {anchor id -> note text} for fnContent blocks."""
    found: dict[str, dict[str, str]] = {}
    with zipfile.ZipFile(epub) as zf:
        for member in zf.namelist():
            if not member.lower().endswith(".xhtml"):
                continue
            basename = member.rsplit("/", 1)[-1]
            if basename not in files:
                continue
            raw = zf.read(member).decode("utf-8", "replace")
            notes: dict[str, str] = {}
            for anchor, payload in FN_P.findall(raw):
                text = _strip_tags(payload)
                text = re.sub(r"^\(\d{1,3}\)\s*", "", text)
                if text:
                    notes[anchor] = text
            found[basename] = notes
    return found


def transform(text: str, defs: dict[str, str]) -> tuple[str, dict]:
    lines = text.splitlines()
    heading_marker_drops: list[str] = []
    pieces: list[str] = []
    cursor = 0
    kept: dict[str, str] = {}
    dropped_long: list[str] = []
    dropped_missing: list[str] = []
    for match in REF_MD.finditer(text):
        label, file, anchor = match.groups()
        line_start = text.rfind("\n", 0, match.start()) + 1
        # Heading-line markers: strip them from the title (title notes are
        # dropped by design; the H1 must stay identical to display_title).
        prefix = text[line_start : match.start()]
        if prefix.startswith("#"):
            pieces.append(text[cursor : match.start()])
            cursor = match.end()
            heading_marker_drops.append(anchor)
            continue
        pieces.append(text[cursor : match.start()])
        cursor = match.end()
        note = defs.get(anchor)
        if note is None:
            dropped_missing.append(anchor)
            continue
        if len(note) >= LONG_NOTE_CHARS:
            dropped_long.append(anchor)
            continue
        if label not in kept:
            kept[label] = note
            pieces.append(f"[^{label}]")
    pieces.append(text[cursor:])
    new_body = re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()

    if kept:
        definitions = "\n\n".join(f"[^{label}]: {note}" for label, note in kept.items())
        markdown = f"{new_body}\n\n{definitions}\n"
    else:
        markdown = f"{new_body}\n"
    return markdown, {
        "heading_marker_drops": heading_marker_drops,
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
    audit = json.loads(
        (WORK / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8")
    )
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}

    files_needed = {m.group(2) for item in manifest for m in REF_MD.finditer((CHAPTERS / item["filename"]).read_text(encoding="utf-8"))}
    defs_by_file = load_defs(EPUB, files_needed)
    all_defs = {anchor: note for notes in defs_by_file.values() for anchor, note in notes.items()}

    report: list[dict] = []
    total: Counter[str] = Counter()
    for item in manifest:
        path = CHAPTERS / item["filename"]
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        refs = list(REF_MD.finditer(text))
        if refs:
            markdown, stats = transform(text, all_defs)
        else:
            markdown, stats = text, {
                "heading_marker_drops": [],
                "kept": [],
                "dropped_long": [],
                "dropped_missing": [],
            }
        # Strip the importer-left original def blocks (duokan backlink lines).
        leftover = [
            line for line in markdown.splitlines() if DEF_BACKLINK.match(line.strip())
        ]
        if leftover:
            markdown = re.sub(
                r"(?m)^\[\(\d{1,3}\)\]\([^)]*\).*\n?", "", markdown
            )
            markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"
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
        for key in ("kept", "dropped_long", "dropped_missing", "heading_marker_drops"):
            total[key] += len(stats[key])
        if leftover or refs:
            report.append(
                {"id": item["id"], "filename": item["filename"],
                 "stripped_backlink_defs": len(leftover), **stats}
            )
        print(
            f"{item['filename'][:34]:36s} refs={len(refs):4d} kept={len(stats['kept']):4d} "
            f"long={len(stats['dropped_long']):4d} stripped={len(leftover):4d}"
        )

    # Disambiguate repeated display titles by volume position, clean the two
    # H1s polluted by footnote markup, and drop the printed 目 录 chapter
    # (index-style page that trips the render heuristics).
    volume_renames = {
        "epub-0006": "译者前言",
        "epub-0021": "结论（第二卷）",
        "epub-0030": "结论（第三卷）",
        "epub-0022": "引文索引（第二卷）",
        "epub-0031": "引文索引（第三卷）",
    }
    dropped: list[str] = []
    kept_items: list[dict] = []
    for item in manifest:
        new_title = volume_renames.get(item["id"])
        if new_title and item.get("display_title") != new_title:
            path = CHAPTERS / item["filename"]
            if path.is_file():
                source = path.read_text(encoding="utf-8")
                source = re.sub(r"(?m)^# .*$", f"# {new_title}", source, count=1)
                path.write_text(source, encoding="utf-8")
            item["title"] = new_title
            item["display_title"] = new_title
            entry = audit_by_id.get(item["id"])
            if entry is not None:
                entry["markdown_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        kept_items.append(item)
    audit["chapters"] = [
        entry for entry in audit["chapters"]
        if entry.get("chapter_id") not in dropped
    ]
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
    print("totals:", dict(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
