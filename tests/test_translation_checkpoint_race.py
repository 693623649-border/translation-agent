import tempfile
import unittest
import hashlib
from pathlib import Path

from book_pipeline import (
    PageRecord,
    PageStore,
    StalePageSourceError,
    StalePageTranslationError,
    load_page_records,
    save_page_record,
)
from pipeline_profiles import ModelProfile


class TranslationCheckpointRaceTests(unittest.TestCase):
    @staticmethod
    def translation_identity():
        return ModelProfile(
            name="translation",
            adapter="openai-chat",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            credential_env="DEEPSEEK_TEST_API_KEY",
        ).identity(
            target_language="简体中文",
            prompt_version="translation-v2",
        )

    def test_stale_translation_does_not_overwrite_newer_ocr_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = PageStore(output_dir)
            original = PageRecord(7, "古い OCR 本文", language="ja", ocr_model="ocr-v1")
            save_page_record(output_dir, original)
            expected_text_sha256 = original.text_sha256

            refreshed = PageRecord(
                7,
                "更新された OCR 本文",
                language="ja",
                ocr_model="ocr-v2",
            )
            save_page_record(output_dir, refreshed)

            with self.assertRaises(StalePageSourceError):
                store.commit_translation(
                    7,
                    expected_text_sha256=expected_text_sha256,
                    translated_text="陈旧 OCR 对应的译文",
                    identity=self.translation_identity(),
                )

            [saved] = load_page_records(output_dir)
            self.assertEqual(saved.text, refreshed.text)
            self.assertEqual(saved.ocr_model, "ocr-v2")
            self.assertEqual(saved.translated_text, "")
            markdown = output_dir / "pages" / "page_0007.md"
            self.assertEqual(markdown.read_text(encoding="utf-8"), refreshed.text + "\n")

    def test_commit_translation_merges_identity_into_current_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = PageStore(output_dir)
            source = PageRecord(3, "翻訳対象の本文", language="ja", ocr_model="ocr-v2")
            save_page_record(output_dir, source)
            identity = self.translation_identity()

            committed = store.commit_translation(
                3,
                expected_text_sha256=source.text_sha256,
                translated_text="需要翻译的正文",
                identity=identity,
            )

            [saved] = load_page_records(output_dir)
            self.assertEqual(committed, saved)
            self.assertEqual(saved.text, source.text)
            self.assertEqual(saved.ocr_model, source.ocr_model)
            self.assertEqual(saved.translated_text, "需要翻译的正文")
            self.assertEqual(saved.translation_source_sha256, source.text_sha256)
            self.assertEqual(saved.translation_provider, identity.provider)
            self.assertEqual(saved.translation_model, identity.model)
            self.assertEqual(
                saved.translation_target_language,
                identity.target_language,
            )
            self.assertEqual(saved.translation_fingerprint, identity.fingerprint)
            self.assertTrue(saved.translation_is_fresh)

    def test_stale_normalization_does_not_overwrite_new_model_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            store = PageStore(output_dir)
            source = PageRecord(5, "翻訳対象", language="ja", ocr_model="ocr-v2")
            save_page_record(output_dir, source)
            old_identity = self.translation_identity()
            old = store.commit_translation(
                5,
                expected_text_sha256=source.text_sha256,
                translated_text="舊譯文",
                identity=old_identity,
            )
            old_translation_sha256 = hashlib.sha256(
                old.translated_text.encode("utf-8")
            ).hexdigest()

            new_identity = ModelProfile(
                name="new-translation",
                adapter="openai-chat",
                provider="deepseek",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
            ).identity(
                target_language="简体中文",
                prompt_version="translation-v3",
            )
            store.commit_translation(
                5,
                expected_text_sha256=source.text_sha256,
                translated_text="新模型译文",
                identity=new_identity,
            )

            with self.assertRaises(StalePageTranslationError):
                store.update_translation_text(
                    5,
                    expected_text_sha256=source.text_sha256,
                    expected_translation_sha256=old_translation_sha256,
                    expected_translation_fingerprint=old.translation_fingerprint,
                    translated_text="旧译文",
                )

            [saved] = load_page_records(output_dir)
            self.assertEqual(saved.translated_text, "新模型译文")
            self.assertEqual(saved.translation_fingerprint, new_identity.fingerprint)


if __name__ == "__main__":
    unittest.main()
