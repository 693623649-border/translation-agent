import json
import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path

import fitz

from regenerate_reviewed_chapters import (
    Note,
    SourceBlock,
    assign_markers,
    chapter_range,
    make_units,
    page_layout_hints,
    parse_unit_output,
    protect_references,
    reconstruct_chapter,
    render_reviewed_chapter,
)


LONG_SPACE_SEPARATOR = "\n" + (" " * 24) + "\n"


def write_page(output_dir: Path, pdf_page: int, text: str) -> None:
    page_path = output_dir / "pages" / f"page_{pdf_page:04d}.json"
    page_path.parent.mkdir(parents=True, exist_ok=True)
    page_path.write_text(
        json.dumps(
            {
                "pdf_page": pdf_page,
                "text": text,
                "language": "en",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class ReviewedChapterReconstructionTests(unittest.TestCase):
    def test_colon_before_cross_page_prose_continuation_is_joined_not_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_page(
                output,
                10,
                "The chapter poses two related questions:"
                + LONG_SPACE_SEPARATOR
                + "101 First note.",
            )
            write_page(
                output,
                11,
                "what is actual and what is virtual?101",
            )

            blocks, _notes = reconstruct_chapter(
                output,
                "chapter",
                {"id": "chapter", "title": "Chapter", "index": ""},
                10,
                11,
                paragraph_starts_by_page={10: set(), 11: set()},
                quote_starts_by_page={10: set(), 11: set()},
            )

            body = [item for item in blocks if item.kind == "body"]
            self.assertEqual(len(body), 1)
            self.assertEqual(
                body[0].text,
                "The chapter poses two related questions: what is actual and what is "
                "virtual?101",
            )
            self.assertFalse(body[0].quote)

    def test_displayed_quote_continuing_across_pages_remains_one_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_page(output, 20, "A displayed quotation continues without")
            write_page(
                output,
                21,
                "a terminal break on the next page.201"
                + LONG_SPACE_SEPARATOR
                + "201 Source note.",
            )
            first_line = "A displayed quotation continues without"
            second_line = "a terminal break on the next page.201"

            blocks, _notes = reconstruct_chapter(
                output,
                "chapter",
                {"id": "chapter", "title": "Chapter", "index": ""},
                20,
                21,
                paragraph_starts_by_page={20: {first_line}, 21: {second_line}},
                quote_starts_by_page={20: {first_line}, 21: {second_line}},
            )

            body = [item for item in blocks if item.kind == "body"]
            self.assertEqual(len(body), 1)
            self.assertEqual(
                body[0].text,
                "A displayed quotation continues without a terminal break on the next "
                "page.201",
            )
            self.assertTrue(body[0].quote)

    def test_chapter_six_real_layout_reconstruction_baseline(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        output = repository / "outputs" / "2019boydphd"
        source_pdf = repository / "book" / "2019boydphd.pdf"
        toc_path = output / "toc.json"
        page_dir = output / "pages"
        if not source_pdf.is_file() or not toc_path.is_file() or not page_dir.is_dir():
            self.skipTest("2019boydphd PDF/checkpoints are not available in this checkout")

        page_files = sorted(page_dir.glob("page_*.json"))
        if not page_files:
            self.skipTest("2019boydphd page checkpoints are not available")
        last_page = max(int(path.stem.rsplit("_", 1)[1]) for path in page_files)
        entries = json.loads(toc_path.read_text(encoding="utf-8"))["entries"]
        entry, start, end = chapter_range(entries, "ch-6", last_page)
        paragraph_starts: dict[int, set[str]] = {}
        quote_starts: dict[int, set[str]] = {}
        with fitz.open(source_pdf) as document:
            for pdf_page in range(start, end + 1):
                paragraph_starts[pdf_page], quote_starts[pdf_page] = page_layout_hints(
                    document,
                    pdf_page,
                )

        blocks, notes = reconstruct_chapter(
            output,
            "ch-6",
            entry,
            start,
            end,
            paragraph_starts,
            quote_starts,
        )
        body = [item for item in blocks if item.kind == "body"]

        self.assertEqual((start, end), (265, 310))
        self.assertEqual(len(body), 87)
        self.assertEqual(sum(item.quote for item in body), 19)
        self.assertEqual(sum(item.kind == "figure" for item in blocks), 8)
        self.assertEqual(len(notes), 93)
        self.assertEqual(list(notes), list(range(753, 846)))
        self.assertLess(max(len(item.text) for item in body), 4000)

    def test_reconstruct_joins_body_across_pages_before_moving_footnotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_page(
                output,
                10,
                "The argument uses music and professional voiceovers, as"
                + LONG_SPACE_SEPARATOR
                + "101 First note.",
            )
            write_page(
                output,
                11,
                "well as richly illustrated environments.101\n\n"
                "A second, complete paragraph.102"
                + LONG_SPACE_SEPARATOR
                + "102 Second note.",
            )

            blocks, notes = reconstruct_chapter(
                output,
                "chapter",
                {"id": "chapter", "title": "Chapter", "index": ""},
                10,
                11,
            )

            body = [item for item in blocks if item.kind == "body"]
            self.assertEqual(len(body), 2)
            self.assertEqual(
                body[0].text,
                "The argument uses music and professional voiceovers, as well as "
                "richly illustrated environments.101",
            )
            self.assertEqual(body[0].pdf_page, 10)
            self.assertNotIn("First note", body[0].text)
            self.assertEqual(list(notes), [101, 102])

    def test_reconstruct_carries_a_footnote_across_the_next_physical_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_page(
                output,
                20,
                "A complete source sentence.201"
                + LONG_SPACE_SEPARATOR
                + "201 A long explanatory note that",
            )
            write_page(
                output,
                21,
                "Another complete source sentence.202"
                + LONG_SPACE_SEPARATOR
                + "continues on the next physical page.\n"
                "202 A separate second note.",
            )

            blocks, notes = reconstruct_chapter(
                output,
                "chapter",
                {"id": "chapter", "title": "Chapter", "index": ""},
                20,
                21,
            )

            self.assertEqual(
                notes[201].text,
                "A long explanatory note that continues on the next physical page.",
            )
            self.assertEqual(notes[202].text, "A separate second note.")
            self.assertEqual(
                [item.text for item in blocks if item.kind == "body"],
                [
                    "A complete source sentence.201",
                    "Another complete source sentence.202",
                ],
            )

    def test_translation_markers_and_reference_markers_must_be_complete(self) -> None:
        blocks = [
            SourceBlock("body", "First paragraph.301", 30),
            SourceBlock("body", "Second paragraph.302", 31),
        ]
        notes = OrderedDict(
            (
                (301, Note(301, ["First note."])),
                (302, Note(302, ["Second note."])),
            )
        )
        protect_references(blocks, notes)
        assign_markers(blocks, notes)

        units = make_units(blocks, notes, max_chars=1000)
        body_unit = next(unit for unit in units if unit.kind == "body")
        self.assertEqual(body_unit.markers, ("P0001", "P0002"))
        self.assertEqual(body_unit.source.count("⟦R301⟧"), 1)
        self.assertEqual(body_unit.source.count("⟦R302⟧"), 1)

        parsed = parse_unit_output(
            body_unit,
            "⟦P0001⟧ 第一段。⟦R301⟧\n\n"
            "⟦P0002⟧ 第二段。⟦R302⟧",
        )
        self.assertEqual(tuple(parsed), body_unit.markers)

        invalid_outputs = (
            "⟦P0001⟧ 第一段。⟦R301⟧",
            "⟦P0002⟧ 第二段。⟦R302⟧\n\n"
            "⟦P0001⟧ 第一段。⟦R301⟧",
            "⟦P0001⟧ 第一段。⟦R301⟧\n\n"
            "⟦P0002⟧ 第二段。",
        )
        for response in invalid_outputs:
            with self.subTest(response=response), self.assertRaisesRegex(
                ValueError, "Marker mismatch|Reference marker mismatch"
            ):
                parse_unit_output(body_unit, response)

    def test_render_does_not_serialize_physical_pages_or_internal_markers(self) -> None:
        blocks = [
            SourceBlock("heading", "## A reviewed section", 219),
            SourceBlock("body", "Source paragraph.401", 219, marker="P0001"),
            SourceBlock("figure", "Figure 14", 244),
        ]
        notes = OrderedDict(((401, Note(401, ["Source note."])),))
        translations = {
            "P0001": "连续的中文正文⟦R401⟧。",
            "N0401": "完整的中文注释。",
        }

        rendered = render_reviewed_chapter(
            "第五章 测试标题",
            blocks,
            notes,
            translations,
        )

        self.assertIn("# 第五章 测试标题", rendered)
        self.assertIn("## A reviewed section", rendered)
        self.assertIn("连续的中文正文〔401〕。", rendered)
        self.assertIn("> **图14**：原稿插图，详见带目录 PDF 参考版。", rendered)
        self.assertIn("### 注释", rendered)
        self.assertIn("**401**　完整的中文注释。", rendered)
        for marker in (
            "pdf_page",
            "source-pdf",
            "pdf-pages",
            "PDF_PAGE",
            "pagebreak",
            "P0001",
            "N0401",
            "R401",
            "⟦",
            "⟧",
        ):
            self.assertNotIn(marker, rendered)
        self.assertNotIn("219", rendered)
        self.assertNotIn("244", rendered)


if __name__ == "__main__":
    unittest.main()
