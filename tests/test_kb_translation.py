"""Offline tests for the Chinese-normalisation gate (no network calls)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kb_translation import (
    KbTranslationError,
    classify_row,
    ensure_chinese_rows,
    load_translation_sidecar,
    normalise_corpus_file,
    plan_translation,
    translate_texts,
)


class _FakeTranslator:
    """Echoes a deterministic Chinese placeholder while preserving markers."""

    provider_name = "fake"
    model = "fake-translator"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, prompt: str) -> str:
        self.calls += 1
        segments = [line for line in prompt.splitlines() if line.strip()]
        markers = [line for line in segments if line.startswith("<<<SEG")]
        return "\n".join(f"{marker}\n[译]中文译文" for marker in markers)


class _MarkerBreakingTranslator(_FakeTranslator):
    """Returns a marker index that cannot match any batch size."""

    def translate(self, prompt: str) -> str:
        return "<<<SEG 0007>>>\n标记错位"


class _FlakyBatchTranslator(_FakeTranslator):
    """Duplicates a marker on multi-segment batches, like a real flaky response."""

    def translate(self, prompt: str) -> str:
        markers = [line for line in prompt.splitlines() if line.startswith("<<<SEG")]
        if len(markers) > 1:
            return "\n".join(f"{marker}\n[坏]坏回包" for marker in [*markers, markers[-1]])
        return super().translate(prompt)


class _EchoTranslator(_FakeTranslator):
    """Returns the segment unchanged, like a model that judges it already Chinese."""

    def translate(self, prompt: str) -> str:
        segments = prompt.split("\n\n")[1:]
        return "\n\n".join(segment.strip() for segment in segments if segment.strip())


class ClassificationTests(unittest.TestCase):
    def test_chinese_body_passes_through(self) -> None:
        verdict = classify_row("第一章 欲望机器", "它在各处发挥着自己的功能，时而不停歇，时而断断续续。")
        self.assertFalse(verdict["needs_translation"])
        self.assertEqual(verdict["reason"], "already_chinese")

    def test_japanese_body_needs_translation(self) -> None:
        verdict = classify_row(
            "序",
            "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。十五年前の私は、"
            "いろいろ重苦しいものを背負わされた病身で、それを書きつづってきた。",
        )
        self.assertTrue(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["language"], "ja")

    def test_english_body_needs_translation(self) -> None:
        verdict = classify_row(
            "1. The Desiring Machines",
            "It breathes, it heats, it eats. It shits and fucks. What a mistake to have ever "
            "said the it. Everywhere it is machines, real ones.",
        )
        self.assertTrue(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["language"], "en")

    def test_reference_material_is_exempt(self) -> None:
        for title in ("本书重要名词英德汉文对照表", "主要参考书目", "索引", "译名对照表"):
            verdict = classify_row(
                title,
                "action | Handlung | 行动  Adorno, Theodor, The Authoritarian Personality, 1950.",
            )
            self.assertFalse(verdict["needs_translation"], (title, verdict))
            self.assertIn(verdict["reason"], {"reference_material", "already_chinese"})

    def test_image_shrapnel_is_not_prose(self) -> None:
        verdict = classify_row("封面", "![cover](../Images/cover.jpg)")
        self.assertFalse(verdict["needs_translation"])
        self.assertEqual(verdict["reason"], "not_prose")

    def test_index_continuation_is_exempt_by_content(self) -> None:
        """Only the first index chunk carries the label; continuations are lettered."""

        verdict = classify_row(
            "A",
            "Ellison, Ralph 54, 100, 406\n"
            "Adorno, T.W. 42-44, 407, 408\n"
            "Austin, Jane 214, 6, 13, 21, 31\n"
            "Aron, Raymond 216, 217, 221\n"
            "Arendt, Hannah 282\n",
        )
        self.assertFalse(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["reason"], "reference_material")

    def test_isolated_index_entry_is_exempt(self) -> None:
        verdict = classify_row("E", "恩格斯 (Engels, Frederick) 264, 387")
        self.assertFalse(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["reason"], "reference_material")

    def test_prose_with_numbers_is_still_translated(self) -> None:
        """A guard against over-exempting: prose citing page numbers is content."""

        verdict = classify_row(
            "Chapter 7",
            "The argument turns on the distinction between the two editions. In 1901 he "
            "published the second volume, and by 1905 the third had appeared in Paris. "
            "Readers who followed the debate will recognize the same claim restated here.",
        )
        self.assertTrue(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["reason"], "non_chinese_body")

    def test_plan_counts_reasons(self) -> None:
        rows = [
            {"id": "a" * 40, "title": "第一章", "chapter_id": "c1", "chapter_order": 1, "content": "这是一段中文正文。"},
            {
                "id": "b" * 40,
                "title": "序",
                "chapter_id": "c2",
                "chapter_order": 2,
                "content": "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。",
            },
        ]
        plan = plan_translation(rows)
        self.assertEqual(plan["chunk_count"], 2)
        self.assertEqual(plan["translate_count"], 1)
        self.assertEqual(plan["skipped"]["already_chinese"], 1)


class TranslateTextsTests(unittest.TestCase):
    def test_translates_one_to_one(self) -> None:
        translator = _FakeTranslator()
        result = translate_texts(["一段日文", "第二段"], translator)
        self.assertEqual(result, ["[译]中文译文", "[译]中文译文"])
        self.assertEqual(translator.calls, 1)

    def test_batches_on_character_budget(self) -> None:
        translator = _FakeTranslator()
        translate_texts(["x" * 100, "y" * 100, "z" * 100], translator, batch_chars=150)
        self.assertEqual(translator.calls, 3)

    def test_marker_mismatch_fails_closed(self) -> None:
        with self.assertRaises(KbTranslationError):
            translate_texts(["一段日文", "第二段"], _MarkerBreakingTranslator())

    def test_oversized_segment_is_split_and_rejoined(self) -> None:
        """A chapter dumped into one paragraph must not be sent whole."""

        class _RecordingTranslator(_FakeTranslator):
            def __init__(self) -> None:
                super().__init__()
                self.prompts: list[str] = []

            def translate(self, prompt: str) -> str:
                self.prompts.append(prompt)
                return super().translate(prompt)

        translator = _RecordingTranslator()
        # Longer than the per-request segment ceiling, with sentence enders.
        long_text = "这是一段很长的日文。" * 400
        result = translate_texts([long_text], translator, batch_chars=100_000)
        markers = [line for line in translator.prompts[0].splitlines() if line.startswith("<<<SEG")]
        self.assertGreater(len(markers), 1, "oversized segment was not split")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0])

    def test_invented_placeholder_is_stripped(self) -> None:
        class _TokenInventingTranslator(_FakeTranslator):
            def translate(self, prompt: str) -> str:
                markers = [line for line in prompt.splitlines() if line.startswith("<<<SEG")]
                return "\n".join(f"{marker}\n[译]中文⟦SEMANTIC_TOKEN_0007⟧段落" for marker in markers)

        result = translate_texts(["一段日文"], _TokenInventingTranslator())
        self.assertEqual(result, ["[译]中文段落"])

    def test_flaky_batch_degrades_to_single_segments(self) -> None:
        """A duplicated marker on a batch must be retried per segment, not fail the book."""

        translator = _FlakyBatchTranslator()
        result = translate_texts(["第一段", "第二段"], translator, batch_chars=100, concurrency=1)
        self.assertEqual(result, ["[译]中文译文", "[译]中文译文"])

    def test_single_segment_mismatch_still_raises(self) -> None:
        with self.assertRaises(KbTranslationError):
            translate_texts(["唯一一段"], _MarkerBreakingTranslator(), concurrency=1)

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            translate_texts([""], _FakeTranslator())


class EnsureChineseRowsTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            {
                "id": "a" * 40,
                "title": "第一章",
                "chapter_id": "c1",
                "chapter_order": 1,
                "content": "这是一段中文正文，不需要翻译。",
            },
            {
                "id": "b" * 40,
                "title": "序",
                "chapter_id": "c2",
                "chapter_order": 2,
                "content": "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。",
            },
        ]

    def test_preserves_ids_and_coordinates(self) -> None:
        rows, report = ensure_chinese_rows(self._rows(), _FakeTranslator())
        self.assertEqual([row["id"] for row in rows], ["a" * 40, "b" * 40])
        self.assertEqual([row["chapter_id"] for row in rows], ["c1", "c2"])
        self.assertEqual([row["chapter_order"] for row in rows], [1, 2])
        self.assertEqual(len(report["translated"]), 1)
        self.assertEqual(report["translated"][0]["id"], "b" * 40)
        self.assertEqual(len(report["translated"][0]["source_sha256"]), 64)

    def test_untouched_row_keeps_exact_content(self) -> None:
        rows, _ = ensure_chinese_rows(self._rows(), _FakeTranslator())
        self.assertEqual(rows[0]["content"], "这是一段中文正文，不需要翻译。")

    def test_model_returning_source_is_recorded_as_unchanged(self) -> None:
        """A no-op translation must not be booked as a translation."""

        rows = [
            {
                "id": "b" * 40,
                "title": "序",
                "chapter_id": "c2",
                "chapter_order": 2,
                "content": "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。",
            }
        ]
        translated, report = ensure_chinese_rows(rows, _EchoTranslator())
        self.assertEqual(translated[0]["content"], rows[0]["content"])
        self.assertEqual(report["translated_count"], 0)
        self.assertEqual(report["unchanged_count"], 1)
        self.assertEqual(report["unchanged"][0]["reason"], "model_returned_source")

    def test_duplicate_ids_do_not_misalign(self) -> None:
        """A repeated id must not make a Chinese row absorb another row's translation."""

        rows = [
            {"id": "x" * 40, "title": "第一章", "chapter_id": "c1", "chapter_order": 1, "content": "这是中文正文。"},
            {
                "id": "x" * 40,
                "title": "序",
                "chapter_id": "c2",
                "chapter_order": 2,
                "content": "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。",
            },
        ]
        translated, report = ensure_chinese_rows(rows, _FakeTranslator())
        self.assertEqual(translated[0]["content"], "这是中文正文。")
        self.assertEqual(translated[1]["content"], "[译]中文译文")
        self.assertEqual(report["translated_count"], 1)


