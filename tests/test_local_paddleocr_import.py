from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from local_paddleocr_import import (  # noqa: E402
    _mark_visually_blank_pages,
    _model_id,
    _previous_fallback_evidence_state,
    _validate_pages,
    _write_fallback_report,
    select_xiaomi_fallback_pages,
)
from ocr_quality_gate import read_json_object
from PIL import Image, ImageDraw


class LocalPaddleImportTests(unittest.TestCase):
    def test_structural_anomaly_does_not_trigger_xiaomi_without_page_failure_or_calibrated_low_confidence(self) -> None:
        self.assertEqual(select_xiaomi_fallback_pages([], []), [])
        self.assertEqual(select_xiaomi_fallback_pages([4], []), [4])
        self.assertEqual(select_xiaomi_fallback_pages([], [8]), [8])
        self.assertEqual(select_xiaomi_fallback_pages([4, 8], [8, 9]), [4, 8, 9])

    def test_v6_model_identity_is_source_pdf_bound(self) -> None:
        import argparse

        args = argparse.Namespace(
            backend="paddleocr",
            dpi=200,
            max_side=3000,
            jpeg_quality=90,
            reading_direction="vertical",
        )
        first = _model_id(args, "a" * 64)
        second = _model_id(args, "b" * 64)
        self.assertIn("PP-OCRv6-medium", first)
        self.assertIn("src" + "a" * 64, first)
        self.assertNotEqual(first, second)

    def test_empty_primary_checkpoint_is_an_explicit_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            pages = work / "pages"
            pages.mkdir()
            (pages / "page_0001.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": "",
                        "ocr_model": "primary",
                        "notes": json.dumps({"hard_failure": True}),
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                _validate_pages(work, [1], "primary", allow_hard_failures=True),
                [1],
            )
            with self.assertRaisesRegex(RuntimeError, "blank_text"):
                _validate_pages(work, [1], "primary")

    def test_only_visually_blank_failed_page_becomes_blank_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "images").mkdir()
            (work / "pages").mkdir()
            Image.new("RGB", (100, 100), "white").save(work / "images" / "page_0001.jpg")
            image = Image.new("RGB", (100, 100), "white")
            ImageDraw.Draw(image).text((10, 10), "TEXT", fill="black")
            image.save(work / "images" / "page_0002.jpg")
            for page in (1, 2):
                (work / "pages" / f"page_{page:04d}.json").write_text(
                    json.dumps(
                        {
                            "pdf_page": page,
                            "text": "",
                            "ocr_model": "primary",
                            "notes": json.dumps({"hard_failure": True}),
                        }
                    ),
                    encoding="utf-8",
                )
            _mark_visually_blank_pages(work, [1, 2])
            blank = json.loads((work / "pages" / "page_0001.json").read_text(encoding="utf-8"))
            text_page = json.loads((work / "pages" / "page_0002.json").read_text(encoding="utf-8"))
            self.assertEqual(blank["text"], "[空白页]")
            self.assertFalse(json.loads(blank["notes"])["hard_failure"])
            self.assertEqual(text_page["text"], "")
            self.assertTrue(json.loads(text_page["notes"])["hard_failure"])

    def test_punctuation_diff_always_queues_review_without_auto_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            work = root / "work"
            (work / "pages").mkdir(parents=True)
            primary_text = "本文。"
            primary_notes = {"source_image_sha256": "image-hash", "score_avg": 0.99}
            (work / "pages" / "page_0001.json").write_text(
                json.dumps(
                    {
                        "pdf_page": 1,
                        "text": primary_text,
                        "ocr_model": "primary",
                        "notes": json.dumps(primary_notes),
                    }
                ),
                encoding="utf-8",
            )
            _write_fallback_report(
                output,
                {"path": "source.pdf", "sha256": "f" * 64},
                "primary",
                "xiaomi",
                [1],
                work,
                {1: {"text": "本文!", "text_sha256": "fallback-hash", "model_id": "xiaomi", "notes": {"status": "passed", "finish_reason": "stop"}}},
                {"disagreement_cer_threshold": 1.0},
            )
            report = read_json_object(output / "audit" / "ocr-fallbacks.json")
            page = report["pages"][0]
            self.assertTrue(page["punctuation_diff"])
            self.assertTrue(page["review_required"])
            self.assertIn("punctuation_difference", page["reasons"])
            checkpoint = json.loads((work / "pages" / "page_0001.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["text"], primary_text)


    def test_truncated_fallback_candidate_always_requires_review_and_preserves_raw_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            (work / "pages").mkdir(parents=True)
            primary = "本文。"
            (work / "pages" / "page_0001.json").write_text(json.dumps({
                "pdf_page": 1, "text": primary, "ocr_model": "primary",
                "notes": json.dumps({"source_image_sha256": "image-hash", "score_avg": .99}),
            }), encoding="utf-8")
            _write_fallback_report(
                root / "out", {"path": "source.pdf", "sha256": "f" * 64},
                "primary", "xiaomi", [1], work,
                {1: {"error": "finish_reason_length", "raw_text": "本文尾部截断", "model_id": "xiaomi",
                     "notes": {"status": "failed", "hard_failure": True,
                               "finish_reason": "length", "failure_reason": "retry_finish_reason_length"}}},
                {"disagreement_cer_threshold": 1.0},
            )
            page = read_json_object(root / "out" / "audit" / "ocr-fallbacks.json")["pages"][0]
            self.assertTrue(page["review_required"])
            self.assertIn("fallback_hard_failure", page["reasons"])
            self.assertEqual(page["fallback_candidate_raw_text"], "本文尾部截断")
            self.assertEqual(page["fallback_candidate_status"], "failed")

    def test_old_fallback_evidence_is_legacy_and_not_reused_as_passed(self) -> None:
        entry = {
            "primary_text_sha256": "primary-hash",
            "image_sha256": "image-hash",
            "fallback_model_id": "xiaomi-v2",
            "fallback_text": "可能被截断的旧候选",
        }
        state = _previous_fallback_evidence_state(
            {"schema_version": 1}, entry,
            primary_text_sha256="primary-hash", image_sha256="image-hash",
            expected_fallback_model_id="xiaomi-v3",
        )
        self.assertEqual(state, "legacy")
        self.assertEqual(
            _previous_fallback_evidence_state(
                {"fallback_evidence_version": 2},
                {**entry, "fallback_model_id": "xiaomi-v3", "fallback_candidate_status": "passed"},
                primary_text_sha256="primary-hash", image_sha256="image-hash",
                expected_fallback_model_id="xiaomi-v3",
            ),
            "current",
        )

    def test_legacy_fallback_evidence_is_preserved_and_queued_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            (work / "pages").mkdir(parents=True)
            (work / "pages" / "page_0001.json").write_text(json.dumps({
                "pdf_page": 1, "text": "本文。", "ocr_model": "primary",
                "notes": json.dumps({"source_image_sha256": "image-hash"}),
            }), encoding="utf-8")
            _write_fallback_report(
                root / "out", {"path": "source.pdf", "sha256": "f" * 64},
                "primary", None, [], work, {}, {"disagreement_cer_threshold": 1.0},
                legacy_candidates={1: {
                    "error": "legacy_fallback_evidence_unverified",
                    "raw_text": "可能被截断的旧候选",
                    "model_id": "xiaomi-v2",
                    "notes": {"status": "legacy_unresolved", "hard_failure": True},
                }},
            )
            report = read_json_object(root / "out" / "audit" / "ocr-fallbacks.json")
            page = report["pages"][0]
            self.assertEqual(report["attempted_pages"], [])
            self.assertEqual(report["review_pages"], [1])
            self.assertEqual(page["fallback_candidate_status"], "legacy_unresolved")
            self.assertEqual(page["fallback_candidate_raw_text"], "可能被截断的旧候选")
            self.assertIn("fallback_hard_failure", page["reasons"])

    def test_structural_anomaly_requires_review_even_when_models_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            work = root / "work"
            (work / "pages").mkdir(parents=True)
            text = "高い確信度でも「閉じ忘れ"
            (work / "pages" / "page_0001.json").write_text(json.dumps({
                "pdf_page": 1, "text": text,
                "notes": json.dumps({"score_avg": .999, "source_image_sha256": "image"}),
            }), encoding="utf-8")
            _write_fallback_report(root / "out", {"path": "source.pdf", "sha256": "hash"},
                                   "primary", "xiaomi", [1], work,
                                   {1: {"text": text}}, {})
            report = read_json_object(root / "out" / "audit" / "ocr-fallbacks.json")
            self.assertEqual(report["review_pages"], [1])
            self.assertIn("unclosed_document_quote", report["pages"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
