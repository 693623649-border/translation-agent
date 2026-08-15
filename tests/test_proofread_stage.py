import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from book_pipeline import (
    PROOFREAD_PROMPT_VERSION,
    ChatOCRProofreader,
    ModelIdentity,
    PageRecord,
    PageStore,
    StalePageSourceError,
    TocEntry,
    compile_chapters,
    main,
    output_status,
    proofread_ocr_pages,
    save_page_record,
)


def identity(*, model: str = "deepseek-v4-flash") -> ModelIdentity:
    return ModelIdentity(
        provider="deepseek",
        adapter="openai-chat",
        base_url="https://api.deepseek.com",
        model=model,
        target_language="ja",
        prompt_version=PROOFREAD_PROMPT_VERSION,
    )


class FakeProofreader:
    def __init__(self, output: str = "校勘された日本語本文") -> None:
        self.output = output
        self.inputs: list[tuple[str, str]] = []

    def proofread(self, text: str, *, language: str) -> str:
        self.inputs.append((text, language))
        return self.output


class FakeChatClient:
    def __init__(self, output: str = "校勘された日本語本文") -> None:
        self.output = output
        self.prompts: list[tuple[str, str]] = []

    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
        self.prompts.append((prompt, system))
        return self.output

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
            model="deepseek-v4-flash",
            target_language=target_language,
            prompt_version=prompt_version,
        )


