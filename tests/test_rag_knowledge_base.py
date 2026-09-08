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
    RagFormatError,
    RagIndexStaleError,
    RagKnowledgeBase,
    RagProviderError,
    ZhipuEmbeddingProvider,
    build_embedding_index,
    load_metadata_sidecar,
    manifest_path_for,
    metadata_sidecar_path_for,
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

    def test_runtime_diagnostics_alias_cache_and_author_filter(self):
        self._write()
        kb = RagKnowledgeBase.open(self.knowledge_path)
        self.assertEqual(kb.retrieve("量子", authors={"不存在"}), [])
        with patch("rag_knowledge_base._tokens", wraps=__import__("rag_knowledge_base")._tokens) as tokenize:
            context = kb.retrieve_context("quantum", aliases={"quantum": ["量子"]})
            self.assertEqual(context.hits[0].id, ROWS[0]["id"])
            self.assertTrue(all(len(call.args[0]) < 30 for call in tokenize.call_args_list))
        self.assertEqual(context.diagnostics["effective_mode"], "lexical")
        self.assertEqual(context.diagnostics["fallback_reason"], "embedding_provider_unavailable")
        self.assertEqual(context.diagnostics["query_variants"], ["quantum", "量子"])

    def test_context_shares_budget_and_returns_actual_excerpts(self):
        rows = [dict(row, content="前文。" * 800 + "量子关键证据。" + str(i)) for i, row in enumerate(ROWS)]
        self._write(rows)
        context = RagKnowledgeBase.open(self.knowledge_path).retrieve_context("量子", max_chars=600)
        self.assertEqual(len(context.hits), 2)
        self.assertLessEqual(len(context.text), 600)
        for hit in context.hits:
            self.assertIn("量子关键证据", hit.content)
            self.assertIn(hit.content, context.text)
            self.assertGreater(len(hit.source_content), len(hit.content))

    def test_reranker_sees_candidates_before_top_k(self):
        self._write()
        class Reverse:
            def rerank(self, query, hits):
                return list(reversed(hits))
        kb = RagKnowledgeBase.open(self.knowledge_path)
        before = kb.retrieve("量子 历史", top_k=2)
        after = kb.retrieve("量子 历史", top_k=1, reranker=Reverse())
        self.assertEqual(after[0].id, before[-1].id)

    def test_incremental_embeddings_reuse_content_not_ids(self):
        self._write()
        provider = FakeEmbeddingProvider()
        build_embedding_index(self.knowledge_path, provider, batch_size=1)
        changed = [dict(row) for row in ROWS]
        changed[0]["content"] = "量子变化"
        self._write(changed)
        build_embedding_index(self.knowledge_path, provider, batch_size=1)
        self.assertEqual(provider.document_calls, 3)
        self.assertTrue(RagKnowledgeBase.open(self.knowledge_path).embedding_ready)

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
        self.assertEqual(hits[0].retrieval_mode, "hybrid")
        self.assertIn(f"[KB:{'a' * 40}]", context.text)
        self.assertEqual([hit.id for hit in context.hits], [hit.id for hit in hits])
        self.assertEqual(context.hits[0].source_content, hits[0].content)

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
                mode="semantic",
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


