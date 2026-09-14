"""Offline tests for in-place DOCX translation (no network calls)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx_translation import (
    DocxTranslationError,
    needs_translation,
    translate_docx,
    verify_translated_docx,
)


class _FakeTranslator:
    """Returns a Chinese placeholder while honouring the segment contract."""

    provider_name = "fake"
    model = "fake-translator"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, prompt: str) -> str:
        self.calls += 1
        markers = [line for line in prompt.splitlines() if line.startswith("<<<SEG")]
        return "\n".join(f"{marker}\n[译]中文段落" for marker in markers)


class NeedsTranslationTests(unittest.TestCase):
    def test_kana_only_heading_is_translated(self) -> None:
        """Short headings fall below the language detector; kana is decisive."""

        for heading in ("あとがき", "エクリチュル", "はじめに"):
            self.assertTrue(needs_translation(heading), heading)

    def test_mixed_heading_is_translated(self) -> None:
        for heading in ("意識と自然", "漱石とジンル", "内側から見た生"):
            self.assertTrue(needs_translation(heading), heading)

    def test_kanji_only_short_string_is_left_alone(self) -> None:
        """Kanji-only names and titles read as Chinese."""

        for text in ("漱石論集成", "柄谷行人", "作品解説"):
            self.assertFalse(needs_translation(text), text)

    def test_folios_and_ornaments_are_skipped(self) -> None:
        for text in ("", "   ", "270", "-", "—", "（12）"):
            self.assertFalse(needs_translation(text), text)

    def test_english_prose_is_translated(self) -> None:
        self.assertTrue(
            needs_translation(
                "Language study is not merely a matter of the vocal organs, and the "
                "English address was delivered to a student audience."
            )
        )

    def test_chinese_paragraph_is_left_alone(self) -> None:
        self.assertFalse(
            needs_translation("现代性与消费主义在频繁制造差异的同时，也放大了这种唯我论。")
        )


class TranslateDocxTests(unittest.TestCase):
    def _document(self, directory: Path) -> Path:
        path = directory / "book.docx"
        document = Document()
        document.add_heading("意識と自然", level=1)
        document.add_paragraph("できない恐しいことに表現を与えることを望んだか、われわれには知るすべがない。")
        document.add_paragraph("现代性与消费主义在频繁制造差异的同时，也放大了这种唯我论。")
        document.add_paragraph("柄谷行人")
        document.save(str(path))
        return path

    def test_writes_a_separate_chinese_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            before = source.read_bytes()
            report = translate_docx(source, _FakeTranslator())
            output = Path(report["output"])
            self.assertTrue(output.is_file())
            self.assertTrue(output.name.endswith("_中文.docx"))
            self.assertEqual(source.read_bytes(), before)

    def test_preserves_heading_style(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            report = translate_docx(source, _FakeTranslator())
            translated = Document(report["output"])
            heading = translated.paragraphs[0]
            self.assertEqual(heading.style.name, "Heading 1")
            self.assertEqual(heading.text, "[译]中文段落")

    def test_leaves_chinese_and_kanji_only_paragraphs_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            report = translate_docx(source, _FakeTranslator())
            body = [p.text for p in Document(report["output"]).paragraphs]
            self.assertIn("现代性与消费主义在频繁制造差异的同时，也放大了这种唯我论。", body)
            self.assertIn("柄谷行人", body)
            self.assertEqual(report["translate_count"], 2)

    def test_runs_are_preserved_when_paragraph_has_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            report = translate_docx(source, _FakeTranslator())
            self.assertEqual(report["flattened_paragraphs"], 0)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            translator = _FakeTranslator()
            report = translate_docx(source, None, dry_run=True)
            self.assertEqual(report["status"], "dry_run")
            self.assertFalse(report["written"])
            self.assertEqual(translator.calls, 0)
            self.assertEqual(len(list(Path(directory).glob("*.docx"))), 1)

    def test_nothing_to_translate_skips_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn.docx"
            document = Document()
            document.add_paragraph("这是一段纯中文正文，不需要翻译。")
            document.save(str(path))
            report = translate_docx(path, _FakeTranslator())
            self.assertEqual(report["status"], "nothing_to_do")
            self.assertFalse(report["written"])

    def test_verify_reports_remaining_non_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = self._document(Path(directory))
            self.assertEqual(verify_translated_docx(source)["pending"], 2)
            report = translate_docx(source, _FakeTranslator())
            self.assertEqual(verify_translated_docx(report["output"])["pending"], 0)


if __name__ == "__main__":
    unittest.main()
