import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import UnknownTargetError
from pipeline_graph.book import BookGraphConfigurationError
from translation_agent_api import (
    GraphRunRequest,
    RunRequest,
    plan_graph,
    prepare_graph,
    run_book,
)


class TranslationAgentApiTests(unittest.TestCase):
    def test_rag_embedding_mode_is_serialized_without_credentials(self) -> None:
        enabled = RunRequest(output_dir="out", rag_embed=True).to_argv()
        disabled = RunRequest(output_dir="out", rag_embed=False).to_argv()
        automatic = RunRequest(output_dir="out", rag_embed=None).to_argv()

        self.assertIn("--rag-embed", enabled)
        self.assertIn("--no-rag-embed", disabled)
        self.assertNotIn("--rag-embed", automatic)
        self.assertFalse(any("API_KEY" in value for value in enabled))
        with self.assertRaisesRegex(ValueError, "generate_knowledge_base"):
            RunRequest(
                output_dir="out",
                generate_knowledge_base=False,
                rag_embed=True,
            ).to_argv()

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

    def test_docx_author_is_serialized_as_optional_metadata(self) -> None:
        request = RunRequest(
            output_dir="outputs/book",
            phase="docx",
            title="Book",
            author="Author",
        )

        argv = request.to_argv()

        self.assertEqual(argv[argv.index("--author") + 1], "Author")
        self.assertNotIn(
            "--author",
            RunRequest(
                output_dir="outputs/book",
                phase="docx",
                title="Book",
            ).to_argv(),
        )

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
            verification_profile="word",
            require_all_reviewed=True,
        )
        argv = request.to_argv()
        self.assertIn("verify", argv)
        self.assertEqual(argv.count("--chapter-id"), 2)
        self.assertIn("chapter-5", argv)
        self.assertIn("chapter-6", argv)
        self.assertIn("--require-all-reviewed", argv)
        self.assertEqual(
            argv[argv.index("--verification-profile") + 1],
            "word",
        )
        self.assertIn("--report", argv)

    def test_graph_api_plans_standalone_docx_without_pdf_or_ocr(self) -> None:
        request = GraphRunRequest(
            pipeline=RunRequest(
                output_dir="outputs/book",
                phase="docx",
                title="Book",
            )
        )
        self.assertEqual(
            plan_graph(request),
            (
                "core.chapters.load",
                "core.reconstruct.semantic",
                "core.publication.sanitize",
                "core.publish.docx",
            ),
        )

    def test_graph_api_exposes_explicit_text_pdf_mode(self) -> None:
        request = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="all",
                translate_non_chinese=True,
            ),
            source_mode="text-pdf",
            text_pdf_reflow=True,
            text_pdf_strip_leading_page_number_offset=1,
        )

        plan = plan_graph(request)

        self.assertEqual(plan[0:3], (
            "core.source.inspect",
            "core.pages.text_extract",
            "core.pages.translate",
        ))
        self.assertNotIn("core.pages.ocr", plan)

    def test_text_pdf_recipe_selects_source_mode_when_api_omits_it(self) -> None:
        recipe = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "text-pdf-full-publication.toml"
        )
        request = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="all",
            ),
            recipe=recipe,
        )

        plan = plan_graph(request)

        self.assertIn("core.pages.text_extract", plan)
        self.assertNotIn("core.pages.ocr", plan)
        self.assertEqual(request.graph_options().source_mode, "text-pdf")

    def test_explicit_scanned_pdf_api_mode_conflicts_with_text_pdf_recipe(self) -> None:
        recipe = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "text-pdf-full-publication.toml"
        )
        request = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="all",
            ),
            recipe=recipe,
            source_mode="scanned-pdf",
        )

        with self.assertRaisesRegex(
            BookGraphConfigurationError,
            "explicit source_mode='scanned-pdf'.*core.pages.text_extract",
        ):
            plan_graph(request)

    def test_word_recipe_plans_a_verified_word_report(self) -> None:
        recipe = (
            Path(__file__).resolve().parents[1]
            / "recipes"
            / "chinese-pdf-word.toml"
        )
        request = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="all",
                title="Book",
            ),
            recipe=recipe,
        )

        plan = plan_graph(request)

        self.assertEqual(plan[-1], "core.publication.verify.word")
        self.assertIn("core.publish.docx", plan)
        self.assertNotIn("core.publish.epub", plan)
        self.assertNotIn("core.publish.knowledge_base", plan)
        self.assertNotIn("core.publish.reference_pdf", plan)

    def test_graph_api_exposes_replaceable_registry_before_execution(self) -> None:
        request = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="all",
            ),
            disable_nodes=(
                "core.publish.epub",
                "core.publish.knowledge_base",
                "core.publish.reference_pdf",
                "core.publication.verify",
            ),
            targets=("publication.docx",),
        )
        prepared = prepare_graph(request)
        sanitizer = next(
            node
            for node in prepared.graph.nodes
            if node.name == "core.publication.sanitize"
        )
        self.assertIs(prepared.graph.remove(sanitizer.name), sanitizer)
        with self.assertRaises(UnknownTargetError):
            prepared.plan()
        prepared.graph.add(sanitizer)
        self.assertIn("core.publication.sanitize", plan_graph(request))

    def test_legacy_force_is_delegated_without_forcing_absent_graph_nodes(self) -> None:
        status = GraphRunRequest(
            pipeline=RunRequest(
                output_dir="outputs/book",
                phase="status",
                force=True,
            )
        )
        self.assertEqual(status.graph_options().force_nodes, frozenset())

        ocr = GraphRunRequest(
            pipeline=RunRequest(
                input_pdf="book.pdf",
                output_dir="outputs/book",
                phase="ocr",
                force=True,
            )
        )
        self.assertEqual(
            ocr.graph_options().force_nodes,
            frozenset(),
        )
        prepared = prepare_graph(ocr)
        ocr_node = next(
            node for node in prepared.graph.nodes if node.name == "core.pages.ocr"
        )
        self.assertFalse(ocr_node.cache)

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
                # The selected glm_vision profile is the remote fallback; the
                # test asserts profile-based identity, so hold the local
                # PaddleOCR probe off (its availability is host-dependent).
                patch("book_pipeline.paddle_local_available", return_value=False),
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


