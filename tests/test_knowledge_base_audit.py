from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from book_pipeline import split_text, write_knowledge_base
from knowledge_base_audit import (
    audit_knowledge_base,
    audit_rows,
    chapter_expectation,
    chapter_expectations,
    reader_body,
)


SOURCE_NAME = "book.epub"


def _rows_for(chapters: list[dict], *, source_name: str = SOURCE_NAME) -> list[dict]:
    rows: list[dict] = []
    for chapter in chapters:
        body = chapter["body"]
        for chunk_index, chunk in enumerate(split_text(body, 4000), start=1):
            if not chunk.strip():
                continue
            rows.append(
                {
                    "id": hashlib.sha1(
                        (
                            f"{source_name}:{chapter['id']}:"
                            f"{chapter['row_source']}:{chunk_index}"
                        ).encode("utf-8")
                    ).hexdigest(),
                    "title": chapter["title"],
                    "chapter_id": chapter["id"],
                    "chapter_order": chapter["order"],
                    "content": chunk,
                }
            )
    return rows


def _codes(issues: list[dict]) -> set[str]:
    return {str(issue["code"]) for issue in issues}


class ReaderBodyTests(unittest.TestCase):
    def test_reader_body_drops_the_validated_document_heading(self) -> None:
        self.assertEqual(reader_body("# 第一章\n\n正文。\n"), "正文。")
        self.assertEqual(reader_body(""), "")
        self.assertEqual(reader_body("# 扉页\n"), "")

    def test_chapter_expectation_carries_the_publisher_view(self) -> None:
        expectation = chapter_expectation(
            chapter_id="ch-1",
            title="第一章",
            order=3,
            markdown="# 第一章\n\n正文。\n",
            reviewed_override=True,
        )
        self.assertEqual(expectation["body"], "正文。")
        self.assertEqual(expectation["row_source"], "reviewed")

    def test_chapter_expectations_pair_manifest_with_markdown(self) -> None:
        expectations = chapter_expectations(
            [
                {
                    "id": "ch-1",
                    "display_title": "第一章",
                    "sequence": 1,
                    "reviewed_override": False,
                }
            ],
            [{"markdown": "# 第一章\n\n正文。\n"}],
        )
        self.assertEqual(
            expectations,
            [
                {
                    "id": "ch-1",
                    "title": "第一章",
                    "order": 1,
                    "body": "正文。",
                    "row_source": "compiled",
                }
            ],
        )


