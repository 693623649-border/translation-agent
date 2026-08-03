import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import fitz

from book_pipeline import (
    PROOFREAD_PROMPT_VERSION,
    ModelIdentity,
    PageRecord,
    PageStore,
    PhysicalPageOCRText,
    TocEntry,
    build_docx,
    build_epub,
    compile_chapters,
    join_physical_page_texts,
    ocr_pdf,
    proofread_ocr_pages,
    save_page_record,
    translate_non_chinese_pages,
)


def proofread_identity() -> ModelIdentity:
    return ModelIdentity(
        provider="deepseek",
        adapter="openai-chat",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        target_language="ja",
        prompt_version=PROOFREAD_PROMPT_VERSION,
    )


class PhysicalBoundaryCheckpointTests(unittest.TestCase):
    def test_ocr_result_remains_a_string_and_carries_ordered_pages(self) -> None:
        result = PhysicalPageOCRText(("右ページ。", "左ページ。"))

        self.assertIsInstance(result, str)
        self.assertEqual(result, "右ページ。\n\n左ページ。")
        self.assertEqual(result.physical_page_texts, ("右ページ。", "左ページ。"))

    def test_legacy_checkpoint_without_physical_pages_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            page_path = output / "pages/page_0001.json"
            page_path.parent.mkdir(parents=True)
            page_path.write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": "旧 checkpoint 本文",
                        "language": "ja",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            record = PageStore(output).load(1)

            self.assertEqual(record.raw_physical_pages, ("旧 checkpoint 本文",))
            self.assertEqual(record.compile_physical_pages_for(None), ("旧 checkpoint 本文",))

    def test_ocr_pdf_persists_structure_but_page_markdown_has_no_marker(self) -> None:
        class OCR:
            ocr_model = "structured-test"

            def ocr_image(self, image_path: Path) -> tuple[str, str]:
                return PhysicalPageOCRText(("右頁。", "左頁。")), "request"

            def close(self) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            document = fitz.open()
            document.new_page()
            document.save(source)
            document.close()
            output = root / "output"

            records = ocr_pdf(
                source,
                output,
                OCR(),
                start_page=1,
                end_page=1,
                concurrency=1,
                dpi=72,
                max_image_side=800,
                jpeg_quality=80,
                keep_page_images=False,
                force=False,
            )

            self.assertEqual(records[0].physical_page_texts, ["右頁。", "左頁。"])
            checkpoint = json.loads(
                (output / "pages/page_0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint["physical_page_texts"], ["右頁。", "左頁。"])
            page_markdown = (output / "pages/page_0001.md").read_text(
                encoding="utf-8"
            )
            self.assertEqual(page_markdown, "右頁。\n\n左頁。\n")
            self.assertNotIn("physical_page", page_markdown)

    def test_proofread_and_translation_preserve_boundary_without_markers(self) -> None:
        class Proofreader:
            def __init__(self) -> None:
                self.inputs: list[str] = []

            def proofread(self, text: str, *, language: str) -> str:
                self.inputs.append(text)
                return f"校勘：{text}"

        class Translator:
            def __init__(self) -> None:
                self.inputs: list[str] = []

            def translate(
                self,
                text: str,
                *,
                source_language: str,
                target_language: str,
            ) -> str:
                self.inputs.append(text)
                return f"译文：{text}"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw_pages = ["右頁の本文です。", "左頁の本文です。"]
            record = PageRecord(
                10,
                join_physical_page_texts(raw_pages),
                language="ja",
                physical_page_texts=raw_pages,
            )
            save_page_record(output, record)
            proofreader = Proofreader()

            proofread_ocr_pages(
                [record],
                output,
                proofreader,
                language="ja",
                identity=proofread_identity(),
                force=False,
                concurrency=1,
            )

            self.assertEqual(proofreader.inputs, raw_pages)
            self.assertEqual(
                record.proofread_physical_page_texts,
                [f"校勘：{text}" for text in raw_pages],
            )
            translator = Translator()
            translate_non_chinese_pages(
                [record],
                output,
                translator,
                target_language="简体中文",
                source_language="ja",
                translation_provider="deepseek",
                translation_model="deepseek-v4-flash",
                force=False,
                concurrency=1,
            )

            expected_sources = [f"校勘：{text}" for text in raw_pages]
            self.assertEqual(translator.inputs, expected_sources)
            self.assertEqual(
                record.translated_physical_page_texts,
                [f"译文：{text}" for text in expected_sources],
            )
            saved = json.loads(
                (output / "pages/page_0010.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["physical_page_texts"], raw_pages)
            self.assertEqual(len(saved["proofread_physical_page_texts"]), 2)
            self.assertEqual(len(saved["translated_physical_page_texts"]), 2)


class PhysicalBoundaryCompilationTests(unittest.TestCase):
    def test_odd_printed_page_boundary_selects_exact_half_everywhere(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            previous_only = "PREVIOUS_CHAPTER_PHYSICAL_PAGE"
            next_only = "NEXT_CHAPTER_PHYSICAL_PAGE"
            records = [
                PageRecord(page, f"中间正文 {page}")
                for page in range(5, 10)
            ]
            boundary_pages = [
                f"16\n{previous_only}",
                f"17\n第二章\n终点\n{next_only}",
            ]
            records.append(
                PageRecord(
                    10,
                    join_physical_page_texts(boundary_pages),
                    physical_page_texts=boundary_pages,
                )
            )
            payload = {
                "page_offset": 2,
                "printed_pages_per_pdf_page": 2,
                "entries": [
                    TocEntry(
                        "chapter-1",
                        "第一章",
                        "起点",
                        1,
                        "chapter",
                        6,
                        pdf_page=5,
                    ).__dict__,
                    TocEntry(
                        "chapter-2",
                        "第二章",
                        "终点",
                        1,
                        "chapter",
                        17,
                        pdf_page=10,
                    ).__dict__,
                ],
            }

            manifest, rows = compile_chapters(
                root / "source.pdf",
                output,
                records,
                payload,
                granularity="chapter",
            )
            first = (output / "chapters" / manifest[0]["filename"]).read_text(
                encoding="utf-8"
            )
            second = (output / "chapters" / manifest[1]["filename"]).read_text(
                encoding="utf-8"
            )

            self.assertIn(previous_only, first)
            self.assertNotIn(next_only, first)
            self.assertIn(next_only, second)
            self.assertNotIn(previous_only, second)
            for markdown in (first, second):
                self.assertNotIn("source-pdf", markdown)
                self.assertNotIn("PDF_PAGE", markdown)
                self.assertNotIn("pagebreak", markdown)
                self.assertNotIn("physical_page", markdown)

            first_kb = "\n".join(
                row["content"] for row in rows if row["chapter_id"] == "chapter-1"
            )
            second_kb = "\n".join(
                row["content"] for row in rows if row["chapter_id"] == "chapter-2"
            )
            self.assertIn(previous_only, first_kb)
            self.assertNotIn(next_only, first_kb)
            self.assertIn(next_only, second_kb)
            self.assertNotIn(previous_only, second_kb)

            epub_path = output / "book.epub"
            docx_path = output / "book.docx"
            build_epub(
                epub_path,
                output / "chapters",
                manifest,
                book_title="测试书",
                language="zh-CN",
            )
            build_docx(
                docx_path,
                output / "chapters",
                manifest,
                book_title="测试书",
            )
            with zipfile.ZipFile(epub_path) as archive:
                epub_text = "\n".join(
                    archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                    if name.endswith(".xhtml")
                )
            with zipfile.ZipFile(docx_path) as archive:
                docx_text = archive.read("word/document.xml").decode("utf-8")
            for rendered in (epub_text, docx_text):
                self.assertNotIn("source-pdf", rendered)
                self.assertNotIn("PDF_PAGE", rendered)
                self.assertNotIn("pagebreak", rendered)
                self.assertNotIn("physical_page", rendered)


if __name__ == "__main__":
    unittest.main()
