import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from translation_agent.knowledge import global_knowledge_base as kb_index

from translation_agent.knowledge.global_knowledge_base import (
    _report_status,
    search,
    status,
    sync_outputs,
    verify_sources,
)


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


class IncrementalSyncTests(unittest.TestCase):
    setUp = GlobalKnowledgeBaseTests.setUp
    _workspace = GlobalKnowledgeBaseTests._workspace

    def _sync(self, expected):
        with patch.object(kb_index, "_ingest_workspace", wraps=kb_index._ingest_workspace) as ingest:
            result = sync_outputs(self.outputs, self.db)
        self.assertEqual([call.args[2].name for call in ingest.call_args_list], expected)
        self.assertEqual(result["updated_workspaces"], expected)
        self.assertEqual(status(self.db)["integrity"], "ok")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chunks").fetchone(),
                             db.execute("SELECT count(*) FROM chunks_fts").fetchone())
            self.assertEqual(db.execute("SELECT id FROM chunks EXCEPT SELECT id FROM chunks_fts").fetchall(), [])
            self.assertEqual(db.execute("SELECT id FROM chunks_fts EXCEPT SELECT id FROM chunks").fetchall(), [])
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
        return result

    def test_five_books_cold_unchanged_and_single_book_change(self):
        for index in range(5):
            folder = self.outputs / f"book{index}"
            folder.mkdir()
            (folder / "knowledge_base.jsonl").write_text("".join(
                json.dumps({"id": hashlib.sha1(f"{index}:{row}".encode()).hexdigest(),
                            "title": "title", "chapter_id": "chapter-1", "chapter_order": 1,
                            "content": f"original content book{index} paragraph {row}"}) + "\n"
                for row in range(100)
            ), encoding="utf-8")
        with patch.object(kb_index, "_insert_chunk", wraps=kb_index._insert_chunk) as insert:
            cold = self._sync([f"book{i}" for i in range(5)])
        self.assertEqual(insert.call_count, 500)
        self.assertTrue(cold["rebuilt"])
        before, stamp = self.db.read_bytes(), self.db.stat().st_mtime_ns
        with patch.object(kb_index, "_create_schema") as schema, patch.object(kb_index, "_insert_chunk") as insert:
            hot = self._sync([])
        schema.assert_not_called()
        insert.assert_not_called()
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(self.db.stat().st_mtime_ns, stamp)
        self.assertEqual(cold["counts"], hot["counts"])
        path = self.outputs / "book0" / "knowledge_base.jsonl"
        content = path.read_text(encoding="utf-8").replace("original", "revised!", 1)
        old_stat = path.stat()
        path.write_text(content, encoding="utf-8")
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        with patch.object(kb_index, "_create_schema") as schema, patch.object(kb_index, "_insert_chunk", wraps=kb_index._insert_chunk) as insert:
            self._sync(["book0"])
        schema.assert_not_called()
        self.assertEqual(insert.call_count, 100)
        self.assertEqual(search("revised", db_path=self.db)[0]["workspace"], "book0")
        self.assertEqual(status(self.db)["workspace_count"], 5)
        self.assertTrue(verify_sources(self.db)["current"])

    def _manifest_chapter(self, filename):
        folder = self._workspace("book", kb=False)
        self._workspace("untouched")
        chapter = folder / "chapters" / filename
        chapter.parent.mkdir(parents=True, exist_ok=True)
        (folder / "chapters" / "001.md").replace(chapter)
        chapter.write_text("# One\n\nOLDMARKER alpha text.\n", encoding="utf-8")
        manifest = folder / "chapters.json"
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        rows[0]["filename"] = filename
        manifest.write_text(json.dumps(rows), encoding="utf-8")
        return chapter

    def test_nested_manifest_chapter_changes_refresh_only_its_workspace(self):
        chapter = self._manifest_chapter("part/one.md")
        self._sync(["book", "untouched"])
        self._sync([])
        self.assertEqual(len(search("OLDMARKER", db_path=self.db)), 1)
        chapter.write_text("# One\n\nNEWMARKER beta text.\n", encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(search("OLDMARKER", db_path=self.db), [])
        self.assertEqual(len(search("NEWMARKER", db_path=self.db)), 1)
        self.assertTrue(verify_sources(self.db)["current"])

    def test_manifest_chapter_with_other_extension_is_tracked(self):
        chapter = self._manifest_chapter("part/one.txt")
        self._sync(["book", "untouched"])
        self._sync([])
        chapter.write_text("# One\n\nNEWMARKER beta text.\n", encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(search("OLDMARKER", db_path=self.db), [])
        self.assertEqual(len(search("NEWMARKER", db_path=self.db)), 1)
        self.assertTrue(verify_sources(self.db)["current"])

    def _symlink(self, link, target):
        try:
            link.symlink_to(target)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                self.skipTest(f"Windows symbolic-link privilege is unavailable: {exc}")
            raise

    def test_same_content_external_chapter_link_is_rejected_before_hot_cache_hit(self):
        folder = self._workspace("book", kb=False)
        self._sync(["book"])
        original = self.db.read_bytes()
        chapter = folder / "chapters" / "001.md"
        outside = self.root / "outside.md"
        outside.write_bytes(chapter.read_bytes())
        chapter.unlink()
        self._symlink(chapter, outside)
        with patch.object(kb_index, "_ingest_workspace") as ingest:
            with self.assertRaisesRegex(ValueError, "Missing or unsafe chapter file"):
                sync_outputs(self.outputs, self.db)
        ingest.assert_not_called()
        self.assertEqual(self.db.read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "Missing or unsafe chapter file"):
            sync_outputs(self.outputs, self.root / "fresh.sqlite3")

    def test_same_content_external_reviewed_link_is_rejected_before_hot_cache_hit(self):
        folder = self._workspace("book", kb=False)
        reviewed = folder / "reviewed_chapters" / "chapter-1.md"
        reviewed.parent.mkdir()
        reviewed.write_bytes((folder / "chapters" / "001.md").read_bytes())
        self._sync(["book"])
        original = self.db.read_bytes()
        outside = self.root / "outside.md"
        outside.write_bytes(reviewed.read_bytes())
        reviewed.unlink()
        self._symlink(reviewed, outside)
        with patch.object(kb_index, "_ingest_workspace") as ingest:
            with self.assertRaisesRegex(ValueError, "Unsafe reviewed chapter file"):
                sync_outputs(self.outputs, self.db)
        ingest.assert_not_called()
        self.assertEqual(self.db.read_bytes(), original)

    def test_same_content_internal_chapter_link_keeps_cold_and_hot_behavior(self):
        folder = self._workspace("book", kb=False)
        self._sync(["book"])
        chapter = folder / "chapters" / "001.md"
        inside = folder / "chapters" / "part" / "body.txt"
        inside.parent.mkdir()
        inside.write_bytes(chapter.read_bytes())
        chapter.unlink()
        self._symlink(chapter, inside)
        original = self.db.read_bytes()
        self._sync([])
        self.assertEqual(self.db.read_bytes(), original)
        fresh = self.root / "fresh.sqlite3"
        result = sync_outputs(self.outputs, fresh)
        self.assertEqual(result["updated_workspaces"], ["book"])
        self.assertEqual(search("章节正文", db_path=self.db)[0]["excerpt"],
                         search("章节正文", db_path=fresh)[0]["excerpt"])

    def test_added_and_removed_books_refresh_fts_and_leave_other_books(self):
        self._workspace("book0")
        self._workspace("book1")
        self._sync(["book0", "book1"])
        self._workspace("book2")
        self._sync(["book2"])
        shutil.rmtree(self.outputs / "book0")
        result = self._sync([])
        self.assertEqual(result["removed_workspaces"], ["book0"])
        self.assertEqual({hit["workspace"] for hit in search("正式正文", db_path=self.db)}, {"book1", "book2"})
        self.assertTrue(verify_sources(self.db)["current"])

    def test_removing_last_book_publishes_empty_index_but_empty_first_sync_still_fails(self):
        with self.assertRaisesRegex(ValueError, "No output workspaces"):
            sync_outputs(self.outputs, self.db)
        self._workspace("book")
        self._sync(["book"])
        shutil.rmtree(self.outputs / "book")
        result = self._sync([])
        self.assertEqual(result["removed_workspaces"], ["book"])
        self.assertEqual(result["workspaces"], 0)
        self.assertEqual(search("正式正文", db_path=self.db), [])
        self.assertTrue(verify_sources(self.db)["current"])
        before = self.db.read_bytes()
        self._sync([])
        self.assertEqual(self.db.read_bytes(), before)

    def test_chapter_page_and_asset_add_change_delete_refresh_only_their_book(self):
        folder = self._workspace("book", kb=False, page=True)
        self._workspace("untouched")
        self._sync(["book", "untouched"])
        chapter = folder / "chapters" / "001.md"
        chapter.write_text("# 第一章\n\n修订章节内容。", encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(search("修订章节", db_path=self.db)[0]["workspace"], "book")
        manifest = folder / "chapters.json"
        rows = json.loads(manifest.read_text(encoding="utf-8"))
        rows.append({"id": "chapter-2", "sequence": 2, "title": "第二章", "filename": "002.md"})
        manifest.write_text(json.dumps(rows), encoding="utf-8")
        second = folder / "chapters" / "002.md"
        second.write_text("# 第二章\n\n新增章节内容。", encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(len(search("新增章节", db_path=self.db)), 1)
        second.unlink()
        manifest.write_text(json.dumps(rows[:1]), encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(search("新增章节", db_path=self.db), [])
        reviewed = folder / "reviewed_chapters" / "chapter-1.md"
        reviewed.parent.mkdir()
        reviewed.write_text("# 第一章\n\n审定存档正文。", encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(len(search("审定存档", db_path=self.db, scope="archive")), 1)
        reviewed.unlink()
        self._sync(["book"])
        self.assertEqual(search("审定存档", db_path=self.db, scope="archive"), [])
        page = folder / "pages" / "page_0001.json"
        page.write_text(json.dumps({"pdf_page": 1, "text": "更新页正文"}), encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(len(search("更新页正文", db_path=self.db, scope="pages")), 1)
        added = folder / "pages" / "page_0002.json"
        added.write_text(json.dumps({"pdf_page": 2, "text": "新增页正文"}), encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(len(search("新增页", db_path=self.db, scope="pages")), 1)
        page.unlink()
        added.unlink()
        self._sync(["book"])
        self.assertEqual(search("更新页正文", db_path=self.db, scope="pages"), [])
        asset = folder / "nested" / "new.png"
        asset.parent.mkdir()
        asset.write_bytes(b"asset-a")
        self._sync(["book"])
        asset.write_bytes(b"asset-b")
        self._sync(["book"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT sha256 FROM assets WHERE path='book/nested/new.png'").fetchone()[0], hashlib.sha256(b"asset-b").hexdigest())
        asset.unlink()
        self._sync(["book"])
        self.assertTrue(verify_sources(self.db)["current"])

    def test_apparatus_sidecar_added_changed_and_removed_updates_weights(self):
        folder = self._workspace("book")
        self._workspace("untouched")
        self._sync(["book", "untouched"])
        row_id = json.loads((folder / "knowledge_base.jsonl").read_text(encoding="utf-8"))["id"]
        sidecar = folder / "knowledge_base.apparatus.json"
        for weight in (0.25, 0.75, None):
            if weight is None:
                sidecar.unlink()
            else:
                sidecar.write_text(json.dumps({"annotations": {row_id: {"is_apparatus": True, "default_weight": weight}}}), encoding="utf-8")
            self._sync(["book"])
            with closing(sqlite3.connect(self.db)) as db:
                actual = db.execute("SELECT apparatus_weight FROM chunks WHERE source_row_id=?", (row_id,)).fetchone()[0]
            self.assertEqual(actual, 1.0 if weight is None else weight)

    def test_release_status_refreshes_on_report_content_and_timestamp_dependencies(self):
        folder = self._workspace("book")
        self._workspace("untouched")
        self._sync(["book", "untouched"])
        audit = folder / "audit"
        audit.mkdir()
        report = audit / "epub-release-report.json"
        report.write_text('{"status":"passed"}', encoding="utf-8")
        self._sync(["book"])

        def saved_status():
            with closing(sqlite3.connect(self.db)) as db:
                return db.execute("SELECT report_status FROM workspaces WHERE name='book'").fetchone()[0]

        self.assertEqual(saved_status(), "passed")
        watched = [folder / "toc.json", audit / "semantic-review.json", audit / "review-decisions.jsonl",
                   folder / "new.epub", folder / "book.docx", folder / "book_带目录.pdf"]
        for item in watched:
            item.write_text("{}", encoding="utf-8")
            stamp = report.stat().st_mtime_ns + 10**9
            os.utime(item, ns=(stamp, stamp))
            self._sync(["book"])
            self.assertEqual(saved_status(), "stale")
            os.utime(report, ns=(stamp + 10**9, stamp + 10**9))
            self._sync(["book"])
            self.assertEqual(saved_status(), "passed")
        canonical = audit / "release-report.json"
        canonical.write_text('{"status":"failed"}', encoding="utf-8")
        self._sync(["book"])
        self.assertEqual(saved_status(), "failed")
        canonical.unlink()
        self._sync(["book"])
        self.assertEqual(saved_status(), "passed")
        report.unlink()
        self._sync(["book"])
        self.assertEqual(saved_status(), "missing")

    def test_incremental_failure_after_deletion_and_ingestion_preserves_old_database(self):
        folder = self._workspace("book")
        self._workspace("untouched")
        self._sync(["book", "untouched"])
        original = self.db.read_bytes()
        (folder / "knowledge_base.jsonl").write_text("broken", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            sync_outputs(self.outputs, self.db)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(len(search("正式正文", db_path=self.db)), 2)
        self.assertEqual(list(self.root.glob(".global-kb-*")), [])

    def test_write_during_ingestion_does_not_publish_mixed_snapshot(self):
        folder = self._workspace("book")
        self._sync(["book"])
        original = self.db.read_bytes()
        path = folder / "knowledge_base.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("正式正文", "新定正文"), encoding="utf-8")
        ingest = kb_index._ingest_workspace

        def changing(db, root, workspace):
            counts = ingest(db, root, workspace)
            path.write_text(path.read_text(encoding="utf-8").replace("新定正文", "再次变更"), encoding="utf-8")
            return counts

        with patch.object(kb_index, "_ingest_workspace", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "changed during sync"):
                sync_outputs(self.outputs, self.db)
        self.assertEqual(self.db.read_bytes(), original)

    def test_schema_normalization_and_sync_version_changes_force_full_rebuild(self):
        self._workspace("book0")
        self._workspace("book1")
        self._sync(["book0", "book1"])
        for kind in ("schema", "normalization", "sync_version", "missing_fingerprints"):
            with self.subTest(kind=kind), closing(sqlite3.connect(self.db)) as db:
                if kind == "schema":
                    db.execute("PRAGMA user_version=1")
                elif kind == "normalization":
                    db.execute("UPDATE meta SET value='nfkc-old' WHERE key='search_normalization'")
                elif kind == "sync_version":
                    db.execute("UPDATE meta SET value='1' WHERE key='sync_version'")
                else:
                    db.execute("DELETE FROM meta WHERE key='workspace_fingerprints'")
                db.commit()
            with patch.object(kb_index, "_create_schema", wraps=kb_index._create_schema) as schema:
                result = self._sync(["book0", "book1"])
            self.assertTrue(result["rebuilt"])
            schema.assert_called_once()
        self._sync([])


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

    PATH = Path(__file__).parent / "fixtures" / "global_kb_retrieval_cases.json"

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


class ReleaseReportDiscoveryTests(unittest.TestCase):
    """EPUB workspaces publish audit/epub-release-report.json instead of the
    unified audit/release-report.json; both must satisfy the cross-book status."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "book"
        (self.workspace / "audit").mkdir(parents=True)

    def _write(self, name, *, status_value="passed"):
        path = self.workspace / "audit" / name
        path.write_text(json.dumps({"status": status_value}), encoding="utf-8")
        return path

    def test_a_workspace_without_any_report_is_missing(self):
        self.assertEqual(_report_status(self.workspace, []), "missing")

    def test_epub_native_report_marks_the_workspace_passed(self):
        self._write("epub-release-report.json")
        self.assertEqual(_report_status(self.workspace, []), "passed")

    def test_canonical_report_takes_precedence_over_the_epub_report(self):
        self._write("epub-release-report.json")
        self._write("release-report.json", status_value="failed")
        self.assertEqual(_report_status(self.workspace, []), "failed")

    def test_reader_output_newer_than_the_epub_report_is_stale(self):
        report = self._write("epub-release-report.json")
        reader = self.workspace / "knowledge_base.jsonl"
        reader.write_text("{}\n", encoding="utf-8")
        stamp = report.stat().st_mtime_ns + 10**9
        os.utime(reader, ns=(stamp, stamp))
        self.assertEqual(_report_status(self.workspace, [reader]), "stale")

    def test_unreadable_epub_report_is_invalid(self):
        (self.workspace / "audit" / "epub-release-report.json").write_text(
            "not json", encoding="utf-8"
        )
        self.assertEqual(_report_status(self.workspace, []), "invalid")


if __name__ == "__main__":
    unittest.main()
