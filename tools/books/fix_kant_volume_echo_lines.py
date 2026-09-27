"""Source-level repairs for the ten split Kant volumes.

1. Title-page echo: a body line whose normalized text equals the chapter
   display_title duplicates the chapter H1 (title-page layout).  The Word
   publisher's running-title heuristic removes it, so remove it from the
   Markdown at source instead (project precedent from 文学理论).
2. Duplicated identical H1 lines: keep the first.
3. Content-free scaffold chapters (body empty behind the H1): drop the item,
   its file, and its audit entry.
4. Heading lines polluted with flattened inline note links
   (``<sup>(N)</sup>[...](partNNNN.xhtml#chN)``): strip the markup so the
   Markdown H1 matches what Word renders.
5. Refresh every audit digest afterwards.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from book_pipeline import normalize_match_text
from publication_semantics import (
    markdown_footnote_contract_sha256,
    parse_markdown_footnotes,
)

OUT_ROOT = Path("outputs")
DROP_EMPTY_TITLES = {"目录", "封面", "版权", "版权页"}
INLINE_NOTE_JUNK = re.compile(
    r"(<sup>\(\d{1,3}\)</sup>)?\[(<sup>\(\d{1,3}\)</sup>)?[^\]]*\]\(part\d+\.xhtml#[^)]*\)"
)


def clean_inline_junk(text: str) -> str:
    text = INLINE_NOTE_JUNK.sub("", text)
    text = re.sub(r"<sup>.*?</sup>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_heading(line: str) -> str:
    if not line.startswith("#"):
        return line
    level, _, text = line.partition(" ")
    text = INLINE_NOTE_JUNK.sub("", text)
    text = re.sub(r"<sup>.*?</sup>", "", text)
    return f"{level} {text.strip()}".rstrip()


def fix_volume(volume_dir: Path) -> dict:
    stats: Counter = Counter()
    manifest = json.loads((volume_dir / "chapters.json").read_text(encoding="utf-8"))
    audit = json.loads(
        (volume_dir / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8")
    )
    kept_manifest: list[dict] = []
    kept_ids: set[str] = set()
    for item in manifest:
        path = volume_dir / "chapters" / item["filename"]
        lines = path.read_text(encoding="utf-8").splitlines()
        title_norm = normalize_match_text(str(item.get("display_title") or ""))

        cleaned: list[str] = []
        seen_h1: list[str] = []
        for position, line in enumerate(lines):
            if line.startswith("# ") or re.match(r"^#{1,6} ", line):
                line = clean_heading(line)
            if re.match(r"^# ", line):
                normalized_heading = normalize_match_text(line[2:])
                if normalized_heading and normalized_heading in seen_h1:
                    stats["duplicate_h1_removed"] += 1
                    continue
                seen_h1.append(normalized_heading)
            body_position = position - 2  # skip H1 + blank line
            if (
                0 <= body_position <= 8
                and line.strip()
                and not line.startswith("#")
                and title_norm
                and normalize_match_text(line) == title_norm
            ):
                stats["title_echo_removed"] += 1
                continue
            cleaned.append(line)
        text = "\n".join(cleaned).strip() + "\n"

        inventory = parse_markdown_footnotes(text)
        body_chars = len(re.sub(r"\s+", "", inventory.body))
        title_in_drop = normalize_match_text(str(item.get("display_title") or "")) in {
            normalize_match_text(name) for name in DROP_EMPTY_TITLES
        }
        if body_chars == 0 and (title_in_drop or len(kept_manifest) > 0):
            stats["scaffold_dropped"] += 1
            path.unlink(missing_ok=True)
            continue

        path.write_text(text, encoding="utf-8")
        kept_manifest.append({**item, "sequence": len(kept_manifest) + 1})
        kept_ids.add(item["id"])

    for position, item in enumerate(kept_manifest, start=1):
        item["sequence"] = position
        clean = clean_inline_junk(str(item.get("display_title") or ""))
        if clean != item.get("display_title"):
            stats["display_title_cleaned"] += 1
            item["display_title"] = clean
            item["title"] = clean
    kept_ids = {item["id"] for item in kept_manifest}
    audit["chapters"] = [
        entry for entry in audit["chapters"] if entry["chapter_id"] in kept_ids
    ]
    audit["summary"]["chapter_count"] = len(audit["chapters"])
    audit["summary"]["footnote_count"] = 0
    for item in kept_manifest:
        path = volume_dir / "chapters" / item["filename"]
        text = path.read_text(encoding="utf-8")
        inventory = parse_markdown_footnotes(text)
        for entry in audit["chapters"]:
            if entry["chapter_id"] == item["id"]:
                entry["markdown_sha256"] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                entry["footnote_contract_sha256"] = (
                    markdown_footnote_contract_sha256(text)
                )
                entry["footnote_count"] = len(inventory.definitions)
                audit["summary"]["footnote_count"] += len(inventory.definitions)

    (volume_dir / "chapters.json").write_text(
        json.dumps(kept_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (volume_dir / "audit" / "semantic-reconstruction.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return stats


def main() -> None:
    grand: Counter = Counter()
    for volume_dir in sorted(OUT_ROOT.glob("* (康德) (汉译世界学术名著丛书)")):
        stats = fix_volume(volume_dir)
        grand.update(stats)
        if stats:
            print(volume_dir.name.split(" (康德)")[0][:20], dict(stats))
    print("grand totals:", dict(grand))


if __name__ == "__main__":
    main()
