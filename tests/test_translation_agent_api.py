import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_agent_api import RunRequest, run_book


class TranslationAgentApiTests(unittest.TestCase):
    def test_profile_request_never_serializes_api_key(self) -> None:
        request = RunRequest(
            input_pdf="book.pdf",
            output_dir="outputs/book",
            phase="translate",
            config="pipeline.toml",
            translation_profile="deepseek_pro",
            translate_non_chinese=True,
        )
        argv = request.to_argv()
        self.assertIn("deepseek_pro", argv)
        self.assertNotIn("--translation-api-key", argv)
        self.assertNotIn("--api-key", argv)

    def test_low_code_advanced_options_are_structured(self) -> None:
        request = RunRequest(
            input_pdf="book.pdf",
            output_dir="outputs/book",
            toc_pages="6-10",
            page_offset=12,
            front_matter_pages=50,
            ocr_reading_direction="vertical",
            keep_page_images=True,
            force=True,
            generate_epub=False,
            generate_docx=False,
            generate_knowledge_base=False,
            generate_bookmarked_pdf=False,
        )
        argv = request.to_argv()
        for expected in (
            "--toc-pages",
            "6-10",
            "--page-offset",
            "12",
            "--ocr-reading-direction",
            "vertical",
            "--keep-page-images",
            "--force",
            "--no-epub",
            "--no-docx",
            "--no-kb",
            "--no-bookmarked-pdf",
        ):
            self.assertIn(expected, argv)

    def test_status_request_does_not_require_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("translation_agent_api.main", return_value=0) as mocked:
                result = run_book(
                    RunRequest(output_dir=output, phase="status")
                )
        self.assertTrue(result.ok)
        argv = mocked.call_args.args[0]
        self.assertEqual(argv[:4], ["--output-dir", str(output), "--phase", "status"])

    def test_run_result_status_uses_selected_translation_profile(self) -> None:
        config = """
[profiles.deepseek_pro]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
credential_env = "DEEPSEEK_TEST_KEY"

[pipeline]
translation_profile = "deepseek_pro"
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.toml"
            config_path.write_text(config, encoding="utf-8")
            with (
                patch("translation_agent_api.main", return_value=0),
                patch(
                    "translation_agent_api.output_status",
                    return_value={"translations_profile_fresh": 0},
                ) as mocked_status,
            ):
                result = run_book(
                    RunRequest(
                        output_dir=root,
                        phase="status",
                        config=config_path,
                        translation_profile="deepseek_pro",
                    )
                )
        self.assertTrue(result.ok)
        identity = mocked_status.call_args.kwargs["expected_translation_identity"]
        self.assertEqual(identity.provider, "deepseek")
        self.assertEqual(identity.model, "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
