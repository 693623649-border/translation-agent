from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from book_pipeline import write_knowledge_base
from rag_evaluation import (
    evaluate_corpus_growth,
    evaluate_retrieval,
    load_evaluation_cases,
    validate_evaluation_cases,
)
from rag_knowledge_base import RagKnowledgeBase, initialize_rag_manifest


ROWS = [
    {
        "id": "a" * 40,
        "title": "量子叠加",
        "chapter_id": "chapter-1",
        "chapter_order": 1,
        "content": "量子叠加描述微观系统的多种可能状态同时存在。",
    },
    {
        "id": "b" * 40,
        "title": "历史方法",
        "chapter_id": "chapter-2",
        "chapter_order": 2,
        "content": "历史研究依赖档案、年代与来源互证的方法论。",
    },
    {
        "id": "c" * 40,
        "title": "光合作用",
        "chapter_id": "chapter-3",
        "chapter_order": 3,
        "content": "光合作用把光能转换为化学能储存于糖类分子。",
    },
]


class _LexicalOnlyProvider:
    """Provider stub whose vectors make lexical-only corpora comparable."""

    provider_name = "stub"
    model = "stub-v1"

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        return [1.0, 0.0, 0.0]


class ValidateEvaluationCasesTests(unittest.TestCase):
    def test_accepts_minimal_case_and_normalizes_extras(self) -> None:
        validated = validate_evaluation_cases(
            [{"id": "q1", "query": "问题", "category": "concept"}]
        )
        self.assertEqual(validated[0]["id"], "q1")
        self.assertFalse(validated[0]["unanswerable"])

    def test_rejects_missing_fields_and_duplicate_ids(self) -> None:
        with self.assertRaises(ValueError):
            validate_evaluation_cases([{"query": "无 id"}])
        with self.assertRaises(ValueError):
            validate_evaluation_cases(
                [
                    {"id": "q", "query": "一"},
                    {"id": "q", "query": "二"},
                ]
            )

    def test_loads_jsonl_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text(
                json.dumps({"id": "q1", "query": "叠加态"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(load_evaluation_cases(path)[0]["query"], "叠加态")


class EvaluateRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kb_path = self.root / "knowledge_base.jsonl"
        write_knowledge_base(self.kb_path, ROWS)
        initialize_rag_manifest(self.kb_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_three_layer_metrics_and_diagnostics(self) -> None:
        cases = [
            {
                "id": "q1",
                "query": "微观系统的多种可能状态",
                "category": "concept",
                "relevant_ids": ["a" * 40],
                "evidence_substrings": ["多种可能状态"],
            },
            {
                "id": "q2",
                "query": "档案与来源互证",
                "category": "paraphrase",
                "relevant_ids": ["b" * 40],
                "evidence_substrings": ["来源互证"],
            },
        ]
        report = evaluate_retrieval(
            RagKnowledgeBase.open(self.kb_path),
            cases,
            mode="lexical",
            candidate_depth=10,
            top_k=2,
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(len(report["queries"]), 2)
        for row in report["queries"]:
            self.assertTrue(row["candidate_hit_at_n"])
            self.assertTrue(row["final_hit_at_k"])
            self.assertEqual(row["mrr"], 1.0)
            self.assertEqual(row["context_evidence_coverage"], 1.0)
            self.assertFalse(row["fallback"])
            self.assertIn("effective_mode", row["final_diagnostics"])
        self.assertEqual(
            report["categories"]["concept"]["metrics"]["final_hit_at_k"],
            1.0,
        )

    def test_unanswerable_queries_never_count_as_hits(self) -> None:
        cases = [
            {
                "id": "absent",
                "query": "板块构造的驱动机制",  # not in corpus
                "category": "unanswerable",
                "relevant_ids": [],
                "evidence_substrings": [],
                "unanswerable": True,
            }
        ]
        report = evaluate_retrieval(
            self.kb_path,
            cases,
            mode="lexical",
            candidate_depth=5,
            top_k=2,
        )
        row = report["queries"][0]
        self.assertFalse(row["final_hit_at_k"])
        self.assertEqual(row["book_recall_at_k"], None)

    def test_argument_validation(self) -> None:
        kb = RagKnowledgeBase.open(self.kb_path)
        cases = [{"id": "q", "query": "量子"}]
        with self.assertRaises(ValueError):
            evaluate_retrieval(kb, cases, candidate_depth=0)
        with self.assertRaises(ValueError):
            evaluate_retrieval(kb, cases, candidate_depth=2, top_k=5)

    def test_corpus_growth_compares_named_stages(self) -> None:
        other_rows = [
            {
                "id": "d" * 40,
                "title": "干涉实验",
                "chapter_id": "chapter-1",
                "chapter_order": 1,
                "content": "双缝干涉实验显示量子叠加的波动图样。",
            }
        ]
        other_path = self.root / "other" / "knowledge_base.jsonl"
        other_path.parent.mkdir()
        write_knowledge_base(other_path, other_rows)
        initialize_rag_manifest(other_path)
        cases = [
            {
                "id": "q1",
                "query": "量子叠加",
                "category": "concept",
                "relevant_ids": ["a" * 40, "d" * 40],
            }
        ]
        report = evaluate_corpus_growth(
            {
                "single": self.kb_path,
                "grown": other_path,
            },
            cases,
            mode="lexical",
            candidate_depth=10,
            top_k=5,
        )
        self.assertEqual(sorted(report), ["grown", "single"])
        self.assertEqual(report["single"]["summary"]["query_count"], 1)
        single_rate = report["single"]["summary"]["metrics"]["final_hit_at_k"]
        grown_rate = report["grown"]["summary"]["metrics"]["final_hit_at_k"]
        self.assertIsNotNone(single_rate)
        self.assertIsNotNone(grown_rate)


if __name__ == "__main__":
    unittest.main()
