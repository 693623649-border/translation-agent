"""Reproducible retrieval evaluation without translation/runtime dependencies."""
from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def _normalize(text: str) -> str:
    """NFKC + casefold + whitespace removal; punctuation remains significant."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def validate_evaluation_cases(cases: Sequence[Mapping]) -> list[dict]:
    result, seen = [], set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise ValueError(f"case {index}: expected an object")
        item = dict(case)
        for field in ("id", "query"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ValueError(f"case {index}: {field} must be non-empty text")
        if item["id"] in seen:
            raise ValueError(f"duplicate case id: {item['id']}")
        seen.add(item["id"])
        for field in ("relevant_ids", "relevant_books", "evidence_substrings"):
            values = item.get(field, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not value.strip() for value in values
            ):
                raise ValueError(f"case {item['id']}: {field} must be a list of non-empty strings")
            item[field] = list(dict.fromkeys(values))
        if not isinstance(item.get("unanswerable", False), bool):
            raise ValueError(f"case {item['id']}: unanswerable must be boolean")
        item.setdefault("unanswerable", False)
        if item["unanswerable"] and any(item[key] for key in ("relevant_ids", "relevant_books", "evidence_substrings")):
            raise ValueError(f"case {item['id']}: unanswerable cannot have positive labels")
        item.setdefault("category", "uncategorized")
        if not isinstance(item["category"], str) or not item["category"].strip():
            raise ValueError(f"case {item['id']}: category must be non-empty text")
        result.append(item)
    return result


def load_evaluation_cases(path: Path | str) -> list[dict]:
    """Load UTF-8 JSONL annotations and reject malformed/duplicate cases."""
    cases = []
    with Path(path).open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON") from exc
    return validate_evaluation_cases(cases)


def _identities(kb: Any, hit: Any) -> set[str]:
    identities = {str(hit.id)}
    metadata = kb.chunk_metadata(str(hit.id))
    for key in ("parent_id", "source_id", "source_chunk_id", "canonical_id"):
        if metadata.get(key):
            identities.add(str(metadata[key]))
    return identities


def _matches(kb: Any, hit: Any, case: Mapping) -> set[str]:
    if case["relevant_ids"]:
        return _identities(kb, hit) & set(case["relevant_ids"])
    content = _normalize(hit.content)
    return {value for value in case["evidence_substrings"] if _normalize(value) in content}


def _ranking_metrics(kb: Any, hits: Sequence, case: Mapping) -> dict:
    expected = set(case["relevant_ids"] or case["evidence_substrings"])
    if not expected or case["unanswerable"]:
        return {"recall": None, "hit": None, "mrr": None, "ndcg": None}
    found, gains = set(), []
    for hit in hits:
        matches = _matches(kb, hit, case)
        gains.append(int(bool(matches - found)))
        found.update(matches)
    reciprocal = next((1 / rank for rank, gain in enumerate(gains, 1) if gain), 0.0)
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(expected), len(hits)) + 1))
    return {"recall": len(found) / len(expected), "hit": float(bool(found)),
            "mrr": reciprocal, "ndcg": dcg / ideal if ideal else 0.0}


def _mean(rows: Sequence[Mapping], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else None


def _summary(rows: Sequence[Mapping]) -> dict:
    metrics = ("candidate_recall_at_n", "candidate_hit_at_n", "final_hit_at_k",
               "mrr", "ndcg_at_k", "context_evidence_coverage", "book_recall_at_k", "latency_ms")
    return {"query_count": len(rows), "answerable_count": sum(not row["unanswerable"] for row in rows),
            "unanswerable_count": sum(row["unanswerable"] for row in rows),
            "metrics": {key: _mean(rows, key) for key in metrics},
            "metric_sample_counts": {key: sum(row.get(key) is not None for row in rows) for key in metrics},
            "unanswerable_returned_any_rate": _mean([row for row in rows if row["unanswerable"]], "returned_any")}


def evaluate_retrieval(knowledge_base: Any, cases: Sequence[Mapping], *,
                       embedding_provider: Any = None, mode: str = "hybrid",
                       candidate_depth: int = 60, top_k: int = 5,
                       max_chars: int = 12000, **retrieval_options: Any) -> dict:
    """Evaluate labelled candidates, final hits, and actual delivered context.

    Candidate evaluation removes the per-book cap. Three retrieval calls are
    timed together; latency therefore measures the evaluation, not one search.
    Book-only annotations never become paragraph relevance judgements.
    """
    for name, value in (("candidate_depth", candidate_depth), ("top_k", top_k), ("max_chars", max_chars)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if candidate_depth < top_k:
        raise ValueError("candidate_depth must be at least top_k")
    validated = validate_evaluation_cases(cases)
    if isinstance(knowledge_base, (str, Path)):
        from rag_knowledge_base import RagKnowledgeBase
        knowledge_base = RagKnowledgeBase.open(knowledge_base)
    kb = knowledge_base
    options = dict(retrieval_options, embedding_provider=embedding_provider, mode=mode,
                   candidate_depth=candidate_depth)
    rows = []
    for case in validated:
        start = time.perf_counter()
        candidate_diagnostics, final_diagnostics = {}, {}
        candidates = kb.retrieve(case["query"], top_k=candidate_depth, **dict(options, per_book_cap=None, diagnostics=candidate_diagnostics))
        hits = kb.retrieve(case["query"], top_k=top_k, **dict(options, diagnostics=final_diagnostics))
        context = kb.retrieve_context(case["query"], top_k=top_k, max_chars=max_chars, **options)
        elapsed = (time.perf_counter() - start) * 1000
        candidate_metrics = _ranking_metrics(kb, candidates, case)
        final_metrics = _ranking_metrics(kb, hits, case)
        evidence = case["evidence_substrings"]
        context_text = _normalize("\n".join(hit.content for hit in context.hits))
        coverage = sum(_normalize(value) in context_text for value in evidence) / len(evidence) if evidence else None
        books = set(case["relevant_books"])
        matched_books = {str(getattr(hit, "book", "")) for hit in hits} & books
        actual_modes = sorted({hit.retrieval_mode for hit in hits})
        row = {"id": case["id"], "query": case["query"], "category": case["category"],
               "unanswerable": case["unanswerable"],
               "candidate_recall_at_n": candidate_metrics["recall"], "candidate_hit_at_n": candidate_metrics["hit"],
               "final_hit_at_k": final_metrics["hit"], "mrr": final_metrics["mrr"], "ndcg_at_k": final_metrics["ndcg"],
               "context_evidence_coverage": coverage, "book_recall_at_k": len(matched_books) / len(books) if books else None,
               "candidate_ids": [hit.id for hit in candidates], "final_ids": [hit.id for hit in hits],
               "context_ids": [hit.id for hit in context.hits], "context_chars": len(context.text),
               "effective_modes": actual_modes, "effective_mode": final_diagnostics.get("effective_mode"),
               "fallback": bool(final_diagnostics.get("fallback_reason")),
               "fallback_reason": final_diagnostics.get("fallback_reason"),
               "candidate_diagnostics": candidate_diagnostics, "final_diagnostics": final_diagnostics,
               "context_diagnostics": getattr(context, "diagnostics", {}),
               "returned_any": float(bool(hits)), "latency_ms": elapsed}
        rows.append(row)
    return {"schema_version": 1, "requested_mode": mode,
            "provider": type(embedding_provider).__name__ if embedding_provider is not None else None,
            "candidate_depth": candidate_depth, "top_k": top_k, "max_chars": max_chars,
            "summary": _summary(rows), "categories": {category: _summary([row for row in rows if row["category"] == category])
                                                       for category in sorted({row["category"] for row in rows})},
            "queries": rows}


def evaluate_corpus_growth(corpora: Mapping[str, Any], cases: Sequence[Mapping], **options: Any) -> dict:
    """Compare the same annotations against explicitly selected corpus stages."""
    return {str(name): evaluate_retrieval(corpus, cases, **options) for name, corpus in corpora.items()}
