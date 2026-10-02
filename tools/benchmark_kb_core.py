"""Compare a saved implementation/index with the current local KB core.

No model requests are made. Corpus files are read only; JSON reports are the
only output. Use a task-start copy of the modules and database as --baseline.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.util
import json
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import global_knowledge_base as current_global
import rag_knowledge_base as current_rag


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _measure(call, repetitions: int):
    samples = []
    for _ in range(repetitions):
        started = time.perf_counter()
        result = call()
        samples.append((time.perf_counter() - started) * 1000)
    return result, round(statistics.median(samples), 3)


def _hits_key(hits):
    return [(hit["id"], hit["score"]) for hit in hits]


def run(args):
    baseline = args.baseline.resolve()
    old_global = _load_module(baseline / "global_knowledge_base.py", "benchmark_old_global")
    old_rag = _load_module(baseline / "rag_knowledge_base.py", "benchmark_old_rag")
    fixture = json.loads(args.cases.read_text(encoding="utf-8"))
    report = {"scope": "local corpus; lexical queries only; no provider calls",
              "baseline": str(baseline), "database": str(args.db.resolve()),
              "repetitions": args.repetitions, "rag": [], "global_queries": []}
    files = sorted(args.outputs.glob("*/knowledge_base.jsonl"),
                   key=lambda path: path.stat().st_size, reverse=True)[:args.books]
    for path in files:
        records = {}
        for name, module in (("before", old_rag), ("after", current_rag)):
            gc.collect()
            started = time.perf_counter()
            corpus = module.RagKnowledgeBase.open(path)
            opened = (time.perf_counter() - started) * 1000
            queries = []
            signatures = []
            for query in ("社会 个人 文学", "自然", "精神胜利"):
                # Warm corpus statistics before timing repeated retrieval.
                corpus.retrieve(query, mode="lexical", top_k=5, auto_route=False)
                hits, elapsed = _measure(lambda: corpus.retrieve(
                    query, mode="lexical", top_k=5, auto_route=False), args.repetitions)
                signatures.append([(hit.id, hit.score) for hit in hits])
                queries.append({"query": query, "median_ms": elapsed, "hits": len(hits)})
            records[name] = {"open_ms": round(opened, 3), "queries": queries,
                             "signatures": signatures, "rows": len(corpus._rows)}
            del corpus
        equal = records["before"].pop("signatures") == records["after"].pop("signatures")
        report["rag"].append({"book": path.parent.name, "same_ids_and_scores": equal, **records})

    comparisons = []
    for case in fixture["cases"]:
        settings = dict(workspace=case.get("workspace_filter"), limit=fixture["top_k"])
        before = old_global.search(case["query"], db_path=baseline / "baseline.sqlite3", **settings)
        after = current_global.search(case["query"], db_path=args.db, **settings)
        comparisons.append({"id": case["id"], "same_ids_and_scores": _hits_key(before) == _hits_key(after)})
    report["reviewed_query_comparison"] = comparisons
    for query in ("无器官身体", "资本主义", "自然", "自", "自然 正式"):
        before, old_ms = _measure(lambda: old_global.search(
            query, db_path=baseline / "baseline.sqlite3", limit=10), args.repetitions)
        after, new_ms = _measure(lambda: current_global.search(
            query, db_path=args.db, limit=10), args.repetitions)
        report["global_queries"].append({"query": query, "before_ms": old_ms,
                                         "after_ms": new_ms, "hits": len(after),
                                         "same_ids_and_scores": _hits_key(before) == _hits_key(after)})
    if not all(item["same_ids_and_scores"] for item in report["rag"] + comparisons + report["global_queries"]):
        report["equivalent"] = False
    else:
        report["equivalent"] = True
    report["evaluation"] = current_global.evaluate_retrieval(args.cases, db_path=args.db)
    unchanged, elapsed = _measure(lambda: current_global.sync_outputs(args.outputs, args.db), 1)
    report["unchanged_sync"] = {"elapsed_ms": elapsed, **unchanged}
    hashes_path = baseline / "corpus-hashes.json"
    if hashes_path.is_file():
        expected = json.loads(hashes_path.read_text(encoding="utf-8"))
        changed = []
        for relative, digest in expected.items():
            path = PROJECT_ROOT / relative
            actual = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    actual.update(block)
            if actual.hexdigest() != digest:
                changed.append(relative)
        report["corpus_preserved"] = {"files": len(expected), "changed": changed}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--outputs", type=Path, default=PROJECT_ROOT / "outputs")
    parser.add_argument("--db", type=Path, default=current_global.DEFAULT_DB)
    parser.add_argument("--cases", type=Path,
                        default=PROJECT_ROOT / "tests/fixtures/global_kb_retrieval_cases.local.json")
    parser.add_argument("--report", type=Path,
                        default=PROJECT_ROOT / "work/kb-core-optimization/benchmark.json")
    parser.add_argument("--books", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.books < 1 or args.repetitions < 1:
        parser.error("books and repetitions must be positive")
    report = run(args)
    print(json.dumps({"report": str(args.report.resolve()), "equivalent": report["equivalent"],
                      "quality_passed": report["evaluation"]["passed"],
                      "corpus_preserved": report.get("corpus_preserved")}, ensure_ascii=True), flush=True)
    preserved = not report.get("corpus_preserved", {}).get("changed")
    return 0 if report["equivalent"] and report["evaluation"]["passed"] and preserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
