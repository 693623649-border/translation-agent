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
        for title in ("本书重要名词英德汉文对照表", "主要参考书目", "主要参考作品", "索引", "译名对照表"):
            verdict = classify_row(
                title,
                "action | Handlung | 行动  Adorno, Theodor, The Authoritarian Personality, 1950.",
            )
            self.assertFalse(verdict["needs_translation"], (title, verdict))
            self.assertIn(verdict["reason"], {"reference_material", "already_chinese"})

    def test_chinese_prose_with_japanese_residue_is_exempt(self) -> None:
        """OCR leaves shop names and plate fragments inside translated prose.

        Re-running the translator on such a chunk is a no-op (the model returns
        it unchanged), so the residue floor keeps the gate from blocking the
        release forever instead of reporting genuine untranslated text.
        """

        verdict = classify_row(
            "第三章 〈变体少女文字〉",
            "正如其名，是阁楼房间的意象。房间这一设定（マチルド・イン・ザ・ギレット直译的话，"
            "即“阁楼里的玛蒂尔德”），作为店很有名。这里摆放着泰迪熊（テディベア）的毛绒玩具"
            "和家居服等，本来就应该这样放置；实际上在 Mani 就是如此展示的。奶锅将花纹朝这边，"
            "作为〈可爱之物〉放在白色凸窗的一角，画面描绘得十分细致。",
        )
        self.assertFalse(verdict["needs_translation"], verdict)
        self.assertEqual(verdict["reason"], "chinese_with_quote_residue")

    def test_japanese_colophon_is_not_exempted_by_the_residue_floor(self) -> None:
        """The densest Japanese apparatus still sits above the kana ceiling.

        A colophon is mostly Han (dates, addresses, company names) with only
        24 % kana — the closest real Japanese comes to the ceiling — so this
        case pins the boundary the floor must not cross.
        """

        verdict = classify_row(
            "奥付",
            "大塚英志(か・えい) 1958年、東京生まれ。【著者紹介】江藤淳と少女フェミニズム的戦後 "
            "サブカルチ+一文学論序章 2001年11月10日初版第1刷発行 著者——大塚英志 発行者—菊池明郎 "
            "発行所—株式会社筑摩書房 東京都台東区藏前2-5-3郵便番号111-8755振替00160-8-4123 "
            "印刷——三松堂印刷 製本——積信堂 ©EIJIOTSUKA2001 ISBN4-480-82347-6 C0095 Printed inJapan "
            "乱丁・落丁本の場合は、御面倒ですが下記に御送付下さい。送料小社負担てお取替致しす。 "
            "ご注文・お問い合わせも下記へお願いいたします。 331-8507さい",
        )
        self.assertTrue(verdict["needs_translation"], verdict)

    def test_english_dominant_prose_with_han_residue_is_not_exempted(self) -> None:
        """A Han trace inside English prose must not flip the verdict to Chinese."""

        verdict = classify_row(
            "Chapter 1",
            "The historical novel developed as a response to the crisis of representation in "
            "the nineteenth century. Critics argued that the genre negotiates between private "
            "experience and public history, and its formal conventions continue to shape "
            "contemporary fiction across many national traditions. "
            "柄谷行人认为，这与日本近代文学的形成密切相关，尤其是在言文一致运动之后的时期，"
            "文学与国家的想象力之间存在着复杂的联系，这一点值得进一步考察。",
        )
        self.assertTrue(verdict["needs_translation"], verdict)

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

    def test_dropped_placeholder_degrades_to_single_segments(self) -> None:
        """A batch that loses protected placeholders follows the same ladder.

        Measured against a real provider: a five-segment batch came back with
        every ``<<<SEG nnnn>>>`` marker intact but all ⟦SEMANTIC_TOKEN_xxxx⟧
        placeholders dropped.  That is the same 1:1 contract break as a marker
        mismatch, so it must retry and degrade to single segments instead of
        aborting the whole book.
        """

        class _TokenDroppingOnBatches(_FakeTranslator):
            def translate(self, prompt: str) -> str:
                markers = [line for line in prompt.splitlines() if line.startswith("<<<SEG")]
                if len(markers) > 1:
                    return "\n".join(f"{marker}\n[译]中文译文" for marker in markers)
                return f"{markers[0]}\n[译]中文⟦SEMANTIC_TOKEN_0000⟧译文"

        result = translate_texts(
            ["本文[^1]の続き", "第二段[^2]の続き"],
            _TokenDroppingOnBatches(),
            batch_chars=100,
            concurrency=1,
        )
        self.assertEqual(result, ["[译]中文[^1]译文", "[译]中文[^2]译文"])

    def test_single_segment_mismatch_still_raises(self) -> None:
        with self.assertRaises(KbTranslationError):
            translate_texts(["唯一一段"], _MarkerBreakingTranslator(), concurrency=1)

    def test_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            translate_texts([""], _FakeTranslator())

    def test_extra_instructions_reach_every_prompt(self) -> None:
        """Book-specific directives ride along on every batch and retry call."""

        class _PromptRecordingTranslator(_FakeTranslator):
            def __init__(self) -> None:
                super().__init__()
                self.prompts: list[str] = []

            def translate(self, prompt: str) -> str:
                self.prompts.append(prompt)
                return super().translate(prompt)

        translator = _PromptRecordingTranslator()
        long_a = "あ" * 60 + "。"
        long_b = "い" * 60 + "。"
        result = translate_texts(
            [long_a, long_b],
            translator,
            batch_chars=100,
            concurrency=1,
            extra_instructions="禁止用省略号概括任何内容。",
        )
        self.assertEqual(result, ["[译]中文译文", "[译]中文译文"])
        self.assertEqual(len(translator.prompts), 2)
        for prompt in translator.prompts:
            self.assertIn("禁止用省略号概括任何内容。", prompt)

    def test_default_prompt_carries_no_extra_instructions(self) -> None:
        class _PromptRecordingTranslator(_FakeTranslator):
            def __init__(self) -> None:
                super().__init__()
                self.prompts: list[str] = []

            def translate(self, prompt: str) -> str:
                self.prompts.append(prompt)
                return super().translate(prompt)

        translator = _PromptRecordingTranslator()
        translate_texts(["一段日文。"], translator)
        self.assertNotIn("禁止", translator.prompts[0])


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
