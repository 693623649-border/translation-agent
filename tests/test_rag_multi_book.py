from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from book_pipeline import write_knowledge_base
from rag_knowledge_base import (
    CachedQueryProvider,
    RagEmbeddingUnavailableError,
    RagError,
    RagProviderError,
    build_embedding_index,
    retrieve_multi_book,
)


class CountingEmbeddingProvider:
    provider_name = "test-provider"
    model = "test-embedding-v1"

    def __init__(self) -> None:
        self.query_calls = 0

    @staticmethod
    def _vector(text: str) -> list[float]:
        return [
            float(text.count("量子")),
            float(text.count("历史")),
            1.0,
        ]

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        self.query_calls += 1
        return self._vector(text)


def _rows(prefix: str, seeds: list[tuple[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "id": hashlib.sha1(f"{prefix}{index}".encode("utf-8")).hexdigest(),
            "title": title,
            "chapter_id": f"chapter-{index}",
            "chapter_order": index,
            "content": content,
        }
        for index, (title, content) in enumerate(seeds, start=1)
    ]


PHYSICS_ROWS = _rows(
    "a",
    [
        ("量子叠加", "量子叠加描述微观系统的多种可能状态。"),
        ("量子测量", "量子测量问题困扰了一代物理学家。"),
    ],
)
HISTORY_ROWS = _rows(
    "b",
    [
        ("历史方法", "历史研究依赖档案、年代与来源互证。"),
        ("编年史话", "编年史把事件按年份排列成叙述。"),
    ],
)


class RetrieveMultiBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.physics = self._workspace("physics", PHYSICS_ROWS)
        self.history = self._workspace("history", HISTORY_ROWS)

    def _workspace(self, name: str, rows: list[dict[str, object]]) -> Path:
        directory = self.root / name
        directory.mkdir()
        path = directory / "knowledge_base.jsonl"
        write_knowledge_base(path, rows)
        return path

    def test_hybrid_fan_out_embeds_query_once_and_labels_workspaces(self):
        provider = CountingEmbeddingProvider()
        build_embedding_index(self.physics, provider, batch_size=1)
        build_embedding_index(self.history, provider, batch_size=1)
        context = retrieve_multi_book(
            [("physics", self.physics), ("history", self.history)],
            "量子",
            mode="hybrid",
            top_k=4,
            embedding_provider=provider,
        )
        self.assertEqual(provider.query_calls, 1)
        self.assertTrue(context.diagnostics["semantic_used"])
        # Inner hybrid scores carry over: both-channel physics chunks
        # (~2/61) outrank single-channel history chunks (~1/61) from any
        # library, so the matching book leads the fused ranking.
        books = [hit.book for hit in context.hits]
        self.assertEqual(books, ["physics", "physics", "history", "history"])
        self.assertIn("量子", context.hits[0].content)
        self.assertGreater(context.hits[0].score, context.hits[2].score)
        self.assertIn("physics", context.text)
        report = context.diagnostics["multi_book"]
        self.assertEqual(report["workspaces_searched"], ["physics", "history"])
        self.assertEqual(sorted(report["semantic_ready"]), ["history", "physics"])
        self.assertEqual(report["lexical_only"], [])
        self.assertEqual(report["errored"], {})

    def test_workspace_without_index_joins_lexically_and_is_disclosed(self):
        provider = CountingEmbeddingProvider()
        build_embedding_index(self.physics, provider, batch_size=1)
        context = retrieve_multi_book(
            [("physics", self.physics), ("history", self.history)],
            "历史 档案",
            mode="hybrid",
            top_k=4,
            embedding_provider=provider,
        )
        self.assertEqual(provider.query_calls, 1)
        self.assertEqual(context.hits[0].book, "history")
        self.assertEqual(context.hits[0].retrieval_mode, "lexical")
        report = context.diagnostics["multi_book"]
        self.assertEqual(report["lexical_only"], ["history"])
        self.assertEqual(report["per_workspace"]["history"]["fallback_reason"], "embedding_provider_unavailable")

    def test_hybrid_without_any_ready_index_fails_loud(self):
        with self.assertRaises(RagEmbeddingUnavailableError):
            retrieve_multi_book(
                [("physics", self.physics), ("history", self.history)],
                "量子",
                mode="hybrid",
            )

    def test_per_workspace_cap_limits_flooding(self):
        provider = CountingEmbeddingProvider()
        build_embedding_index(self.physics, provider, batch_size=1)
        build_embedding_index(self.history, provider, batch_size=1)
        context = retrieve_multi_book(
            [("physics", self.physics), ("history", self.history)],
            "量子",
            mode="hybrid",
            top_k=5,
            per_book_cap=1,
            embedding_provider=provider,
        )
        books = [hit.book for hit in context.hits]
        self.assertEqual(books.count("physics"), 1)
        self.assertLessEqual(len(context.hits), 2)

    def test_duplicate_content_across_workspaces_is_merged(self):
        duplicate_rows = _rows(
            "c",
            [
                ("量子叠加", "量子叠加描述微观系统的多种可能状态。"),
                ("独有章节", "这一段只出现在重复工作区里。"),
            ],
        )
        mirror = self._workspace("mirror", duplicate_rows)
        context = retrieve_multi_book(
            [("physics", self.physics), ("mirror", mirror)],
            "量子叠加",
            mode="lexical",
            top_k=4,
        )
        contents = [hit.content for hit in context.hits]
        self.assertEqual(contents.count("量子叠加描述微观系统的多种可能状态。"), 1)
        duplicate_sources = [
            hit.duplicate_sources for hit in context.hits if hit.duplicate_sources
        ]
        self.assertTrue(duplicate_sources, "merged duplicate should record its source")

    def test_mixed_embedding_identities_fail_loud(self):
        provider = CountingEmbeddingProvider()

        class OtherModel(CountingEmbeddingProvider):
            model = "other-model"

        build_embedding_index(self.physics, provider, batch_size=1)
        build_embedding_index(self.history, OtherModel(), batch_size=1)
        with self.assertRaises(RagProviderError):
            retrieve_multi_book(
                [("physics", self.physics), ("history", self.history)],
                "量子",
                mode="hybrid",
            )

    def test_broken_workspace_is_reported_instead_of_aborting(self):
        broken = self.root / "broken" / "knowledge_base.jsonl"
        broken.parent.mkdir()
        broken.write_text("{not json}\n", encoding="utf-8")
        provider = CountingEmbeddingProvider()
        build_embedding_index(self.physics, provider, batch_size=1)
        context = retrieve_multi_book(
            [("physics", self.physics), ("broken", broken)],
            "量子",
            mode="hybrid",
            embedding_provider=provider,
        )
        report = context.diagnostics["multi_book"]
        self.assertIn("broken", report["errored"])
        self.assertEqual(report["workspaces_searched"], ["physics"])

    def test_no_opens_at_all_raises(self):
        missing = self.root / "ghost" / "knowledge_base.jsonl"
        with self.assertRaises(RagError):
            retrieve_multi_book([("ghost", missing)], "量子", mode="lexical")

    def test_cached_query_provider_reuses_vector(self):
        inner = CountingEmbeddingProvider()
        cached = CachedQueryProvider(inner)
        self.assertEqual(cached.provider_name, "test-provider")
        self.assertEqual(cached.model, "test-embedding-v1")
        first = cached.embed_query("量子")
        second = cached.embed_query("量子")
        self.assertEqual(inner.query_calls, 1)
        self.assertEqual(first, second)
        cached.embed_query("历史")
        self.assertEqual(inner.query_calls, 2)


if __name__ == "__main__":
    unittest.main()