class NormaliseCorpusFileTests(unittest.TestCase):
    def _corpus(self, directory: Path) -> Path:
        path = directory / "knowledge_base.jsonl"
        rows = [
            {"id": "a" * 40, "title": "第一章", "chapter_id": "c1", "chapter_order": 1, "content": "这是一段中文正文。"},
            {
                "id": "b" * 40,
                "title": "序",
                "chapter_id": "c2",
                "chapter_order": 2,
                "content": "早いもので、この本の初版が刊行されてから、すでに十五年の歳月がたった。",
            },
        ]
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = self._corpus(Path(directory))
            before = corpus.read_bytes()
            result = normalise_corpus_file(corpus, dry_run=True)
            self.assertEqual(result["status"], "dry_run")
            self.assertEqual(result["translate_count"], 1)
            self.assertFalse(result["written"])
            self.assertEqual(corpus.read_bytes(), before)
            self.assertFalse((Path(directory) / "knowledge_base.translation.json").exists())

    def test_translation_rewrites_corpus_and_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = self._corpus(Path(directory))
            result = normalise_corpus_file(corpus, _FakeTranslator())
            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["written"])
            rows = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[1]["content"], "[译]中文译文")
            self.assertEqual(rows[0]["content"], "这是一段中文正文。")
            sidecar = load_translation_sidecar(corpus)
            self.assertEqual(sidecar["translated_count"], 1)
            self.assertEqual(sidecar["model"], "fake-translator")

    def test_sidecar_rejects_stale_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            corpus = self._corpus(Path(directory))
            normalise_corpus_file(corpus, _FakeTranslator())
            corpus.write_text(corpus.read_text(encoding="utf-8") + "", encoding="utf-8")
            rows = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
            rows[0]["content"] = "改动后的内容"
            corpus.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
            with self.assertRaises(KbTranslationError):
                load_translation_sidecar(corpus)

    def test_nothing_to_translate_skips_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "knowledge_base.jsonl"
            path.write_text(
                json.dumps(
                    {"id": "a" * 40, "title": "第一章", "chapter_id": "c1", "chapter_order": 1, "content": "纯中文正文。"},
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            result = normalise_corpus_file(path)
            self.assertEqual(result["status"], "nothing_to_do")
            self.assertFalse(result["written"])


if __name__ == "__main__":
    unittest.main()
