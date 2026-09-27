import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image

from book_pipeline import PageStore, TRANSLATION_PROMPT_VERSION
from extract_textbook_layer import (
    TEXT_LAYER_MODEL,
    extract_page_texts,
    extract_text_layer,
    main,
    reflow_logical_text,
)
from pipeline_profiles import ModelIdentity


def make_text_pdf(path: Path, pages: list[str]) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(
            fitz.Rect(72, 72, 540, 740),
            text,
            fontsize=11,
        )
    document.save(path)
    document.close()


class TextLayerExtractorTests(unittest.TestCase):
    def test_cli_extracts_all_pages_and_does_not_invent_toc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "book.pdf"
            output = root / "output"
            make_text_pdf(
                pdf,
                [
                    "First English page with enough words for detection.",
                    "Second English page with another complete paragraph.",
                ],
            )

            self.assertEqual(main([str(pdf), "-o", str(output)]), 0)
            records = PageStore(output).load_all()

            self.assertEqual(len(records), 2)
            self.assertTrue(all(record.language == "en" for record in records))
            self.assertTrue(
                all(record.ocr_model == TEXT_LAYER_MODEL for record in records)
            )
            self.assertFalse((output / "toc.json").exists())
            self.assertTrue((output / "pages" / "page_0001.md").exists())

    def test_identical_rerun_preserves_fresh_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "book.pdf"
            output = root / "output"
            make_text_pdf(pdf, ["A complete English source paragraph for translation."])
            extract_text_layer(pdf, output)
            store = PageStore(output)
            source = store.load(1)
            identity = ModelIdentity(
                provider="deepseek",
                adapter="openai-chat",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                target_language="简体中文",
                prompt_version=TRANSLATION_PROMPT_VERSION,
            )
            store.commit_translation(
                1,
                expected_text_sha256=source.effective_text_sha256,
                translated_text="一段完整的中文译文。",
                identity=identity,
            )

            written, preserved, issues = extract_text_layer(pdf, output)
            result = store.load(1)

            self.assertEqual((written, preserved, issues), (0, 1, []))
            self.assertEqual(result.translated_text, "一段完整的中文译文。")
            self.assertTrue(result.translation_is_fresh_for(identity))

    def test_changed_text_requires_force_and_force_invalidates_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_pdf = root / "first.pdf"
            changed_pdf = root / "changed.pdf"
            output = root / "output"
            make_text_pdf(first_pdf, ["The original embedded source paragraph."])
            make_text_pdf(changed_pdf, ["A changed embedded source paragraph."])
            extract_text_layer(first_pdf, output)
            store = PageStore(output)
            source = store.load(1)
            identity = ModelIdentity(
                provider="deepseek",
                adapter="openai-chat",
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
                target_language="简体中文",
                prompt_version=TRANSLATION_PROMPT_VERSION,
            )
            store.commit_translation(
                1,
                expected_text_sha256=source.effective_text_sha256,
                translated_text="旧译文。",
                identity=identity,
            )

            with self.assertRaisesRegex(ValueError, "differs.*--force"):
                extract_text_layer(changed_pdf, output)
            self.assertEqual(store.load(1).translated_text, "旧译文。")

            written, preserved, _issues = extract_text_layer(
                changed_pdf,
                output,
                force=True,
            )
            replaced = store.load(1)
            self.assertEqual((written, preserved), (1, 0))
            self.assertIn("changed embedded source", replaced.text)
            self.assertEqual(replaced.translated_text, "")
            self.assertFalse(replaced.translation_is_fresh)

    def test_image_only_page_fails_before_any_checkpoint_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "mixed.pdf"
            output = root / "output"
            image_buffer = BytesIO()
            Image.new("RGB", (20, 20), "white").save(image_buffer, format="PNG")
            document = fitz.open()
            text_page = document.new_page()
            text_page.insert_text((72, 72), "This page has an embedded text layer.")
            image_page = document.new_page()
            image_page.insert_image(
                fitz.Rect(72, 72, 200, 200),
                stream=image_buffer.getvalue(),
            )
            document.save(pdf)
            document.close()

            with self.assertRaisesRegex(ValueError, r"PDF 2 \(image-only\)"):
                extract_text_layer(pdf, output)

            self.assertFalse(output.exists())

    def test_boundary_blank_pages_are_explicit_checkpoints_but_internal_gaps_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "boundary-blanks.pdf"
            output = root / "output"
            make_text_pdf(
                pdf,
                [
                    "",
                    "A complete embedded-text body page.",
                    "",
                ],
            )

            extract_text_layer(pdf, output)
            records = PageStore(output).load_all()

            self.assertEqual([record.text for record in records], [
                "[空白页]",
                "A complete embedded-text body page.",
                "[空白页]",
            ])
            self.assertTrue(records[0].ocr_model.startswith(TEXT_LAYER_MODEL))
            self.assertIn("boundary-blank", records[0].ocr_model)

            internal = root / "internal-blank.pdf"
            make_text_pdf(
                internal,
                [
                    "A complete first embedded-text body page.",
                    "",
                    "A complete final embedded-text body page.",
                ],
            )
            with self.assertRaisesRegex(ValueError, r"PDF 2 \(empty-text\)"):
                extract_text_layer(internal, root / "internal-output")

    def test_standalone_stripped_page_number_is_explicit_blank_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "number-only-page.pdf"
            output = root / "output"
            make_text_pdf(
                pdf,
                [
                    "Front matter text.",
                    "Chapter body before a source blank.",
                    "1",
                    "Chapter body after a source blank.",
                ],
            )

            extract_text_layer(
                pdf,
                output,
                strip_leading_page_number_offset=2,
            )
            records = PageStore(output).load_all()

            self.assertEqual(records[2].text, "[空白页]")
            self.assertIn("boundary-blank", records[2].ocr_model)

    def test_optional_headings_mark_only_unique_exact_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "book.pdf"
            output = root / "output"
            headings = root / "headings.json"
            make_text_pdf(
                pdf,
                [
                    "Unique Heading\nBody paragraph.\nRepeated Heading",
                    "Unique Heading\nRepeated Heading\nMore body text.",
                ],
            )
            headings.write_text(
                json.dumps(
                    [
                        {
                            "title": "Canonical Unique Heading",
                            "level": 2,
                            "aliases": ["Unique Heading"],
                            "replacement": "审定后的唯一标题",
                            "pdf_page": 2,
                        },
                        {"title": "Repeated Heading", "level": 3},
                        {"title": "Missing Heading", "level": 2},
                    ]
                ),
                encoding="utf-8",
            )

            _written, _preserved, issues = extract_text_layer(
                pdf,
                output,
                headings_json=headings,
            )
            records = PageStore(output).load_all()

            self.assertIn("Unique Heading", records[0].text)
            self.assertNotIn("## 审定后的唯一标题", records[0].text)
            self.assertIn("## 审定后的唯一标题", records[1].text)
            self.assertNotIn("## Unique Heading", records[1].text)
            self.assertNotIn("### Repeated Heading", records[0].text)
            self.assertNotIn("### Repeated Heading", records[1].text)
            self.assertEqual(
                {issue.title for issue in issues},
                {"Repeated Heading", "Missing Heading"},
            )
            repeated = next(
                issue for issue in issues if issue.title == "Repeated Heading"
            )
            self.assertEqual(len(repeated.matches), 2)

    def test_page_number_stripping_is_opt_in_and_requires_exact_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "numbered.pdf"
            make_text_pdf(
                pdf,
                [
                    "99\nFirst page body.",
                    "1\nSecond page body.\n2",
                    "Third page body.\n2",
                ],
            )

            default_text = extract_page_texts(pdf)
            stripped_text = extract_page_texts(
                pdf,
                strip_leading_page_number_offset=1,
            )

            self.assertTrue(default_text[1].startswith("1\n"))
            self.assertTrue(stripped_text[0].startswith("99\n"))
            self.assertTrue(stripped_text[1].startswith("Second page body."))
            self.assertTrue(stripped_text[1].endswith("\n2"))
            self.assertTrue(stripped_text[2].endswith("\n2"))

    def test_reflow_joins_visual_wraps_and_preserves_paragraphs_and_headings(self) -> None:
        source = (
            "## Section title\n"
            "This is a visual\nline wrap with an inter-\nnational word.\n\n"
            "A second paragraph\nstays separate."
        )

        self.assertEqual(
            reflow_logical_text(source),
            "## Section title\n\n"
            "This is a visual line wrap with an international word.\n\n"
            "A second paragraph stays separate.",
        )

    def test_default_extraction_retains_logical_line_and_paragraph_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "paragraphs.pdf"
            make_text_pdf(
                pdf,
                [
                    "First visual line\nsecond visual line.\n\n"
                    "A separate paragraph remains separate."
                ],
            )

            extracted = extract_page_texts(pdf)

            self.assertIn("First visual line\nsecond visual line.", extracted[0])
            self.assertIn(
                "second visual line.\nA separate paragraph remains separate.",
                extracted[0],
            )


if __name__ == "__main__":
    unittest.main()
