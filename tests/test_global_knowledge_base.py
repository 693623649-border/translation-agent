import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from global_knowledge_base import search, status, sync_outputs, verify_sources


class GlobalKnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.db = self.root / "global.sqlite3"

    def _workspace(self, name, *, kb=True, legacy=False, page=False):
        folder = self.outputs / name
        chapters = folder / "chapters"
        chapters.mkdir(parents=True)
        (folder / "chapters.json").write_text(json.dumps([{
            "id": "chapter-1", "sequence": 1, "display_title": "第一章",
            "filename": "001.md",
        }], ensure_ascii=False), encoding="utf-8")
        (chapters / "001.md").write_text("# 第一章\n\n自然主义章节正文。\n", encoding="utf-8")
        if kb:
            row = {
                "id": hashlib.sha1(name.encode()).hexdigest(),
                "title": "第一章", "chapter_id": "chapter-1",
                "chapter_order": 1, "content": "自然主义正式正文。",
            }
            if legacy:
                row.update({"source_pdf": "original.pdf", "pdf_page_start": 7})
            (folder / "knowledge_base.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        if page:
            pages = folder / "pages"
            pages.mkdir()
            (pages / "page_0001.json").write_text(json.dumps({
                "pdf_page": 1, "text": "誤認識の原文。", "proofread_text": "校正後の原文。",
                "translated_text": "译文页内容。", "language": "ja",
            }, ensure_ascii=False), encoding="utf-8")
        (folder / f"{name}.docx").write_bytes(b"fake asset")
        return folder

    def test_imports_every_workspace_and_separates_search_tiers(self):
        canonical = self._workspace("canonical", page=True)
        legacy = self._workspace("legacy", legacy=True)
        fallback = self._workspace("fallback", kb=False)
        before = (legacy / "knowledge_base.jsonl").read_bytes()
        result = sync_outputs(self.outputs, self.db)
        self.assertEqual(result["workspaces"], 3)
        self.assertEqual(status(self.db)["integrity"], "ok")
        self.assertTrue(verify_sources(self.db)["current"])
        self.assertEqual((legacy / "knowledge_base.jsonl").read_bytes(), before)
        self.assertEqual(len(search("正式正文", db_path=self.db)), 2)
        self.assertEqual(search("章节正文", db_path=self.db)[0]["workspace"], "fallback")
        self.assertEqual(search("校正後", db_path=self.db), [])
        page_hit = search("校正後", db_path=self.db, scope="pages")[0]
        self.assertEqual(page_hit["kind"], "source_page")
        self.assertEqual(search("誤認識", db_path=self.db, scope="pages")[0]["kind"], "raw_ocr")
        self.assertEqual(search("译文页", db_path=self.db, scope="pages")[0]["kind"], "page_translation")
        self.assertEqual(search("章节正文", db_path=self.db, scope="archive")[0]["kind"], "chapter_snapshot")
        self.assertEqual(search("自然", db_path=self.db, workspace="fallback")[0]["workspace"], "fallback")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT count(*) FROM source_files").fetchone()[0], 9)
        self.assertTrue(canonical.exists())
        self.assertTrue(fallback.exists())

    def test_failed_sync_keeps_previous_complete_database(self):
        folder = self._workspace("book")
        sync_outputs(self.outputs, self.db)
        original = self.db.read_bytes()
        (folder / "knowledge_base.jsonl").write_text("not json\n", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            sync_outputs(self.outputs, self.db)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(status(self.db)["workspace_count"], 1)
        self.assertFalse(verify_sources(self.db)["current"])

    @unittest.skipUnless(importlib.util.find_spec("opencc"), "OpenCC is not installed")
    def test_simplified_query_finds_traditional_source(self):
        folder = self._workspace("traditional")
        path = folder / "knowledge_base.jsonl"
        row = json.loads(path.read_text(encoding="utf-8"))
        row["content"] = "臺灣兒少NGO關注人口販賣與社會規訓。"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        sync_outputs(self.outputs, self.db)
        hits = search("台湾儿少NGO 人口贩卖", db_path=self.db)
        self.assertEqual(hits[0]["workspace"], "traditional")


class ApparatusDemotionTests(unittest.TestCase):
    """The per-book apparatus sidecar must keep index/TOC chunks out of rank 1."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.db = self.root / "global.sqlite3"
        self.folder = self.outputs / "book"
        (self.folder / "chapters").mkdir(parents=True)
        (self.folder / "chapters.json").write_text(json.dumps([{
            "id": "chapter-1", "sequence": 1, "display_title": "第一章",
            "filename": "001.md",
        }], ensure_ascii=False), encoding="utf-8")
        (self.folder / "chapters" / "001.md").write_text("# 第一章\n\n正文。\n", encoding="utf-8")
        # An index repeats every headword, so it is the strongest lexical match
        # for a concept query unless the apparatus weight demotes it.
        self.body_id = hashlib.sha1(b"body-row").hexdigest()
        self.index_id = hashlib.sha1(b"index-row").hexdigest()
        rows = [
            {"id": self.body_id, "title": "第一章", "chapter_id": "chapter-1", "chapter_order": 1,
             "content": "资本论研究资本主义的生产方式，以及和它相适应的生产关系和交换关系；"
                        "剩余价值的生产是资本积累的前提，而资本积累又反过来扩大生产。"},
            {"id": self.index_id, "title": "索引", "chapter_id": "chapter-1", "chapter_order": 2,
             "content": "索引：资本 生产 剩余价值 资本 生产 剩余价值 资本 生产 剩余价值。"},
        ]
        with (self.folder / "knowledge_base.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _sidecar(self, weight=0.25):
        (self.folder / "knowledge_base.apparatus.json").write_text(json.dumps({
            "schema_version": 1,
            "documents_sha256": "0" * 64,
            "titles_sha256": "0" * 64,
            "annotations": {
                self.body_id: {"apparatus_kind": "body", "default_weight": 1.0,
                               "is_apparatus": False, "reason": "no_apparatus_signal"},
                self.index_id: {"apparatus_kind": "index", "default_weight": weight,
                                "is_apparatus": True, "reason": "exact_section_label"},
            },
        }, ensure_ascii=False), encoding="utf-8")

    def _query(self):
        return search("资本 生产 剩余价值", db_path=self.db, per_book_cap=2)

    def _weights(self):
        with closing(sqlite3.connect(self.db)) as db:
            return dict(db.execute(
                "SELECT source_row_id, apparatus_weight FROM chunks "
                "WHERE source_row_id IS NOT NULL AND kind = 'knowledge_base'"))

    def test_sidecar_demotes_index_below_body_prose(self):
        self._sidecar()
        sync_outputs(self.outputs, self.db)
        self.assertEqual(self._weights(),
                         {self.body_id: 1.0, self.index_id: 0.25})
        self.assertEqual(self._query()[0]["source_row_id"], self.body_id)

    def test_without_sidecar_the_index_would_win(self):
        """Guards the test above: the demotion, not the fixture, decides rank 1."""
        sync_outputs(self.outputs, self.db)
        self.assertEqual(self._weights(), {self.body_id: 1.0, self.index_id: 1.0})
        self.assertEqual(self._query()[0]["source_row_id"], self.index_id)

    def test_stale_index_schema_is_reported_not_crashed(self):
        self._sidecar()
        sync_outputs(self.outputs, self.db)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("PRAGMA user_version = 1")
            db.commit()
        with self.assertRaisesRegex(ValueError, "schema version 1"):
            search("资本", db_path=self.db)
        with self.assertRaisesRegex(ValueError, "schema version 1"):
            status(self.db)


class LocalFixtureTests(unittest.TestCase):
    """The machine-local retrieval fixture must stay self-consistent."""

    PATH = Path(__file__).parent / "fixtures" / "global_kb_retrieval_cases.local.json"

    def test_fixture_covers_every_group_it_declares(self):
        fixture = json.loads(self.PATH.read_text(encoding="utf-8"))
        self.assertEqual(fixture["schema_version"], 1)
        self.assertGreaterEqual(fixture["top_k"], 5)
        self.assertIsInstance(fixture["expected_workspaces"], int)
        groups = {case["group"] for case in fixture["cases"]}
        self.assertEqual(groups, set(fixture["thresholds"]))
        ids = [case["id"] for case in fixture["cases"]]
        self.assertEqual(len(ids), len(set(ids)))
        for case in fixture["cases"]:
            self.assertTrue(case["query"].strip())
            self.assertTrue(case["expected_workspace"].strip())
            for metric, floor in fixture["thresholds"][case["group"]].items():
                self.assertIn(metric, {"hit_at_1", "hit_at_5"})
                self.assertTrue(0.0 <= floor <= 1.0)
            if case["group"] == "chapter":
                self.assertTrue(case.get("expected_chapter_id"))
                self.assertTrue(case.get("workspace_filter"))


if __name__ == "__main__":
    unittest.main()
