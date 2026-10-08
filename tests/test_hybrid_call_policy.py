"""Production hybrid calls must never silently degrade to lexical retrieval."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import translation_agent_api as api
import rag_knowledge_base as rag
from book_pipeline import write_knowledge_base
from knowledge_base_cli import main
from tests.test_knowledge_base_cli import ROWS, FakeZhipuProvider, MissingKeyProvider


class HybridCallPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name)
        self.path = self.output / "knowledge_base.jsonl"
        write_knowledge_base(self.path, ROWS)
        rag.initialize_rag_manifest(self.path)
        self.provider = FakeZhipuProvider()

    def ready(self):
        rag.build_embedding_index(self.path, self.provider)

    def test_default_and_none_execute_both_channels_and_keep_citations(self):
        self.ready()
        for options in ({}, {"mode": None}):
            with self.subTest(options=options), patch.object(
                rag, "_lexical_scores", wraps=rag._lexical_scores
            ) as lexical, patch.object(
                self.provider, "embed_query", wraps=self.provider.embed_query
            ) as semantic:
                context = api.retrieve_knowledge_base_context(
                    self.output, "量子", embedding_provider=self.provider,
                    chapter_ids=("chapter-1",), max_chars=150, **options
                )
                self.assertTrue(lexical.called)
                self.assertTrue(semantic.called)
                self.assertTrue(context.hits)
                self.assertTrue(all(h.retrieval_mode == "hybrid" for h in context.hits))
                self.assertTrue(all(h.chapter_id == "chapter-1" for h in context.hits))
                self.assertIn("[KB:", context.text)
                self.assertLessEqual(len(context.text), 150)

    def test_missing_vectors_raise_but_explicit_lexical_diagnostic_works(self):
        with self.assertRaises(rag.RagEmbeddingUnavailableError):
            api.retrieve_knowledge_base_context(self.output, "量子")
        self.assertTrue(api.retrieve_knowledge_base_context(
            self.output, "量子", mode="lexical"
        ).hits)
        stderr = io.StringIO()
        self.assertNotEqual(main(["retrieve", str(self.output), "量子"],
                                 stdout=stderr), 0)
        self.assertIn("ready embedding index", stderr.getvalue())

    def test_provider_identity_failure_and_dimension_failure_are_errors(self):
        self.ready()
        self.provider.model = "wrong"
        with self.assertRaises(rag.RagProviderError):
            api.retrieve_knowledge_base_context(self.output, "量子", embedding_provider=self.provider)
        self.provider.model = "embedding-3"
        with patch.object(self.provider, "embed_query", return_value=[1.0]):
            with self.assertRaises(rag.RagError):
                api.retrieve_knowledge_base_context(self.output, "量子", embedding_provider=self.provider)
        with self.assertRaisesRegex(rag.RagProviderError, "missing key"):
            api.retrieve_knowledge_base_context(self.output, "量子", embedding_provider=MissingKeyProvider())

    def test_cli_provider_failure_does_not_fallback(self):
        self.ready()
        stderr = io.StringIO()
        with patch("knowledge_base_cli.ZhipuEmbeddingProvider", MissingKeyProvider):
            self.assertNotEqual(main(["retrieve", str(self.output), "量子"],
                                     stdout=stderr), 0)
        self.assertIn("missing key", stderr.getvalue())

    def test_auto_provider_uses_index_identity_and_supports_empty_filter(self):
        self.ready()
        with patch.object(rag, "ZhipuEmbeddingProvider", return_value=self.provider) as ctor:
            context = api.retrieve_knowledge_base_context(self.output, "量子", chapter_ids=("missing",))
        self.assertFalse(context.hits)
        ctor.assert_called_once_with(model="embedding-3", dimensions=3)

    def test_real_zhipu_adapter_missing_key_errors_without_network(self):
        class WideProvider(FakeZhipuProvider):
            @staticmethod
            def _vector(text):
                return [1.0] * 256
        rag.build_embedding_index(self.path, WideProvider())
        with patch.dict(rag.os.environ, {}, clear=True):
            with self.assertRaises(rag.RagProviderError):
                api.retrieve_knowledge_base_context(self.output, "query")
