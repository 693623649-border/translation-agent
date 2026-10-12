from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_quality_gate import (
    ocr_structure_anomalies,
    quotation_signature,
    calibrated_confidence_threshold,
    calibrated_disagreement_threshold,
    character_error_rate,
    comparison_reasons,
    punctuation_signature,
    review_sample_pages,
)


class OcrQualityGateTests(unittest.TestCase):
    def test_structure_allows_cross_page_quotation(self):
        self.assertEqual(ocr_structure_anomalies({1: '彼は「本文', 2: '続き」と言う。'}), {})

    def test_structure_allows_nested_angle_quotes_across_pages(self):
        self.assertEqual(ocr_structure_anomalies({17: '〈ギクリ', 18: '続き〉。〈散髪', 19: '続き〉。〈昭和《生田', 20: '続き》。〉'}), {})
        self.assertEqual(quotation_signature('〈昭和《生田》。〉'), tuple('〈《》〉'))

    def test_structure_detects_missing_angle_quote_close(self):
        self.assertIn('unclosed_document_quote', ocr_structure_anomalies({19: '〈昭和《生田', 20: '本文》。'})[19])
        self.assertNotEqual(quotation_signature('〈本文〉'), quotation_signature('本文'))

    def test_structure_flags_high_confidence_text_without_using_confidence(self):
        issues = ocr_structure_anomalies({1: '「本文', 2: 'へ\n余計な＞記号'})
        self.assertIn('unclosed_document_quote', issues[1])
        self.assertIn('isolated_kana_he', issues[2])
        self.assertIn('angle_bracket_noise', issues[2])

    def test_structure_does_not_silently_discard_repeated_prose(self):
        self.assertEqual(set(ocr_structure_anomalies({i: '「本文\n続き。' for i in range(1,4)})), {1, 2, 3})

    def test_structure_detects_mismatched_nesting(self):
        self.assertIn('unmatched_closing_quote', ocr_structure_anomalies({1: '「『本文」』'})[1])

    def test_quote_signature_allows_natural_sentence_punctuation(self):
        self.assertEqual(quotation_signature('「正文、です。」'), quotation_signature('「正文，是这样。」'))


    def test_punctuation_signature_preserves_exact_order_and_glyphs(self) -> None:
        self.assertEqual(punctuation_signature("「本文、です。」"), tuple("「、。」"))
        self.assertNotEqual(
            punctuation_signature("「本文、です。」"),
            punctuation_signature('"本文,です."'),
        )

    def test_character_error_rate_ignores_whitespace(self) -> None:
        self.assertEqual(character_error_rate("a b\nc", "abc"), 0.0)
        self.assertAlmostEqual(character_error_rate("abc", "axc"), 1 / 3)

    def test_punctuation_difference_is_independent_of_cer_threshold(self) -> None:
        reasons = comparison_reasons("正文。" * 1000, "正文！" + "正文。" * 999, cer_threshold=1.0)
        self.assertIn("punctuation_difference", reasons)
        self.assertNotIn("model_cer_difference", reasons)

    def test_uncalibrated_disagreement_is_reviewed(self) -> None:
        self.assertIn(
            "uncalibrated_model_difference",
            comparison_reasons("原文。", "異文。", cer_threshold=None),
        )

    def test_sample_size_is_three_to_ten_and_stable(self) -> None:
        sample = review_sample_pages(range(1, 224), source_sha256="a" * 64)
        self.assertEqual(len(sample), 10)
        self.assertEqual(sample, review_sample_pages(range(1, 224), source_sha256="a" * 64))
        self.assertEqual(
            len(review_sample_pages(range(1, 8), source_sha256="b" * 64)), 3
        )
        self.assertEqual(
            len(review_sample_pages([1, 2], source_sha256="b" * 64)), 2
        )

    def test_confidence_threshold_requires_ten_human_labels(self) -> None:
        labels = [
            {"confidence": 0.7 + index / 100, "matches_image": index != 0}
            for index in range(10)
        ]
        self.assertEqual(calibrated_confidence_threshold(labels[:9]), None)
        self.assertEqual(calibrated_confidence_threshold(labels), 0.7)

    def test_disagreement_threshold_requires_clean_separation(self) -> None:
        labels = [
            {"model_cer": 0.01, "primary_matches_image": True}
            for _ in range(9)
        ] + [{"model_cer": 0.10, "primary_matches_image": False}]
        self.assertAlmostEqual(calibrated_disagreement_threshold(labels), 0.055)
        labels[-1]["model_cer"] = 0.005
        self.assertIsNone(calibrated_disagreement_threshold(labels))


if __name__ == "__main__":
    unittest.main()
