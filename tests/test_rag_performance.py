"""Regression coverage for validated snapshot loading and bounded ranking."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import rag_knowledge_base as rag
from book_pipeline import write_knowledge_base
from rag_apparatus import annotate_apparatus


class Provider:
    provider_name = "local-test"
    model = "local-test"

    def embed_documents(self, texts):
        return [[1.0, 1.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 1.0]


class RagPerformanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "knowledge_base.jsonl"
        self.rows = [dict(id=f"{index:040x}", title="ＡＢＣ 标题", chapter_id=f"c{index}",
                         chapter_order=index % 3, content=f"量子 历史　研究 {index}")
                     for index in range(30)]
        write_knowledge_base(self.path, self.rows)

    def test_ready_snapshot_parses_corpus_and_vectors_once(self):
        rag.build_embedding_index(self.path, Provider())
        with patch.object(rag, "_load_knowledge_snapshot", wraps=rag._load_knowledge_snapshot) as corpus, \
             patch.object(rag, "_load_embedding_index", wraps=rag._load_embedding_index) as vectors:
            kb = rag.RagKnowledgeBase.open(self.path)
        self.assertTrue(kb.embedding_ready)
        self.assertEqual(corpus.call_count, 1)
        self.assertEqual(vectors.call_count, 1)
        self.assertEqual(kb.manifest, rag.read_rag_manifest(self.path))

    def test_vector_digest_binds_loaded_bytes_when_file_changes_during_parse(self):
        rag.build_embedding_index(self.path, Provider())
        index = rag.vector_index_path_for(self.path)
        original = index.read_bytes()
        replacement = original.replace(b"1.0", b"2.0")
        self.assertNotEqual(original, replacement)
        validate = rag._validate_vector
        replaced = False

        def replace_during_parse(*args, **kwargs):
            nonlocal replaced
            if not replaced:
                index.write_bytes(replacement)
                replaced = True
            return validate(*args, **kwargs)

        with patch.object(rag, "_validate_vector", side_effect=replace_during_parse):
            kb = rag.RagKnowledgeBase.open(self.path)
        self.assertEqual(kb.embedding_metadata.index_sha256, hashlib.sha256(original).hexdigest())
        self.assertEqual(kb._vectors[self.rows[0]["id"]], (1.0, 1.0))
        self.assertNotEqual(kb.embedding_metadata.index_sha256, hashlib.sha256(index.read_bytes()).hexdigest())
        with self.assertRaises(rag.RagIndexStaleError):
            rag.RagKnowledgeBase.open(self.path)

    def test_cached_normalization_preserves_scores_and_filtered_statistics(self):
        kb = rag.RagKnowledgeBase.open(self.path)
        for rows in (self.rows, self.rows[::2]):
            for query in ("量子历史", "ａｂｃ", "历史 研究", "不存在", "---"):
                expected = rag._lexical_scores(rows, query)
                with patch.object(rag, "_normalized_text", wraps=rag._normalized_text) as normalize:
                    actual = rag._lexical_scores(rows, query, kb._lexical_terms,
                                                 kb._lexical_statistics, kb._compact_documents)
                self.assertEqual(expected, actual)
                self.assertTrue(all(call.args[0] == query for call in normalize.call_args_list))

    def test_bounded_ranking_matches_full_sort_including_ties_and_hybrid(self):
        rag.build_embedding_index(self.path, Provider())
        kb = rag.RagKnowledgeBase.open(self.path)
        for mode, provider in (("lexical", None), ("semantic", Provider()), ("hybrid", Provider())):
            kwargs = dict(top_k=4, candidate_depth=7, mode=mode, embedding_provider=provider)
            actual = kb.retrieve("量子", **kwargs)
            with patch.object(rag.heapq, "nsmallest", side_effect=lambda n, values, key: sorted(values, key=key)[:n]):
                expected = kb.retrieve("量子", **kwargs)
            self.assertEqual(actual, expected)

    def test_reopen_rejects_changed_corpus_with_same_size_and_timestamp(self):
        rag.RagKnowledgeBase.open(self.path)
        original = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace("量子".encode(), "原子".encode()))
        os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(rag.RagIndexStaleError):
            rag.RagKnowledgeBase.open(self.path)

    def test_reopen_validates_vector_and_manifest_changes(self):
        rag.build_embedding_index(self.path, Provider())
        rag.RagKnowledgeBase.open(self.path)
        index = rag.vector_index_path_for(self.path)
        original = index.read_bytes()
        index.write_bytes(original + b"\n")
        for reader in (rag.RagKnowledgeBase.open, rag.read_rag_manifest):
            with self.assertRaises(rag.RagIndexStaleError):
                reader(self.path)
        index.write_bytes(original)
        manifest_path = rag.manifest_path_for(self.path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["retrieval"]["embedding"]["model"] = "changed-model"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(rag.RagIndexStaleError):
            rag.RagKnowledgeBase.open(self.path)

    def test_reopen_validates_metadata_and_apparatus_changes(self):
        annotate_apparatus(self.path)
        rag.RagKnowledgeBase.open(self.path)
        annotation = self.path.with_suffix(".apparatus.json")
        payload = json.loads(annotation.read_text(encoding="utf-8"))
        payload["annotations"][self.rows[0]["id"]]["default_weight"] = -1
        annotation.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(rag.RagIndexStaleError):
            rag.RagKnowledgeBase.open(self.path)
        annotation.unlink()
        metadata = rag.metadata_sidecar_path_for(self.path)
        metadata.write_text("{}", encoding="utf-8")
        with self.assertRaises(rag.RagFormatError):
            rag.RagKnowledgeBase.open(self.path)


if __name__ == "__main__":
    unittest.main()
