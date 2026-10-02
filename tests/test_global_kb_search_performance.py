"""Search contract regressions for SQL ranking and short-term posting lookup."""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import global_knowledge_base as kb


class IndexedSearchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.database = self.root / "index.sqlite3"

    def workspace(self, name, contents, *, titles=None):
        folder = self.outputs / name
        (folder / "chapters").mkdir(parents=True)
        (folder / "chapters.json").write_text(json.dumps([{
            "id": "chapter-1", "sequence": 1, "display_title": "正文",
            "filename": "001.md",
        }]), encoding="utf-8")
        (folder / "chapters" / "001.md").write_text("# 正文\n\n正文。\n", encoding="utf-8")
        rows = [{
            "id": hashlib.sha1(f"{name}:{index}".encode()).hexdigest(),
            "title": titles[index] if titles else "正文",
            "chapter_id": "chapter-1", "chapter_order": index + 1,
            "content": content,
        } for index, content in enumerate(contents)]
        (folder / "knowledge_base.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8")
        return rows

    def sync(self):
        kb.sync_outputs(self.outputs, self.database, require_chinese=False)

    def test_pair_lookup_does_not_cross_punctuation_or_content_columns(self):
        rows = self.workspace("book", ["自然现象。", "自，然现象。", "然现象。"],
                              titles=["正文", "正文", "自"])
        self.sync()
        hits = kb.search("自然", db_path=self.database, per_book_cap=0)
        self.assertEqual([hit["source_row_id"] for hit in hits], [rows[0]["id"]])

    def test_single_char_and_multiple_short_words_keep_substring_semantics(self):
        rows = self.workspace("book", ["自然主义正式正文。", "自，然主义正式正文。",
                                      "自然主义正文。"])
        self.sync()
        hits = kb.search("自然 正式", db_path=self.database, per_book_cap=0)
        self.assertEqual([hit["source_row_id"] for hit in hits], [rows[0]["id"]])
        hits = kb.search("自 正", db_path=self.database, per_book_cap=0)
        self.assertEqual({hit["source_row_id"] for hit in hits}, {row["id"] for row in rows})

    def test_kana_and_hangul_short_words_are_indexed(self):
        self.workspace("book", ["かなの文章。 한글 본문。", "か，な。 한 글。"])
        self.sync()
        for query in ("かな", "한글"):
            with self.subTest(query=query):
                hits = kb.search(query, db_path=self.database, per_book_cap=0)
                self.assertEqual(len(hits), 1)
                self.assertIn(query, hits[0]["content"])

    def test_workspace_name_and_title_remain_searchable(self):
        self.workspace("自然", ["这是一段正文。"])
        self.workspace("other", ["这也是正文。"], titles=["自然"])
        self.sync()
        hits = kb.search("自然", db_path=self.database)
        self.assertEqual({hit["workspace"] for hit in hits}, {"自然", "other"})

    def test_punctuation_only_query_keeps_literal_fallback(self):
        self.workspace("book", ["正文……标记。", "普通正文。"])
        self.sync()
        hits = kb.search("……", db_path=self.database, per_book_cap=0)
        self.assertEqual(len(hits), 1)

    def test_short_index_keeps_postings_without_storing_token_text(self):
        self.workspace("book", ["自然主义正式正文。"])
        self.sync()
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute("SELECT rowid, tokens FROM chunks_short_fts").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(tokens is None for _rowid, tokens in rows))
        self.assertTrue(kb.search("自然", db_path=self.database))

    def test_sql_cap_matches_uncapped_order_before_book_limit(self):
        for name, count in (("book_a", 30), ("book_b", 4), ("book_c", 3)):
            self.workspace(name, ["资本主义与自然的发展。"] * count)
        self.sync()
        for query in ("资本主义", "自然"):
            all_hits = kb.search(query, db_path=self.database, limit=100, per_book_cap=0)
            for cap in (1, 2):
                with self.subTest(query=query, cap=cap):
                    counts = Counter()
                    expected = []
                    for hit in all_hits:
                        if counts[hit["workspace"]] < cap:
                            expected.append((hit["id"], hit["score"]))
                            counts[hit["workspace"]] += 1
                        if len(expected) == 5:
                            break
                    actual = kb.search(query, db_path=self.database, limit=5, per_book_cap=cap)
                    self.assertEqual([(hit["id"], hit["score"]) for hit in actual], expected)

    def test_short_query_uses_match_index_and_cap_fetches_only_final_rows(self):
        self.workspace("book", ["自然主义正式正文。"] * 30)
        self.sync()
        statements = []
        connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(kb.sqlite3, "connect", side_effect=traced_connect):
            hits = kb.search("自然", db_path=self.database, limit=2)
        self.assertEqual(len(hits), 1)
        query = next(sql for sql in statements if sql.startswith("WITH scored"))
        self.assertIn("chunks_short_fts MATCH", query)
        self.assertIn("ROW_NUMBER()", query)
        self.assertIn("LIMIT 2", query)
        with closing(connect(self.database)) as connection:
            plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
        self.assertTrue(any("VIRTUAL TABLE INDEX" in item and ":M" in item for item in plan), plan)


if __name__ == "__main__":
    unittest.main()