class ProofreadRecordTests(unittest.TestCase):
    def test_legacy_record_and_translation_remain_fresh_without_overlay(self) -> None:
        record = PageRecord(
            1,
            "日本語 OCR",
            language="ja",
            translated_text="中文译文",
            translation_source_sha256="",
            translation_provider="deepseek",
            translation_model="deepseek-v4-flash",
            translation_target_language="简体中文",
        )
        record.translation_source_sha256 = record.text_sha256

        self.assertEqual(record.effective_text, record.text)
        self.assertEqual(record.effective_text_sha256, record.text_sha256)
        self.assertTrue(record.translation_is_fresh)
        self.assertEqual(record.compile_text, "中文译文")

    def test_fresh_overlay_is_effective_and_invalidates_raw_text_translation(self) -> None:
        record = PageRecord(
            1,
            "誤つた OCR",
            language="ja",
            translated_text="旧译文",
            translation_provider="deepseek",
            translation_model="deepseek-v4-flash",
            translation_target_language="简体中文",
            proofread_text="誤った OCR",
            proofread_provider="deepseek",
            proofread_model="deepseek-v4-flash",
            proofread_language="ja",
        )
        record.proofread_source_sha256 = record.text_sha256
        record.translation_source_sha256 = record.text_sha256

        self.assertTrue(record.proofread_is_fresh)
        self.assertEqual(record.effective_text, "誤った OCR")
        self.assertFalse(record.translation_is_fresh)
        self.assertEqual(record.compile_text, "誤った OCR")

    def test_stale_overlay_falls_back_to_raw_ocr(self) -> None:
        record = PageRecord(
            1,
            "更新された OCR",
            language="ja",
            proofread_text="古い校勘",
            proofread_source_sha256="not-the-current-sha",
            proofread_provider="deepseek",
            proofread_model="deepseek-v4-flash",
            proofread_language="ja",
        )

        self.assertFalse(record.proofread_is_fresh)
        self.assertEqual(record.effective_text, "更新された OCR")

    def test_compile_reads_fresh_overlay_when_translation_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = PageRecord(
                1,
                "誤つた OCR",
                language="ja",
                proofread_text="誤った OCR",
                proofread_provider="deepseek",
                proofread_model="deepseek-v4-flash",
                proofread_language="ja",
            )
            record.proofread_source_sha256 = record.text_sha256
            toc = {
                "entries": [
                    TocEntry(
                        id="chapter-1",
                        index="第一章",
                        title="本文",
                        level=1,
                        kind="chapter",
                        printed_page=1,
                        pdf_page=1,
                    ).__dict__
                ]
            }

            manifest, _ = compile_chapters(
                Path("book.pdf"),
                output,
                [record],
                toc,
                granularity="chapter",
            )
            markdown = (output / "chapters" / manifest[0]["filename"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("誤った OCR", markdown)
            self.assertNotIn("誤つた OCR", markdown)


class ProofreadCheckpointTests(unittest.TestCase):
    def test_commit_preserves_raw_ocr_markdown_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            source = PageRecord(3, "誤つた OCR", language="ja", ocr_model="ocr-v1")
            save_page_record(output, source)

            committed = PageStore(output).commit_proofread(
                3,
                expected_text_sha256=source.text_sha256,
                proofread_text="誤った OCR",
                identity=identity(),
            )

            self.assertEqual(committed.text, source.text)
            self.assertEqual(committed.effective_text, "誤った OCR")
            self.assertTrue(committed.proofread_is_fresh_for(identity()))
            self.assertEqual(
                (output / "pages/page_0003.md").read_text(encoding="utf-8"),
                source.text + "\n",
            )

    def test_stale_proofread_cannot_overwrite_new_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            old = PageRecord(4, "古い OCR", language="ja")
            save_page_record(output, old)
            save_page_record(output, PageRecord(4, "新しい OCR", language="ja"))

            with self.assertRaises(StalePageSourceError):
                PageStore(output).commit_proofread(
                    4,
                    expected_text_sha256=old.text_sha256,
                    proofread_text="古い OCR の校勘",
                    identity=identity(),
                )

            saved = PageStore(output).load(4)
            self.assertEqual(saved.text, "新しい OCR")
            self.assertEqual(saved.proofread_text, "")

    def test_new_overlay_rejects_translation_started_from_raw_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            source = PageRecord(5, "誤つた OCR", language="ja")
            save_page_record(output, source)
            store = PageStore(output)
            store.commit_proofread(
                5,
                expected_text_sha256=source.text_sha256,
                proofread_text="誤った OCR",
                identity=identity(),
            )

            translation_identity = ModelIdentity(
                provider="deepseek",
                adapter="openai-chat",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                target_language="简体中文",
                prompt_version="book-translation-v2",
            )
            with self.assertRaises(StalePageSourceError):
                store.commit_translation(
                    5,
                    expected_text_sha256=source.text_sha256,
                    translated_text="旧来源译文",
                    identity=translation_identity,
                )


class ProofreadExecutionTests(unittest.TestCase):
    def test_prompt_version_invalidates_legacy_japanese_only_cache(self) -> None:
        self.assertEqual(
            PROOFREAD_PROMPT_VERSION,
            "book-ocr-proofread-zh-ja-v2",
        )

    def test_runner_is_resumable_and_binds_profile_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = PageRecord(1, "これは誤つた本文です。", language="ja")
            save_page_record(output, record)
            fake = FakeProofreader()

            proofread_ocr_pages(
                [record],
                output,
                fake,
                language="ja",
                identity=identity(),
                force=False,
                concurrency=2,
            )
            proofread_ocr_pages(
                [PageStore(output).load(1)],
                output,
                fake,
                language="ja",
                identity=identity(),
                force=False,
                concurrency=2,
            )

            self.assertEqual(fake.inputs, [("これは誤つた本文です。", "ja")])
            self.assertTrue(PageStore(output).load(1).proofread_is_fresh_for(identity()))

    def test_runner_recovers_misdetected_japanese_but_skips_pure_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            japanese = PageRecord(
                1,
                "漢字中心だが仮名を含む。",
                language="zh",
            )
            chinese = PageRecord(2, "这是纯中文正文。", language="zh")
            save_page_record(output, japanese)
            save_page_record(output, chinese)
            fake = FakeProofreader()

            proofread_ocr_pages(
                [japanese, chinese],
                output,
                fake,
                language="ja",
                identity=identity(),
                force=False,
                concurrency=2,
            )

            self.assertEqual(fake.inputs, [(japanese.text, "ja")])
            self.assertTrue(PageStore(output).load(1).proofread_is_fresh)
            self.assertFalse(PageStore(output).load(2).proofread_is_fresh)

    def test_runner_treats_zh_and_zh_cn_as_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            simplified = PageRecord(1, "这是中文 OCR 正文。", language="zh")
            locale_tagged = PageRecord(2, "这是另一页中文正文。", language="zh-CN")
            save_page_record(output, simplified)
            save_page_record(output, locale_tagged)
            fake = FakeProofreader(output="校勘后的中文正文")
            zh_cn_identity = ModelIdentity(
                provider="deepseek",
                adapter="openai-chat",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                target_language="zh-CN",
                prompt_version=PROOFREAD_PROMPT_VERSION,
            )

            proofread_ocr_pages(
                [simplified, locale_tagged],
                output,
                fake,
                language="zh-CN",
                identity=zh_cn_identity,
                force=False,
                concurrency=2,
            )

            self.assertCountEqual(
                fake.inputs,
                [(simplified.text, "zh-CN"), (locale_tagged.text, "zh-CN")],
            )
            self.assertTrue(
                PageStore(output).load(1).proofread_is_fresh_for(zh_cn_identity)
            )
            self.assertTrue(
                PageStore(output).load(2).proofread_is_fresh_for(zh_cn_identity)
            )

    def test_chat_prompt_explicitly_forbids_translation(self) -> None:
        client = FakeChatClient()
        result = ChatOCRProofreader(client, max_chars=100).proofread(
            "これは日本語です。",
            language="ja",
        )

        self.assertEqual(result, client.output)
        prompt, system = client.prompts[0]
        self.assertIn("严禁翻译", prompt)
        self.assertIn("输出必须仍是原文日语", prompt)
        self.assertIn("绝不翻译", system)

    def test_chat_prompt_strictly_preserves_chinese_ocr_for_zh_aliases(self) -> None:
        for language in ("zh", "zh-CN"):
            with self.subTest(language=language):
                client = FakeChatClient(output="校勘后的中文正文")
                result = ChatOCRProofreader(client, max_chars=100).proofread(
                    "这是一段中又 OCR 原文。",
                    language=language,
                )

                self.assertEqual(result, client.output)
                prompt, system = client.prompts[0]
                self.assertIn("严格校勘下面的中文 OCR 原文", prompt)
                self.assertIn("输出必须仍是原稿中文", prompt)
                self.assertIn("不得翻译外文内容", prompt)
                self.assertIn("不得擅自转换简繁体或异体字", prompt)
                self.assertIn("不得因内容重复或看似页眉页脚而自行删除", prompt)
                self.assertIn("脚注及脚注定义", prompt)
                self.assertIn("[原文存疑]", prompt)
                self.assertIn("中文书籍 OCR 校勘员", system)
                self.assertIn("绝不翻译、改写、概述或创作", system)

    def test_cli_proofread_runs_without_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            save_page_record(
                output,
                PageRecord(1, "これは誤つた本文です。", language="ja"),
            )
            client = FakeChatClient()
            with patch("book_pipeline.build_proofread_client", return_value=client):
                code = main(
                    [
                        "-o",
                        str(output),
                        "--phase",
                        "proofread",
                        "--proofread-language",
                        "ja",
                        "--proofread-concurrency",
                        "2",
                    ]
                )

            self.assertEqual(code, 0)
            saved = json.loads(
                (output / "pages/page_0001.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["text"], "これは誤つた本文です。")
            self.assertEqual(saved["proofread_text"], client.output)

    def test_status_reports_fresh_overlay_counts_and_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw = PageRecord(1, "誤つた OCR", language="ja")
            save_page_record(output, raw)
            PageStore(output).commit_proofread(
                1,
                expected_text_sha256=raw.text_sha256,
                proofread_text="誤った OCR",
                identity=identity(),
            )

            status = output_status(
                output,
                expected_proofread_identity=identity(),
            )
            self.assertEqual(status["proofread_pages_source_fresh"], 1)
            self.assertEqual(status["proofread_pages_profile_fresh"], 1)
            self.assertEqual(status["proofread_models"], {"deepseek-v4-flash": 1})
            self.assertEqual(status["effective_text_pages"], 1)


if __name__ == "__main__":
    unittest.main()
