import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from rag_indexing import build_retrieval_corpus, validate_retrieval_sources
from rag_knowledge_base import load_knowledge_rows, load_metadata_sidecar


class RetrievalIndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, name="book", content="第一段内容。\n\n第二段内容。第三段内容。\n最后内容。"):
        folder = self.root / name
        folder.mkdir()
        path = folder / "knowledge_base.jsonl"
        row = {"id": hashlib.sha1(name.encode()).hexdigest(), "title": "第一章",
               "chapter_id": "chapter-1", "chapter_order": 1, "content": content}
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        path.with_suffix(".meta.jsonl").write_text(json.dumps({"id": row["id"],
            "book_id": name, "book_title": "书名", "author": "作者", "language": "zh",
            "custom": "retained"}, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

    def test_offsets_boundaries_and_metadata(self):
        path = self.source()
        result = build_retrieval_corpus([path], self.root / "derived", chunk_chars=12, overlap_chars=2)
        rows = load_knowledge_rows(result["knowledge_base_path"])
        metas = load_metadata_sidecar(result["knowledge_base_path"])
        original = load_knowledge_rows(path)[0]["content"]
        covered = set()
        for row in rows:
            meta = metas[row["id"]]
            start, end = int(meta["source_start"]), int(meta["source_end"])
            self.assertEqual(row["content"], original[start:end])
            self.assertLessEqual(len(row["content"]), 12)
            self.assertEqual(meta["custom"], "retained")
            self.assertIn("书名", row["title"])
            covered.update(range(start, end))
        self.assertEqual(covered, set(range(len(original))))
        self.assertTrue(rows[0]["content"].endswith("\n\n"))

    def test_dedup_and_noop_preserve_vectors(self):
        first = self.source("first")
        second = self.source("second")
        target = self.root / "derived"
        result = build_retrieval_corpus([first, first.parent, second], target)
        self.assertEqual(result["counts"]["sources"], 2)
        self.assertEqual(result["counts"]["output_chunks"], 1)
        meta = next(iter(load_metadata_sidecar(result["knowledge_base_path"]).values()))
        self.assertEqual(len(json.loads(meta["provenance"])), 2)
        vector = target / "knowledge_base.vectors.jsonl"
        vector.write_text("preserve me", encoding="utf-8")
        times = {p.name: p.stat().st_mtime_ns for p in target.iterdir()}
        again = build_retrieval_corpus([first, first.parent, second], target)
        self.assertTrue(again["unchanged"])
        self.assertEqual(times, {p.name: p.stat().st_mtime_ns for p in target.iterdir()})
        self.assertEqual(vector.read_text(), "preserve me")

    def test_protect_source_and_unknown_outputs(self):
        path = self.source()
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            build_retrieval_corpus([path], path.parent)
        other = self.root / "other"
        other.mkdir()
        (other / "knowledge_base.jsonl").write_text("existing")
        with self.assertRaises(ValueError):
            build_retrieval_corpus([path], other)
        self.assertEqual(path.read_bytes(), before)

    def test_stale_source_metadata_and_outputs(self):
        path = self.source()
        target = self.root / "derived"
        build_retrieval_corpus([path], target)
        self.assertFalse(validate_retrieval_sources(target)["stale"])
        path.with_suffix(".meta.jsonl").write_text("")
        self.assertTrue(validate_retrieval_sources(target)["stale"])
        build_retrieval_corpus([path], target)
        self.assertFalse(validate_retrieval_sources(target)["stale"])
        (target / "knowledge_base.jsonl").write_text("")
        self.assertTrue(validate_retrieval_sources(target)["stale"])

    def test_limits_and_different_versions(self):
        path = self.source("first", "abcdef")
        second = self.source("second", "abcdeg")
        for limit, overlap in ((0, 0), (5, 5), (5, -1)):
            with self.assertRaises(ValueError):
                build_retrieval_corpus([path], self.root / "derived", chunk_chars=limit, overlap_chars=overlap)
        result = build_retrieval_corpus([path, second], self.root / "derived", chunk_chars=6, overlap_chars=5)
        self.assertEqual(result["counts"]["output_chunks"], 2)

    def test_interrupted_first_publication_can_recover(self):
        path = self.source()
        target = self.root / "derived"
        with patch("rag_indexing.initialize_rag_manifest", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                build_retrieval_corpus([path], target)
        # Canonical bytes may already be complete but the RAG manifest is not.
        build_retrieval_corpus([path], target)
        self.assertTrue((target / "knowledge_base.rag.json").exists())


if __name__ == "__main__":
    unittest.main()