class RetrieveKnowledgeBaseContextTests(unittest.TestCase):
    def test_retrieval_tuning_arguments_forward_to_knowledge_base(self) -> None:
        from unittest.mock import MagicMock
        import translation_agent_api as api

        knowledge_base = MagicMock()
        with patch.object(api, "RagKnowledgeBase") as open_cls:
            open_cls.open.return_value = knowledge_base
            api.retrieve_knowledge_base_context(
                "outputs/book",
                "查询",
                auto_route=True,
                mode="hybrid",
                book_ids=("呐喊",),
                authors=("鲁迅",),
                languages=("zh",),
                per_book_cap=2,
                candidate_depth=48,
            )

        kwargs = knowledge_base.retrieve_context.call_args.kwargs
        self.assertEqual(kwargs["mode"], "hybrid")
        self.assertEqual(kwargs["book_ids"], {"呐喊"})
        self.assertEqual(kwargs["authors"], {"鲁迅"})
        self.assertEqual(kwargs["languages"], {"zh"})
        self.assertEqual(kwargs["per_book_cap"], 2)
        self.assertEqual(kwargs["candidate_depth"], 48)
        self.assertTrue(kwargs["auto_route"])

    def test_retrieval_defaults_keep_hybrid_parity_with_cli(self) -> None:
        from unittest.mock import MagicMock
        import translation_agent_api as api

        knowledge_base = MagicMock()
        with patch.object(api, "RagKnowledgeBase") as open_cls:
            open_cls.open.return_value = knowledge_base
            api.retrieve_knowledge_base_context("outputs/book", "查询")

        kwargs = knowledge_base.retrieve_context.call_args.kwargs
        self.assertIsNone(kwargs["mode"])
        self.assertIsNone(kwargs["book_ids"])
        self.assertIsNone(kwargs["authors"])
        self.assertIsNone(kwargs["languages"])
        self.assertIsNone(kwargs["per_book_cap"])
        self.assertEqual(kwargs["candidate_depth"], 30)
