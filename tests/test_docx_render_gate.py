from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from docx_render_gate import (
    _interleaved_index_blocks,
    inspect_rendered_pdf,
    verify_docx_render,
)


class DocxRenderGateTests(unittest.TestCase):
    def test_rendered_pdf_detects_blank_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            document = fitz.open()
            document.new_page()
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(path, expected_text="")

        self.assertIn(
            "docx_render_blank_page",
            {issue["code"] for issue in report["issues"]},
        )

    def test_rendered_pdf_reports_text_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "text.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Hello semantic document")
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(
                path,
                expected_text="Hello semantic document",
            )

        self.assertFalse(report["issues"], report["issues"])
        self.assertEqual(report["metrics"]["page_count"], 1)
        self.assertGreaterEqual(report["metrics"]["text_coverage"], 0.9)

    def test_missing_renderer_is_a_hard_failure(self) -> None:
        with patch("docx_render_gate.find_soffice", return_value=None), patch(
            "docx_render_gate._render_docx_with_word", return_value=None
        ):
            report = verify_docx_render("missing.docx")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["issues"][0]["code"], "docx_renderer_missing")

    def test_word_fallback_renders_when_soffice_missing(self) -> None:
        fallback = {
            "status": "passed",
            "issues": [],
            "warnings": [],
            "metrics": {"renderer": "microsoft-word-isolated"},
        }
        with patch("docx_render_gate.find_soffice", return_value=None), patch(
            "docx_render_gate._render_docx_with_word", return_value=fallback
        ) as mocked_fallback:
            report = verify_docx_render("book.docx")

        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["metrics"]["renderer"], "microsoft-word-isolated")
        mocked_fallback.assert_called_once()

    def test_word_fallback_needs_windows_and_script(self) -> None:
        with patch("docx_render_gate.find_soffice", return_value=None), patch(
            "docx_render_gate._WORD_RENDER_SCRIPT", Path("Z:/absent/render.ps1")
        ):
            report = verify_docx_render("missing.docx")

        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["issues"][0]["code"], "docx_renderer_missing")

    def test_invisible_cjk_text_layer_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invisible-cjk.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 100),
                "中文字形缺失检测" * 30,
                fontname="china-s",
                fontsize=11,
                render_mode=3,
            )
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(
                path,
                expected_text="中文字形缺失检测" * 30,
            )

        self.assertIn(
            "docx_render_cjk_glyphs_missing",
            {issue["code"] for issue in report["issues"]},
        )

    def test_interleaved_bibliography_blocks_are_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interleaved-bibliography.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text(
                (72, 72),
                "Press, 1982. Rivera, Elena, The Making of Institutions, 1999.",
            )
            page.insert_text(
                (72, 96),
                "Morgan, Alice, Public Reason: Taylor, Peter, Civic Life, 1979.",
            )
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(
                path,
                expected_text=(
                    "Press, 1982. Rivera, Elena, The Making of Institutions, 1999. "
                    "Morgan, Alice, Public Reason: Taylor, Peter, Civic Life, 1979."
                ),
            )

        self.assertIn(
            "docx_render_interleaved_bibliography",
            {issue["code"] for issue in report["issues"]},
        )
        self.assertEqual(
            report["metrics"]["interleaved_bibliography_page_count"], 1
        )

    def test_normal_bibliography_coauthors_do_not_trigger_interleaving(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coauthors.pdf"
            document = fitz.open()
            page = document.new_page()
            text = (
                "Morgan, Alice, and Taylor, Peter, Civic Life, New York, "
                "Example Press, 1979."
            )
            page.insert_text((72, 72), text)
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(path, expected_text=text)

        codes = {
            issue["code"]
            for issue in [*report["issues"], *report["warnings"]]
        }
        self.assertNotIn("docx_render_interleaved_bibliography", codes)
        self.assertNotIn("docx_render_interleaved_bibliography_warning", codes)

    def test_single_suspicious_bibliography_block_only_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single-suspicious-block.pdf"
            document = fitz.open()
            page = document.new_page()
            text = "Press, 1982. Rivera, Elena, The Making of Institutions, 1999."
            page.insert_text((72, 72), text)
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(path, expected_text=text)

        self.assertNotIn(
            "docx_render_interleaved_bibliography",
            {issue["code"] for issue in report["issues"]},
        )
        self.assertIn(
            "docx_render_interleaved_bibliography_warning",
            {warning["code"] for warning in report["warnings"]},
        )

    def test_bibliography_subtitle_enumeration_does_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subtitle-enumeration.pdf"
            document = fitz.open()
            page = document.new_page()
            text = (
                "Seigel, Jerrold, Bohemian Paris: Culture, Politics, and "
                "Boundaries of Bourgeois Life, New York, Example Press, 1986."
            )
            page.insert_text((72, 72), text)
            document.save(path)
            document.close()

            report = inspect_rendered_pdf(path, expected_text=text)

        codes = {
            issue["code"]
            for issue in [*report["issues"], *report["warnings"]]
        }
        self.assertNotIn("docx_render_interleaved_bibliography", codes)
        self.assertNotIn("docx_render_interleaved_bibliography_warning", codes)

    @staticmethod
    def _write_index_page(path: Path, entries: list[str]) -> str:
        document = fitz.open()
        page = document.new_page()
        for row, text in enumerate(entries):
            page.insert_text((72, 72 + row * 24), text)
        document.save(path)
        document.close()
        return " ".join(entries)

    def test_interleaved_index_blocks_are_a_hard_failure(self) -> None:
        entries = [
            "Adams (Adams) 12, 18",
            "Baker (Baker) 20, 27",
            "Curie (Curie) 31, 39",
            "Darwin (Darwin) 41, 45",
            "Eliot (Eliot) 52, 58",
            "Freud (Freud) 61, 67",
            "Gandhi (Gandhi) 70, 75 Hayek (Hayek) 80, 84",
            "James (James) 90, 94 Keynes (Keynes) 98, 102",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interleaved-index.pdf"
            expected = self._write_index_page(path, entries)
            report = inspect_rendered_pdf(path, expected_text=expected)

        self.assertIn(
            "docx_render_interleaved_index",
            {issue["code"] for issue in report["issues"]},
        )
        self.assertEqual(report["metrics"]["interleaved_index_page_count"], 1)

    def test_clean_index_entries_do_not_trigger_interleaving(self) -> None:
        entries = [
            "Adams (Adams) 12, 18",
            "Baker (Baker) 20, 27",
            "Curie (Curie) 31, 39",
            "Darwin (Darwin) 41, 45",
            "Eliot (Eliot) 52, 58",
            "Freud (Freud) 61, 67",
            "Gandhi (Mohandas Gandhi) 70, 75, 80, 84",
            "King (Martin Luther King, Jr.) 90, 94, 98, 102",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean-index.pdf"
            expected = self._write_index_page(path, entries)
            report = inspect_rendered_pdf(path, expected_text=expected)

        codes = {
            issue["code"]
            for issue in [*report["issues"], *report["warnings"]]
        }
        self.assertNotIn("docx_render_interleaved_index", codes)
        self.assertNotIn("docx_render_interleaved_index_warning", codes)

    def test_wrapped_and_multilingual_index_entries_do_not_trigger(self) -> None:
        entries = [
            "阿伦特，汉娜（Arendt，Hannah） 282，284，290",
            "波德莱尔（Baudelaire，Charles） 102，184，251，329，330，341，343",
            "哈贝马斯（Habermas，Jürgen） 27，43，44，144",
            "卢卡奇（Lukács，Georg） 7，8，15，92，336，435",
            "马尔库塞，赫伯特（Marcuse，Herbert） 44，45，119，120，123，139，142，143，190，191，250，251",
            "萨特（Sartre，Jean-Paul） 6，7，31，99，250，283",
            "梭罗（Thoreau，Henry David） 260，382",
            "韦尔南，让－皮埃尔（Vernant，Jean-Pierre） 1，85，339",
        ]
        blocks = [
            (72.0, 72.0 + row * 24, 520.0, 90.0 + row * 24, text)
            for row, text in enumerate(entries)
        ]

        self.assertEqual(_interleaved_index_blocks(blocks), [])

    def test_single_suspicious_index_block_only_warns(self) -> None:
        entries = [
            "Adams (Adams) 12, 18",
            "Baker (Baker) 20, 27",
            "Curie (Curie) 31, 39",
            "Darwin (Darwin) 41, 45",
            "Eliot (Eliot) 52, 58",
            "Freud (Freud) 61, 67",
            "Gandhi (Gandhi) 70, 75",
            "James (James) 90, 94 Keynes (Keynes) 98, 102",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "single-interleaved-index.pdf"
            expected = self._write_index_page(path, entries)
            report = inspect_rendered_pdf(path, expected_text=expected)

        self.assertNotIn(
            "docx_render_interleaved_index",
            {issue["code"] for issue in report["issues"]},
        )
        self.assertIn(
            "docx_render_interleaved_index_warning",
            {warning["code"] for warning in report["warnings"]},
        )


if __name__ == "__main__":
    unittest.main()
