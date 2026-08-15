from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from compile_service import ChapterCompileRequest, run_chapter_compile
from pipeline_profiles import ModelIdentity


def _identity() -> ModelIdentity:
    return ModelIdentity(
        provider="deepseek",
        adapter="openai-chat",
        base_url="https://api.example.invalid",
        model="translator",
        target_language="简体中文",
        prompt_version="translation-v1",
    )


class CompileServiceTests(unittest.TestCase):
    def test_service_preserves_mapping_granularity_and_translation_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            source = Path(directory) / "source.pdf"
            toc_path = output / "toc.json"
            records = (
                SimpleNamespace(pdf_page=1, ocr_model="text-layer/v1"),
                SimpleNamespace(pdf_page=2, ocr_model="text-layer/v1-blank"),
            )
            identity = _identity()
            mapped_toc = {
                "page_offset": 4,
                "printed_pages_per_pdf_page": 2,
                "entries": [],
            }
            manifest = [{"id": "chapter-1", "filename": "001.md"}]
            rows = [{"id": "row-1", "content": "Body"}]
            compiler = SimpleNamespace(
                parse_model_prefixes=Mock(return_value=("text-layer/",)),
                load_toc=Mock(return_value={"entries": []}),
                apply_page_mapping=Mock(return_value=mapped_toc),
                write_json=Mock(),
                resolve_compile_granularity=Mock(return_value="section"),
                compile_chapters=Mock(return_value=(manifest, rows)),
            )
            request = ChapterCompileRequest(
                source_pdf=source,
                output_dir=output,
                page_records=records,
                source_page_count=2,
                publication_title="Book",
                toc_path=toc_path,
                page_offset=4,
                printed_pages_per_pdf_page=2,
                granularity=None,
                require_complete_ocr=True,
                required_ocr_model_prefix="text-layer/",
                require_translation=False,
                expected_translation_identity=identity,
            )
            with patch(
                "compile_service._legacy_compiler",
                return_value=compiler,
            ):
                result = run_chapter_compile(request)

        compiler.apply_page_mapping.assert_called_once_with(
            {"entries": []},
            list(records),
            page_offset=4,
            source_page_count=2,
            printed_pages_per_pdf_page=2,
        )
        compiler.write_json.assert_called_once_with(
            toc_path.resolve(),
            mapped_toc,
        )
        compiler.resolve_compile_granularity.assert_called_once_with(
            output.resolve(),
            mapped_toc,
            None,
        )
        self.assertIs(
            compiler.compile_chapters.call_args.kwargs[
                "expected_translation_identity"
            ],
            identity,
        )
        self.assertFalse(
            compiler.compile_chapters.call_args.kwargs["require_translation"]
        )
        self.assertEqual(result.manifest, manifest)
        self.assertEqual(result.knowledge_rows, rows)
        self.assertEqual(result.granularity, "section")
        self.assertEqual(result.manifest_path, output.resolve() / "chapters.json")

    def test_complete_ocr_requires_exact_source_page_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ChapterCompileRequest(
                source_pdf=Path(directory) / "source.pdf",
                output_dir=Path(directory) / "output",
                page_records=(
                    SimpleNamespace(pdf_page=1, ocr_model="ocr/v1"),
                    SimpleNamespace(pdf_page=4, ocr_model="ocr/v1"),
                ),
                source_page_count=3,
                publication_title="Book",
                require_complete_ocr=True,
            )
            with self.assertRaisesRegex(
                ValueError,
                r"missing=\[2, 3\], extra=\[4\]",
            ):
                run_chapter_compile(request)

    def test_required_ocr_prefix_rejects_mixed_checkpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ChapterCompileRequest(
                source_pdf=Path(directory) / "source.pdf",
                output_dir=Path(directory) / "output",
                page_records=(
                    SimpleNamespace(pdf_page=1, ocr_model="ocr/v1"),
                    SimpleNamespace(pdf_page=2, ocr_model="manual/page"),
                ),
                source_page_count=2,
                publication_title="Book",
                required_ocr_model_prefix="ocr/",
            )
            compiler = SimpleNamespace(
                parse_model_prefixes=Mock(return_value=("ocr/",)),
            )
            with (
                patch(
                    "compile_service._legacy_compiler",
                    return_value=compiler,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    r"cached pages do not match: \[\(2, 'manual/page'\)\]",
                ),
            ):
                run_chapter_compile(request)

    def test_comma_prefixes_accept_mixed_models_without_remapping_toc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            records = (
                SimpleNamespace(pdf_page=1, ocr_model="ocr/v1"),
                SimpleNamespace(pdf_page=2, ocr_model="manual/page"),
            )
            toc_payload = {
                "page_offset": 3,
                "printed_pages_per_pdf_page": 1,
                "entries": [],
            }
            compiler = SimpleNamespace(
                parse_model_prefixes=Mock(return_value=("ocr/", "manual/")),
                load_toc=Mock(return_value=toc_payload),
                apply_page_mapping=Mock(),
                write_json=Mock(),
                resolve_compile_granularity=Mock(return_value="chapter"),
                compile_chapters=Mock(return_value=([], [])),
            )
            request = ChapterCompileRequest(
                source_pdf=Path(directory) / "source.pdf",
                output_dir=output,
                page_records=records,
                source_page_count=2,
                publication_title="Book",
                required_ocr_model_prefix="ocr/, manual/",
            )
            with patch(
                "compile_service._legacy_compiler",
                return_value=compiler,
            ):
                result = run_chapter_compile(request)

        compiler.parse_model_prefixes.assert_called_once_with("ocr/, manual/")
        compiler.apply_page_mapping.assert_not_called()
        compiler.write_json.assert_not_called()
        self.assertIs(result.toc_payload, toc_payload)

    def test_empty_page_records_preserve_legacy_compile_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            request = ChapterCompileRequest(
                source_pdf=Path(directory) / "source.pdf",
                output_dir=Path(directory) / "output",
                page_records=(),
                source_page_count=1,
                publication_title="Book",
            )
            with self.assertRaisesRegex(ValueError, "No page OCR records found"):
                run_chapter_compile(request)


if __name__ == "__main__":
    unittest.main()
