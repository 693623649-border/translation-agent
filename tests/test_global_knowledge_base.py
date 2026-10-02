import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import closing
from pathlib import Path

from global_knowledge_base import (
    _assert_sources_stable,
    _stat,
    evaluate_retrieval,
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
            # The global sync validates the sidecar against the corpus bytes,
            # so the digest must be real; a stale one aborts the sync.
            "documents_sha256": hashlib.sha256(
                (self.folder / "knowledge_base.jsonl").read_bytes()).hexdigest(),
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


class _WorkspaceTestCase(unittest.TestCase):
    """Shared scaffold: workspaces with a knowledge base and optional audits."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.outputs = self.root / "outputs"
        self.outputs.mkdir()
        self.db = self.root / "global.sqlite3"

    def _workspace(self, name, rows, *, report=None):
        folder = self.outputs / name
        (folder / "chapters").mkdir(parents=True)
        (folder / "chapters.json").write_text(json.dumps([{
            "id": "chapter-1", "sequence": 1, "display_title": "第一章",
            "filename": "001.md",
        }], ensure_ascii=False), encoding="utf-8")
        (folder / "chapters" / "001.md").write_text(
            "# 第一章\n\n章节正文。\n", encoding="utf-8")
        with (folder / "knowledge_base.jsonl").open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if report is not None:
            audit = folder / "audit"
            audit.mkdir()
            (audit / "release-report.json").write_text(
                json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return folder

    @staticmethod
    def _row(row_id, content, *, title="第一章", order=1):
        return {"id": hashlib.sha1(row_id.encode()).hexdigest(), "title": title,
                "chapter_id": "chapter-1", "chapter_order": order,
                "content": content}

    @staticmethod
    def _annotation(is_apparatus, weight=None):
        return {"apparatus_kind": "index" if is_apparatus else "body",
                "default_weight": weight if weight is not None
                else (0.25 if is_apparatus else 1.0),
                "is_apparatus": is_apparatus,
                "reason": ("exact_section_label" if is_apparatus
                           else "no_apparatus_signal")}

    def _sidecar(self, folder, annotations):
        (folder / "knowledge_base.apparatus.json").write_text(json.dumps({
            "schema_version": 1,
            "documents_sha256": hashlib.sha256(
                (folder / "knowledge_base.jsonl").read_bytes()).hexdigest(),
            "titles_sha256": "0" * 64,
            "annotations": annotations,
        }, ensure_ascii=False), encoding="utf-8")


class ApparatusSidecarValidationTests(_WorkspaceTestCase):
    """An apparatus sidecar that exists but is invalid must abort the sync.

    Silently ignoring a broken sidecar would reset every curated demotion to
    weight 1 with nothing in the output revealing the loss.
    """

    def test_corrupt_sidecar_aborts_and_keeps_previous_database(self):
        folder = self._workspace("book", [self._row("body", "资本论研究生产方式。")])
        sync_outputs(self.outputs, self.db)
        original = self.db.read_bytes()
        (folder / "knowledge_base.apparatus.json").write_text(
            "not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unreadable apparatus sidecar"):
            sync_outputs(self.outputs, self.db)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(status(self.db)["workspace_count"], 1)

    def test_stale_digest_aborts_sync(self):
        folder = self._workspace("book", [self._row("body", "资本论研究生产方式。")])
        (folder / "knowledge_base.apparatus.json").write_text(json.dumps({
            "schema_version": 1, "documents_sha256": "0" * 64,
            "titles_sha256": "0" * 64, "annotations": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(
                ValueError, "does not match knowledge-base bytes"):
            sync_outputs(self.outputs, self.db)

    def test_incomplete_annotations_abort_sync(self):
        folder = self._workspace("book", [self._row("body", "资本论研究生产方式。")])
        self._sidecar(folder, {})  # covers none of the published rows
        with self.assertRaisesRegex(ValueError, "do not cover"):
            sync_outputs(self.outputs, self.db)


class SourceFreshnessTests(_WorkspaceTestCase):
    """verify must notice every file that can change index content or state."""

    def test_release_report_change_invalidates_verify(self):
        self._workspace("book", [self._row("body", "资本论研究生产方式。")],
                        report={"status": "passed"})
        sync_outputs(self.outputs, self.db)
        self.assertTrue(verify_sources(self.db)["current"])
        report = self.outputs / "book" / "audit" / "release-report.json"
        report.write_text(json.dumps({"status": "failed"}), encoding="utf-8")
        audit = verify_sources(self.db)
        self.assertFalse(audit["current"])
        self.assertIn("book/audit/release-report.json",
                      audit["changed_sources"])
        # report_status stays a snapshot of sync time until the next sync.
        self.assertEqual(status(self.db)["workspaces"][0]["report_status"],
                         "passed")

    def test_new_apparatus_sidecar_requires_resync(self):
        folder = self._workspace("book", [self._row("body", "资本论研究生产方式。")])
        sync_outputs(self.outputs, self.db)
        self.assertTrue(verify_sources(self.db)["current"])
        self._sidecar(folder,
                      {hashlib.sha1(b"body").hexdigest(): self._annotation(False)})
        audit = verify_sources(self.db)
        self.assertFalse(audit["current"])
        self.assertIn("book/knowledge_base.apparatus.json", audit["new_sources"])
        sync_outputs(self.outputs, self.db)
        self.assertTrue(verify_sources(self.db)["current"])

    def test_review_decision_change_invalidates_verify(self):
        self._workspace("book", [self._row("body", "资本论研究生产方式。")])
        audit_dir = self.outputs / "book" / "audit"
        audit_dir.mkdir()
        (audit_dir / "review-decisions.jsonl").write_text("{}\n", encoding="utf-8")
        sync_outputs(self.outputs, self.db)
        (audit_dir / "review-decisions.jsonl").write_text(
            '{"id": 1}\n', encoding="utf-8")
        result = verify_sources(self.db)
        self.assertFalse(result["current"])
        self.assertIn("book/audit/review-decisions.jsonl",
                      result["changed_sources"])


class ShortQueryTests(_WorkspaceTestCase):
    """Short CJK words must combine instead of vanishing from the query."""

    def test_two_char_words_match_across_gap(self):
        self._workspace("book", [self._row("body", "自然主义正式正文。")])
        sync_outputs(self.outputs, self.db)
        hits = search("自然 正式", db_path=self.db)
        self.assertEqual(len(hits), 1)
        self.assertIn("自然主义正式正文", hits[0]["excerpt"])

    def test_short_word_does_not_drop_mixed_matches(self):
        """Guard: ANDing short words into trigram queries fragments chapters.

        A chapter query names several aspect words that may span chunks, so
        chunks matching the long words must stay in the pool even when they
        lack the two-character word (measured regression on the local
        fixture: chapter-01 and chapter-16 lost rank 1 this way).
        """
        rows = [
            self._row("a", "自然主义的哲学体系描述。", order=1),
            self._row("b", "形式主义的哲学体系描述。", order=2),
        ]
        self._workspace("book", rows)
        sync_outputs(self.outputs, self.db)
        hits = search("自然 哲学体系", db_path=self.db, per_book_cap=0)
        self.assertEqual(
            {hit["source_row_id"] for hit in hits},
            {hashlib.sha1(b"a").hexdigest(), hashlib.sha1(b"b").hexdigest()})

    def test_short_query_demotes_apparatus_chunk(self):
        rows = [
            self._row("index", "索引：资本 生产 剩余价值 资本 生产 剩余价值。",
                      title="索引", order=1),
            self._row("body", "资本论研究资本主义的生产方式。", order=2),
        ]
        folder = self._workspace("book", rows)
        self._sidecar(folder, {
            hashlib.sha1(b"index").hexdigest(): self._annotation(True),
            hashlib.sha1(b"body").hexdigest(): self._annotation(False),
        })
        sync_outputs(self.outputs, self.db)
        hits = search("资本", db_path=self.db, per_book_cap=0)
        self.assertEqual([hit["source_row_id"] for hit in hits],
                         [hashlib.sha1(b"body").hexdigest(),
                          hashlib.sha1(b"index").hexdigest()])


class PerBookCapTests(_WorkspaceTestCase):
    """The per-book cap must survive the SQL fetch cut."""

    def test_cap_returns_hits_from_every_matching_book(self):
        # 250 matching chunks in one book used to push the only match of the
        # other book past the fetch limit, returning a single hit.
        rows = [self._row(f"a{i}", f"剩余价值的生产方式 {i}。") for i in range(250)]
        self._workspace("many", rows)
        self._workspace("one", [self._row("b0", "剩余价值的生产方式。")])
        sync_outputs(self.outputs, self.db)
        hits = search("剩余价值", db_path=self.db, limit=10)
        self.assertEqual(len(hits), 2)
        self.assertEqual({hit["workspace"] for hit in hits}, {"many", "one"})

    def test_cap_two_keeps_two_per_book(self):
        rows = [self._row(f"a{i}", f"剩余价值的生产方式 {i}。") for i in range(5)]
        self._workspace("many", rows)
        self._workspace("one", [self._row("b0", "剩余价值的生产方式。")])
        sync_outputs(self.outputs, self.db)
        hits = search("剩余价值", db_path=self.db, limit=5, per_book_cap=2)
        self.assertEqual(Counter(hit["workspace"] for hit in hits),
                         Counter({"many": 2, "one": 1}))


@unittest.skipUnless(importlib.util.find_spec("opencc"), "OpenCC is not installed")
class EvaluationGateTests(_WorkspaceTestCase):
    """Extra books must not fail the gate; a shrinking corpus must."""

    def _fixture(self, expected_workspaces):
        return {
            "schema_version": 1, "top_k": 5,
            "expected_workspaces": expected_workspaces,
            "thresholds": {"g": {"hit_at_1": 0.5, "hit_at_5": 0.5}},
            "cases": [{"id": "c1", "group": "g", "query": "剩余价值",
                       "expected_workspace": "booka"}],
        }

    def test_extra_workspaces_do_not_fail_evaluation(self):
        self._workspace("booka", [self._row("a", "剩余价值的生产方式。")])
        self._workspace("bookb", [self._row("b", "形式主义的哲学体系。")])
        sync_outputs(self.outputs, self.db)
        path = self.root / "cases.json"
        path.write_text(json.dumps(self._fixture(1)), encoding="utf-8")
        self.assertTrue(evaluate_retrieval(path, db_path=self.db)["passed"])

    def test_missing_workspaces_fail_evaluation(self):
        self._workspace("booka", [self._row("a", "剩余价值的生产方式。")])
        sync_outputs(self.outputs, self.db)
        path = self.root / "cases.json"
        path.write_text(json.dumps(self._fixture(5)), encoding="utf-8")
        result = evaluate_retrieval(path, db_path=self.db)
        self.assertFalse(result["passed"])
        self.assertTrue(any("workspace_count" in failure
                            for failure in result["failures"]))


class ChineseGateTests(_WorkspaceTestCase):
    """Reader-tier chunks must be Chinese before they enter the global index."""

    JAPANESE = ("漱石論集成における「畏怖」の概念を、内側から見た生の問題として"
                "もう一度読み直す必要があるだろう。")

    def test_foreign_kb_aborts_sync_and_keeps_previous_database(self):
        self._workspace("book", [self._row("body", self.JAPANESE)])
        sync_outputs(self.outputs, self.db, require_chinese=False)
        original = self.db.read_bytes()
        with self.assertRaisesRegex(ValueError,
                                    "Chinese-language quality gate"):
            sync_outputs(self.outputs, self.db)
        self.assertEqual(self.db.read_bytes(), original)
        self.assertEqual(status(self.db)["chinese_gate"], "allowed")

    def test_allow_foreign_syncs_and_records_bypass(self):
        self._workspace("book", [self._row("body", self.JAPANESE)])
        result = sync_outputs(self.outputs, self.db, require_chinese=False)
        self.assertEqual(result["workspaces"], 1)
        self.assertEqual(status(self.db)["chinese_gate"], "allowed")
        self.assertEqual(status(self.db)["chunks_by_kind"]["knowledge_base"], 1)

    def test_chinese_kb_passes_gate(self):
        self._workspace("book", [self._row("body", "资本论研究资本主义的生产方式。")])
        result = sync_outputs(self.outputs, self.db)
        self.assertEqual(result["workspaces"], 1)
        self.assertEqual(status(self.db)["chinese_gate"], "enforced")

    def test_reference_chunk_is_exempt(self):
        listing = "\n".join(
            f"柄谷行人 (Karatani, Kojin) {page}" for page in range(20, 44)
        )
        self._workspace("book", [self._row("index", listing, title="术语索引")])
        result = sync_outputs(self.outputs, self.db)
        self.assertEqual(result["workspaces"], 1)
        self.assertEqual(status(self.db)["chinese_gate"], "enforced")

    def test_foreign_markdown_fallback_is_gated(self):
        folder = self.outputs / "fallback"
        (folder / "chapters").mkdir(parents=True)
        (folder / "chapters.json").write_text(json.dumps([{
            "id": "chapter-1", "sequence": 1, "display_title": "第一章",
            "filename": "001.md",
        }], ensure_ascii=False), encoding="utf-8")
        (folder / "chapters" / "001.md").write_text(
            "# 第一章\n\n" + self.JAPANESE, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fallback"):
            sync_outputs(self.outputs, self.db)


class SyncStabilityTests(unittest.TestCase):
    """A source that changes mid-sync must fail the sync, not the index."""

    def test_stability_check_detects_mid_sync_change(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.json"
            path.write_text("{}", encoding="utf-8")
            recorded = _stat(path)
            _assert_sources_stable([recorded])
            path.write_text('{"changed": true}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError,
                                        "Sources changed during sync"):
                _assert_sources_stable([recorded])


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
