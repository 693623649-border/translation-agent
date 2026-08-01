import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from book_pipeline import (
    ModelIdentity,
    PageRecord,
    TocEntry,
    compile_chapters,
    main,
    save_page_record,
)


class FakeTextClient:
    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
        return "这是中文译文。"

    def model_identity(
        self,
        *,
        target_language: str,
        prompt_version: str,
    ) -> ModelIdentity:
        return ModelIdentity(
            provider="deepseek",
            adapter="openai-chat",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            target_language=target_language,
            prompt_version=prompt_version,
        )


class IndependentPhaseTests(unittest.TestCase):
    def test_compile_rejects_translation_from_another_model_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            record = PageRecord(
                1,
                "日本語本文",
                language="ja",
                translated_text="旧模型译文",
                translation_provider="deepseek",
                translation_model="deepseek-v4-flash",
                translation_target_language="简体中文",
            )
            record.translation_source_sha256 = record.text_sha256
            expected = FakeTextClient().model_identity(
                target_language="简体中文",
                prompt_version="book-translation-v2",
            )
            payload = {
                "entries": [
                    TocEntry(
                        "chapter",
                        "第一章",
                        "正文",
                        1,
                        "chapter",
                        1,
                        pdf_page=1,
                    ).__dict__
                ]
            }
            with self.assertRaisesRegex(ValueError, "Missing or stale translation"):
                compile_chapters(
                    Path("book.pdf"),
                    Path(directory),
                    [record],
                    payload,
                    granularity="chapter",
                    require_translation=True,
                    expected_translation_identity=expected,
                )

    def test_translate_runs_from_page_checkpoints_without_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            save_page_record(
                output,
                PageRecord(1, "これは本文です。", language="ja", ocr_model="test"),
            )
            with patch(
                "book_pipeline.build_translation_client",
                return_value=FakeTextClient(),
            ):
                code = main(
                    [
                        "-o",
                        str(output),
                        "--phase",
                        "translate",
                        "--translate-non-chinese",
                        "--translation-source-language",
                        "ja",
                    ]
                )
            self.assertEqual(code, 0)
            saved = json.loads(
                (output / "pages/page_0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["translation_model"], "deepseek-v4-pro")
            self.assertTrue(saved["translation_fingerprint"])

    def test_docx_runs_from_chapters_without_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_正文.md").write_text(
                "# 正文\n\n测试内容。\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps(
                    [
                        {
                            "sequence": 1,
                            "display_title": "正文",
                            "filename": "001_正文.md",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            code = main(
                [
                    "-o",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "测试书",
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue((output / "测试书.docx").exists())

    def test_epub_runs_from_chapters_without_pdf_or_source_page_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_正文.md").write_text(
                "# 正文\n\n测试内容。\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps(
                    [
                        {
                            "sequence": 1,
                            "display_title": "正文",
                            "filename": "001_正文.md",
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            code = main(
                [
                    "-o",
                    str(output),
                    "--phase",
                    "epub",
                    "--title",
                    "测试书",
                ]
            )
            self.assertEqual(code, 0)
            artifact = output / "测试书.epub"
            self.assertTrue(artifact.exists())
            with zipfile.ZipFile(artifact) as archive:
                rendered = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore")
                    for name in archive.namelist()
                    if name.endswith((".xhtml", ".html"))
                )
            self.assertIn("测试内容", rendered)
            self.assertNotIn("来源 PDF 页", rendered)
            self.assertNotIn("PDF_PAGE", rendered)


if __name__ == "__main__":
    unittest.main()
