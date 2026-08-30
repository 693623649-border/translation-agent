from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence
from unittest.mock import patch

from book_pipeline import build_parser, write_knowledge_base
from pipeline_graph.book import _knowledge_base_is_current
from rag_knowledge_base import (
    RagEmbeddingUnavailableError,
    RagIndexStaleError,
    RagKnowledgeBase,
    RagProviderError,
    ZhipuEmbeddingProvider,
    build_embedding_index,
    manifest_path_for,
    vector_index_path_for,
    zhipu_embedding_enabled,
)
from translation_agent_api import (
    build_knowledge_base_embedding_index,
    retrieve_knowledge_base_context,
)


ROWS = [
    {
        "id": "a" * 40,
        "title": "量子理论",
        "chapter_id": "chapter-1",
        "chapter_order": 1,
        "content": "量子叠加描述微观系统的多种可能状态。",
    },
    {
        "id": "b" * 40,
        "title": "历史方法",
        "chapter_id": "chapter-2",
        "chapter_order": 2,
        "content": "历史研究依赖档案、年代与来源互证。",
    },
]


class FakeEmbeddingProvider:
    provider_name = "test-provider"
    model = "test-embedding-v1"

    def __init__(self) -> None:
        self.document_calls = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            float(text.count("量子")),
            float(text.count("历史")),
            1.0,
        ]

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        return self._vector(text)


class InvalidEmbeddingProvider(FakeEmbeddingProvider):
    model = "broken"

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return []


class FakeZhipuEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(dict(kwargs))
        inputs = list(kwargs["input"])  # type: ignore[arg-type]
        dimensions = int(kwargs["dimensions"])  # type: ignore[arg-type]
        data = []
        for index in reversed(range(len(inputs))):
            vector = [0.0] * dimensions
            vector[index] = float(index + 1)
            data.append(SimpleNamespace(index=index, embedding=vector))
        return SimpleNamespace(data=data)


class RagKnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.knowledge_path = Path(self.tempdir.name) / "knowledge_base.jsonl"

    def _write(self, rows: list[dict[str, object]] | None = None) -> None:
        write_knowledge_base(self.knowledge_path, rows or ROWS)

    def test_publisher_creates_rag_manifest_and_lexical_retrieval(self) -> None:
        self._write()

        manifest = json.loads(
            manifest_path_for(self.knowledge_path).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["kind"], "translation-agent.rag-knowledge-base")
        self.assertEqual(manifest["documents"]["chunk_count"], 2)
        self.assertEqual(manifest["retrieval"]["lexical"]["status"], "ready")
        self.assertEqual(
            manifest["retrieval"]["embedding"]["status"],
            "awaiting_provider",
        )

        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve("量子系统", top_k=1)

        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, "a" * 40)
        self.assertEqual(hits[0].retrieval_mode, "lexical")

    def test_embedding_sidecar_enables_cosine_retrieval_and_context(self) -> None:
        self._write()
        provider = FakeEmbeddingProvider()

        metadata = build_embedding_index(
            self.knowledge_path,
            provider,
            batch_size=1,
        )
        self.assertEqual(metadata.dimensions, 3)
        self.assertEqual(metadata.chunk_count, 2)

        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "量子问题",
            top_k=1,
            embedding_provider=provider,
        )
        context = knowledge_base.retrieve_context(
            "量子问题",
            top_k=1,
            embedding_provider=provider,
        )

        self.assertEqual(hits[0].id, "a" * 40)
        self.assertEqual(hits[0].retrieval_mode, "semantic")
        self.assertIn(f"[KB:{'a' * 40}]", context.text)
        self.assertEqual(context.hits, tuple(hits))

    def test_unchanged_ready_index_skips_repeat_embedding_cost(self) -> None:
        self._write()
        provider = FakeEmbeddingProvider()

        first = build_embedding_index(self.knowledge_path, provider)
        second = build_embedding_index(self.knowledge_path, provider)

        self.assertEqual(first, second)
        self.assertEqual(provider.document_calls, 1)

    def test_rewriting_documents_invalidates_existing_embedding_index(self) -> None:
        self._write()
        provider = FakeEmbeddingProvider()
        build_embedding_index(self.knowledge_path, provider)

        changed = [dict(row) for row in ROWS]
        changed[0]["content"] = "量子文本已经更新。"
        self._write(changed)

        manifest = json.loads(
            manifest_path_for(self.knowledge_path).read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["retrieval"]["embedding"]["status"],
            "awaiting_provider",
        )
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        with self.assertRaises(RagEmbeddingUnavailableError):
            knowledge_base.retrieve(
                "量子",
                embedding_provider=provider,
            )

    def test_invalid_embedding_batch_does_not_overwrite_ready_index(self) -> None:
        self._write()
        build_embedding_index(self.knowledge_path, FakeEmbeddingProvider())
        vector_path = vector_index_path_for(self.knowledge_path)
        manifest_path = manifest_path_for(self.knowledge_path)
        before_vector_sha = hashlib.sha256(vector_path.read_bytes()).hexdigest()
        before_manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

        with self.assertRaises(RagProviderError):
            build_embedding_index(
                self.knowledge_path,
                InvalidEmbeddingProvider(),
            )

        self.assertEqual(
            hashlib.sha256(vector_path.read_bytes()).hexdigest(),
            before_vector_sha,
        )
        self.assertEqual(
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            before_manifest_sha,
        )

    def test_vector_tampering_is_rejected_by_manifest_checksum(self) -> None:
        self._write()
        build_embedding_index(self.knowledge_path, FakeEmbeddingProvider())
        vector_path = vector_index_path_for(self.knowledge_path)
        lines = vector_path.read_text(encoding="utf-8").splitlines()
        first_vector = json.loads(lines[1])
        first_vector["embedding"][0] += 0.25
        lines[1] = json.dumps(first_vector, ensure_ascii=False, sort_keys=True)
        vector_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with self.assertRaises(RagIndexStaleError):
            RagKnowledgeBase.open(self.knowledge_path)

    def test_chapter_filter_is_applied_before_ranking(self) -> None:
        self._write()
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)

        hits = knowledge_base.retrieve(
            "研究方法",
            top_k=5,
            chapter_ids={"chapter-2"},
        )

        self.assertEqual([hit.chapter_id for hit in hits], ["chapter-2"])

    def test_public_api_builds_index_and_returns_augmented_context(self) -> None:
        self._write()
        provider = FakeEmbeddingProvider()

        metadata = build_knowledge_base_embedding_index(
            self.tempdir.name,
            provider,
        )
        context = retrieve_knowledge_base_context(
            self.tempdir.name,
            "历史证据",
            top_k=1,
            embedding_provider=provider,
        )

        self.assertEqual(metadata.provider_name, provider.provider_name)
        self.assertEqual(context.hits[0].id, "b" * 40)

    def test_graph_cache_requires_current_rag_manifest(self) -> None:
        self._write()
        saved = {
            "path": str(self.knowledge_path.resolve()),
            "sha256": hashlib.sha256(self.knowledge_path.read_bytes()).hexdigest(),
        }
        pipeline_argv = ["--output-dir", str(self.knowledge_path.parent)]
        context = SimpleNamespace(
            output_dir=self.knowledge_path.parent.resolve(),
            require=lambda name: pipeline_argv if name == "pipeline.argv" else None,
            fingerprints={},
        )

        # The graph's argument parser loads the repository .env when one
        # exists, and other tests invoking book_pipeline.main() leak those
        # values into os.environ; unit tests must run against a clean
        # environment instead.
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("book_pipeline.load_env_file", lambda path: None),
        ):
            self.assertTrue(
                _knowledge_base_is_current(context, {"publication.knowledge_base": saved})
            )
            manifest_path_for(self.knowledge_path).unlink()
            self.assertFalse(
                _knowledge_base_is_current(context, {"publication.knowledge_base": saved})
            )

    def test_zhipu_provider_uses_embedding3_and_preserves_response_order(self) -> None:
        endpoint = FakeZhipuEmbeddings()
        client = SimpleNamespace(embeddings=endpoint)
        provider = ZhipuEmbeddingProvider(
            client=client,
            dimensions=256,
        )

        vectors = provider.embed_documents(["第一段", "第二段"])

        self.assertEqual(provider.provider_name, "zhipu")
        self.assertEqual(provider.model, "embedding-3")
        self.assertEqual(provider.base_url, "https://open.bigmodel.cn/api/paas/v4/")
        self.assertEqual(len(vectors), 2)
        self.assertEqual(vectors[0][0], 1.0)
        self.assertEqual(vectors[1][1], 2.0)
        self.assertEqual(
            endpoint.calls,
            [
                {
                    "model": "embedding-3",
                    "input": ["第一段", "第二段"],
                    "dimensions": 256,
                }
            ],
        )

    def test_zhipu_provider_requires_environment_key_before_network(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            provider = ZhipuEmbeddingProvider(dimensions=256)
            with self.assertRaisesRegex(RagProviderError, "ZHIPU_API_KEY"):
                provider.embed_query("测试")

    def test_zhipu_auto_activation_honors_explicit_override(self) -> None:
        with patch.dict(os.environ, {"ZHIPU_API_KEY": "test-key"}, clear=True):
            self.assertTrue(zhipu_embedding_enabled(None))
            self.assertTrue(zhipu_embedding_enabled(True))
            self.assertFalse(zhipu_embedding_enabled(False))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(zhipu_embedding_enabled(None))
            self.assertTrue(zhipu_embedding_enabled(True))

    def test_rag_embedding_cli_mode_defaults_to_key_detection(self) -> None:
        parser = build_parser()

        self.assertIsNone(parser.parse_args([]).rag_embed)
        self.assertTrue(parser.parse_args(["--rag-embed"]).rag_embed)
        self.assertFalse(parser.parse_args(["--no-rag-embed"]).rag_embed)


if __name__ == "__main__":
    unittest.main()
