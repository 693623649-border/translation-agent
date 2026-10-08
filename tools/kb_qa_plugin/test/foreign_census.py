"""Census: which workspaces hold non-Chinese reader chunks, and how many.

Reads the per-workspace corpora directly through the same classifier the
release gate uses, so the numbers here are the numbers the gate will enforce.
"""

import json
import sys
from pathlib import Path

ROOT = Path(r"E:\Deeplearning\translation-agent")
sys.path.insert(0, str(ROOT))

from kb_translation import classify_row  # noqa: E402

rows_out = []
total_pending = 0
total_exempt = 0
for corpus in sorted((ROOT / "outputs").rglob("knowledge_base.jsonl")):
    pending = 0
    chunk_count = 0
    languages: dict[str, int] = {}
    exempt: dict[str, int] = {}
    for line in corpus.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        chunk_count += 1
        verdict = classify_row(str(row.get("title") or ""), str(row.get("content") or ""))
        if verdict["needs_translation"]:
            pending += 1
            languages[verdict["language"]] = languages.get(verdict["language"], 0) + 1
        else:
            exempt[verdict["reason"]] = exempt.get(verdict["reason"], 0) + 1
    if pending:
        rows_out.append((pending, chunk_count, corpus.parent.name, languages))
        total_pending += pending
        total_exempt += sum(exempt.values())

rows_out.sort(reverse=True)
print(f"工作区总数（含待译）：{len(rows_out)}")
print(f"待译块合计：{total_pending}")
print()
print(f"{'待译':>5} {'总块':>6}  {'语言':<22} 工作区")
for pending, chunk_count, name, languages in rows_out:
    lang = ",".join(f"{k}:{v}" for k, v in sorted(languages.items(), key=lambda kv: -kv[1]))
    print(f"{pending:>5} {chunk_count:>6}  {lang:<22} {name[:64]}")
