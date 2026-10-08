"""Judge the quality of the freshly translated rows.

Translation can only be as good as the source it was given. The Japanese
workspaces were built from OCR scans, and a source block that was already
fragmentary (scan noise, column-split lines, ellipsis-joined scraps) yields a
fragmentary translation — the pipeline faithfully rendered damage. This reports
that distinction instead of assuming every row came out clean.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u3400-\u9fff]")
ELLIPSIS = re.compile(r"[…‥]|\.\.\.")


def main() -> int:
    rows_checked = 0
    damaged: list[tuple[str, str, str]] = []
    for corpus in sorted((ROOT / "outputs").rglob("knowledge_base.jsonl")):
        backup = corpus.with_suffix(".translation-source.jsonl")
        if not backup.is_file():
            continue
        originals = {
            json.loads(line)["id"]: json.loads(line)
            for line in backup.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        rows = {
            json.loads(line)["id"]: json.loads(line)
            for line in corpus.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        }
        for row_id, original in originals.items():
            row = rows.get(row_id)
            if row is None:
                continue
            rows_checked += 1
            source = str(original.get("content") or "")
            target = str(row.get("content") or "")
            # A source already full of ellipsis-joined scraps is damaged input,
            # not a damaged translation.
            source_ratio = len(ELLIPSIS.findall(source)) / max(len(source), 1) * 100
            target_ratio = len(ELLIPSIS.findall(target)) / max(len(target), 1) * 100
            kana = len(KANA.findall(target))
            han = len(HAN.findall(target))
            if source_ratio > 1.5 or (target_ratio > 1.5 and kana > han * 0.1):
                damaged.append((corpus.parent.name, source[:90], target[:90]))

    print(f"translated rows checked : {rows_checked}")
    print(f"fragmentary rows        : {len(damaged)}")
    if damaged:
        by_workspace: dict[str, int] = {}
        for workspace, _, _ in damaged:
            by_workspace[workspace] = by_workspace.get(workspace, 0) + 1
        for workspace, count in sorted(by_workspace.items(), key=lambda kv: -kv[1]):
            print(f"   {count:>4}  {workspace[:64]}")
        print()
        print("例（源已碎裂 → 译文随之碎裂）：")
        for workspace, source, target in damaged[:3]:
            print(f"  · {workspace[:34]}")
            print(f"    源: {source!r}")
            print(f"    译: {target!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
