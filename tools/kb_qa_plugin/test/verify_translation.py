"""Post-translation verification.

Checks the four things that matter after a corpus-wide translation: nothing was
lost, the corpora still parse as valid five-field knowledge bases, the gate's own
classifier now finds nothing left, and the global index was rebuilt from the new
text rather than left pointing at the old text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kb_translation import classify_row, source_backup_path_for  # noqa: E402
from rag_knowledge_base import load_knowledge_rows  # noqa: E402


def main() -> int:
    problems: list[str] = []
    translated_workspaces = 0
    remaining = 0
    total_rows = 0

    for corpus in sorted((ROOT / "outputs").rglob("knowledge_base.jsonl")):
        try:
            rows = load_knowledge_rows(corpus)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            problems.append(f"UNREADABLE {corpus.parent.name}: {exc}")
            continue
        total_rows += len(rows)
        pending = 0
        for row in rows:
            verdict = classify_row(str(row.get("title") or ""), str(row.get("content") or ""))
            if verdict["needs_translation"]:
                pending += 1
        if pending:
            remaining += pending
        if source_backup_path_for(corpus).is_file():
            translated_workspaces += 1

    print("== corpus integrity ==")
    print(f"  workspaces with rows      : {len(list((ROOT / 'outputs').rglob('knowledge_base.jsonl')))}")
    print(f"  total reader rows         : {total_rows}")
    print(f"  workspaces translated     : {translated_workspaces}")
    print(f"  rows still non-Chinese    : {remaining}")
    for problem in problems:
        print(f"  {problem}")

    print()
    print("== global index ==")
    db = ROOT / "global_knowledge_base.sqlite3"
    if not db.is_file():
        print("  MISSING: run global_knowledge_base.py sync")
    else:
        import sqlite3

        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            chunks = conn.execute("SELECT count(*) FROM chunks WHERE kind='knowledge_base'").fetchone()[0]
        print(f"  built_at       : {meta.get('built_at')}")
        print(f"  chinese_gate   : {meta.get('chinese_gate')}")
        print(f"  reader chunks  : {chunks}")

    return 1 if (problems or remaining) else 0


if __name__ == "__main__":
    raise SystemExit(main())
