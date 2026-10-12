import unittest
from concurrent.futures import ThreadPoolExecutor

from paragraph_translation import (
    ParagraphTranslator, TranslationQualityError, split_paragraph_chunks,
    validate_source_quotes, validate_translation,
)


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def chat_text(self, prompt, *, system):
        self.calls.append((prompt, system))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class ParagraphTranslationTests(unittest.TestCase):
    def test_short_japanese_book_title_inside_quotes_must_be_translated(self):
        with self.assertRaisesRegex(TranslationQualityError, 'Japanese'):
            validate_translation('『花を持てる女』', '他读了『花を持てる女』。')

    def test_mixed_japanese_quote_is_rejected_before_glyph_difference(self):
        with self.assertRaisesRegex(TranslationQualityError, 'Japanese'):
            validate_translation('「彼は本を読んだ。私は彼の家へ行った。」',
                                 '中文导语。彼は本を読んだ。私は彼の家へ行った。')

    def test_unreviewed_draft_does_not_claim_source_review_or_insert_quote(self):
        translator = ParagraphTranslator(Client(['他叫作「猫。']))
        with self.assertRaises(TranslationQualityError):
            translator.translate('彼は「猫。')
        self.assertEqual(translator.last_telemetry['requests'], 0)
        self.assertEqual(translator.translate('彼は「猫。', allow_unreviewed_source=True), '他叫作「猫。')
        self.assertTrue(translator.last_telemetry['source_review_pending'])

    def test_complete_paragraph_and_context_in_one_request(self):
        source = '彼は「猫」と言った。\n\n私は考えた。'
        client = Client(['他说「猫」。\n\n我思考着。'])
        translator = ParagraphTranslator(client)
        self.assertEqual(translator.translate(source, context_before='先前内容', context_after='后来内容'),
                         '他说「猫」。\n\n我思考着。')
        self.assertEqual(len(client.calls), 1)
        self.assertIn(source, client.calls[0][0])
        self.assertIn('<context_before>\n先前内容', client.calls[0][0])
        self.assertEqual(translator.last_telemetry['requests'], 1)

    def test_source_error_blocks_before_any_call(self):
        client = Client([])
        translator = ParagraphTranslator(client)
        with self.assertRaisesRegex(TranslationQualityError, 'OCR image review'):
            translator.translate('彼は「猫と言った。')
        self.assertEqual(client.calls, [])
        self.assertEqual(translator.last_telemetry['requests'], 0)

    def test_cross_page_quotes_keep_page_boundaries(self):
        translator = ParagraphTranslator(Client(['猫」他说。']))
        self.assertEqual(translator.translate('猫」と言った。', context_before='彼は「'), '猫」他说。')
        with self.assertRaises(TranslationQualityError):
            validate_source_quotes('猫」と言った。', '没有开引号')

    def test_repair_once_without_recursive_subdivision(self):
        client = Client(['他说“猫”。', '他说「猫」。'])
        translator = ParagraphTranslator(client)
        self.assertEqual(translator.translate('彼は「猫」と言った。'), '他说「猫」。')
        self.assertEqual(translator.last_telemetry['requests'], 2)
        self.assertEqual(translator.last_telemetry['retries'], 1)

    def test_nested_angle_quotes_continue_across_pages(self):
        translator = ParagraphTranslator(Client(['理发后我读到《生日很好》。']))
        self.assertEqual(
            translator.translate('散髪して《生日很好》を読んだ。', context_before='〈', context_after='〉'),
            '理发后我读到《生日很好》。',
        )
        validate_source_quotes('〈昭和《生田', context_after='生日》という〉')
        validate_source_quotes('生日》という〉', context_before='〈昭和《生田')
        with self.assertRaises(TranslationQualityError):
            validate_translation('〈猫〉', '《猫》')
        with self.assertRaises(TranslationQualityError):
            validate_source_quotes('〈昭和《生田〉》')

    def test_failed_output_stops_after_two_requests(self):
        client = Client(['', ''])
        translator = ParagraphTranslator(client)
        with self.assertRaisesRegex(TranslationQualityError, 'two requests'):
            translator.translate('彼は考えた。')
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(translator.last_telemetry['request_seconds']), 2)

    def test_plain_prose_does_not_require_array(self):
        client = Client(['我思考着、随后说道「猫」。'])
        translator = ParagraphTranslator(client)
        self.assertEqual(translator.translate('私は考え、「猫」と言った。'), '我思考着、随后说道「猫」。')
        self.assertEqual(len(client.calls), 1)

    def test_extra_ellipsis_and_untranslated_phrase_rejected(self):
        for output in ('我思考着……', '我思考着...', '我思考着コトバ'):
            with self.subTest(output=output), self.assertRaises(TranslationQualityError):
                validate_translation('私は考えた。', output)

    def test_every_source_punctuation_glyph_must_be_preserved(self):
        with self.assertRaisesRegex(TranslationQualityError, "punctuation"):
            validate_translation("これは（例）です。", "这是例子！")
        with self.assertRaisesRegex(TranslationQualityError, "punctuation"):
            validate_translation("本文、続く。", "正文，继续。")

    def test_nesting_and_glyph_changes_rejected(self):
        for output in ('「猫』和『狗」', '“猫”和“狗”'):
            with self.assertRaises(TranslationQualityError):
                validate_translation('「猫」と『犬』', output)

    def test_long_paragraph_only_splits_at_sentence_end(self):
        self.assertEqual(split_paragraph_chunks('甲乙丙。丁戊己。', 4), ['甲乙丙。', '丁戊己。'])
        with self.assertRaisesRegex(TranslationQualityError, 'sentence exceeds'):
            split_paragraph_chunks('甲乙丙丁戊己。', 4)

    def test_structural_validation_is_not_semantic_proof(self):
        # Deliberately documents the limitation: an independent image/semantic
        # review must assess meaning; no heuristic can certify it from structure.
        validate_translation('私は考えた。', '完全无关的内容。')

    def test_thread_local_telemetry(self):
        class EchoClient:
            def chat_text(self, prompt, *, system):
                return '我思考着。'
        translator = ParagraphTranslator(EchoClient())
        def translate(_):
            translator.translate('私は考えた。')
            return translator.last_telemetry
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(translate, range(2)))
        self.assertIsNot(first, second)
        self.assertEqual(translator.last_telemetry, {})


if __name__ == '__main__':
    unittest.main()