class AuditRowsTests(unittest.TestCase):
    def _chapters(self) -> list[dict]:
        return [
            chapter_expectation(
                chapter_id="cover", title="扉页", order=1, markdown="# 扉页\n"
            ),
            chapter_expectation(
                chapter_id="ch-1",
                title="第一章",
                order=2,
                markdown="# 第一章\n\n" + "甲" * 4100 + "\n",
            ),
        ]

    def test_valid_corpus_passes_and_exempts_empty_chapters(self) -> None:
        chapters = self._chapters()
        rows = _rows_for(chapters)
        self.assertEqual(len(rows), 2)
        issues, metrics = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        self.assertEqual(issues, [])
        self.assertEqual(metrics["chunk_count"], 2)
        self.assertEqual(metrics["covered_chapter_count"], 1)
        self.assertEqual(metrics["content_match_chapter_count"], 1)
        self.assertEqual(metrics["empty_chapter_count"], 1)
        self.assertEqual(metrics["chapter_count"], 2)

    def test_stable_id_drift_is_reported(self) -> None:
        chapters = self._chapters()
        rows = _rows_for(chapters)
        rows[-1]["id"] = hashlib.sha1(b"foreign").hexdigest()
        issues, _ = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        self.assertIn("knowledge_base_stable_ids_mismatch", _codes(issues))

    def test_chapter_without_reader_body_keeps_no_chunk(self) -> None:
        chapters = self._chapters()
        rows = [row for row in _rows_for(chapters) if row["chapter_id"] != "ch-1"]
        issues, _ = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        self.assertIn("knowledge_base_chapter_missing", _codes(issues))

    def test_tampered_chunk_breaks_the_lossless_rejoin(self) -> None:
        chapters = self._chapters()
        rows = _rows_for(chapters)
        rows[-1]["content"] = "篡改过的正文"
        issues, metrics = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        self.assertIn("knowledge_base_chapter_content_mismatch", _codes(issues))
        self.assertEqual(metrics["content_match_chapter_count"], 0)

    def test_structural_damage_uses_the_shared_issue_codes(self) -> None:
        chapters = self._chapters()
        rows = _rows_for(chapters)
        duplicate = dict(rows[0])
        duplicate["extra"] = "not canonical"
        rows.append(duplicate)
        rows.append(
            {
                "id": "not-a-digest",
                "title": "第一章",
                "chapter_id": "ch-1",
                "chapter_order": 1,
                "content": "   ",
            }
        )
        rows.append(
            {
                "id": hashlib.sha1(b"ghost").hexdigest(),
                "title": "幽灵章节",
                "chapter_id": "ghost",
                "chapter_order": 9,
                "content": "正文",
            }
        )
        issues, _ = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        codes = _codes(issues)
        for expected in (
            "knowledge_base_fields_invalid",
            "knowledge_base_id_invalid",
            "knowledge_base_content_empty",
            "knowledge_base_duplicate_ids",
            "knowledge_base_order_mismatch",
            "knowledge_base_row_order_invalid",
            "knowledge_base_unknown_chapter",
            "knowledge_base_stable_ids_mismatch",
        ):
            self.assertIn(expected, codes)

    def test_unknown_chapter_rows_do_not_satisfy_coverage(self) -> None:
        chapters = self._chapters()
        rows = [
            {
                "id": hashlib.sha1(b"x").hexdigest(),
                "title": "幽灵章节",
                "chapter_id": "ghost",
                "chapter_order": 9,
                "content": "正文",
            }
        ]
        issues, _ = audit_rows(rows, source_name=SOURCE_NAME, chapters=chapters)
        codes = _codes(issues)
        self.assertIn("knowledge_base_unknown_chapter", codes)
        self.assertIn("knowledge_base_chapter_missing", codes)


class AuditKnowledgeBaseFileTests(unittest.TestCase):
    def test_file_audit_reports_presence_and_manifest_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            chapters_dir = root / "chapters"
            chapters_dir.mkdir()
            (chapters_dir / "001.md").write_text(
                "# 第一章\n\n正文段落。\n", encoding="utf-8"
            )
            manifest = [
                {
                    "id": "ch-1",
                    "sequence": 1,
                    "display_title": "第一章",
                    "filename": "001.md",
                    "reviewed_override": False,
                }
            ]
            expectations = chapter_expectations(
                manifest,
                [{"markdown": (chapters_dir / "001.md").read_text(encoding="utf-8")}],
            )
            path = root / "knowledge_base.jsonl"
            write_knowledge_base(path, _rows_for(expectations))
            report = audit_knowledge_base(
                path, source_name=SOURCE_NAME, chapters=expectations
            )
            self.assertTrue(report["present"])
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["metrics"]["rag_lexical_status"], "ready")
            self.assertIn(
                report["metrics"]["rag_embedding_status"],
                {"awaiting_provider", "pending", "ready", "disabled"},
            )

            missing = audit_knowledge_base(
                root / "absent.jsonl", source_name=SOURCE_NAME, chapters=expectations
            )
            self.assertFalse(missing["present"])
            self.assertEqual(missing["issues"], [])

            path.write_text("{not json}\n", encoding="utf-8")
            broken = audit_knowledge_base(
                path, source_name=SOURCE_NAME, chapters=expectations
            )
            self.assertIn("knowledge_base_invalid_jsonl", _codes(broken["issues"]))

    def test_missing_rag_manifest_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expectations = [
                chapter_expectation(
                    chapter_id="ch-1", title="第一章", order=1, markdown="# 第一章\n\n正文。\n"
                )
            ]
            path = root / "knowledge_base.jsonl"
            path.write_text(
                json.dumps(_rows_for(expectations)[0], ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            report = audit_knowledge_base(
                path, source_name=SOURCE_NAME, chapters=expectations
            )
            self.assertIn(
                "knowledge_base_rag_manifest_invalid", _codes(report["issues"])
            )
            self.assertEqual(report["metrics"]["rag_lexical_status"], "invalid")


if __name__ == "__main__":
    unittest.main()
