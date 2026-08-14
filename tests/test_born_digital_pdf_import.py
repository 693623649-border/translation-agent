from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import fitz

from born_digital_pdf_import import apply_translations, import_born_digital_pdf


def _text_pdf(path: Path, pages: list[str], *, outline: list[list[object]] | None = None) -> None:
    document = fitz.open()
    for text in pages:
        page = document.new_page()
        page.insert_textbox(fitz.Rect(72, 72, 540, 740), text, fontsize=11)
    document.set_metadata({"title": "Digital Book", "author": "A. Author"})
    if outline:
        document.set_toc(outline)
    document.save(path)
    document.close()


class BornDigitalPdfImportTests(unittest.TestCase):
    def test_outline_creates_semantic_chapters_and_translation_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "book.pdf"
            output = root / "output"
            _text_pdf(
                pdf,
                [
                    "First chapter contains enough embedded English text for semantic extraction and translation.",
                    "Second chapter contains another complete paragraph from the embedded text layer.",
                ],
                outline=[[1, "Chapter One", 1], [1, "Chapter Two", 2]],
            )

            result = import_born_digital_pdf(pdf, output)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            toc = json.loads((output / "toc.json").read_text(encoding="utf-8"))
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "passed")
            self.assertEqual([item["display_title"] for item in manifest], ["Chapter One", "Chapter Two"])
            self.assertEqual(
                [(item["pdf_page"], item["end_pdf_page"]) for item in manifest],
                [(1, 1), (2, 2)],
            )
            self.assertTrue(all(item["granularity"] == "all" for item in manifest))
            self.assertEqual(
                [item["id"] for item in toc["entries"]],
                [item["id"] for item in manifest],
            )
            self.assertTrue((output / "semantic" / "source_chapters" / manifest[0]["filename"]).is_file())
            self.assertGreater(result["translation_unit_count"], 2)
            self.assertEqual(audit["source"]["metadata"]["author"], "A. Author")

    def test_no_outline_becomes_one_chapter_without_heading_guess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "book.pdf"
            output = root / "output"
            _text_pdf(pdf, ["LARGE LOOKING TITLE\nA sufficiently long body paragraph remains in the single document chapter."])

            result = import_born_digital_pdf(pdf, output)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertEqual(result["chapter_count"], 1)
            self.assertEqual(manifest[0]["display_title"], "Digital Book")
            self.assertIn("pdf_outline_missing_single_chapter", {issue["code"] for issue in audit["issues"]})

    def test_image_only_coverage_blocks_and_requests_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "scan.pdf"
            output = root / "output"
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
            pixmap.clear_with(255)
            image = pixmap.tobytes("png")
            document = fitz.open()
            for _ in range(2):
                page = document.new_page()
                page.insert_image(fitz.Rect(30, 30, 560, 760), stream=image)
            document.save(pdf)
            document.close()

            result = import_born_digital_pdf(pdf, output)
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertTrue(result["release_blocked"])
            self.assertIn("pdf_text_layer_too_sparse", {issue["code"] for issue in audit["issues"]})
            self.assertFalse((output / "chapters.json").exists())

    def test_blank_front_and_back_pages_are_warned_not_treated_as_scans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "book.pdf"
            output = root / "output"
            _text_pdf(
                pdf,
                [
                    "",
                    "A complete embedded English body page contains enough reliable text for semantic import.",
                    "",
                ],
            )

            result = import_born_digital_pdf(pdf, output)
            audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertFalse(result["release_blocked"])
            self.assertEqual(
                audit["source"]["text_layer_quality"]["boundary_empty_pages"],
                [1, 3],
            )
            self.assertIn(
                "pdf_empty_pages_retained",
                {issue["code"] for issue in audit["issues"]},
            )

    def test_uri_link_and_grid_table_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "structured.pdf"
            output = root / "output"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Visit example for a complete linked English sentence and reference.")
            page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(72, 58, 145, 78), "uri": "https://example.com"})
            x0, y0, width, height = 72, 130, 240, 70
            for x in (x0, x0 + width / 2, x0 + width):
                page.draw_line((x, y0), (x, y0 + height))
            for y in (y0, y0 + height / 2, y0 + height):
                page.draw_line((x0, y), (x0 + width, y))
            page.insert_text((82, 152), "Name")
            page.insert_text((202, 152), "Value")
            page.insert_text((82, 187), "Power")
            page.insert_text((202, 187), "Freedom")
            page.insert_text((72, 250), "Additional body text ensures the embedded text quality threshold is safely met.")
            document.set_metadata({"title": "Structured PDF"})
            document.save(pdf)
            document.close()

            result = import_born_digital_pdf(pdf, output)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            markdown = (output / "chapters" / manifest[0]["filename"]).read_text(encoding="utf-8")
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertFalse(result["release_blocked"])
            self.assertIn("https://example.com", markdown)
            self.assertIn("| Name | Value |", markdown)
            self.assertEqual(audit["summary"]["table_count"], 1)

    def test_same_page_outline_destinations_block_instead_of_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "ambiguous.pdf"
            output = root / "output"
            _text_pdf(
                pdf,
                ["A complete English page has enough text for both outline labels but no reliable coordinates."],
                outline=[[1, "One", 1], [2, "Two", 1]],
            )

            result = import_born_digital_pdf(pdf, output)
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertTrue(result["release_blocked"])
            self.assertIn("pdf_outline_same_page_ambiguous", {issue["code"] for issue in audit["issues"]})

    def test_unresolved_visible_superscript_blocks_semantic_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "superscript.pdf"
            output = root / "output"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "A complete embedded-text paragraph has an unresolved note marker",
                fontsize=11,
            )
            page.insert_text((411, 68), "1", fontsize=7)
            page.insert_text(
                (72, 105),
                "Additional ordinary text keeps the source-quality gate reliable.",
                fontsize=11,
            )
            document.set_metadata({"title": "Superscript Book"})
            document.save(pdf)
            document.close()

            result = import_born_digital_pdf(pdf, output)
            audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertTrue(result["release_blocked"])
            self.assertIn(
                "pdf_visible_superscript_unresolved",
                {
                    issue["code"]
                    for chapter in audit["chapters"]
                    for issue in chapter["issues"]
                },
            )

    def test_shared_translation_contract_applies_hash_bound_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "book.pdf"
            output = root / "output"
            _text_pdf(pdf, ["A complete embedded English paragraph is ready for accurate translation."])
            import_born_digital_pdf(pdf, output)
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for unit in units:
                unit["translated_markdown"] = unit["source_markdown"].replace(
                    "Digital Book", "数字图书"
                ).replace(
                    "A complete embedded English paragraph is ready for accurate translation.",
                    "一段完整的内嵌英文段落已经可以准确翻译。",
                )
            translations = root / "translations.jsonl"
            translations.write_text(
                "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
                encoding="utf-8",
            )
            reconstruction_path = output / "audit" / "semantic-reconstruction.json"
            reconstruction_before = reconstruction_path.read_bytes()

            report = apply_translations(output, translations)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            translation_audit = json.loads(
                (output / "audit" / "semantic-translation.json").read_text(
                    encoding="utf-8"
                )
            )
            translated = (output / "chapters" / manifest[0]["filename"]).read_bytes()

            self.assertEqual(report["status"], "passed")
            self.assertEqual(manifest[0]["display_title"], "数字图书")
            self.assertEqual(reconstruction_path.read_bytes(), reconstruction_before)
            self.assertEqual(
                translation_audit["chapters"][0]["markdown_sha256"],
                hashlib.sha256(translated).hexdigest(),
            )
            self.assertEqual(
                translation_audit["contract_mode"],
                "born-digital-pdf-translated-markdown",
            )

    def test_summary_blocked_audit_prevents_pdf_translation_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "book.pdf"
            output = root / "output"
            _text_pdf(pdf, ["A complete embedded English paragraph is ready for accurate translation."])
            import_born_digital_pdf(pdf, output)
            audit_path = output / "audit" / "semantic-reconstruction.json"
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["summary"]["release_blocked"] = True
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            before = audit_path.read_bytes()
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for unit in units:
                unit["translated_markdown"] = unit["source_markdown"].replace(
                    "Digital Book", "数字图书"
                ).replace(
                    "A complete embedded English paragraph is ready for accurate translation.",
                    "一段完整的内嵌英文段落已经可以准确翻译。",
                )
            translations = root / "translations.jsonl"
            translations.write_text(
                "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "summary is blocked"):
                apply_translations(output, translations)

            self.assertEqual(audit_path.read_bytes(), before)
            self.assertFalse((output / "audit" / "semantic-translation.json").exists())


if __name__ == "__main__":
    unittest.main()
