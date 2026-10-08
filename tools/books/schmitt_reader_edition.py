"""Reader-edition pass for the two Schmitt DOCX workspaces.

Two defects in the published Word files, both fixed at the chapter-markdown
source and then rebuilt through the framework:

1. Incomplete TOC — the compiler folded only level-1 headings into chapters,
   so the navigation pane shows the four treatises but none of their internal
   sections.  Every level-2 TOC entry that owns a pdf_page is injected as an
   ``##`` heading at its title line inside the owning chapter.

2. Overweight footnotes — 53+25 of the OCR-recovered footnote definitions are
   150+ characters of editorial apparatus that fill half of each printed page
   (the reader-edition long-footnote policy threshold).  Long definitions and
   their in-text reference markers are removed; short notes stay as real Word
   footnotes with continuous automatic numbering.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

WS_ROOT = Path("outputs")
LONG_NOTE_CHARS = 150
DEF_RE = re.compile(r"(?m)^\[\^([^\]]+)\]:\s*(.+?)(?=^\[\^|\Z)", re.S)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def locate_section(title: str, body: str) -> tuple[int, int, int] | None:
    """Find where a section heading sits inside a chapter markdown.

    The OCR text renders headings in several shapes: a bare title line, a
    numbered heading ("一、国家的和政治的"), a two-line title plus year
    ("重版序" / "[1963]"), or a title glued to preceding prose ("1963年3月
    一、国家的和政治的").  Returns (line_index, slice_start, slice_end) where
    the slice marks the heading text to lift out of its line.
    """

    candidates = [normalize(title)]
    for pattern in (r"（[^）]*）", r"\[[^\]]*\]", r"〔[^〕]*〕"):
        stripped = re.sub(pattern, "", title)
        candidate = normalize(stripped)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    lines = body.splitlines()
    for candidate in candidates:
        if len(candidate) < 2:
            continue
        numbered = re.compile(r"[一二三四五六七八九十]{1,2}、" + re.escape(candidate))
        for index, line in enumerate(lines):
            stripped_line = normalize(line)
            if not stripped_line or stripped_line.startswith("#"):
                continue
            if stripped_line == candidate or stripped_line.startswith(candidate):
                return index, 0, len(line)
            numbered_match = numbered.search(line)
            if numbered_match:
                return index, numbered_match.start(), len(line)
            plain = line.find(candidate)
            if plain >= 0 and len(line) <= len(candidate) + 6:
                return index, plain, plain + len(candidate)
    return None


def inject_heading(body: str, title: str) -> tuple[str, bool]:
    """Insert an ``##`` heading at the section boundary; return (body, ok)."""

    found = locate_section(title, body)
    if found is None:
        return body, False
    index, start, end = found
    lines = body.splitlines()
    line = lines[index]
    prefix, heading, suffix = line[:start], line[start:end], line[end:]
    replacement = []
    if prefix.strip():
        replacement.append(prefix)
    replacement.append(f"## {heading.strip()}")
    if suffix.strip():
        replacement.append(suffix)
    lines[index : index + 1] = replacement
    return "\n".join(lines), True


def prune_long_footnotes(body: str) -> tuple[str, int]:
    """Remove 150+ char footnote definitions together with their references."""

    defs = {match.group(1): match.group(2) for match in DEF_RE.finditer(body)}
    long_ids = {
        note_id
        for note_id, text in defs.items()
        if len(re.sub(r"\s+", "", text)) >= LONG_NOTE_CHARS
    }
    if not long_ids:
        return body, 0
    body = DEF_RE.sub(
        lambda match: "" if match.group(1) in long_ids else match.group(0),
        body,
    )
    for note_id in long_ids:
        body = body.replace(f"[^{note_id}]", "")
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body, len(long_ids)


def main() -> int:
    report: dict[str, dict] = {}
    for workspace in ("政治的概念", "政治的神学"):
        root = WS_ROOT / workspace
        toc = json.loads((root / "toc.json").read_text(encoding="utf-8"))
        entries = toc["entries"] if isinstance(toc, dict) else toc
        manifest = json.loads((root / "chapters.json").read_text(encoding="utf-8"))
        items = manifest if isinstance(manifest, list) else manifest["chapters"]

        stats = {"sections_injected": 0, "sections_missed": [], "long_notes_pruned": 0}
        for entry in entries:
            if entry["level"] != 2 or not entry.get("pdf_page"):
                continue
            owner = next(
                (
                    item
                    for item in items
                    if item["pdf_page"] <= entry["pdf_page"] <= item.get("end_pdf_page", 0)
                ),
                None,
            )
            if owner is None:
                stats["sections_missed"].append(entry["title"])
                continue
            path = root / "chapters" / str(owner["filename"])
            body = path.read_text(encoding="utf-8")
            body, injected = inject_heading(body, entry["title"])
            if not injected:
                stats["sections_missed"].append(entry["title"])
                continue
            stats["sections_injected"] += 1
            path.write_text(body, encoding="utf-8", newline="\n")

        for path in sorted((root / "chapters").glob("*.md")):
            body = path.read_text(encoding="utf-8")
            body, pruned = prune_long_footnotes(body)
            stats["long_notes_pruned"] += pruned
            path.write_text(body, encoding="utf-8", newline="\n")
        report[workspace] = stats
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
