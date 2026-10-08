"""Offline policy regression tests; never call an embedding service."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tools.kb_qa_plugin import kb_qa as qa


class HybridPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "index.sqlite"
        self.db.touch()
        self.row = dict(id="fts", workspace="book", title="chapter", excerpt="量子发现片段",
                        score=1, chapter_order=1, report_status="passed", source_path="source")
        self.hit = dict(id="hybrid", workspace="book", book="book", title="chapter",
                        content="量子正文证据", score=1, channels="lexical,semantic", stage="deep")
        self.report = dict(hits=[self.hit], diagnostics={"used_mode": "hybrid"})

    def run_ask(self, report=None, **kwargs):
        with patch.object(qa, "_load_global_module", return_value=SimpleNamespace(search=lambda *a, **k: [self.row])), \
             patch.object(qa, "shelf", return_value={"shelf": []}), \
             patch.object(qa, "resolve_workspace", return_value=Path(self.tmp.name)), \
             patch.object(qa, "_deep_search", side_effect=report if isinstance(report, Exception) else None,
                          return_value=report or self.report):
            return qa.ask("量子", db=self.db, outputs=self.tmp.name, **kwargs)

    def test_discovery_never_enters_formal_evidence(self):
        result = self.run_ask()
        self.assertEqual([h["id"] for h in result["hits"]], ["hybrid"])
        self.assertNotIn("发现片段", result["context"])
        self.assertEqual(result["discovery"][0]["id"], "fts")
        self.assertEqual(result["diagnostics"]["effective_mode"], "hybrid")

    def test_failures_are_unavailable_not_fts_answers(self):
        with self.assertRaisesRegex(qa.CorpusError, "hybrid unavailable"):
            self.run_ask(qa.CorpusError("missing vectors"))

    def test_empty_success_differs_from_unavailable(self):
        result = self.run_ask(dict(hits=[], diagnostics={"used_mode": "hybrid"}))
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["diagnostics"]["effective_mode"], "hybrid")

    def test_explicit_diagnostic_flags(self):
        result = self.run_ask(semantic=False, deep=False)
        self.assertTrue(result["diagnostics"]["diagnostic_only"])
        self.assertEqual(result["diagnostics"]["effective_mode"], "lexical")
        self.assertEqual(result["diagnostics"]["requested_mode"], "lexical")

    def test_reader_scope_required(self):
        with self.assertRaisesRegex(ValueError, "reader"):
            qa.ask("量子", scope="pages")

    def test_evidence_gate_and_budget_remain(self):
        hit = dict(self.hit, title="unmatched", content="毫不相关")
        result = self.run_ask(dict(hits=[hit], diagnostics={"used_mode": "hybrid"}))
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["diagnostics"]["deep_rejected_ids"], ["hybrid"])
        hits, dropped = qa._apply_budget([dict(self.hit, content="证" * 900),
                                          dict(self.hit, id="second", content="据" * 900)],
                                         max_chars=1000, hit_chars=700)
        self.assertEqual(dropped, ["second"])
        self.assertTrue(hits[0]["truncated"])
        self.assertEqual(hits[0]["id"], "hybrid")

    def test_partial_coverage_keeps_successful_hybrid(self):
        rows = [self.row, dict(self.row, workspace="missing")]
        with patch.object(qa, "_load_global_module", return_value=SimpleNamespace(search=lambda *a, **k: rows)), \
             patch.object(qa, "shelf", return_value={"shelf": []}), \
             patch.object(qa, "resolve_workspace", return_value=Path(self.tmp.name)), \
             patch.object(qa, "_deep_search", side_effect=[self.report, qa.CorpusError("no vectors")]):
            result = qa.ask("量子", db=self.db, outputs=self.tmp.name)
        self.assertTrue(result["diagnostics"]["partial_coverage"])
        self.assertEqual(result["diagnostics"]["effective_mode"], "hybrid")
        self.assertEqual([h["id"] for h in result["hits"]], ["hybrid"])

    def test_real_rrf_with_fake_provider_and_embedding_failure(self):
        from book_pipeline import write_knowledge_base
        from rag_knowledge_base import build_embedding_index, RagProviderError
        from tests.test_rag_knowledge_base import FakeEmbeddingProvider, ROWS
        path = Path(self.tmp.name) / "knowledge_base.jsonl"
        write_knowledge_base(path, ROWS)
        provider = FakeEmbeddingProvider()
        build_embedding_index(path, provider)
        with patch("knowledge_base_cli._provider_from_manifest", return_value=provider):
            result = qa._deep_search(path.parent, "量子", top_k=2, max_chars=2000,
                                     semantic=True, apparatus_weight=None)
            self.assertEqual(result["diagnostics"]["effective_mode"], "hybrid")
            self.assertTrue(any("lexical" in h["channels"] and "semantic" in h["channels"]
                                for h in result["hits"]))
            self.assertTrue(result["context"])
            with patch.object(provider, "embed_query", side_effect=RagProviderError("offline failure")):
                with self.assertRaisesRegex(qa.CorpusError, "offline failure"):
                    qa._deep_search(path.parent, "量子", top_k=2, max_chars=2000,
                                    semantic=True, apparatus_weight=None)

    def test_deep_missing_provider_cannot_claim_hybrid(self):
        from rag_knowledge_base import RagKnowledgeBase
        base = Path(self.tmp.name)
        path = base / "knowledge_base.jsonl"
        path.touch()
        with patch("knowledge_base_cli._knowledge_base_path", return_value=path), \
             patch.object(RagKnowledgeBase, "open", return_value=SimpleNamespace(embedding_ready=False)), \
             patch("knowledge_base_cli._provider_from_manifest", return_value=None):
            with self.assertRaisesRegex(qa.CorpusError, "hybrid unavailable"):
                qa._deep_search(base, "量子", top_k=3, max_chars=100, semantic=True, apparatus_weight=None)


if __name__ == "__main__":
    unittest.main()
