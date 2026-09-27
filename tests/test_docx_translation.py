"""Offline tests for in-place DOCX translation (no network calls)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document
from docx_translation import (
    DocxTranslationError,
    clean_running_heads,
    needs_translation,
    strip_running_heads,
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


class RunningHeadTests(unittest.TestCase):
    def test_running_head_with_title_and_year_range_is_removed(self) -> None:
        text = "…如下写道。29 第1章 在世界中心呼喊爱的人 1995年—99年。全4部作预定中的第2作…"
        cleaned, count = strip_running_heads(text)
        self.assertEqual(count, 1)
        self.assertNotIn("第1章", cleaned)
        self.assertIn("全4部作预定中的第2作", cleaned)

    def test_folio_and_section_label_is_removed(self) -> None:
        cleaned, count = strip_running_heads("…发挥着功能。7 序文 世界系这一亡灵……12日在其网站上…")
        self.assertEqual(count, 1)
        self.assertNotIn("序文", cleaned)
        self.assertIn("12日在其网站上", cleaned)

    def test_folio_before_prose_is_not_swallowed(self) -> None:
        """A page number followed by ordinary prose must not eat the prose."""

        text = "…参考文献 251 后记 面向新的世界系的诞生 Niconico动画 作为交流"
        cleaned, count = strip_running_heads(text)
        self.assertEqual(count, 1)
        self.assertIn("Niconico动画 作为交流", cleaned)

    def test_plain_chapter_reference_is_left_alone(self) -> None:
        text = "正如第1章所述，这个问题在第2章还会出现。"
        cleaned, count = strip_running_heads(text)
        self.assertEqual(count, 0)
        self.assertEqual(cleaned, text)


class CleanRunningHeadsTests(unittest.TestCase):
    def test_cleans_every_paragraph_without_a_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.docx"
            document = Document()
            document.add_heading("序章", level=1)
            document.add_paragraph("正文。7 序文 世界系这一亡灵……接下来的内容。")
            document.add_paragraph("这一段没有页眉。")
            document.save(str(path))

            report = clean_running_heads(path)

            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["heads_removed"], 1)
            body = [p.text for p in Document(str(path)).paragraphs]
            self.assertNotIn("序文", "".join(body))
            self.assertIn("这一段没有页眉。", body)

    def test_reports_nothing_to_do(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "book.docx"
            document = Document()
            document.add_paragraph("干净的正文，没有页眉。")
            document.save(str(path))
            self.assertEqual(clean_running_heads(path)["status"], "nothing_to_do")


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
