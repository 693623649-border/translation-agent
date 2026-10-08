"""Final state of the corpus, the translated workspaces, and the global index."""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

workspaces = 0
rows = 0
for corpus in ROOT.glob("outputs/**/knowledge_base.jsonl"):
    workspaces += 1
    rows += sum(1 for line in corpus.read_text(encoding="utf-8-sig").splitlines() if line.strip())

translated = [
    backup
    for backup in ROOT.glob("outputs/**/knowledge_base.translation-source.jsonl")
    if backup.stat().st_size > 0
]
ready = 0
awaiting: list[str] = []
for backup in translated:
    manifest = backup.parent / "knowledge_base.rag.json"
    status = "no_manifest"
    if manifest.is_file():
        status = json.loads(manifest.read_text(encoding="utf-8"))["retrieval"]["embedding"].get("status")
    if status == "ready":
        ready += 1
    else:
        awaiting.append(f"{backup.parent.name} ({status})")

db_path = ROOT / "global_knowledge_base.sqlite3"
with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
    meta = dict(db.execute("SELECT key, value FROM meta"))
    reader = db.execute("SELECT count(*) FROM chunks WHERE kind='knowledge_base'").fetchone()[0]
    page = db.execute("SELECT count(*) FROM chunks WHERE kind='source_page'").fetchone()[0]
    shelf = db.execute("SELECT count(*) FROM workspaces").fetchone()[0]

print(f"  workspaces              : {workspaces}")
print(f"  reader rows (on disk)   : {rows}")
print(f"  workspaces translated   : {len(translated)}")
print(f"  ...rebuilt vectors      : {ready}")
for entry in awaiting:
    print(f"  ...STILL AWAITING       : {entry}")
print(f"  global index built_at   : {meta.get('built_at')}")
print(f"  global index workspaces : {shelf}")
print(f"  global index reader     : {reader}")
print(f"  global index pages      : {page}")