class HybridRoutingTests(unittest.TestCase):
    """Phase-1 retrieval plan: RRF fusion, routing filters, per-book caps."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.knowledge_path = Path(self.tempdir.name) / "knowledge_base.jsonl"
        # Big book A dominates the corpus (6 chunks); small book B has 2.
        self.rows = [
            {
                "id": f"a{i}" + "0" * 38,
                "title": f"大书 第{i}章",
                "chapter_id": f"01_大书:ch-{i:02d}",
                "chapter_order": i,
                "content": (
                    "量子叠加描述微观系统的多种可能状态。" * (3 if i == 1 else 1)
                    if i == 1
                    else "历史研究依赖档案、年代与来源互证。"
                ),
            }
            for i in range(1, 7)
        ] + [
            {
                "id": "b" + "0" * 39,
                "title": "小书 量子史",
                "chapter_id": "02_小书:ch-01",
                "chapter_order": 1,
                "content": "量子力学的历史脉络与人物。量子 量子。",
            },
            {
                "id": "c" + "0" * 39,
                "title": "小书 量子史 附录",
                "chapter_id": "02_小书:ch-02",
                "chapter_order": 2,
                "content": "量子力学史的年表与档案来源。",
            },
        ]
        write_knowledge_base(self.knowledge_path, self.rows)
        provider = FakeEmbeddingProvider()
        build_embedding_index(self.knowledge_path, provider)
        self.provider = provider

    def test_hybrid_fusion_prefers_dual_channel_hits(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "量子 历史",
            top_k=5,
            embedding_provider=self.provider,
            mode="hybrid",
        )
        self.assertTrue(hits)
        self.assertTrue(all(hit.retrieval_mode == "hybrid" for hit in hits))
        # The first hit must be recalled by BOTH channels (RRF rewards it).
        self.assertEqual(hits[0].channels, "lexical+semantic")
        # Channels labels only use the two known values.
        self.assertTrue(
            all(
                hit.channels in ("lexical", "semantic", "lexical+semantic")
                for hit in hits
            )
        )

    def test_hybrid_degrades_to_lexical_without_embedding(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve("量子", top_k=3, mode="hybrid")
        self.assertTrue(hits)
        self.assertTrue(all(hit.retrieval_mode == "lexical" for hit in hits))

    def test_semantic_mode_without_provider_degrades_to_lexical(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve("量子", top_k=3, mode="semantic")
        self.assertTrue(hits)
        self.assertTrue(all(hit.retrieval_mode == "lexical" for hit in hits))

    def test_per_book_cap_balances_results(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "历史",
            top_k=5,
            embedding_provider=self.provider,
            mode="hybrid",
            per_book_cap=2,
        )
        # Even though 01_大书 owns most 历史 chunks, at most 2 survive.
        big_book = [h for h in hits if h.book == "01_大书"]
        self.assertLessEqual(len(big_book), 2)
        self.assertTrue(any(h.book == "02_小书" for h in hits))

    def test_book_filter_routes_to_requested_book(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "量子 历史",
            top_k=5,
            embedding_provider=self.provider,
            mode="hybrid",
            book_ids={"02_小书"},
        )
        self.assertTrue(hits)
        self.assertTrue(all(h.book == "02_小书" for h in hits))

    def test_single_book_route_disables_per_book_cap(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "历史",
            top_k=5,
            mode="lexical",
            book_ids={"01_大书"},
            per_book_cap=2,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(len(hits[0].duplicate_sources), 4)
        self.assertTrue(all(hit.book == "01_大书" for hit in hits))

    def test_auto_route_matches_title_variants_and_author_books(self) -> None:
        sidecar = metadata_sidecar_path_for(self.knowledge_path)
        sidecar.write_text(
            "\n".join(
                json.dumps(
                    {
                        "id": row["id"],
                        "book_id": row["chapter_id"].split(":", 1)[0],
                        "book_title": (
                            "日本的思想"
                            if row["chapter_id"].startswith("01_")
                            else "共同幻想論"
                        ),
                        "author": (
                            "丸山真男"
                            if row["chapter_id"].startswith("01_")
                            else "吉本隆明"
                        ),
                        "language": "zh",
                    },
                    ensure_ascii=False,
                )
                for row in self.rows
            )
            + "\n",
            encoding="utf-8",
        )
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)

        inferred = knowledge_base.infer_query_routes("共同幻想与国家有什么关系")
        self.assertEqual(inferred["book_ids"], frozenset({"共同幻想論"}))
        hits = knowledge_base.retrieve(
            "共同幻想 量子 历史",
            top_k=5,
            mode="lexical",
            per_book_cap=1,
            auto_route=True,
        )
        self.assertEqual(len(hits), 2)
        self.assertTrue(any(hit.book == "共同幻想論" for hit in hits))
        self.assertTrue(any(hit.book != "共同幻想論" for hit in hits))

        title_wins = knowledge_base.infer_query_routes(
            "丸山真男如何理解日本思想的结构"
        )
        self.assertEqual(title_wins["book_ids"], frozenset({"日本的思想"}))

        comparison = knowledge_base.infer_query_routes(
            "比较共同幻想与丸山真男的思想"
        )
        self.assertEqual(
            comparison["book_ids"],
            frozenset({"共同幻想論", "日本的思想"}),
        )
        self.assertEqual(comparison["authors"], frozenset({"丸山真男"}))

    def test_hybrid_candidate_depth_must_cover_top_k(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        with self.assertRaisesRegex(ValueError, "candidate_depth"):
            knowledge_base.retrieve(
                "量子 历史",
                top_k=5,
                embedding_provider=self.provider,
                mode="hybrid",
                candidate_depth=4,
            )

    def test_metadata_sidecar_rejects_duplicate_and_stale_ids(self) -> None:
        sidecar = metadata_sidecar_path_for(self.knowledge_path)
        duplicate = json.dumps(
            {"id": self.rows[0]["id"], "book_title": "大书"},
            ensure_ascii=False,
        )
        sidecar.write_text(duplicate + "\n" + duplicate + "\n", encoding="utf-8")
        with self.assertRaisesRegex(RagFormatError, "Duplicate sidecar"):
            load_metadata_sidecar(self.knowledge_path)

        sidecar.write_text(
            json.dumps(
                {"id": "d" * 40, "book_title": "陈旧书目"},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RagIndexStaleError, "absent"):
            RagKnowledgeBase.open(self.knowledge_path)

    def test_metadata_sidecar_enables_author_and_language_routing(self) -> None:
        sidecar = metadata_sidecar_path_for(self.knowledge_path)
        sidecar.write_text(
            "\n".join(
                json.dumps(
                    {
                        "id": row["id"],
                        "book_id": row["chapter_id"].split(":", 1)[0],
                        "book_title": "大书" if row["chapter_id"].startswith("01_") else "小书",
                        "author": "作者甲" if row["chapter_id"].startswith("01_") else "作者乙",
                        "language": "zh",
                    },
                    ensure_ascii=False,
                )
                for row in self.rows
            )
            + "\n",
            encoding="utf-8",
        )
        loaded = load_metadata_sidecar(self.knowledge_path)
        self.assertEqual(len(loaded), len(self.rows))

        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        hits = knowledge_base.retrieve(
            "量子 历史",
            top_k=5,
            embedding_provider=self.provider,
            mode="hybrid",
            authors={"作者乙"},
        )
        self.assertTrue(hits)
        self.assertTrue(all(h.book == "小书" for h in hits))

        hits_zh = knowledge_base.retrieve(
            "量子",
            top_k=2,
            embedding_provider=self.provider,
            mode="hybrid",
            languages={"zh"},
        )
        self.assertEqual(len(hits_zh), 2)

    def test_context_prefix_carries_book_and_channels(self) -> None:
        knowledge_base = RagKnowledgeBase.open(self.knowledge_path)
        context = knowledge_base.retrieve_context(
            "量子 历史",
            top_k=2,
            embedding_provider=self.provider,
            mode="hybrid",
        )
        self.assertIn("[01_大书]", context.text)
        self.assertIn("(lexical+semantic)", context.text)


if __name__ == "__main__":
    unittest.main()
