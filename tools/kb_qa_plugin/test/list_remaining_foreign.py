"""List the rows the Chinese gate still flags, with enough context to route them.

Each entry is classified by whether the row is genuinely foreign prose or Chinese
text that merely retains a Japanese term — the distinction decides whether
translating it again is the right fix.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb_translation import classify_row  # noqa: E402

KANA = re.compile(r"[\u3040-\u30ff]")
HAN = re.compile(r"[\u3400-\u9fff]")


def main() -> int:
    buckets: dict[str, list[tuple[str, str]]] = {}
    for corpus in sorted((ROOT / "outputs").rglob("knowledge_base.jsonl")):
        for line in corpus.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            verdict = classify_row(str(row.get("title") or ""), str(row.get("content") or ""))
            if not verdict["needs_translation"]:
                continue
            content = str(row.get("content") or "")
            kana = len(KANA.findall(content))
            han = len(HAN.findall(content))
            ratio = kana / max(kana + han, 1)
            # >30% kana is foreign prose; a handful of kana inside Chinese is a
            # retained term, not an untranslated block.
            kind = "外文正文" if ratio > 0.30 else "中文夹日文术语"
            buckets.setdefault(kind, []).append((corpus.parent.name, content))

    for kind, entries in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"== {kind}: {len(entries)} 行 ==")
        by_workspace: dict[str, int] = {}
        for workspace, _ in entries:
            by_workspace[workspace] = by_workspace.get(workspace, 0) + 1
        for workspace, count in sorted(by_workspace.items(), key=lambda kv: -kv[1]):
            print(f"   {count:>3}  {workspace[:66]}")
        for workspace, content in entries[:2]:
            print(f"   例 · {workspace[:40]}: {content[:120]!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
