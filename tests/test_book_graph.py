from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

import book_pipeline
import graph_pipeline
from publication_verifier import verify_publication
from pipeline_graph import (
    GraphContext,
    GraphExecutor,
    NodeExecutionError,
    NodeResult,
    NodeSpec,
    OutputDirectoryLock,
    OutputDirectoryLockedError,
    PipelineGraph,
    Recipe,
    stable_fingerprint,
)
from pipeline_graph.book import (
    ART_CHAPTERS,
    ART_DOCX,
    ART_EPUB,
    ART_KB,
    ART_PAGES_IMPORTED,
    ART_PAGES_RAW,
    ART_READER_CHAPTERS,
    ART_SEMANTIC_CHAPTERS,
    ART_REFERENCE_PDF,
    ART_REPORT,
    ART_SOURCE,
    ART_TOC,
    ART_WORD_REPORT,
    BookGraphConfigurationError,
    BookGraphOptions,
    LegacyStageError,
    NODE_CHAPTERS_LOAD,
    NODE_COMPILE,
    NODE_DOCX,
    NODE_EPUB,
    NODE_KB,
    NODE_OCR,
    NODE_PAGES_IMPORT,
    NODE_PAGES_LOAD,
    NODE_PROOFREAD,
    NODE_REFERENCE_PDF,
    NODE_SANITIZE,
    NODE_SEMANTIC,
    NODE_SOURCE,
    NODE_STATUS,
    NODE_TOC_LOAD,
    NODE_TOC_OUTLINE,
    NODE_TOC_PIPELINE,
    NODE_TEXT_EXTRACT,
    NODE_TRANSLATE,
    NODE_VERIFY,
    NODE_VERIFY_WORD,
    PreparedBookGraph,
    SemanticReconstructionError,
    SourceBindingError,
    _phase_argv,
    _single_file_is_current,
    _translation_stage_semantics,
    prepare_book_graph,
)


def _plan_names(prepared: PreparedBookGraph) -> list[str]:
    return [node.name for node in prepared.plan()]


