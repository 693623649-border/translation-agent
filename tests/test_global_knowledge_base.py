import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
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
        with sqlite3.connect(self.db) as db:
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


if __name__ == "__main__":
    unittest.main()
