from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import pymupdf

import book_pipeline as book
from tools.english_pdf_kb_plugin import english_ingest as english


class EnglishPdfIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "original.pdf"
        with pymupdf.open() as pdf:
            for value in (
                "Editorial preface for the English source book.",
                "Essay One. A selected English original passage.",
                "Essay Two. Another selected English original passage.",
            ):
                page = pdf.new_page()
                page.insert_text((60, 80), value)
            pdf.save(self.source)
        self.workspace = self.root / "workspace"
        english.prepare({
            "source": str(self.source), "workspace": str(self.workspace),
            "title": "英文原著中文选本", "author": "原书编者",
        })
        manifest = english.read(self.workspace / english.MANIFEST)
        english.extract({"pages": [1, 2, 3]}, self.workspace, manifest)
        self.manifest = manifest

    def translate(self, page: int, text: str) -> None:
        record = next(item for item in book.load_page_records(self.workspace) if item.pdf_page == page)
        record.translated_text = text
        record.translation_source_sha256 = record.effective_text_sha256
        record.translation_provider = "reviewed"
        record.translation_model = "human-checked"
        record.translation_target_language = "zh-CN"
        book.save_page_record(self.workspace, record)

    def plan(self, *, editorial_page: int = 1, clips: list[dict] | None = None) -> dict:
        toc = {
            "page_offset": 0, "printed_pages_per_pdf_page": 1, "toc_pdf_pages": [1],
            "entries": [
                {"id": "editor", "index": "", "title": "Editorial Preface", "level": 1,
                 "kind": "frontmatter", "printed_page": editorial_page, "pdf_page": editorial_page},
                {"id": "one", "index": "", "title": "Essay One", "level": 1,
                 "kind": "section", "printed_page": 2, "pdf_page": 2},
                {"id": "two", "index": "", "title": "Essay Two", "level": 1,
                 "kind": "section", "printed_page": 3, "pdf_page": 3},
            ],
        }
        english.write(self.workspace / "toc.json", toc)
        return {
            "schema_version": 1, "source_sha256": self.manifest["source_sha256"],
            "toc_sha256": english.sha(self.workspace / "toc.json"),
            "publication_title": "英文原著中文选本",
            "entries": [
                {"id": "editor", "role": "editorial"},
                {"id": "one", "role": "selection", "reader_title": "选文一",
                 "source_review": "对照 PDF 第2页确认正文起点与选文角色"},
                {"id": "two", "role": "selection", "reader_title": "选文二",
                 "source_review": "对照 PDF 第3页确认正文起点与选文角色"},
            ],
            "clips": clips or [],
        }

    def test_prepare_extract_is_idempotent_and_source_bound(self) -> None:
        self.assertEqual(english.status({"workspace": str(self.workspace)})["checkpoint_pages"], 3)
        result = english.extract({"pages": [3, 1]}, self.workspace, self.manifest)
        self.assertEqual(result["written_pages"], [])
        self.assertEqual(result["reused_pages"], [3, 1])
        with self.assertRaisesRegex(english.GateError, "unique"):
            english.extract({"pages": [2, 2]}, self.workspace, self.manifest)
        with self.assertRaisesRegex(english.GateError, "another source"):
            english.prepare({
                "source": str(self.source), "workspace": str(self.workspace),
                "title": "另一书名", "author": "原书编者",
            })

    def test_reviewed_toc_import_uses_no_model_and_exact_page_ranges(self) -> None:
        self.plan()
        toc_source = self.root / "reviewed-toc.json"
        english.write(toc_source, english.read(self.workspace / "toc.json"))
        (self.workspace / "toc.json").unlink()
        result = english._run_pipeline(
            self.workspace, self.manifest, {"toc_file": str(toc_source)}, "toc",
        )
        self.assertEqual(result["method"], "reviewed-file")
        self.assertEqual(result["entries"], 3)
        self.assertEqual(english._ranges([3, 1]), [(1, 1), (3, 3)])

    def test_plan_requires_reviewed_shared_editorial_boundary(self) -> None:
        self.translate(2, "编者说明。\n\n选文一\n\n甲段正文。")
        self.translate(3, "选文二\n\n乙段正文。")
        plan = self.plan(editorial_page=2)
        path = self.root / "plan.json"
        english.write(path, plan)
        with self.assertRaisesRegex(english.GateError, "keep_from"):
            english.set_plan({"plan_file": str(path)}, self.workspace, self.manifest)
        page = next(item for item in book.load_page_records(self.workspace) if item.pdf_page == 2)
        plan["clips"] = [{
            "pdf_page": 2, "translated_sha256": english.text_sha(page.translated_text),
            "keep_from": "选文一",
            "source_review": "源页第2页编者说明在选文标题之前，已核对版面",
        }]
        english.write(path, plan)
        result = english.set_plan({"plan_file": str(path)}, self.workspace, self.manifest)
        self.assertEqual(result["roles"]["selection"], 2)
        records, ledger = english._compiled_records(self.workspace, plan)
        self.assertEqual(next(item for item in records if item.pdf_page == 2).translated_text,
                         "选文一\n\n甲段正文。")
        self.assertTrue(ledger[0]["removed_prefix"].startswith("编者说明"))
        original = next(item for item in book.load_page_records(self.workspace) if item.pdf_page == 2)
        self.assertTrue(original.translated_text.startswith("编者说明"))

    def test_compiler_keeps_editorial_out_of_canonical_reader_corpus(self) -> None:
        self.translate(2, "选文一\n\n甲段正文。")
        self.translate(3, "选文二\n\n乙段正文。")
        plan = self.plan()
        path = self.root / "plan.json"
        english.write(path, plan)
        english.set_plan({"plan_file": str(path)}, self.workspace, self.manifest)
        candidate, chapters, rows, _toc, _plan = english._compile(self.workspace, self.manifest)
        self.assertEqual(len(chapters), 2)
        self.assertTrue(rows)
        self.assertTrue(all(set(row) == {"id", "title", "chapter_id", "chapter_order", "content"} for row in rows))
        self.assertNotIn("Editorial Preface", "\n".join(row["content"] for row in rows))
        self.assertEqual(len(english.read(candidate / "audit" / "editorial-material-index.json")["items"]), 1)
        self.assertFalse(english.status({"workspace": str(self.workspace)})["release_ready"])

    def test_small_book_full_release_and_lexical_registration(self) -> None:
        self.translate(2, "选文一\n\n甲段正文。")
        self.translate(3, "选文二\n\n乙段正文。")
        path = self.root / "plan.json"
        english.write(path, self.plan())
        english.set_plan({"plan_file": str(path)}, self.workspace, self.manifest)
        built = english.publish(self.workspace, self.manifest)
        self.assertEqual(built["chapters"], 2)
        reused = english.publish(self.workspace, english.read(self.workspace / english.MANIFEST))
        self.assertEqual(reused["candidate"], built["candidate"])
        self.assertTrue(reused["reused"])
        manifest = english.read(self.workspace / english.MANIFEST)
        verified = english.verify(self.workspace, manifest)
        self.assertTrue(verified["release_ready"], verified["failed_checks"])
        self.assertEqual(verified["summary"]["passed"], 16)
        candidate = Path(verified["candidate"])
        review_path = self.root / "layout-review.json"
        english.write(review_path, {
            "docx_sha256": english.sha(next(candidate.glob("*.docx"))),
            "reviewer": "test reviewer",
            "checked_pages": [1, 2],
            "note": "Synthetic cover and first chapter were rendered and checked.",
        })
        registered = english.register(
            {"layout_review_file": str(review_path), "embedding_mode": "off"},
            self.workspace, english.read(self.workspace / english.MANIFEST),
        )
        self.assertEqual(registered["lexical_status"], "ready")
        self.assertTrue(english.status({"workspace": str(self.workspace)})["release_ready"])
        chapter = next((candidate / "chapters").glob("*.md"))
        chapter.write_text(chapter.read_text(encoding="utf-8") + "额外字", encoding="utf-8")
        self.assertFalse(english.status({"workspace": str(self.workspace)})["verification_current"])


if __name__ == "__main__":
    unittest.main()
