"""Split the imported Kant set work dir into ten per-volume deliverable dirs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

WORK = Path(".tmp/kant-set/work")
CHAPTERS = WORK / "chapters"
OUT_ROOT = Path("outputs")
EPUB = Path(
    r"book/康德著作集（套装10册）（汉译世界学术名著丛书） (康德) "
    r"(z-library.sk, 1lib.sk, z-lib.sk).epub"
)

VOLUMES = [
    (1, 12, "纯粹理性批判"),
    (13, 26, "实践理性批判"),
    (27, 36, "论优美感和崇高感"),
    (37, 55, "法的形而上学原理——权利科学"),
    (56, 66, "判断力批判（上）"),
    (67, 75, "判断力批判（下）"),
    (76, 87, "道德形上学探本"),
    (88, 100, "任何一种能够作为科学出现的未来形而上学导论"),
    (101, 115, "历史理性批判文集"),
    (116, 130, "逻辑学讲义"),
]
DROP_TITLES = {"封面", "版权", "版权页"}
ASSET_RE = re.compile(r"!\[[^\]]*\]\(assets/([^)]+)\)")


def part_no(item: dict) -> int:
    match = re.search(r"part(\d{4})", str(item.get("source_href", "")))
    return int(match.group(1)) if match else -1


def main() -> int:
    manifest = json.loads((WORK / "chapters.json").read_text(encoding="utf-8"))
    audit = json.loads(
        (WORK / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8")
    )
    audit_by_id = {entry["chapter_id"]: entry for entry in audit["chapters"]}
    zipped = zipfile.ZipFile(EPUB)
    image_by_hash = {
        hashlib.sha256(zipped.read(name)).hexdigest()[:12]: zipped.read(name)
        for name in zipped.namelist()
        if "/Images/" in name or "/media/" in name
    }

    for start, end, title in VOLUMES:
        volume_dir = OUT_ROOT / f"{title} (康德) (汉译世界学术名著丛书)"
        (volume_dir / "chapters").mkdir(parents=True, exist_ok=True)
        subset = [
            item
            for item in manifest
            if start <= part_no(item) <= end
            and str(item.get("display_title")) not in DROP_TITLES
        ]
        for position, item in enumerate(subset, start=1):
            item["sequence"] = position
        kept_hrefs = {str(item.get("source_href")) for item in subset}

        needed_images: set[str] = set()
        for item in subset:
            source = shutil.copyfile(
                CHAPTERS / item["filename"],
                volume_dir / "chapters" / item["filename"],
            )
            text = Path(source).read_text(encoding="utf-8")
            needed_images.update(ASSET_RE.findall(text))
        if needed_images:
            assets = volume_dir / "chapters" / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            source_assets = CHAPTERS / "assets"
            for hashed in sorted(needed_images):
                target = assets / hashed
                if target.is_file():
                    continue
                origin = source_assets / hashed
                if origin.is_file():
                    shutil.copyfile(origin, target)
                    continue
                key = hashed.rsplit(".", 1)[0].split("-")[-1]
                blob = image_by_hash.get(key)
                if blob is None:
                    print(f"{title}: UNRESOLVED image {hashed}")
                    continue
                target.write_bytes(blob)

        (volume_dir / "chapters.json").write_text(
            json.dumps(subset, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        volume_audit = {
            **audit,
            "chapters": [
                entry
                for entry in audit["chapters"]
                if str(entry.get("source_href")) in kept_hrefs
            ],
            "summary": {
                **audit["summary"],
                "chapter_count": len(subset),
                "footnote_count": sum(
                    entry.get("footnote_count", 0)
                    for entry in audit["chapters"]
                    if str(entry.get("source_href")) in kept_hrefs
                ),
            },
        }
        (volume_dir / "audit").mkdir(exist_ok=True)
        (volume_dir / "audit" / "semantic-reconstruction.json").write_text(
            json.dumps(volume_audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        notes = WORK / "audit" / "reader-edition-notes.json"
        if notes.is_file():
            shutil.copyfile(notes, volume_dir / "audit" / "reader-edition-notes.json")
        print(f"{title}: {len(subset)} chapters, {len(needed_images)} images")

    return 0


if __name__ == "__main__":
    main()