def _files_sha256(paths: list[Path]) -> str:
    """Mirror the graph's content digest for directory/bundle test artifacts."""

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _file_fixture(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _pages_fixture(directory: Path) -> dict[str, object]:
    pages = sorted(directory.glob("page_*.json"))
    return {
        "path": str(directory.resolve()),
        "sha256": _files_sha256(pages),
    }


def _chapters_fixture(
    chapter_dir: Path,
    manifest_path: Path,
) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = [manifest_path]
    files.extend(chapter_dir / str(item["filename"]) for item in manifest)
    return {
        "chapter_dir": str(chapter_dir.resolve()),
        "manifest": str(manifest_path.resolve()),
        "sha256": _files_sha256(files),
    }


class BookGraphPlanningTests(unittest.TestCase):
    def test_text_pdf_mode_replaces_ocr_with_explicit_text_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                    "--translate-non-chinese",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    text_pdf_reflow=True,
                ),
            )

        names = _plan_names(prepared)
        self.assertEqual(names[:5], [
            NODE_SOURCE,
            NODE_TEXT_EXTRACT,
            NODE_TRANSLATE,
            NODE_TOC_PIPELINE,
            NODE_COMPILE,
        ])
        self.assertNotIn(NODE_OCR, names)

    def test_text_pdf_mode_rejects_ocr_and_proofread_phases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            common = [
                str(Path(directory) / "book.pdf"),
                "--output-dir",
                str(Path(directory) / "output"),
            ]
            for phase in ("ocr", "proofread"):
                with self.subTest(phase=phase), self.assertRaisesRegex(
                    BookGraphConfigurationError, "text-pdf"
                ):
                    prepare_book_graph(
                        [*common, "--phase", phase],
                        options=BookGraphOptions(source_mode="text-pdf"),
                    )

    def test_scanned_pdf_default_does_not_auto_select_text_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ]
            )

        self.assertIn(NODE_OCR, _plan_names(prepared))
        self.assertNotIn(NODE_TEXT_EXTRACT, _plan_names(prepared))

    def test_text_pdf_specific_options_require_explicit_mode(self) -> None:
        with self.assertRaisesRegex(BookGraphConfigurationError, "text_pdf_.*require"):
            BookGraphOptions(text_pdf_reflow=True)

    def test_text_pdf_mode_cannot_disable_its_required_source_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(
            BookGraphConfigurationError, "requires core.pages.text_extract"
        ):
            prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    disabled_nodes=frozenset({NODE_TEXT_EXTRACT}),
                ),
            )

    def test_text_pdf_recipe_selects_text_source_without_auto_detection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recipe = Recipe(
                id="text-pdf",
                targets=(ART_DOCX,),
                enable=(NODE_TEXT_EXTRACT,),
                disable=(NODE_OCR, NODE_KB, NODE_EPUB, NODE_REFERENCE_PDF, NODE_VERIFY),
                required_plugins=(),
            )
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ],
                recipe=recipe,
            )

        self.assertIn(NODE_TEXT_EXTRACT, _plan_names(prepared))
        self.assertNotIn(NODE_OCR, _plan_names(prepared))

    def test_text_pdf_handler_imports_pages_without_calling_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            output = root / "output"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "A complete embedded English text layer.")
            document.save(source)
            document.close()
            prepared = prepare_book_graph(
                [
                    str(source),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    target_artifacts=frozenset({ART_PAGES_RAW}),
                ),
            )

            with patch("pipeline_graph.book._ocr_page_handler") as ocr:
                result = prepared.execute()

            self.assertEqual(result.executed, (NODE_SOURCE, NODE_TEXT_EXTRACT))
            ocr.assert_not_called()
            record = book_pipeline.PageStore(output).load(1)
            self.assertEqual(record.ocr_model, "text-layer/pymupdf-v1")
            self.assertIn("embedded English", record.text)

    def test_text_pdf_handler_fails_closed_on_image_only_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            output = root / "output"
            document = fitz.open()
            document.new_page()
            document.save(source)
            document.close()
            prepared = prepare_book_graph(
                [
                    str(source),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    target_artifacts=frozenset({ART_PAGES_RAW}),
                ),
            )

            with self.assertRaisesRegex(NodeExecutionError, "complete embedded text layer"):
                prepared.execute()

            self.assertFalse(any((output / "pages").glob("page_*.json")))

    def test_text_pdf_translate_phase_includes_source_and_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "translate",
                    "--translate-non-chinese",
                ],
                options=BookGraphOptions(source_mode="text-pdf"),
            )

        self.assertEqual(
            _plan_names(prepared),
            [NODE_SOURCE, NODE_TEXT_EXTRACT, NODE_TRANSLATE],
        )

    def test_text_pdf_compile_can_build_toc_from_outline_in_fresh_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "compile",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    toc_source="outline",
                    target_artifacts=frozenset({ART_CHAPTERS}),
                ),
            )

        self.assertEqual(
            _plan_names(prepared),
            [NODE_SOURCE, NODE_TEXT_EXTRACT, NODE_TOC_OUTLINE, NODE_COMPILE],
        )

    def test_text_pdf_compile_forces_text_layer_page_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(source_mode="text-pdf"),
            )
            compile_node = next(
                node for node in prepared.graph.nodes if node.name == NODE_COMPILE
            )
            output = Path(directory) / "output"
            pages = output / "pages"
            pages.mkdir(parents=True)
            (pages / "page_0001.json").write_text("{}", encoding="utf-8")
            toc = output / "toc.json"
            toc.write_text("{}", encoding="utf-8")
            prepared.context.values.update({
                ART_SOURCE: {"path": str(Path(directory) / "book.pdf")},
                ART_PAGES_RAW: {"path": str(pages)},
                ART_TOC: {"path": str(toc)},
            })

            with (
                patch("pipeline_graph.book._require_source_argument"),
                patch("pipeline_graph.book._materialize_pages_artifact"),
                patch("pipeline_graph.book._materialize_toc_artifact"),
                patch("pipeline_graph.book._run_phase") as run_phase,
                patch("pipeline_graph.book._snapshot_chapter_drafts"),
                patch("pipeline_graph.book._draft_chapters_artifact", return_value={"sha256": "0" * 64})
            ):
                compile_node.handler(prepared.context)

            add = run_phase.call_args.kwargs["add"]
            self.assertEqual(
                add[add.index("--required-ocr-model-prefix") + 1],
                "text-layer/pymupdf-v1",
            )

    def test_text_pdf_executes_through_semantic_reader_without_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            output = root / "output"
            document = fitz.open()
            for title, body in (
                ("Chapter One", "A complete first embedded-text paragraph."),
                ("Chapter Two", "A complete second embedded-text paragraph."),
            ):
                page = document.new_page()
                page.insert_textbox(
                    fitz.Rect(72, 72, 540, 740),
                    f"{title}\n{body}",
                    fontsize=11,
                )
            document.set_toc([[1, "Chapter One", 1], [1, "Chapter Two", 2]])
            document.save(source)
            document.close()

            prepared = prepare_book_graph(
                [
                    str(source),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    toc_source="outline",
                    target_artifacts=frozenset({ART_READER_CHAPTERS}),
                ),
            )

            with patch("pipeline_graph.book._ocr_page_handler") as ocr:
                result = prepared.execute()

            self.assertEqual(
                result.executed,
                (
                    NODE_SOURCE,
                    NODE_TEXT_EXTRACT,
                    NODE_TOC_OUTLINE,
                    NODE_COMPILE,
                    NODE_SEMANTIC,
                    NODE_SANITIZE,
                ),
            )
            ocr.assert_not_called()
            self.assertEqual(
                json.loads((output / "chapters.json").read_text(encoding="utf-8"))[0][
                    "display_title"
                ],
                "Chapter One",
            )
            audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(audit["status"], "passed")
            self.assertFalse(audit["summary"]["release_blocked"])
            canonical = output / "chapters" / audit["chapters"][0]["filename"]
            self.assertEqual(
                audit["chapters"][0]["markdown_sha256"],
                hashlib.sha256(canonical.read_bytes()).hexdigest(),
            )
            self.assertIn(
                "source_markdown_sha256",
                audit["chapters"][0],
            )
            self.assertTrue(
                prepared.context.value_validators[ART_SEMANTIC_CHAPTERS](
                    prepared.context,
                    prepared.context.values[ART_SEMANTIC_CHAPTERS],
                )
            )

            verification = verify_publication(
                output,
                source_pdf=source,
                require_epub=False,
                require_docx=False,
                require_knowledge_base=False,
                require_bookmarked_pdf=False,
            )
            semantics = next(
                check
                for check in verification["checks"]
                if check["id"] == "semantics.integrity"
            )
            self.assertEqual(semantics["status"], "passed")

            rerun = prepare_book_graph(
                [
                    str(source),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "all",
                ],
                options=BookGraphOptions(
                    source_mode="text-pdf",
                    toc_source="outline",
                    target_artifacts=frozenset({ART_READER_CHAPTERS}),
                ),
            ).execute()
            self.assertIn(NODE_SEMANTIC, rerun.skipped)
            self.assertIn(NODE_SANITIZE, rerun.skipped)
            rerun_audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                rerun_audit["chapters"][0]["markdown_sha256"],
                hashlib.sha256(canonical.read_bytes()).hexdigest(),
            )

    def test_semantic_argv_fingerprint_ignores_credentials_and_throughput(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / "output")
            common = ["--output-dir", output, "--phase", "docx", "--title", "Book"]
            baseline = prepare_book_graph(common)
            tuned = prepare_book_graph(
                [
                    *common,
                    "--api-key",
                    "glm-secret",
                    "--ocr-api-key",
                    "ocr-secret",
                    "--translation-api-key",
                    "translation-secret",
                    "--api-key-env",
                    "GLM_KEY_TWO",
                    "--ocr-api-key-env",
                    "OCR_KEY_TWO",
                    "--translation-api-key-env",
                    "TRANSLATION_KEY_TWO",
                    "--concurrency",
                    "9",
                    "--ocr-concurrency",
                    "8",
                    "--proofread-concurrency",
                    "7",
                    "--translation-concurrency",
                    "6",
                    "--api-timeout",
                    "999",
                    "--translation-api-timeout",
                    "998",
                    "--ocr-delay",
                    "1.5",
                    "--proofread-delay",
                    "2.5",
                    "--translation-delay",
                    "3.5",
                ]
            )

        self.assertEqual(
            baseline.context.fingerprints["pipeline.argv"],
            tuned.context.fingerprints["pipeline.argv"],
        )

    def test_control_fingerprint_tracks_config_content_but_stage_semantics_are_scoped(
        self,
    ) -> None:
        template = """
[profiles.text]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.example.invalid"
model = "{model}"
credential_env = "{credential_env}"
timeout = {timeout}
concurrency = {concurrency}
thinking = "disabled"

[pipeline]
translation_profile = "text"
""".strip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "pipeline.toml"
            argv = [
                "--output-dir",
                str(root / "output"),
                "--phase",
                "docx",
                "--config",
                str(config),
            ]

            def fingerprints() -> tuple[str, str]:
                prepared = prepare_book_graph(argv)
                args = book_pipeline.build_parser().parse_args(argv)
                return (
                    prepared.context.fingerprints["pipeline.argv"],
                    stable_fingerprint(_translation_stage_semantics(args)),
                )

            config.write_text(
                template.format(
                    model="model-a",
                    credential_env="KEY_A",
                    timeout=30,
                    concurrency=2,
                ),
                encoding="utf-8",
            )
            first_control, first_translation = fingerprints()
            config.write_text(
                template.format(
                    model="model-a",
                    credential_env="KEY_B",
                    timeout=300,
                    concurrency=20,
                ),
                encoding="utf-8",
            )
            operational_control, operational_translation = fingerprints()
            config.write_text(
                template.format(
                    model="model-b",
                    credential_env="KEY_B",
                    timeout=300,
                    concurrency=20,
                ),
                encoding="utf-8",
            )
            model_control, model_translation = fingerprints()

        self.assertNotEqual(first_control, operational_control)
        self.assertNotEqual(operational_control, model_control)
        self.assertEqual(first_translation, operational_translation)
        self.assertNotEqual(first_translation, model_translation)

    def test_docx_cache_ignores_unrelated_translation_profile_change(self) -> None:
        template = """
[profiles.text]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.example.invalid"
model = "{model}"
credential_env = "DEEPSEEK_API_KEY"

[pipeline]
translation_profile = "text"
""".strip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            chapters = output / "chapters"
            chapters.mkdir(parents=True)
            (chapters / "001.md").write_text(
                "# Chapter\n\nBody.\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001.md"}]),
                encoding="utf-8",
            )
            config = root / "pipeline.toml"
            argv = [
                "--output-dir",
                str(output),
                "--phase",
                "docx",
                "--title",
                "Book",
                "--config",
                str(config),
            ]

            def fake_docx(
                output_path: Path,
                _chapter_dir: Path,
                _manifest: list[dict[str, object]],
                *,
                book_title: str,
                author: str | None = None,
            ) -> None:
                self.assertEqual(book_title, "Book")
                self.assertIsNone(author)
                output_path.write_bytes(b"stable docx")

            with patch(
                "pipeline_graph.book.legacy.build_docx",
                side_effect=fake_docx,
            ) as build_docx:
                config.write_text(
                    template.format(model="translation-model-a"),
                    encoding="utf-8",
                )
                first = prepare_book_graph(argv).execute()
                config.write_text(
                    template.format(model="translation-model-b"),
                    encoding="utf-8",
                )
                second = prepare_book_graph(argv).execute()

            self.assertIn(NODE_DOCX, first.executed)
            self.assertIn(NODE_DOCX, second.skipped)
            build_docx.assert_called_once()

    def test_reference_pdf_target_does_not_plan_page_loading_or_compile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Start")
                document.save(source_pdf)
            output = root / "output"
            output.mkdir()
            (output / "toc.json").write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {
                                "level": 1,
                                "title": "Start",
                                "pdf_page": 1,
                                "printed_page": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                ],
                options=BookGraphOptions(
                    target_artifacts=frozenset({ART_REFERENCE_PDF})
                ),
            )

        self.assertEqual(
            _plan_names(prepared),
            [NODE_SOURCE, "core.toc.load", NODE_REFERENCE_PDF],
        )
        self.assertNotIn(NODE_PAGES_LOAD, _plan_names(prepared))
        self.assertNotIn(NODE_OCR, _plan_names(prepared))
        self.assertNotIn(NODE_COMPILE, _plan_names(prepared))

    def test_plugin_docx_provider_is_still_required_and_verified(self) -> None:
        plugin_name = "acme.publish.docx"
        replacement = NodeSpec(
            name=plugin_name,
            handler=lambda context: NodeResult(
                outputs={ART_DOCX: {"path": "plugin.docx", "sha256": "abc"}}
            ),
            requires=frozenset({ART_READER_CHAPTERS}),
            provides=frozenset({ART_DOCX}),
        )

        class EntryPoint:
            name = "acme_docx"

            @staticmethod
            def load():
                return replacement

        recipe = Recipe(
            id="plugin-docx",
            targets=(ART_WORD_REPORT,),
            enable=(plugin_name,),
            disable=(NODE_DOCX,),
            required_plugins=("acme_docx",),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "pipeline_graph.recipe.NodeRegistry._entry_points",
                return_value=(EntryPoint(),),
            ),
        ):
            output = Path(directory) / "output"
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "Book",
                    "--no-kb",
                    "--no-epub",
                    "--no-bookmarked-pdf",
                ],
                recipe=recipe,
                plugin_allowlist=("acme_docx",),
            )
            names = _plan_names(prepared)
            by_name = {node.name: node for node in prepared.graph.nodes}
            self.assertLess(
                names.index(plugin_name),
                names.index(NODE_VERIFY_WORD),
            )
            self.assertEqual(
                by_name[NODE_VERIFY_WORD].requires,
                frozenset(
                    {
                        "pipeline.argv",
                        ART_SOURCE,
                        ART_PAGES_RAW,
                        ART_TOC,
                        ART_SEMANTIC_CHAPTERS,
                        ART_READER_CHAPTERS,
                        ART_DOCX,
                    }
                ),
            )

            report = output / "audit" / "word-release-report.json"

            def successful_verify(argv: list[str]) -> int:
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('{"ok": true}\n', encoding="utf-8")
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=successful_verify,
            ) as legacy_main:
                result = by_name[NODE_VERIFY_WORD].handler(prepared.context)

        verify_argv = legacy_main.call_args.args[0]
        self.assertEqual(
            result.outputs[ART_WORD_REPORT]["path"],
            str(report.resolve()),
        )
        self.assertEqual(
            verify_argv[
                verify_argv.index("--verification-profile") + 1
            ],
            "word",
        )
        self.assertNotIn("--no-docx", verify_argv)
        self.assertIn("--no-kb", verify_argv)
        self.assertIn("--no-epub", verify_argv)
        self.assertIn("--no-bookmarked-pdf", verify_argv)

    def test_import_is_one_node_and_is_removed_from_wrapped_phase_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                    "--import-ocr-dir",
                    str(Path(directory) / "legacy"),
                ]
            )
        names = _plan_names(prepared)
        self.assertEqual(names.count(NODE_PAGES_IMPORT), 1)
        for phase in ("ocr", "translate", "toc", "compile"):
            with self.subTest(phase=phase):
                self.assertNotIn(
                    "--import-ocr-dir",
                    _phase_argv(prepared.context, phase),
                )

    def test_plugin_can_replace_a_disabled_middle_provider(self) -> None:
        replacement = NodeSpec(
            name="acme.cleanup_headers",
            handler=lambda context: NodeResult(
                outputs={ART_READER_CHAPTERS: context[ART_SEMANTIC_CHAPTERS]}
            ),
            requires=frozenset({ART_SEMANTIC_CHAPTERS}),
            provides=frozenset({ART_READER_CHAPTERS}),
        )

        class EntryPoint:
            name = "acme_cleanup"

            @staticmethod
            def load():
                return replacement

        recipe = Recipe(
            id="custom-cleaner",
            targets=(ART_DOCX,),
            enable=(replacement.name,),
            disable=(
                NODE_SANITIZE,
                NODE_KB,
                NODE_EPUB,
                NODE_REFERENCE_PDF,
                NODE_VERIFY,
            ),
            required_plugins=("acme_cleanup",),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "pipeline_graph.recipe.NodeRegistry._entry_points",
                return_value=(EntryPoint(),),
            ),
        ):
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ],
                recipe=recipe,
                plugin_allowlist=("acme_cleanup",),
            )

        names = _plan_names(prepared)
        self.assertNotIn(NODE_SANITIZE, names)
        self.assertEqual(
            names[-4:],
            [NODE_COMPILE, NODE_SEMANTIC, replacement.name, NODE_DOCX],
        )

    def test_all_orders_stages_and_does_not_proofread_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                ]
            )

        names = _plan_names(prepared)
        by_name = {node.name: node for node in prepared.graph.nodes}
        self.assertEqual(
            names,
            [
                NODE_SOURCE,
                NODE_OCR,
                NODE_TOC_PIPELINE,
                NODE_COMPILE,
                NODE_SEMANTIC,
                NODE_SANITIZE,
                NODE_KB,
                NODE_EPUB,
                NODE_DOCX,
                NODE_REFERENCE_PDF,
                NODE_VERIFY,
            ],
        )
        self.assertEqual(
            by_name[NODE_SEMANTIC].requires,
            frozenset({"pipeline.argv", ART_CHAPTERS}),
        )
        self.assertEqual(
            by_name[NODE_SANITIZE].requires,
            frozenset({"pipeline.argv", ART_SEMANTIC_CHAPTERS}),
        )
        self.assertNotIn(NODE_PROOFREAD, names)
        self.assertNotIn(NODE_TRANSLATE, names)

    def test_include_proofread_places_it_after_ocr_and_before_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(Path(directory) / "output"),
                    "--phase",
                    "all",
                    "--translate-non-chinese",
                ],
                options=BookGraphOptions(include_proofread=True),
            )

        names = _plan_names(prepared)
        self.assertEqual(
            names[names.index(NODE_OCR) : names.index(NODE_TOC_PIPELINE) + 1],
            [NODE_OCR, NODE_PROOFREAD, NODE_TRANSLATE, NODE_TOC_PIPELINE],
        )

    def test_compile_splits_sanitizer_and_removed_publishers_from_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            prepared = prepare_book_graph(
                [
                    str(Path(directory) / "book.pdf"),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                ],
                options=BookGraphOptions(
                    disabled_nodes=frozenset({NODE_EPUB, NODE_DOCX})
                ),
            )
            names = _plan_names(prepared)
            by_name = {node.name: node for node in prepared.graph.nodes}

            self.assertLess(names.index(NODE_COMPILE), names.index(NODE_SEMANTIC))
            self.assertLess(names.index(NODE_SEMANTIC), names.index(NODE_SANITIZE))
            self.assertNotIn(NODE_EPUB, names)
            self.assertNotIn(NODE_DOCX, names)
            self.assertEqual(
                by_name[NODE_VERIFY].requires,
                frozenset(
                    {
                        "pipeline.argv",
                        ART_SOURCE,
                        ART_PAGES_RAW,
                        ART_TOC,
                        ART_SEMANTIC_CHAPTERS,
                        ART_READER_CHAPTERS,
                        ART_KB,
                        ART_REFERENCE_PDF,
                    }
                ),
            )

            report = output / "audit" / "release-report.json"

            def successful_verify(argv: list[str]) -> int:
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('{"ok": true}\n', encoding="utf-8")
                self.assertEqual(argv[argv.index("--phase") + 1], "verify")
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=successful_verify,
            ) as legacy_main:
                result = by_name[NODE_VERIFY].handler(prepared.context)

            verify_argv = legacy_main.call_args.args[0]
            self.assertIn("--no-epub", verify_argv)
            self.assertIn("--no-docx", verify_argv)
            self.assertNotIn("--no-kb", verify_argv)
            self.assertNotIn("--no-bookmarked-pdf", verify_argv)
            self.assertEqual(
                result.outputs["publication.report"]["path"],
                str(report.resolve()),
            )

    def test_verify_materializes_all_noncanonical_publications_before_legacy_gate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            staging = output / ".plugin-stage"
            staging.mkdir()
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Source")
                document.save(source_pdf)
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "Book",
                ]
            )
            by_name = {node.name: node for node in prepared.graph.nodes}
            staged = {
                ART_DOCX: staging / "plugin-release.docx",
                ART_EPUB: staging / "plugin-release.epub",
                ART_KB: staging / "plugin-release.jsonl",
                ART_REFERENCE_PDF: staging / "plugin-release.pdf",
            }
            canonical = {
                ART_DOCX: output / "Book.docx",
                ART_EPUB: output / "Book.epub",
                ART_KB: output / "knowledge_base.jsonl",
                ART_REFERENCE_PDF: output / "Book_带目录.pdf",
            }
            expected_contents: dict[str, bytes] = {}
            provider_calls = {artifact_name: 0 for artifact_name in staged}
            providers: list[NodeSpec] = []
            for index, (artifact_name, path) in enumerate(staged.items(), start=1):
                content = f"plugin-{artifact_name}-{index}".encode()
                expected_contents[artifact_name] = content
                canonical[artifact_name].write_bytes(b"STALE canonical output")

                def publish(
                    _context: GraphContext,
                    *,
                    name: str = artifact_name,
                    destination: Path = path,
                    payload: bytes = content,
                ) -> NodeResult:
                    provider_calls[name] += 1
                    destination.write_bytes(payload)
                    return NodeResult(
                        outputs={
                            name: {
                                "path": str(destination.resolve()),
                                "sha256": hashlib.sha256(payload).hexdigest(),
                            }
                        },
                        fingerprints={name: hashlib.sha256(payload).hexdigest()},
                    )

                providers.append(
                    NodeSpec(
                        name=f"plugin.publish.{index}",
                        handler=publish,
                        provides=frozenset({artifact_name}),
                    )
                )

            chapter_dir = staging / "chapters"
            chapter_dir.mkdir()
            (chapter_dir / "001.md").write_text(
                "# Chapter\n\nPLUGIN reader content.\n",
                encoding="utf-8",
            )
            chapter_manifest = staging / "chapters.json"
            chapter_manifest.write_text(
                json.dumps([{"filename": "001.md"}]),
                encoding="utf-8",
            )
            reader_artifact = _chapters_fixture(
                chapter_dir,
                chapter_manifest,
            )
            prepared.context.values[ART_READER_CHAPTERS] = reader_artifact
            semantic_audit = output / "audit" / "semantic-reconstruction.json"
            semantic_audit.parent.mkdir()
            semantic_audit.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "passed",
                        "summary": {"release_blocked": False},
                        "chapters": [
                            {
                                "filename": "001.md",
                                "footnote_count": 0,
                                "issues": [],
                                "release_blocked": False,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            prepared.context.values[ART_SEMANTIC_CHAPTERS] = {
                **reader_artifact,
                "semantic_contract_version": 1,
                "semantic_audit": str(semantic_audit.resolve()),
                "semantic_audit_sha256": hashlib.sha256(
                    semantic_audit.read_bytes()
                ).hexdigest(),
                "semantic_release_blocked": False,
            }
            page_stage = staging / "pages"
            page_stage.mkdir()
            (page_stage / "page_0001.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": "PLUGIN page",
                        "ocr_model": "plugin-ocr",
                    }
                ),
                encoding="utf-8",
            )
            toc_stage = staging / "toc.json"
            toc_stage.write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {
                                "level": 1,
                                "title": "Chapter",
                                "pdf_page": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            prepared.context.values[ART_SOURCE] = {
                "path": str(source_pdf.resolve()),
                "sha256": hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                "page_count": 1,
            }
            prepared.context.values[ART_PAGES_RAW] = _pages_fixture(page_stage)
            prepared.context.values[ART_TOC] = _file_fixture(toc_stage)

            report = output / "audit" / "release-report.json"

            def verify_canonical_files(argv: list[str]) -> int:
                self.assertEqual(argv[argv.index("--phase") + 1], "verify")
                for artifact_name, path in canonical.items():
                    self.assertEqual(path.read_bytes(), expected_contents[artifact_name])
                    self.assertTrue(staged[artifact_name].exists())
                    self.assertEqual(
                        prepared.context.values[artifact_name]["path"],
                        str(staged[artifact_name].resolve()),
                    )
                self.assertEqual(
                    sorted(path.name for path in output.glob("*.docx")),
                    ["Book.docx"],
                )
                self.assertEqual(
                    sorted(path.name for path in output.glob("*.epub")),
                    ["Book.epub"],
                )
                self.assertEqual(
                    sorted(path.name for path in output.glob("*.jsonl")),
                    ["knowledge_base.jsonl"],
                )
                self.assertEqual(
                    sorted(path.name for path in output.glob("*.pdf")),
                    ["Book_带目录.pdf"],
                )
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text('{"ok": true}\n', encoding="utf-8")
                return 0

            graph = PipelineGraph([*providers, by_name[NODE_VERIFY]])
            executor = GraphExecutor(graph)
            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=verify_canonical_files,
            ) as legacy_main:
                first = executor.execute(
                    prepared.context,
                    targets={ART_REPORT},
                )
                for path in canonical.values():
                    path.write_bytes(b"STALE between graph runs")
                second = executor.execute(
                    prepared.context,
                    targets={ART_REPORT},
                )

            self.assertEqual(legacy_main.call_count, 2)
            self.assertEqual(provider_calls, dict.fromkeys(staged, 1))
            self.assertEqual(
                set(second.skipped),
                {provider.name for provider in providers},
            )
            self.assertIn(NODE_VERIFY, first.executed)
            self.assertIn(NODE_VERIFY, second.executed)
            self.assertEqual(
                second.values[ART_REPORT]["path"],
                str(report.resolve()),
            )

    def test_verify_rejects_noncanonical_publication_in_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            source_pdf = root / "book.pdf"
            source_pdf.write_bytes(b"source identity")
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "Book",
                ]
            )
            plugin_docx = output / "plugin-release.docx"
            plugin_docx.write_bytes(b"plugin docx")
            prepared.context.values[ART_DOCX] = {
                "path": str(plugin_docx.resolve()),
                "sha256": hashlib.sha256(plugin_docx.read_bytes()).hexdigest(),
            }
            verify = next(
                node for node in prepared.graph.nodes if node.name == NODE_VERIFY
            )

            with (
                patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                self.assertRaises(BookGraphConfigurationError),
            ):
                verify.handler(prepared.context)

            legacy_main.assert_not_called()
            self.assertTrue(plugin_docx.is_file())
            self.assertFalse((output / "Book.docx").exists())


