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
            printed_pages_per_pdf_page=2,
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
            "--printed-pages-per-pdf-page",
            "2",
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

    def test_proofread_options_are_structured_without_credentials(self) -> None:
        request = RunRequest(
            output_dir="outputs/book",
            phase="proofread",
            config="pipeline.toml",
            proofread_profile="deepseek_flash",
            proofread_language="ja",
            proofread_concurrency=8,
            proofread_delay=0.5,
            proofread_max_chars=9000,
        )
        argv = request.to_argv()
        for expected in (
            "--proofread-profile",
            "deepseek_flash",
            "--proofread-language",
            "ja",
            "--proofread-concurrency",
            "8",
            "--proofread-delay",
            "0.5",
            "--proofread-max-chars",
            "9000",
        ):
            self.assertIn(expected, argv)
        self.assertNotIn("--translation-api-key", argv)

    def test_publication_verification_options_are_structured(self) -> None:
        request = RunRequest(
            output_dir="outputs/book",
            phase="verify",
            verification_report="outputs/book/audit/custom.json",
            verification_chapter_ids=("chapter-5", "chapter-6"),
            require_all_reviewed=True,
        )
        argv = request.to_argv()
        self.assertIn("verify", argv)
        self.assertEqual(argv.count("--chapter-id"), 2)
        self.assertIn("chapter-5", argv)
        self.assertIn("chapter-6", argv)
        self.assertIn("--require-all-reviewed", argv)
        self.assertIn("--report", argv)

    def test_automatic_publication_verification_can_be_disabled(self) -> None:
        argv = RunRequest(
            output_dir="outputs/book",
            verify_publication=False,
        ).to_argv()
        self.assertIn("--no-verify", argv)

    def test_incremental_verification_ids_reject_non_verify_phase(self) -> None:
        with self.assertRaises(ValueError):
            RunRequest(
                output_dir="outputs/book",
                phase="compile",
                verification_chapter_ids=("chapter-5",),
            ).to_argv()

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
[profiles.glm_vision]
adapter = "coding-plan-mcp"
provider = "zhipu"
model = "glm-4.6v"
reading_direction = "vertical"

[profiles.deepseek_pro]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
credential_env = "DEEPSEEK_TEST_KEY"

[pipeline]
ocr_profile = "glm_vision"
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
        self.assertEqual(
            mocked_status.call_args.kwargs["expected_ocr_model_prefix"],
            "coding-plan/glm-4.6v-vision-mcp/vertical-v2",
        )


if __name__ == "__main__":
    unittest.main()