class BookGraphExecutionTests(unittest.TestCase):
    @staticmethod
    def _write_page_checkpoint(
        output: Path,
        *,
        ocr_model: str = "mock-ocr",
    ) -> None:
        pages = output / "pages"
        pages.mkdir(parents=True, exist_ok=True)
        (pages / "page_0001.json").write_text(
            json.dumps(
                {
                    "pdf_page": 1,
                    "text": "正文",
                    "ocr_model": ocr_model,
                }
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _make_one_page_pdf(path: Path) -> None:
        with fitz.open() as document:
            document.new_page()
            document.save(path)

    def test_legacy_main_shares_graph_output_lock_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapters = output / "chapters"
            chapters.mkdir(parents=True)
            (chapters / "001.md").write_text(
                "# Chapter\n\nBody\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001.md"}]),
                encoding="utf-8",
            )
            destination = output / "Locked_Book.docx"
            argv = [
                "--output-dir",
                str(output),
                "--phase",
                "docx",
                "--title",
                "Locked Book",
            ]

            with OutputDirectoryLock(output / ".pipeline_graph" / "output.lock"):
                with (
                    patch("book_pipeline.build_docx") as build_docx,
                    self.assertRaises(OutputDirectoryLockedError),
                ):
                    book_pipeline.main(argv)

            build_docx.assert_not_called()
            self.assertFalse(destination.exists())

    def test_graph_owned_lock_bypasses_nested_legacy_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapters = output / "chapters"
            chapters.mkdir(parents=True)
            (chapters / "001.md").write_text(
                "# Chapter\n\nBody\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001.md"}]),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "Graph Book",
                ]
            )

            def fake_docx(
                output_path: Path,
                _chapter_dir: Path,
                _manifest: list[dict[str, object]],
                *,
                book_title: str,
                author: str | None = None,
            ) -> None:
                self.assertEqual(book_title, "Graph Book")
                self.assertIsNone(author)
                self.assertTrue(
                    (output / ".pipeline_graph" / "output.lock").exists()
                )
                output_path.write_bytes(b"docx")

            with patch("book_pipeline.build_docx", side_effect=fake_docx):
                result = prepared.execute()

            self.assertEqual(
                result.executed,
                (NODE_CHAPTERS_LOAD, NODE_SEMANTIC, NODE_SANITIZE, NODE_DOCX),
            )
            self.assertTrue((output / "Graph_Book.docx").is_file())
            # Advisory ownership is released with the file descriptor; the
            # JSON metadata file intentionally remains for diagnostics.
            self.assertTrue((output / ".pipeline_graph" / "output.lock").is_file())

    def test_public_legacy_main_cannot_bypass_graph_lock_across_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "source.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                    "--ocr-backend",
                    "tesseract",
                ]
            )
            entered_unlocked_stage = threading.Event()
            release_unlocked_stage = threading.Event()
            graph_results: list[object] = []
            graph_errors: list[BaseException] = []

            def fake_unlocked(argv: list[str]) -> int:
                self.assertEqual(argv[argv.index("--phase") + 1], "ocr")
                entered_unlocked_stage.set()
                if not release_unlocked_stage.wait(timeout=5):
                    raise TimeoutError("test did not release the Graph OCR stage")
                self._write_page_checkpoint(
                    output,
                    ocr_model="tesseract/eng/psm-3",
                )
                return 0

            def run_graph() -> None:
                try:
                    graph_results.append(prepared.execute())
                except BaseException as exc:  # pragma: no cover - assertion aid.
                    graph_errors.append(exc)

            with (
                patch(
                    "pipeline_graph.book.legacy._main_unlocked",
                    side_effect=fake_unlocked,
                ) as unlocked_main,
                patch(
                    "pipeline_graph.book.legacy.main",
                    wraps=book_pipeline.main,
                ) as public_main,
            ):
                worker = threading.Thread(target=run_graph, daemon=True)
                worker.start()
                self.assertTrue(entered_unlocked_stage.wait(timeout=2))
                try:
                    with self.assertRaises(OutputDirectoryLockedError):
                        book_pipeline.main(
                            [
                                "--output-dir",
                                str(output),
                                "--phase",
                                "status",
                            ]
                        )
                finally:
                    release_unlocked_stage.set()
                    worker.join(timeout=5)

            self.assertFalse(worker.is_alive())
            self.assertEqual(graph_errors, [])
            self.assertEqual(len(graph_results), 1)
            unlocked_main.assert_called_once()
            public_main.assert_called_once()
            self.assertTrue((output / ".pipeline_graph" / "output.lock").is_file())

    def test_reference_pdf_reads_declared_toc_artifact_not_stale_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "source.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Page")
                document.save(source_pdf)
            output = root / "output"
            output.mkdir()
            source_sha = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
            metadata = output / ".pipeline_graph"
            metadata.mkdir()
            (metadata / "source.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "path": str(source_pdf.resolve()),
                        "sha256": source_sha,
                        "page_count": 1,
                        "source_mode": "scanned-pdf",
                        "adapter": NODE_OCR,
                    }
                ),
                encoding="utf-8",
            )
            (output / "toc.json").write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {"level": 1, "title": "STALE", "pdf_page": 1}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            alternate_toc = root / "plugin-toc.json"
            alternate_toc.write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {"level": 1, "title": "PLUGIN", "pdf_page": 1}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "Book",
                ],
                options=BookGraphOptions(
                    target_artifacts=frozenset({ART_REFERENCE_PDF})
                ),
            )
            prepared.context.values[ART_SOURCE] = {
                "path": str(source_pdf),
                "sha256": source_sha,
                "page_count": 1,
            }
            prepared.context.values[ART_TOC] = _file_fixture(alternate_toc)
            handler = {
                node.name: node for node in prepared.graph.nodes
            }[NODE_REFERENCE_PDF].handler

            result = handler(prepared.context)
            reference_pdf = Path(result.outputs[ART_REFERENCE_PDF]["path"])
            with fitz.open(reference_pdf) as document:
                outline = document.get_toc(simple=True)

        self.assertEqual([row[1] for row in outline], ["PLUGIN"])

    def test_proofread_materializes_declared_pages_before_legacy_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            canonical = output / "pages"
            canonical.mkdir(parents=True)
            (canonical / "page_0001.json").write_text(
                json.dumps({"pdf_page": 1, "text": "STALE"}),
                encoding="utf-8",
            )
            alternate = root / "plugin-pages"
            alternate.mkdir()
            (alternate / "page_0001.json").write_text(
                json.dumps({"pdf_page": 1, "text": "PLUGIN"}),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "proofread",
                ]
            )
            prepared.context.values[ART_PAGES_RAW] = _pages_fixture(alternate)
            handler = {
                node.name: node for node in prepared.graph.nodes
            }[NODE_PROOFREAD].handler

            def fake_proofread(argv: list[str]) -> int:
                self.assertEqual(argv[argv.index("--phase") + 1], "proofread")
                payload = json.loads(
                    (canonical / "page_0001.json").read_text(encoding="utf-8")
                )
                self.assertEqual(payload["text"], "PLUGIN")
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_proofread,
            ):
                handler(prepared.context)

    def test_compile_cache_restores_tampered_canonical_plugin_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Source")
                document.save(source_pdf)
            external_pages = root / "plugin-pages"
            external_pages.mkdir()
            external_page = external_pages / "page_0001.json"
            external_page.write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": "PLUGIN PAGE",
                        "ocr_model": "plugin-ocr",
                    }
                ),
                encoding="utf-8",
            )
            external_toc = root / "plugin-toc.json"
            external_toc.write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {
                                "level": 1,
                                "title": "PLUGIN TOC",
                                "pdf_page": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "output"
            provider_calls = {"pages": 0, "toc": 0}

            def provide_pages(_context: GraphContext) -> NodeResult:
                provider_calls["pages"] += 1
                return NodeResult(
                    outputs={ART_PAGES_RAW: _pages_fixture(external_pages)}
                )

            def provide_toc(_context: GraphContext) -> NodeResult:
                provider_calls["toc"] += 1
                return NodeResult(outputs={ART_TOC: _file_fixture(external_toc)})

            pages_node = NodeSpec(
                name="acme.pages.external",
                handler=provide_pages,
                provides=frozenset({ART_PAGES_RAW}),
            )
            toc_node = NodeSpec(
                name="acme.toc.external",
                handler=provide_toc,
                provides=frozenset({ART_TOC}),
            )

            class PagesEntryPoint:
                name = "acme_external_pages"

                @staticmethod
                def load():
                    return pages_node

            class TocEntryPoint:
                name = "acme_external_toc"

                @staticmethod
                def load():
                    return toc_node

            recipe = Recipe(
                id="external-compile-inputs",
                targets=(ART_CHAPTERS,),
                enable=(pages_node.name, toc_node.name),
                disable=(NODE_PAGES_LOAD, NODE_TOC_LOAD),
                required_plugins=(PagesEntryPoint.name, TocEntryPoint.name),
            )
            with patch(
                "pipeline_graph.recipe.NodeRegistry._entry_points",
                return_value=(PagesEntryPoint(), TocEntryPoint()),
            ):
                prepared = prepare_book_graph(
                    [
                        str(source_pdf),
                        "--output-dir",
                        str(output),
                        "--phase",
                        "compile",
                        "--title",
                        "Book",
                        "--no-verify",
                    ],
                    recipe=recipe,
                    plugin_allowlist=(
                        PagesEntryPoint.name,
                        TocEntryPoint.name,
                    ),
                )

            compile_calls: list[list[str]] = []

            def fake_compile(argv: list[str]) -> int:
                compile_calls.append(list(argv))
                self.assertEqual(argv[argv.index("--phase") + 1], "compile")
                canonical_page = json.loads(
                    (output / "pages" / "page_0001.json").read_text(
                        encoding="utf-8"
                    )
                )
                canonical_toc = json.loads(
                    (output / "toc.json").read_text(encoding="utf-8")
                )
                self.assertEqual(canonical_page["text"], "PLUGIN PAGE")
                self.assertEqual(
                    canonical_toc["entries"][0]["title"],
                    "PLUGIN TOC",
                )
                chapters = output / "chapters"
                chapters.mkdir(parents=True, exist_ok=True)
                (chapters / "001.md").write_text(
                    "# Chapter\n\nCompiled from plugin inputs.\n",
                    encoding="utf-8",
                )
                (output / "chapters.json").write_text(
                    json.dumps([{"filename": "001.md"}]),
                    encoding="utf-8",
                )
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_compile,
            ):
                first = prepared.execute()
                canonical_page = output / "pages" / "page_0001.json"
                canonical_toc = output / "toc.json"
                canonical_page.write_text(
                    json.dumps(
                        {
                            "pdf_page": 1,
                            "text": "CORRUPTED PAGE",
                            "ocr_model": "tampered",
                        }
                    ),
                    encoding="utf-8",
                )
                canonical_toc.write_text(
                    json.dumps(
                        {
                            "entries": [
                                {
                                    "level": 1,
                                    "title": "CORRUPTED TOC",
                                    "pdf_page": 1,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                second = prepared.execute()

            self.assertIn(NODE_COMPILE, first.executed)
            self.assertIn(NODE_COMPILE, second.executed)
            self.assertNotIn(NODE_COMPILE, second.skipped)
            self.assertEqual(len(compile_calls), 2)
            self.assertEqual(provider_calls, {"pages": 1, "toc": 1})
            self.assertIn(pages_node.name, second.skipped)
            self.assertIn(toc_node.name, second.skipped)
            self.assertEqual(canonical_page.read_bytes(), external_page.read_bytes())
            self.assertEqual(canonical_toc.read_bytes(), external_toc.read_bytes())

    def test_ocr_rejects_source_provider_that_disagrees_with_input_argv(self) -> None:
        plugin_name = "acme.source.alternate"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argument_pdf = root / "argument.pdf"
            alternate_pdf = root / "alternate.pdf"
            for path, text_value in (
                (argument_pdf, "argument"),
                (alternate_pdf, "alternate"),
            ):
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), text_value)
                    document.save(path)

            replacement = NodeSpec(
                name=plugin_name,
                handler=lambda _context: NodeResult(
                    outputs={
                        ART_SOURCE: {
                            "path": str(alternate_pdf),
                            "sha256": hashlib.sha256(
                                alternate_pdf.read_bytes()
                            ).hexdigest(),
                        }
                    }
                ),
                provides=frozenset({ART_SOURCE}),
            )

            class EntryPoint:
                name = "acme_source"

                @staticmethod
                def load():
                    return replacement

            recipe = Recipe(
                id="alternate-source",
                targets=(ART_PAGES_RAW,),
                enable=(plugin_name,),
                disable=(NODE_SOURCE,),
                required_plugins=("acme_source",),
            )
            with patch(
                "pipeline_graph.recipe.NodeRegistry._entry_points",
                return_value=(EntryPoint(),),
            ):
                prepared = prepare_book_graph(
                    [
                        str(argument_pdf),
                        "--output-dir",
                        str(root / "output"),
                        "--phase",
                        "ocr",
                    ],
                    recipe=recipe,
                    plugin_allowlist=("acme_source",),
                )

            with (
                patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                self.assertRaises(NodeExecutionError) as raised,
            ):
                prepared.execute()

        self.assertEqual(raised.exception.node_name, NODE_OCR)
        self.assertIsInstance(
            raised.exception.cause,
            BookGraphConfigurationError,
        )
        self.assertIn("must match", str(raised.exception.cause))
        legacy_main.assert_not_called()

    def test_plugin_source_consumers_reject_an_existing_different_pdf_binding(
        self,
    ) -> None:
        cases = (
            ("ocr", "ocr", ART_PAGES_RAW, NODE_OCR),
            ("outline", "toc", ART_TOC, NODE_TOC_OUTLINE),
            (
                "reference",
                "compile",
                ART_REFERENCE_PDF,
                NODE_REFERENCE_PDF,
            ),
            ("knowledge-base", "compile", ART_KB, NODE_KB),
        )
        for label, phase, target, expected_node in cases:
            with self.subTest(consumer=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                argument_pdf = root / "argument.pdf"
                bound_pdf = root / "already-bound.pdf"
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), "argument")
                    document.set_toc([[1, "Argument outline", 1]])
                    document.save(argument_pdf)
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), "bound")
                    document.save(bound_pdf)
                output = root / "output"
                metadata = output / ".pipeline_graph"
                metadata.mkdir(parents=True)
                binding = metadata / "source.json"
                binding.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "path": str(bound_pdf.resolve()),
                            "sha256": hashlib.sha256(
                                bound_pdf.read_bytes()
                            ).hexdigest(),
                            "page_count": 1,
                        }
                    ),
                    encoding="utf-8",
                )
                original_binding = binding.read_bytes()
                if label == "reference":
                    (output / "toc.json").write_text(
                        json.dumps(
                            {
                                "page_offset": 0,
                                "printed_pages_per_pdf_page": 1,
                                "entries": [
                                    {
                                        "level": 1,
                                        "title": "Chapter",
                                        "pdf_page": 1,
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

                plugin_name = f"acme.source.boundary.{label}"
                plugin_source = NodeSpec(
                    name=plugin_name,
                    handler=lambda _context: NodeResult(
                        outputs={
                            ART_SOURCE: {
                                "path": str(argument_pdf.resolve()),
                                "sha256": hashlib.sha256(
                                    argument_pdf.read_bytes()
                                ).hexdigest(),
                                "page_count": 1,
                            }
                        }
                    ),
                    provides=frozenset({ART_SOURCE}),
                )

                class EntryPoint:
                    name = "acme_bound_source"

                    @staticmethod
                    def load():
                        return plugin_source

                recipe = Recipe(
                    id=f"bound-source-{label}",
                    targets=(target,),
                    enable=(plugin_name,),
                    disable=(NODE_SOURCE,),
                    required_plugins=(EntryPoint.name,),
                )
                options = BookGraphOptions(
                    toc_source="outline" if label == "outline" else "pipeline",
                    target_artifacts=frozenset({target}),
                )
                with patch(
                    "pipeline_graph.recipe.NodeRegistry._entry_points",
                    return_value=(EntryPoint(),),
                ):
                    prepared = prepare_book_graph(
                        [
                            str(argument_pdf),
                            "--output-dir",
                            str(output),
                            "--phase",
                            phase,
                            "--title",
                            "Book",
                        ],
                        options=options,
                        recipe=recipe,
                        plugin_allowlist=(EntryPoint.name,),
                    )

                if label == "knowledge-base":
                    reader_dir = root / "reader"
                    reader_dir.mkdir()
                    (reader_dir / "001.md").write_text(
                        "# Chapter\n\nBody.\n",
                        encoding="utf-8",
                    )
                    reader_manifest = root / "reader.json"
                    reader_manifest.write_text(
                        json.dumps([{"filename": "001.md"}]),
                        encoding="utf-8",
                    )
                    prepared.context.values[ART_READER_CHAPTERS] = (
                        _chapters_fixture(reader_dir, reader_manifest)
                    )
                    by_name = {node.name: node for node in prepared.graph.nodes}
                    execute = lambda: GraphExecutor(
                        PipelineGraph(
                            [by_name[plugin_name], by_name[NODE_KB]]
                        )
                    ).execute(prepared.context, targets={ART_KB})
                else:
                    execute = prepared.execute

                with (
                    patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                    patch(
                        "pipeline_graph.book.legacy.build_bookmarked_pdf"
                    ) as build_reference,
                    patch(
                        "pipeline_graph.book.legacy.build_knowledge_rows_from_manifest"
                    ) as build_rows,
                    self.assertRaises(NodeExecutionError) as raised,
                ):
                    execute()

                self.assertEqual(raised.exception.node_name, expected_node)
                self.assertIsInstance(raised.exception.cause, SourceBindingError)
                self.assertIn(
                    "already bound to a different source PDF",
                    str(raised.exception.cause),
                )
                self.assertEqual(binding.read_bytes(), original_binding)
                legacy_main.assert_not_called()
                build_reference.assert_not_called()
                build_rows.assert_not_called()
                self.assertFalse((output / "knowledge_base.jsonl").exists())
                self.assertFalse(any(output.glob("*_带目录.pdf")))
                if label == "ocr":
                    self.assertFalse((output / "pages").exists())
                if label == "outline":
                    self.assertFalse((output / "toc.json").exists())

    def test_ocr_semantic_identity_forces_only_content_changes(self) -> None:
        semantic_changes = {
            "dpi": (
                ["--ocr-backend", "tesseract", "--dpi", "200"],
                ["--ocr-backend", "tesseract", "--dpi", "240"],
            ),
            "base": (
                [
                    "--ocr-backend",
                    "glm-ocr",
                    "--ocr-api-base",
                    "https://ocr-a.example.invalid",
                ],
                [
                    "--ocr-backend",
                    "glm-ocr",
                    "--ocr-api-base",
                    "https://ocr-b.example.invalid",
                ],
            ),
            "command": (
                [
                    "--ocr-backend",
                    "coding-plan-mcp",
                    "--ocr-command",
                    "vision-mcp --variant alpha",
                ],
                [
                    "--ocr-backend",
                    "coding-plan-mcp",
                    "--ocr-command",
                    "vision-mcp --variant beta",
                ],
            ),
        }
        for label, (first_options, changed_options) in semantic_changes.items():
            with self.subTest(change=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_pdf = root / "book.pdf"
                self._make_one_page_pdf(source_pdf)
                output = root / "output"
                common = [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                ]
                calls: list[list[str]] = []

                def fake_ocr(argv: list[str]) -> int:
                    calls.append(list(argv))
                    model = "mock-ocr"
                    if "--ocr-cache-model" in argv:
                        model = argv[argv.index("--ocr-cache-model") + 1]
                    self._write_page_checkpoint(output, ocr_model=model)
                    return 0

                with patch(
                    "pipeline_graph.book.legacy._main_unlocked",
                    side_effect=fake_ocr,
                ):
                    prepare_book_graph([*common, *first_options]).execute()
                    identity_path = output / ".pipeline_graph" / "ocr_identity.json"
                    self.assertTrue(identity_path.is_file())
                    prepare_book_graph([*common, *changed_options]).execute()

                self.assertEqual(len(calls), 2)
                self.assertNotIn("--force", calls[0])
                self.assertIn("--force", calls[1])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"
            common = [
                str(source_pdf),
                "--output-dir",
                str(output),
                "--phase",
                "ocr",
                "--ocr-backend",
                "glm-ocr",
            ]
            calls: list[list[str]] = []

            def fake_operational_ocr(argv: list[str]) -> int:
                calls.append(list(argv))
                model = argv[argv.index("--ocr-cache-model") + 1]
                self._write_page_checkpoint(output, ocr_model=model)
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_operational_ocr,
            ):
                prepare_book_graph(common).execute()
                prepare_book_graph(
                    [
                        *common,
                        "--ocr-concurrency",
                        "9",
                        "--ocr-delay",
                        "1.25",
                        "--api-timeout",
                        "900",
                        "--ocr-api-key",
                        "different-secret",
                    ]
                ).execute()

            self.assertEqual(len(calls), 2)
            self.assertNotIn("--force", calls[0])
            self.assertNotIn("--force", calls[1])

    def test_stage_identity_v2_retains_partial_ocr_semantics_per_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page()
                document.new_page()
                document.save(source_pdf)
            output = root / "output"
            common = [
                str(source_pdf),
                "--output-dir",
                str(output),
                "--phase",
                "ocr",
                "--ocr-backend",
                "tesseract",
                "--tesseract-language",
                "chi_sim",
                "--tesseract-psm",
                "3",
            ]
            calls: list[list[str]] = []

            def fake_ocr(argv: list[str]) -> int:
                calls.append(list(argv))
                args = book_pipeline.build_parser().parse_args(argv)
                first = int(args.start_page or 1)
                last = int(args.end_page or 2)
                model = argv[argv.index("--ocr-cache-model") + 1]
                pages = output / "pages"
                pages.mkdir(parents=True, exist_ok=True)
                for page in range(first, last + 1):
                    (pages / f"page_{page:04d}.json").write_text(
                        json.dumps(
                            {
                                "pdf_page": page,
                                "text": f"第 {page} 页正文",
                                "ocr_model": model,
                            }
                        ),
                        encoding="utf-8",
                    )
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                prepare_book_graph(
                    [
                        *common,
                        "--start-page",
                        "1",
                        "--end-page",
                        "1",
                        "--dpi",
                        "180",
                    ]
                ).execute()
                prepare_book_graph(
                    [
                        *common,
                        "--start-page",
                        "2",
                        "--end-page",
                        "2",
                        "--dpi",
                        "240",
                    ]
                ).execute()
                before_full = json.loads(
                    (
                        output / ".pipeline_graph" / "ocr_identity.json"
                    ).read_text(encoding="utf-8")
                )
                prepare_book_graph([*common, "--dpi", "240"]).execute()

            self.assertEqual(before_full["schema_version"], 2)
            self.assertNotEqual(
                before_full["page_fingerprints"]["1"],
                before_full["page_fingerprints"]["2"],
            )
            self.assertEqual(len(calls), 3)
            self.assertNotIn("--force", calls[0])
            self.assertIn("--force", calls[2])
            after_full = json.loads(
                (output / ".pipeline_graph" / "ocr_identity.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                len(set(after_full["page_fingerprints"].values())),
                1,
            )

    def test_partial_text_stage_max_chars_remains_page_scoped(self) -> None:
        cases = (
            (
                "proofread",
                "--proofread-max-chars",
                (),
            ),
            (
                "translation",
                "--translation-max-chars",
                ("--translate-non-chinese",),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stage, max_chars_option, extra in cases:
                with self.subTest(stage=stage):
                    output = root / stage
                    pages = output / "pages"
                    pages.mkdir(parents=True)
                    for page in (1, 2):
                        (pages / f"page_{page:04d}.json").write_text(
                            json.dumps(
                                {
                                    "pdf_page": page,
                                    "text": f"第 {page} 页正文",
                                    "ocr_model": "fixture-ocr",
                                }
                            ),
                            encoding="utf-8",
                        )
                    phase = "translate" if stage == "translation" else stage
                    common = [
                        "--output-dir",
                        str(output),
                        "--phase",
                        phase,
                        *extra,
                    ]
                    calls: list[list[str]] = []
                    with patch(
                        "pipeline_graph.book.legacy._main_unlocked",
                        side_effect=lambda argv: calls.append(list(argv)) or 0,
                    ):
                        prepare_book_graph(
                            [
                                *common,
                                "--start-page",
                                "1",
                                "--end-page",
                                "1",
                                max_chars_option,
                                "8000",
                            ]
                        ).execute()
                        prepare_book_graph(
                            [
                                *common,
                                "--start-page",
                                "2",
                                "--end-page",
                                "2",
                                max_chars_option,
                                "9000",
                            ]
                        ).execute()
                        before_full = json.loads(
                            (
                                output
                                / ".pipeline_graph"
                                / f"{stage}_identity.json"
                            ).read_text(encoding="utf-8")
                        )
                        prepare_book_graph(
                            [*common, max_chars_option, "9000"]
                        ).execute()

                    self.assertEqual(before_full["schema_version"], 2)
                    self.assertNotEqual(
                        before_full["page_fingerprints"]["1"],
                        before_full["page_fingerprints"]["2"],
                    )
                    self.assertEqual(len(calls), 3)
                    self.assertNotIn("--force", calls[0])
                    self.assertIn("--force", calls[2])

    def test_skip_ocr_rejects_semantic_change_without_updating_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"
            common = [
                str(source_pdf),
                "--output-dir",
                str(output),
                "--phase",
                "ocr",
                "--ocr-backend",
                "tesseract",
                "--tesseract-language",
                "chi_sim",
                "--tesseract-psm",
                "3",
            ]

            def fake_ocr(argv: list[str]) -> int:
                model = argv[argv.index("--ocr-cache-model") + 1]
                self._write_page_checkpoint(output, ocr_model=model)
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                prepare_book_graph([*common, "--dpi", "180"]).execute()

            sidecar = output / ".pipeline_graph" / "ocr_identity.json"
            original_sidecar = sidecar.read_bytes()
            with (
                patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                self.assertRaises(NodeExecutionError) as raised,
            ):
                prepare_book_graph(
                    [*common, "--dpi", "240", "--skip-ocr"]
                ).execute()

            self.assertIsInstance(
                raised.exception.cause,
                BookGraphConfigurationError,
            )
            self.assertIn("changed OCR content settings", str(raised.exception.cause))
            legacy_main.assert_not_called()
            self.assertEqual(sidecar.read_bytes(), original_sidecar)

    def test_skip_ocr_rejects_wrong_exact_model_without_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"
            self._write_page_checkpoint(
                output,
                ocr_model="tesseract/chi_sim/psm-30",
            )
            sidecar = output / ".pipeline_graph" / "ocr_identity.json"

            with (
                patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                self.assertRaises(NodeExecutionError) as raised,
            ):
                prepare_book_graph(
                    [
                        str(source_pdf),
                        "--output-dir",
                        str(output),
                        "--phase",
                        "ocr",
                        "--ocr-backend",
                        "tesseract",
                        "--tesseract-language",
                        "chi_sim",
                        "--tesseract-psm",
                        "3",
                        "--skip-ocr",
                    ],
                    options=BookGraphOptions(adopt_existing_output=True),
                ).execute()

            self.assertIsInstance(
                raised.exception.cause,
                BookGraphConfigurationError,
            )
            self.assertIn("different exact OCR identity", str(raised.exception.cause))
            self.assertIn("psm-30", str(raised.exception.cause))
            legacy_main.assert_not_called()
            self.assertFalse(sidecar.exists())

    def test_text_stage_identity_tracks_chunking_and_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "proofread-output"
            self._write_page_checkpoint(output)
            common = [
                "--output-dir",
                str(output),
                "--phase",
                "proofread",
            ]
            calls: list[list[str]] = []

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=lambda argv: calls.append(list(argv)) or 0,
            ):
                prepare_book_graph([*common, "--proofread-max-chars", "8000"]).execute()
                prepare_book_graph([*common, "--proofread-max-chars", "8000"]).execute()
                prepare_book_graph([*common, "--proofread-max-chars", "9000"]).execute()

            self.assertEqual(len(calls), 3)
            self.assertNotIn("--force", calls[0])
            self.assertNotIn("--force", calls[1])
            self.assertIn("--force", calls[2])

        profile_template = """
[profiles.translation]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.example.invalid"
model = "deepseek-v4-flash"
credential_env = "DEEPSEEK_API_KEY"
thinking = "{thinking}"

[pipeline]
translation_profile = "translation"
""".strip()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "translation-output"
            config = root / "pipeline.toml"
            self._write_page_checkpoint(output)
            common = [
                "--output-dir",
                str(output),
                "--phase",
                "translate",
                "--translate-non-chinese",
                "--config",
                str(config),
            ]
            calls = []

            def run_with_thinking(thinking: str) -> None:
                config.write_text(
                    profile_template.format(thinking=thinking),
                    encoding="utf-8",
                )
                prepare_book_graph(common).execute()

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=lambda argv: calls.append(list(argv)) or 0,
            ):
                run_with_thinking("disabled")
                run_with_thinking("disabled")
                run_with_thinking("enabled")

            self.assertEqual(len(calls), 3)
            self.assertNotIn("--force", calls[0])
            self.assertNotIn("--force", calls[1])
            self.assertIn("--force", calls[2])

    def test_ocr_model_and_automatic_cache_model_remain_complete_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                    "--ocr-backend",
                    "glm-ocr",
                    "--ocr-model",
                    "glm-ocr",
                ]
            )

            def fake_ocr(argv: list[str]) -> int:
                self.assertEqual(argv[argv.index("--ocr-model") + 1], "glm-ocr")
                self.assertEqual(
                    argv[argv.index("--ocr-cache-model") + 1],
                    "glm-ocr",
                )
                self._write_page_checkpoint(output, ocr_model="glm-ocr")
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                prepared.execute()

    def test_ocr_wrapper_injects_exact_tesseract_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page()
                document.save(source_pdf)
            output = root / "output"
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                    "--ocr-backend",
                    "tesseract",
                    "--tesseract-language",
                    "chi_sim+eng",
                    "--tesseract-psm",
                    "6",
                ]
            )

            def fake_ocr(argv: list[str]) -> int:
                self.assertIn("--ocr-cache-model", argv)
                prefix_index = argv.index("--ocr-cache-model")
                self.assertEqual(
                    argv[prefix_index + 1],
                    "tesseract/chi_sim+eng/psm-6",
                )
                pages = output / "pages"
                pages.mkdir(parents=True, exist_ok=True)
                (pages / "page_0001.json").write_text(
                    json.dumps(
                        {
                            "pdf_page": 1,
                            "text": "正文",
                            "ocr_model": "tesseract/chi_sim+eng/psm-6",
                        }
                    ),
                    encoding="utf-8",
                )
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                result = prepared.execute()

        self.assertEqual(result.executed, (NODE_SOURCE, NODE_OCR))

    def test_graph_state_and_events_never_store_raw_api_keys(self) -> None:
        secrets = ("raw-glm-secret", "raw-ocr-secret", "raw-translation-secret")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001.md").write_text("# 第一章\n\n正文\n", encoding="utf-8")
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001.md"}]),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "Book",
                    "--api-key",
                    secrets[0],
                    "--ocr-api-key",
                    secrets[1],
                    "--translation-api-key",
                    secrets[2],
                ]
            )

            def fake_docx(
                output_path: Path,
                _chapter_dir: Path,
                _manifest: list[dict[str, object]],
                *,
                book_title: str,
                author: str | None = None,
            ) -> None:
                self.assertEqual(book_title, "Book")
                self.assertIsNone(author)
                output_path.write_bytes(b"docx")

            with patch(
                "pipeline_graph.book.legacy.build_docx",
                side_effect=fake_docx,
            ):
                result = prepared.execute()
            persisted = (
                result.state_path.read_text(encoding="utf-8")
                + result.events_path.read_text(encoding="utf-8")
            )
            public_values = json.dumps(dict(result.values), ensure_ascii=False)

        for secret in secrets:
            self.assertNotIn(secret, persisted)
            self.assertNotIn(secret, public_values)
        self.assertNotIn("pipeline.argv", result.values)

    def test_endpoint_userinfo_and_query_secrets_are_never_persisted(self) -> None:
        endpoint = (
            "https://endpoint-user:endpoint-password@api.example.invalid/"
            "v1/path-token-secret"
            "?access_token=query-secret"
        )
        secrets = (
            endpoint,
            "endpoint-user",
            "endpoint-password",
            "path-token-secret",
            "query-secret",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "book.pdf"
            self._make_one_page_pdf(source_pdf)
            output = root / "output"

            def fake_ocr(argv: list[str]) -> int:
                model = argv[argv.index("--ocr-cache-model") + 1]
                self._write_page_checkpoint(output, ocr_model=model)
                return 0

            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=fake_ocr,
            ):
                result = prepare_book_graph(
                    [
                        str(source_pdf),
                        "--output-dir",
                        str(output),
                        "--phase",
                        "ocr",
                        "--ocr-backend",
                        "glm-ocr",
                        "--ocr-api-base",
                        endpoint,
                        "--api-base",
                        endpoint,
                    ]
                ).execute()

            sidecar = output / ".pipeline_graph" / "ocr_identity.json"
            identity_text = sidecar.read_text(encoding="utf-8")
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((output / ".pipeline_graph").rglob("*"))
                if path.is_file()
            )
            public_values = json.dumps(dict(result.values), ensure_ascii=False)

        self.assertIn("https://api.example.invalid/path-sha256:", identity_text)
        for secret in secrets:
            self.assertNotIn(secret, identity_text)
            self.assertNotIn(secret, persisted)
            self.assertNotIn(secret, public_values)
        self.assertNotIn("pipeline.argv", result.values)

    def test_cached_file_artifact_must_belong_to_current_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_output = root / "old"
            new_output = root / "new"
            calls: list[Path] = []
            document_bytes = b"docx"
            document_hash = hashlib.sha256(document_bytes).hexdigest()

            def publish(context: GraphContext) -> NodeResult:
                path = context.output_dir / "book.docx"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(document_bytes)
                calls.append(path)
                return NodeResult(
                    outputs={
                        ART_DOCX: {
                            "path": str(path.resolve()),
                            "sha256": document_hash,
                        }
                    },
                    fingerprints={ART_DOCX: document_hash},
                )

            node = NodeSpec(
                name="test.publish.docx",
                handler=publish,
                provides=frozenset({ART_DOCX}),
                fingerprint="same-request",
                cache_validator=_single_file_is_current,
            )
            executor = GraphExecutor(PipelineGraph([node]))
            executor.execute(GraphContext(old_output), targets={ART_DOCX})
            (new_output / ".pipeline_graph").mkdir(parents=True)
            shutil.copyfile(
                old_output / ".pipeline_graph" / "state.json",
                new_output / ".pipeline_graph" / "state.json",
            )

            result = executor.execute(GraphContext(new_output), targets={ART_DOCX})

        self.assertEqual(result.executed, (node.name,))
        self.assertEqual(
            calls,
            [
                (old_output / "book.docx").resolve(),
                (new_output / "book.docx").resolve(),
            ],
        )
        self.assertEqual(
            result.values[ART_DOCX]["path"],
            str((new_output / "book.docx").resolve()),
        )

    def test_import_cache_is_invalidated_by_missing_or_corrupt_target_page(self) -> None:
        for mutation in ("missing", "corrupt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "legacy" / "pages"
                source.mkdir(parents=True)
                (source / "page_0001.json").write_text(
                    '{"pdf_page": 1, "text": "source"}\n',
                    encoding="utf-8",
                )
                output = root / "output"
                output.mkdir()
                argv = [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "status",
                    "--import-ocr-dir",
                    str(source.parent),
                ]
                options = BookGraphOptions(
                    target_artifacts=frozenset({ART_PAGES_IMPORTED})
                )

                def import_page(_source: Path, target: Path) -> int:
                    target_pages = target / "pages"
                    target_pages.mkdir(parents=True, exist_ok=True)
                    (target_pages / "page_0001.json").write_text(
                        '{"pdf_page": 1, "text": "imported"}\n',
                        encoding="utf-8",
                    )
                    return 1

                with patch(
                    "pipeline_graph.book.legacy.import_existing_ocr",
                    side_effect=import_page,
                ):
                    first = prepare_book_graph(argv, options=options).execute()
                self.assertEqual(first.executed, (NODE_PAGES_IMPORT,))

                imported_page = output / "pages" / "page_0001.json"
                if mutation == "missing":
                    imported_page.unlink()
                else:
                    imported_page.write_text("{broken", encoding="utf-8")

                with patch(
                    "pipeline_graph.book.legacy.import_existing_ocr",
                    side_effect=import_page,
                ) as importer:
                    second = prepare_book_graph(argv, options=options).execute()

                self.assertEqual(second.executed, (NODE_PAGES_IMPORT,))
                importer.assert_called_once()

    def test_import_source_shrink_removes_stale_destination_page_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "legacy"
            source_pages = source_root / "pages"
            source_pages.mkdir(parents=True)
            for page in (1, 2):
                (source_pages / f"page_{page:04d}.json").write_text(
                    json.dumps(
                        {
                            "pdf_page": page,
                            "text": f"source page {page}",
                        }
                    ),
                    encoding="utf-8",
                )
            output = root / "output"
            output.mkdir()
            argv = [
                "--output-dir",
                str(output),
                "--phase",
                "status",
                "--import-ocr-dir",
                str(source_root),
            ]
            options = BookGraphOptions(
                target_artifacts=frozenset({ART_PAGES_IMPORTED})
            )

            def import_current_pages(source: Path, target: Path) -> int:
                destination = target / "pages"
                destination.mkdir(parents=True, exist_ok=True)
                imported = 0
                for source_page in sorted((source / "pages").glob("page_*.json")):
                    shutil.copyfile(source_page, destination / source_page.name)
                    (destination / source_page.with_suffix(".md").name).write_text(
                        f"markdown for {source_page.stem}\n",
                        encoding="utf-8",
                    )
                    imported += 1
                return imported

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr",
                side_effect=import_current_pages,
            ):
                first = prepare_book_graph(argv, options=options).execute()
            self.assertEqual(first.executed, (NODE_PAGES_IMPORT,))
            stale_json = output / "pages" / "page_0002.json"
            stale_markdown = output / "pages" / "page_0002.md"
            self.assertTrue(stale_json.is_file())
            self.assertTrue(stale_markdown.is_file())

            (source_pages / "page_0002.json").unlink()
            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr",
                side_effect=import_current_pages,
            ) as importer:
                second = prepare_book_graph(argv, options=options).execute()

            self.assertEqual(second.executed, (NODE_PAGES_IMPORT,))
            importer.assert_called_once()
            self.assertFalse(stale_json.exists())
            self.assertFalse(stale_markdown.exists())
            self.assertTrue((output / "pages" / "page_0001.json").is_file())
            state = json.loads(second.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["nodes"][NODE_PAGES_IMPORT]["metadata"][
                    "removed_stale_pages"
                ],
                [2],
            )

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr"
            ) as cached_importer:
                third = prepare_book_graph(argv, options=options).execute()
            self.assertEqual(third.skipped, (NODE_PAGES_IMPORT,))
            cached_importer.assert_not_called()

    def test_partial_import_preserves_unrelated_existing_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "legacy"
            source_pages = source_root / "pages"
            source_pages.mkdir(parents=True)
            (source_pages / "page_0002.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 2,
                        "text": "new imported page 2",
                    }
                ),
                encoding="utf-8",
            )
            output = root / "output"
            destination = output / "pages"
            destination.mkdir(parents=True)
            unrelated: dict[Path, bytes] = {}
            for page in (1, 3):
                json_path = destination / f"page_{page:04d}.json"
                md_path = destination / f"page_{page:04d}.md"
                json_path.write_text(
                    json.dumps(
                        {
                            "pdf_page": page,
                            "text": f"existing unrelated page {page}",
                        }
                    ),
                    encoding="utf-8",
                )
                md_path.write_text(
                    f"existing unrelated markdown {page}\n",
                    encoding="utf-8",
                )
                unrelated[json_path] = json_path.read_bytes()
                unrelated[md_path] = md_path.read_bytes()
            argv = [
                "--output-dir",
                str(output),
                "--phase",
                "status",
                "--import-ocr-dir",
                str(source_root),
            ]
            options = BookGraphOptions(
                target_artifacts=frozenset({ART_PAGES_IMPORTED})
            )

            def import_partial(source: Path, target: Path) -> int:
                target_pages = target / "pages"
                target_pages.mkdir(parents=True, exist_ok=True)
                source_page = source / "pages" / "page_0002.json"
                shutil.copyfile(source_page, target_pages / source_page.name)
                (target_pages / "page_0002.md").write_text(
                    "imported markdown 2\n",
                    encoding="utf-8",
                )
                return 1

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr",
                side_effect=import_partial,
            ):
                result = prepare_book_graph(argv, options=options).execute()

            self.assertEqual(result.executed, (NODE_PAGES_IMPORT,))
            for path, content in unrelated.items():
                self.assertEqual(path.read_bytes(), content)
            self.assertIn(
                "new imported page 2",
                (destination / "page_0002.json").read_text(encoding="utf-8"),
            )
            management = json.loads(
                (
                    output / ".pipeline_graph" / "import_identity.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(set(management["managed_pages"]), {"2"})

    def test_modified_managed_import_page_is_preserved_when_source_shrinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "legacy"
            source_pages = source_root / "pages"
            source_pages.mkdir(parents=True)
            for page in (1, 2):
                (source_pages / f"page_{page:04d}.json").write_text(
                    json.dumps(
                        {
                            "pdf_page": page,
                            "text": f"imported source page {page}",
                        }
                    ),
                    encoding="utf-8",
                )
            output = root / "output"
            output.mkdir()
            argv = [
                "--output-dir",
                str(output),
                "--phase",
                "status",
                "--import-ocr-dir",
                str(source_root),
            ]
            options = BookGraphOptions(
                target_artifacts=frozenset({ART_PAGES_IMPORTED})
            )

            def import_current_pages(source: Path, target: Path) -> int:
                target_pages = target / "pages"
                target_pages.mkdir(parents=True, exist_ok=True)
                count = 0
                for source_page in sorted((source / "pages").glob("page_*.json")):
                    shutil.copyfile(source_page, target_pages / source_page.name)
                    (target_pages / source_page.with_suffix(".md").name).write_text(
                        f"imported markdown {source_page.stem}\n",
                        encoding="utf-8",
                    )
                    count += 1
                return count

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr",
                side_effect=import_current_pages,
            ):
                prepare_book_graph(argv, options=options).execute()

            protected_json = output / "pages" / "page_0002.json"
            protected_markdown = output / "pages" / "page_0002.md"
            protected_json.write_text(
                json.dumps(
                    {
                        "pdf_page": 2,
                        "text": "人工修订后的第二页",
                        "translation": "人工翻译",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            protected_markdown.write_text(
                "人工修订后的 Markdown\n",
                encoding="utf-8",
            )
            protected_json_content = protected_json.read_bytes()
            protected_markdown_content = protected_markdown.read_bytes()
            (source_pages / "page_0002.json").unlink()

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr",
                side_effect=import_current_pages,
            ) as importer:
                second = prepare_book_graph(argv, options=options).execute()

            self.assertEqual(second.executed, (NODE_PAGES_IMPORT,))
            importer.assert_called_once()
            self.assertEqual(protected_json.read_bytes(), protected_json_content)
            self.assertEqual(
                protected_markdown.read_bytes(),
                protected_markdown_content,
            )
            state = json.loads(second.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["nodes"][NODE_PAGES_IMPORT]["metadata"][
                    "removed_stale_pages"
                ],
                [],
            )
            management = json.loads(
                (
                    output / ".pipeline_graph" / "import_identity.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(set(management["managed_pages"]), {"1"})

            with patch(
                "pipeline_graph.book.legacy.import_existing_ocr"
            ) as cached_importer:
                third = prepare_book_graph(argv, options=options).execute()
            self.assertEqual(third.skipped, (NODE_PAGES_IMPORT,))
            cached_importer.assert_not_called()

    def test_output_directory_is_bound_to_one_source_pdf_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            for path, title in ((first_pdf, "First"), (second_pdf, "Second")):
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), title)
                    document.set_toc([[1, title, 1]])
                    document.save(path)
            output = root / "output"
            first = prepare_book_graph(
                [str(first_pdf), "-o", str(output), "--phase", "toc"],
                options=BookGraphOptions(toc_source="outline"),
            )
            first.execute()
            second = prepare_book_graph(
                [str(second_pdf), "-o", str(output), "--phase", "toc"],
                options=BookGraphOptions(toc_source="outline"),
            )
            with self.assertRaises(NodeExecutionError) as raised:
                second.execute()

        self.assertEqual(raised.exception.node_name, NODE_SOURCE)
        self.assertIsInstance(raised.exception.cause, SourceBindingError)

    def test_output_directory_is_bound_to_one_source_adapter(self) -> None:
        for first_mode, second_mode in (
            ("scanned-pdf", "text-pdf"),
            ("text-pdf", "scanned-pdf"),
        ):
            with self.subTest(
                first=first_mode,
                second=second_mode,
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_pdf = root / "source.pdf"
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), "Embedded text")
                    document.set_toc([[1, "Start", 1]])
                    document.save(source_pdf)
                output = root / "output"
                prepare_book_graph(
                    [str(source_pdf), "-o", str(output), "--phase", "toc"],
                    options=BookGraphOptions(
                        source_mode=first_mode,
                        toc_source="outline",
                    ),
                ).execute()
                binding_path = output / ".pipeline_graph" / "source.json"
                original_binding = json.loads(binding_path.read_text(encoding="utf-8"))
                self.assertEqual(original_binding["schema_version"], 2)
                self.assertEqual(original_binding["source_mode"], first_mode)
                self.assertEqual(
                    original_binding["adapter"],
                    NODE_TEXT_EXTRACT if first_mode == "text-pdf" else NODE_OCR,
                )

                switched = prepare_book_graph(
                    [str(source_pdf), "-o", str(output), "--phase", "toc"],
                    options=BookGraphOptions(
                        source_mode=second_mode,
                        toc_source="outline",
                    ),
                )
                with self.assertRaises(NodeExecutionError) as raised:
                    switched.execute()

                self.assertEqual(raised.exception.node_name, NODE_SOURCE)
                self.assertIsInstance(raised.exception.cause, SourceBindingError)
                self.assertIn("different source adapter", str(raised.exception.cause))
                self.assertEqual(
                    json.loads(binding_path.read_text(encoding="utf-8")),
                    original_binding,
                )

    def test_legacy_source_binding_migrates_from_page_model_identity(self) -> None:
        for source_mode, page_model, adapter in (
            ("scanned-pdf", "tesseract/eng/psm-3", NODE_OCR),
            (
                "text-pdf",
                "text-layer/pymupdf-v1/boundary-blank",
                NODE_TEXT_EXTRACT,
            ),
        ):
            with self.subTest(source_mode=source_mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source_pdf = root / "source.pdf"
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), "Embedded text")
                    document.set_toc([[1, "Start", 1]])
                    document.save(source_pdf)
                output = root / "output"
                self._write_page_checkpoint(output, ocr_model=page_model)
                metadata = output / ".pipeline_graph"
                metadata.mkdir(parents=True, exist_ok=True)
                source_sha = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
                (metadata / "source.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "path": str(source_pdf.resolve()),
                            "sha256": source_sha,
                            "page_count": 1,
                        }
                    ),
                    encoding="utf-8",
                )

                prepare_book_graph(
                    [str(source_pdf), "-o", str(output), "--phase", "toc"],
                    options=BookGraphOptions(
                        source_mode=source_mode,
                        toc_source="outline",
                    ),
                ).execute()

                binding = json.loads(
                    (metadata / "source.json").read_text(encoding="utf-8")
                )
                self.assertEqual(binding["schema_version"], 2)
                self.assertEqual(binding["source_mode"], source_mode)
                self.assertEqual(binding["adapter"], adapter)
                self.assertTrue(binding["migrated_legacy_binding"])

    def test_unbound_legacy_pages_require_explicit_complete_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "source.pdf"
            with fitz.open() as document:
                document.new_page()
                document.set_toc([[1, "Start", 1]])
                document.save(source_pdf)
            output = root / "output"
            pages = output / "pages"
            pages.mkdir(parents=True)
            (pages / "page_0001.json").write_text("{}\n", encoding="utf-8")

            refused = prepare_book_graph(
                [str(source_pdf), "-o", str(output), "--phase", "toc"],
                options=BookGraphOptions(toc_source="outline"),
            )
            with self.assertRaises(NodeExecutionError) as raised:
                refused.execute()
            self.assertIsInstance(raised.exception.cause, SourceBindingError)
            self.assertFalse((output / ".pipeline_graph" / "source.json").exists())

            adopted = prepare_book_graph(
                [str(source_pdf), "-o", str(output), "--phase", "toc"],
                options=BookGraphOptions(
                    toc_source="outline",
                    adopt_existing_output=True,
                ),
            )
            adopted.execute()
            binding = json.loads(
                (output / ".pipeline_graph" / "source.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(binding["adopted_existing_output"])

    def test_source_binding_failure_precedes_checkpoint_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            for path, title in ((first_pdf, "First"), (second_pdf, "Second")):
                with fitz.open() as document:
                    document.new_page().insert_text((72, 72), title)
                    document.set_toc([[1, title, 1]])
                    document.save(path)
            output = root / "output"
            prepare_book_graph(
                [str(first_pdf), "-o", str(output), "--phase", "toc"],
                options=BookGraphOptions(toc_source="outline"),
            ).execute()
            legacy_dir = root / "legacy" / "pages"
            legacy_dir.mkdir(parents=True)
            (legacy_dir / "page_0001.json").write_text(
                '{"pdf_page": 1, "text": "wrong source"}\n',
                encoding="utf-8",
            )
            second = prepare_book_graph(
                [
                    str(second_pdf),
                    "-o",
                    str(output),
                    "--phase",
                    "toc",
                    "--import-ocr-dir",
                    str(legacy_dir.parent),
                ],
                options=BookGraphOptions(toc_source="outline"),
            )
            with (
                patch("pipeline_graph.book.legacy.import_existing_ocr") as importer,
                self.assertRaises(NodeExecutionError),
            ):
                second.execute()
            importer.assert_not_called()

    def test_status_does_not_create_a_missing_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "missing"
            prepared = prepare_book_graph(
                ["--output-dir", str(output), "--phase", "status"]
            )
            self.assertEqual(_plan_names(prepared), [NODE_STATUS])
            with self.assertRaises(FileNotFoundError):
                prepared.execute()
            self.assertFalse(output.exists())

    def test_graph_status_uses_exact_tesseract_and_glm_ocr_identities(self) -> None:
        cases = (
            (
                "tesseract",
                [
                    "--ocr-backend",
                    "tesseract",
                    "--tesseract-language",
                    "chi_sim",
                    "--tesseract-psm",
                    "3",
                ],
                "tesseract/chi_sim/psm-3",
                "tesseract/chi_sim/psm-30",
            ),
            (
                "glm",
                [
                    "--ocr-backend",
                    "coding-plan-mcp",
                    "--ocr-reading-direction",
                    "horizontal",
                ],
                "coding-plan/glm-4.6v-vision-mcp/horizontal-v2",
                "coding-plan/glm-4.6v-vision-mcp/horizontal-v20",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"Z_AI_VISION_MODEL": "glm-4.6v"}):
                for name, model_args, exact, similar_prefix in cases:
                    with self.subTest(backend=name):
                        output = root / name
                        self._write_page_checkpoint(output, ocr_model=exact)
                        pages = output / "pages"
                        (pages / "page_0002.json").write_text(
                            json.dumps(
                                {
                                    "pdf_page": 2,
                                    "text": "正文",
                                    "ocr_model": similar_prefix,
                                }
                            ),
                            encoding="utf-8",
                        )
                        result = prepare_book_graph(
                            [
                                "--output-dir",
                                str(output),
                                "--phase",
                                "status",
                                *model_args,
                            ]
                        ).execute()
                        self.assertEqual(
                            result.values["pipeline.status"][
                                "ocr_pages_profile_fresh"
                            ],
                            1,
                        )

    def test_graph_status_keeps_explicit_ocr_prefix_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            self._write_page_checkpoint(
                output,
                ocr_model="tesseract/chi_sim/psm-3",
            )
            (output / "pages" / "page_0002.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 2,
                        "text": "正文",
                        "ocr_model": "tesseract/chi_sim/psm-30",
                    }
                ),
                encoding="utf-8",
            )
            result = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "status",
                    "--ocr-backend",
                    "tesseract",
                    "--ocr-cache-model-prefix",
                    "tesseract/chi_sim/psm-3",
                ]
            ).execute()

        self.assertEqual(
            result.values["pipeline.status"]["ocr_pages_profile_fresh"],
            2,
        )

    def test_text_pdf_graph_status_uses_text_layer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            self._write_page_checkpoint(
                output,
                ocr_model="text-layer/pymupdf-v1",
            )
            pages = output / "pages"
            (pages / "page_0002.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 2,
                        "text": "[空白页]",
                        "ocr_model": "text-layer/pymupdf-v1/boundary-blank",
                    }
                ),
                encoding="utf-8",
            )
            (pages / "page_0003.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 3,
                        "text": "OCR text",
                        "ocr_model": "tesseract/eng/psm-3",
                    }
                ),
                encoding="utf-8",
            )

            result = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "status",
                    "--ocr-backend",
                    "tesseract",
                    "--tesseract-language",
                    "eng",
                    "--tesseract-psm",
                    "3",
                ],
                options=BookGraphOptions(source_mode="text-pdf"),
            ).execute()

        self.assertEqual(
            result.values["pipeline.status"]["ocr_pages_profile_fresh"],
            2,
        )

    def test_standalone_docx_loads_chapters_without_ocr_or_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_start.md").write_text(
                "# Start\n\nBody.[^note]\n\n[^note]: Source note.\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001_start.md"}]),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "book",
                ]
            )

            def build_fake_docx(
                output_path: Path,
                supplied_chapter_dir: Path,
                manifest: list[dict[str, object]],
                *,
                book_title: str,
                author: str | None = None,
            ) -> None:
                self.assertEqual(supplied_chapter_dir, chapter_dir.resolve())
                self.assertEqual(manifest, [{"filename": "001_start.md"}])
                self.assertEqual(book_title, "book")
                self.assertIsNone(author)
                output_path.write_bytes(b"docx")

            with patch(
                "pipeline_graph.book.legacy.build_docx",
                side_effect=build_fake_docx,
            ) as build_docx:
                result = prepared.execute()

            self.assertEqual(
                result.plan,
                (NODE_CHAPTERS_LOAD, NODE_SEMANTIC, NODE_SANITIZE, NODE_DOCX),
            )
            self.assertNotIn(NODE_SOURCE, result.plan)
            self.assertNotIn(NODE_OCR, result.plan)
            self.assertNotIn(NODE_PAGES_LOAD, result.plan)
            build_docx.assert_called_once()
            self.assertEqual(
                build_docx.call_args.args[0],
                (output / "book.docx").resolve(),
            )
            semantic_audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(semantic_audit["status"], "passed")
            self.assertFalse(semantic_audit["summary"]["release_blocked"])
            self.assertEqual(semantic_audit["summary"]["footnote_count"], 1)
            self.assertEqual(
                semantic_audit["contract_mode"],
                "markdown-footnotes-only",
            )
            semantic_artifact = result.values[ART_SEMANTIC_CHAPTERS]
            self.assertEqual(
                semantic_artifact["semantic_audit"],
                str(
                    (
                        output
                        / ".pipeline_graph"
                        / "draft-semantic-audit.json"
                    ).resolve()
                ),
            )
            self.assertFalse(semantic_artifact["semantic_release_blocked"])

    def test_semantic_and_sanitize_are_stable_across_four_reused_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_start.md").write_text(
                "# Start\n\n"
                "<!-- source-pdf: source.pdf -->\n"
                "<!-- PDF_PAGE: 1 -->\n\n"
                "Body.[^note]\n\n[^note]: Source note.\n",
                encoding="utf-8",
            )
            book_pipeline.write_json(
                output / "chapters.json",
                [{"filename": "001_start.md"}],
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "book",
                ],
                options=BookGraphOptions(
                    force_nodes=frozenset({NODE_SEMANTIC})
                ),
            )

            def build_fake_docx(
                output_path: Path,
                _chapter_dir: Path,
                _manifest: list[dict[str, object]],
                **_kwargs: object,
            ) -> None:
                output_path.write_bytes(b"docx")

            draft_audit_bytes: list[bytes] = []
            semantic_values: list[object] = []
            reader_values: list[object] = []
            executions: list[tuple[str, ...]] = []
            skips: list[tuple[str, ...]] = []
            with patch(
                "pipeline_graph.book.legacy.build_docx",
                side_effect=build_fake_docx,
            ):
                for _ in range(4):
                    result = prepared.execute()
                    draft_audit_bytes.append(
                        (
                            output
                            / ".pipeline_graph"
                            / "draft-semantic-audit.json"
                        ).read_bytes()
                    )
                    semantic_values.append(result.values[ART_SEMANTIC_CHAPTERS])
                    reader_values.append(result.values[ART_READER_CHAPTERS])
                    executions.append(result.executed)
                    skips.append(result.skipped)

            self.assertTrue(
                all(value == draft_audit_bytes[0] for value in draft_audit_bytes)
            )
            self.assertTrue(
                all(value == semantic_values[0] for value in semantic_values)
            )
            self.assertTrue(all(value == reader_values[0] for value in reader_values))
            self.assertTrue(all(NODE_SEMANTIC in executed for executed in executions))
            self.assertIn(NODE_SANITIZE, executions[0])
            self.assertTrue(all(NODE_SANITIZE in skipped for skipped in skips[1:]))

            semantic_artifact = semantic_values[-1]
            reader_artifact = reader_values[-1]
            self.assertIsInstance(semantic_artifact, dict)
            self.assertIsInstance(reader_artifact, dict)
            self.assertEqual(
                semantic_artifact["semantic_audit"],
                str(
                    (
                        output
                        / ".pipeline_graph"
                        / "draft-semantic-audit.json"
                    ).resolve()
                ),
            )
            self.assertEqual(
                reader_artifact["reader_semantic_audit"],
                str(
                    (
                        output
                        / ".pipeline_graph"
                        / "reader-semantic-audit.json"
                    ).resolve()
                ),
            )
            self.assertNotEqual(
                semantic_artifact["semantic_audit"],
                reader_artifact["reader_semantic_audit"],
            )
            self.assertNotIn(
                "source_markdown_sha256",
                json.loads(draft_audit_bytes[-1])["chapters"][0],
            )
            self.assertIn(
                "source_markdown_sha256",
                json.loads(
                    Path(reader_artifact["reader_semantic_audit"]).read_text(
                        encoding="utf-8"
                    )
                )["chapters"][0],
            )
            self.assertIn("PDF_PAGE", (
                output / ".pipeline_graph" / "chapter_drafts" / "001_start.md"
            ).read_text(encoding="utf-8"))
            self.assertNotIn(
                "PDF_PAGE",
                (output / "chapters" / "001_start.md").read_text(
                    encoding="utf-8"
                ),
            )

    def test_compile_semantic_node_requires_reconstruction_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_start.md").write_text(
                "# Start\n\nBody.\n",
                encoding="utf-8",
            )
            manifest_path = output / "chapters.json"
            manifest_path.write_text(
                json.dumps([{"filename": "001_start.md"}]),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    str(root / "source.pdf"),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "book",
                ]
            )
            prepared.context.values[ART_CHAPTERS] = _chapters_fixture(
                chapter_dir,
                manifest_path,
            )
            semantic = next(
                node
                for node in prepared.graph.nodes
                if node.name == NODE_SEMANTIC
            )

            with self.assertRaises(SemanticReconstructionError) as caught:
                semantic.handler(prepared.context)

            self.assertIn(
                "audit is missing after chapter compile",
                str(caught.exception),
            )

    def test_semantic_node_blocks_unclosed_markdown_footnotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_start.md").write_text(
                "# Start\n\nBody with a missing definition.[^missing]\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"filename": "001_start.md"}]),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "book",
                ]
            )

            with (
                patch("pipeline_graph.book.legacy.build_docx") as build_docx,
                self.assertRaises(NodeExecutionError) as caught,
            ):
                prepared.execute()

            self.assertIsInstance(
                caught.exception.cause,
                SemanticReconstructionError,
            )
            self.assertIn("one-to-one closed set", str(caught.exception.cause))
            build_docx.assert_not_called()
            self.assertFalse(
                (output / "audit" / "semantic-reconstruction.json").exists()
            )

    def test_semantic_node_blocks_release_blocked_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            chapter_dir = output / "chapters"
            chapter_dir.mkdir(parents=True)
            (chapter_dir / "001_start.md").write_text(
                "# Start\n\nBody.\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps([{"id": "start", "filename": "001_start.md"}]),
                encoding="utf-8",
            )
            audit_path = output / "audit" / "semantic-reconstruction.json"
            audit_path.parent.mkdir()
            audit_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "blocked",
                        "summary": {
                            "chapter_count": 1,
                            "footnote_count": 0,
                            "issue_count": 1,
                            "blocking_issue_count": 1,
                            "release_blocked": True,
                        },
                        "chapters": [
                            {
                                "chapter_id": "start",
                                "filename": "001_start.md",
                                "footnote_count": 0,
                                "pages": [],
                                "issues": [
                                    {
                                        "code": "semantic_footnote_reference_ambiguous",
                                        "blocking": True,
                                    }
                                ],
                                "release_blocked": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            prepared = prepare_book_graph(
                [
                    "--output-dir",
                    str(output),
                    "--phase",
                    "docx",
                    "--title",
                    "book",
                ]
            )

            with (
                patch("pipeline_graph.book.legacy.build_docx") as build_docx,
                self.assertRaises(NodeExecutionError) as caught,
            ):
                prepared.execute()

            self.assertIsInstance(
                caught.exception.cause,
                SemanticReconstructionError,
            )
            self.assertIn("release_blocked=true", str(caught.exception.cause))
            build_docx.assert_not_called()

    def test_semantic_node_blocks_missing_or_stale_markdown_digest(self) -> None:
        for label, audited_digest in (
            ("missing", None),
            ("stale", "0" * 64),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                chapter_dir = output / "chapters"
                chapter_dir.mkdir(parents=True)
                chapter_text = "# Start\n\nBody.\n"
                (chapter_dir / "001_start.md").write_text(
                    chapter_text,
                    encoding="utf-8",
                )
                manifest_path = output / "chapters.json"
                manifest_path.write_text(
                    json.dumps(
                        [{"id": "start", "filename": "001_start.md"}]
                    ),
                    encoding="utf-8",
                )
                chapter_audit = {
                    "chapter_id": "start",
                    "filename": "001_start.md",
                    "footnote_count": 0,
                    "footnote_contract_sha256": (
                        book_pipeline.markdown_footnote_contract_sha256(
                            chapter_text
                        )
                    ),
                    "pages": [],
                    "issues": [],
                    "release_blocked": False,
                }
                if audited_digest is not None:
                    chapter_audit["markdown_sha256"] = audited_digest
                audit_path = output / "audit" / "semantic-reconstruction.json"
                audit_path.parent.mkdir()
                audit_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "status": "passed",
                            "summary": {
                                "chapter_count": 1,
                                "footnote_count": 0,
                                "issue_count": 0,
                                "blocking_issue_count": 0,
                                "release_blocked": False,
                            },
                            "chapters": [chapter_audit],
                        }
                    ),
                    encoding="utf-8",
                )
                prepared = prepare_book_graph(
                    [
                        "--output-dir",
                        str(output),
                        "--phase",
                        "docx",
                        "--title",
                        "book",
                    ]
                )
                prepared.context.values[ART_CHAPTERS] = _chapters_fixture(
                    chapter_dir,
                    manifest_path,
                )
                semantic = next(
                    node
                    for node in prepared.graph.nodes
                    if node.name == NODE_SEMANTIC
                )

                with self.assertRaises(SemanticReconstructionError) as caught:
                    semantic.handler(prepared.context)

                self.assertIn(
                    "semantic_markdown_digest_stale",
                    str(caught.exception),
                )

    def test_publishers_consume_declared_reader_chapter_bundle(self) -> None:
        from docx import Document

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            canonical_dir = output / "chapters"
            canonical_dir.mkdir(parents=True)
            (canonical_dir / "001.md").write_text(
                "# Chapter\n\nSTALE canonical chapter\n",
                encoding="utf-8",
            )
            (output / "chapters.json").write_text(
                json.dumps(
                    [
                        {
                            "id": "canonical",
                            "sequence": 1,
                            "display_title": "Chapter",
                            "filename": "001.md",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            alternate_dir = root / "plugin-chapters"
            alternate_dir.mkdir()
            (alternate_dir / "001.md").write_text(
                "# Chapter\n\nPLUGIN reader chapter\n",
                encoding="utf-8",
            )
            alternate_manifest = [
                {
                    "id": "plugin",
                    "sequence": 1,
                    "display_title": "Chapter",
                    "filename": "001.md",
                }
            ]
            alternate_manifest_path = root / "plugin-chapters.json"
            alternate_manifest_path.write_text(
                json.dumps(alternate_manifest),
                encoding="utf-8",
            )
            source_pdf = root / "book.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Source")
                document.save(source_pdf)
            source_sha = hashlib.sha256(source_pdf.read_bytes()).hexdigest()
            metadata = output / ".pipeline_graph"
            metadata.mkdir(exist_ok=True)
            (metadata / "source.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "path": str(source_pdf.resolve()),
                        "sha256": source_sha,
                        "page_count": 1,
                        "source_mode": "scanned-pdf",
                        "adapter": NODE_OCR,
                    }
                ),
                encoding="utf-8",
            )

            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "compile",
                    "--title",
                    "Book",
                ]
            )
            prepared.context.values[ART_READER_CHAPTERS] = _chapters_fixture(
                alternate_dir,
                alternate_manifest_path,
            )
            prepared.context.values[ART_SOURCE] = {
                "path": str(source_pdf),
                "sha256": source_sha,
                "page_count": 1,
            }
            by_name = {node.name: node for node in prepared.graph.nodes}

            by_name[NODE_DOCX].handler(prepared.context)
            document = Document(output / "Book.docx")
            word_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            self.assertIn("PLUGIN reader chapter", word_text)
            self.assertNotIn("STALE canonical chapter", word_text)

            def fake_epub(
                output_path: Path,
                chapter_dir: Path,
                manifest: list[dict[str, object]],
                *,
                book_title: str,
                language: str,
            ) -> None:
                self.assertEqual(chapter_dir, alternate_dir.resolve())
                self.assertEqual(manifest, alternate_manifest)
                self.assertEqual(book_title, "Book")
                self.assertEqual(language, "zh-CN")
                output_path.write_bytes(b"epub")

            def fake_knowledge_rows(
                pdf_path: Path,
                chapter_dir: Path,
                manifest: list[dict[str, object]],
            ) -> list[dict[str, object]]:
                self.assertEqual(pdf_path, source_pdf.resolve())
                self.assertEqual(chapter_dir, alternate_dir.resolve())
                self.assertEqual(manifest, alternate_manifest)
                return [{"id": "plugin-row", "content": "PLUGIN"}]

            def fake_write_knowledge_base(
                output_path: Path,
                rows: list[dict[str, object]],
            ) -> None:
                self.assertEqual(rows, [{"id": "plugin-row", "content": "PLUGIN"}])
                output_path.write_text('{"content":"PLUGIN"}\n', encoding="utf-8")

            with (
                patch(
                    "pipeline_graph.book.legacy.build_epub",
                    side_effect=fake_epub,
                ) as build_epub,
                patch(
                    "pipeline_graph.book.legacy.build_knowledge_rows_from_manifest",
                    side_effect=fake_knowledge_rows,
                ) as build_rows,
                patch(
                    "pipeline_graph.book.legacy.write_knowledge_base",
                    side_effect=fake_write_knowledge_base,
                ) as write_kb,
            ):
                epub_result = by_name[NODE_EPUB].handler(prepared.context)
                kb_result = by_name[NODE_KB].handler(prepared.context)

            build_epub.assert_called_once()
            build_rows.assert_called_once()
            write_kb.assert_called_once()
            self.assertEqual(build_epub.call_args.args[1], alternate_dir.resolve())
            self.assertEqual(build_rows.call_args.args[1], alternate_dir.resolve())
            self.assertEqual(
                epub_result.outputs[ART_EPUB]["path"],
                str((output / "Book.epub").resolve()),
            )
            self.assertEqual(
                kb_result.outputs[ART_KB]["path"],
                str((output / "knowledge_base.jsonl").resolve()),
            )

    def test_outline_toc_writes_pdf_page_mapping_directly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_pdf = root / "outlined.pdf"
            with fitz.open() as document:
                document.new_page()
                document.new_page()
                document.set_toc(
                    [
                        [1, "第一章 起点", 1],
                        [2, "第一节 继续", 2],
                    ]
                )
                document.save(source_pdf)

            output = root / "output"
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "toc",
                ],
                options=BookGraphOptions(toc_source="outline"),
            )

            with patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main:
                result = prepared.execute()

            legacy_main.assert_not_called()
            self.assertEqual(
                result.plan,
                (NODE_SOURCE, NODE_TOC_OUTLINE),
            )
            toc = json.loads((output / "toc.json").read_text(encoding="utf-8"))
            self.assertEqual(toc["page_offset"], 0)
            self.assertEqual(toc["printed_pages_per_pdf_page"], 1)
            self.assertEqual(
                [item["pdf_page"] for item in toc["entries"]],
                [1, 2],
            )
            self.assertEqual(
                [item["printed_page"] for item in toc["entries"]],
                [None, None],
            )
            self.assertEqual(
                toc["offset_evidence"],
                [{"source": "pdf-outline", "entry_count": 2}],
            )

    def test_legacy_nonzero_exit_is_attributed_to_the_graph_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            source_pdf = Path(directory) / "book.pdf"
            with fitz.open() as document:
                document.new_page()
                document.save(source_pdf)
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                ]
            )
            with patch("pipeline_graph.book.legacy._main_unlocked", return_value=7):
                with self.assertRaises(NodeExecutionError) as raised:
                    prepared.execute()

        self.assertEqual(raised.exception.node_name, NODE_OCR)
        self.assertIsInstance(raised.exception.cause, LegacyStageError)
        self.assertEqual(raised.exception.cause.phase, "ocr")
        self.assertEqual(raised.exception.cause.exit_code, 7)
        self.assertIs(raised.exception.__cause__, raised.exception.cause)

    def test_legacy_system_exit_is_attributed_to_the_graph_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            source_pdf = Path(directory) / "book.pdf"
            with fitz.open() as document:
                document.new_page()
                document.save(source_pdf)
            prepared = prepare_book_graph(
                [
                    str(source_pdf),
                    "--output-dir",
                    str(output),
                    "--phase",
                    "ocr",
                ]
            )
            with patch(
                "pipeline_graph.book.legacy._main_unlocked",
                side_effect=SystemExit(2),
            ):
                with self.assertRaises(NodeExecutionError) as raised:
                    prepared.execute()

        self.assertEqual(raised.exception.node_name, NODE_OCR)
        self.assertIsInstance(raised.exception.cause, LegacyStageError)
        self.assertEqual(raised.exception.cause.phase, "ocr")
        self.assertEqual(raised.exception.cause.exit_code, 2)

    def test_standalone_publishers_retire_unchanged_previous_title(self) -> None:
        cases = (
            ("docx", ".docx", "pipeline_graph.book.legacy.build_docx"),
            ("epub", ".epub", "pipeline_graph.book.legacy.build_epub"),
        )
        for phase, suffix, builder_name in cases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "output"
                chapter_dir = output / "chapters"
                chapter_dir.mkdir(parents=True)
                (chapter_dir / "001.md").write_text(
                    "# Chapter\n\nReader body.\n",
                    encoding="utf-8",
                )
                (output / "chapters.json").write_text(
                    json.dumps(
                        [
                            {
                                "id": "chapter",
                                "sequence": 1,
                                "display_title": "Chapter",
                                "filename": "001.md",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )

                def build_fake(
                    output_path: Path,
                    *_args: object,
                    **kwargs: object,
                ) -> None:
                    output_path.write_bytes(
                        f"{phase}:{kwargs['book_title']}".encode("utf-8")
                    )

                with patch(builder_name, side_effect=build_fake) as builder:
                    for title in ("Title A", "Title B"):
                        prepared = prepare_book_graph(
                            [
                                "--output-dir",
                                str(output),
                                "--phase",
                                phase,
                                "--title",
                                title,
                            ]
                        )
                        result = prepared.execute()
                        artifact = ART_DOCX if phase == "docx" else ART_EPUB
                        self.assertEqual(
                            Path(result.values[artifact]["path"]).name,
                            f"Title_{title[-1]}{suffix}",
                        )

                self.assertEqual(builder.call_count, 2)
                self.assertFalse((output / f"Title_A{suffix}").exists())
                self.assertTrue((output / f"Title_B{suffix}").is_file())
                self.assertEqual(
                    [path.name for path in output.glob(f"*{suffix}")],
                    [f"Title_B{suffix}"],
                )

    def test_full_publishers_preserve_modified_old_title_for_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            reader_dir = root / "reader"
            reader_dir.mkdir()
            (reader_dir / "001.md").write_text(
                "# Chapter\n\nReader body.\n",
                encoding="utf-8",
            )
            reader_manifest = root / "reader.json"
            reader_manifest.write_text(
                json.dumps(
                    [
                        {
                            "id": "chapter",
                            "sequence": 1,
                            "display_title": "Chapter",
                            "filename": "001.md",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            source_pdf = root / "source.pdf"
            with fitz.open() as document:
                document.new_page().insert_text((72, 72), "Source")
                document.save(source_pdf)
            metadata_dir = output / ".pipeline_graph"
            metadata_dir.mkdir()
            (metadata_dir / "source.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "path": str(source_pdf.resolve()),
                        "sha256": hashlib.sha256(source_pdf.read_bytes()).hexdigest(),
                        "page_count": 1,
                        "source_mode": "scanned-pdf",
                        "adapter": NODE_OCR,
                    }
                ),
                encoding="utf-8",
            )
            toc_path = output / "toc.json"
            toc_path.write_text(
                json.dumps(
                    {
                        "page_offset": 0,
                        "printed_pages_per_pdf_page": 1,
                        "entries": [
                            {
                                "id": "chapter",
                                "level": 1,
                                "title": "Chapter",
                                "pdf_page": 1,
                                "printed_page": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            def publish_for(title: str) -> None:
                prepared = prepare_book_graph(
                    [
                        str(source_pdf),
                        "--output-dir",
                        str(output),
                        "--phase",
                        "compile",
                        "--title",
                        title,
                        "--no-kb",
                        "--no-verify",
                    ]
                )
                prepared.context.values[ART_READER_CHAPTERS] = _chapters_fixture(
                    reader_dir,
                    reader_manifest,
                )
                prepared.context.values[ART_TOC] = _file_fixture(toc_path)
                by_name = {node.name: node for node in prepared.graph.nodes}
                GraphExecutor(
                    PipelineGraph(
                        [
                            by_name[NODE_SOURCE],
                            by_name[NODE_EPUB],
                            by_name[NODE_DOCX],
                            by_name[NODE_REFERENCE_PDF],
                        ]
                    )
                ).execute(
                    prepared.context,
                    targets={ART_EPUB, ART_DOCX, ART_REFERENCE_PDF},
                )

            def build_docx_fake(
                output_path: Path,
                *_args: object,
                book_title: str,
                **_kwargs: object,
            ) -> None:
                output_path.write_bytes(f"DOCX:{book_title}".encode("utf-8"))

            def build_epub_fake(
                output_path: Path,
                *_args: object,
                book_title: str,
                **_kwargs: object,
            ) -> None:
                output_path.write_bytes(f"EPUB:{book_title}".encode("utf-8"))

            def build_reference_fake(
                _source: Path,
                output_path: Path,
                _toc: object,
            ) -> None:
                output_path.write_bytes(f"PDF:{output_path.name}".encode("utf-8"))

            with (
                patch(
                    "pipeline_graph.book.legacy.build_docx",
                    side_effect=build_docx_fake,
                ),
                patch(
                    "pipeline_graph.book.legacy.build_epub",
                    side_effect=build_epub_fake,
                ),
                patch(
                    "pipeline_graph.book.legacy.build_bookmarked_pdf",
                    side_effect=build_reference_fake,
                ),
            ):
                for title in ("Title A", "Title B"):
                    publish_for(title)

                # Unchanged framework-owned A artifacts are safely retired.
                for suffix in (".docx", ".epub", "_带目录.pdf"):
                    self.assertFalse((output / f"Title_A{suffix}").exists())
                    self.assertTrue((output / f"Title_B{suffix}").is_file())
                    self.assertEqual(
                        [path.name for path in output.glob(f"*{suffix}")],
                        [f"Title_B{suffix}"],
                    )

                # Once a human changes the old release, its digest no longer
                # authorizes deletion during the next title migration.
                for suffix in (".docx", ".epub", "_带目录.pdf"):
                    old_path = output / f"Title_B{suffix}"
                    old_path.write_bytes(old_path.read_bytes() + b":USER-EDIT")
                publish_for("Title C")

            for suffix in (".docx", ".epub", "_带目录.pdf"):
                self.assertTrue((output / f"Title_B{suffix}").is_file())
                self.assertTrue((output / f"Title_C{suffix}").is_file())

            report = verify_publication(
                output,
                source_pdf=source_pdf,
                book_title="Title C",
                require_epub=True,
                require_docx=True,
                require_knowledge_base=False,
                require_bookmarked_pdf=True,
            )
            issue_codes = {str(item["code"]) for item in report["errors"]}
            self.assertIn("docx_extra_artifacts", issue_codes)
            self.assertIn("epub_extra_artifacts", issue_codes)
            self.assertIn("bookmarked_pdf_extra_artifacts", issue_codes)

    def test_plan_option_never_executes_the_prepared_graph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with (
                patch.object(PreparedBookGraph, "execute") as execute,
                patch("pipeline_graph.book.legacy._main_unlocked") as legacy_main,
                contextlib.redirect_stdout(stdout),
            ):
                exit_code = graph_pipeline.main(
                    [
                        "--plan",
                        "--phase",
                        "docx",
                        "--output-dir",
                        directory,
                    ]
                )

            self.assertEqual(exit_code, 0)
            execute.assert_not_called()
            legacy_main.assert_not_called()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                [item["name"] for item in payload["nodes"]],
                [NODE_CHAPTERS_LOAD, NODE_SEMANTIC, NODE_SANITIZE, NODE_DOCX],
            )
            self.assertEqual(payload["targets"], [ART_DOCX])

    def test_graph_parser_leaves_legacy_force_flag_for_the_pipeline(self) -> None:
        controls, pipeline_argv = graph_pipeline.build_graph_control_parser().parse_known_args(
            ["--plan", "--force", "--phase", "ocr", "book.pdf"]
        )
        self.assertTrue(controls.plan)
        self.assertIn("--force", pipeline_argv)
        self.assertNotIn("--force", controls.force_node)

    def test_cli_recipe_can_select_text_pdf_when_source_mode_is_omitted(self) -> None:
        recipe = Path(__file__).resolve().parents[1] / "recipes" / "text-pdf-full-publication.toml"
        with tempfile.TemporaryDirectory() as directory:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = graph_pipeline.main(
                    [
                        "--plan",
                        "--recipe",
                        str(recipe),
                        "--phase",
                        "all",
                        "--output-dir",
                        directory,
                        "book.pdf",
                    ]
                )

        self.assertEqual(exit_code, 0)
        names = [item["name"] for item in json.loads(stdout.getvalue())["nodes"]]
        self.assertIn(NODE_TEXT_EXTRACT, names)
        self.assertNotIn(NODE_OCR, names)

    def test_cli_explicit_scanned_pdf_conflicts_with_text_pdf_recipe(self) -> None:
        recipe = Path(__file__).resolve().parents[1] / "recipes" / "text-pdf-full-publication.toml"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                BookGraphConfigurationError,
                "explicit --source-mode scanned-pdf.*core.pages.text_extract",
            ):
                graph_pipeline.main(
                    [
                        "--plan",
                        "--recipe",
                        str(recipe),
                        "--source-mode",
                        "scanned-pdf",
                        "--phase",
                        "all",
                        "--output-dir",
                        directory,
                        "book.pdf",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
