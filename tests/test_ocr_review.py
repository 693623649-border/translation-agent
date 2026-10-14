from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools")]

from ingestion.book_pipeline import PageRecord, load_page_records, save_page_record
from ocr_review import calibrate, confirm_page, initialize
from ingestion.ocr_quality_gate import read_json_object


class OcrReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.output = root / "book"
        self.output.mkdir()
        self.source = root / "source.pdf"
        self.source.write_bytes(b"fixture pdf bytes")
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.model = (
            "paddleocr-local/PP-OCRv6-medium-dpi200-max3000-jpeg90-"
            f"vertical-src{source_hash}-v2"
        )
        (self.output / "chapters.json").write_text(
            json.dumps([{"id": "chapter", "pdf_page": 1, "end_pdf_page": 20}]),
            encoding="utf-8",
        )
        for page in range(1, 21):
            save_page_record(
                self.output,
                PageRecord(
                    pdf_page=page,
                    text="日文原文。" if page != 6 else "",
                    language="ja",
                    ocr_model=self.model,
                    notes=json.dumps(
                        {
                            "score_avg": 0.8,
                            "hard_failure": page == 6,
                            "source_image_sha256": f"image-{page}",
                        }
                    ),
                ),
            )
        (self.output / "audit").mkdir()
        (self.output / "audit" / "ocr-fallbacks.json").write_text(
            json.dumps(
                {
                    "source_pdf_sha256": source_hash,
                    "primary_model_id": self.model,
                    "attempted_pages": [5, 6],
                    "review_pages": [5, 6],
                    "pages": [
                        {
                            "pdf_page": 5,
                            "reasons": ["punctuation_difference"],
                            "fallback_text": "日文原文！",
                        },
                        {
                            "pdf_page": 6,
                            "reasons": ["primary_hard_failure"],
                            "fallback_text": "候補識別文。",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_uses_three_page_minimum_and_adds_anomaly_pages(self) -> None:
        plan = initialize(self.output, self.source)
        self.assertEqual(len(plan["sample_pages"]), 3)
        self.assertEqual(set(plan["fallback_review_pages"]), {5, 6})
        self.assertEqual(
            set(plan["required_pages"]), set(plan["sample_pages"]) | {5, 6}
        )
        self.assertTrue(all(plan["page_bindings"][str(page)]["image_sha256"] for page in plan["required_pages"]))

    def test_image_bound_agent_suggestion_requires_an_extra_human_review(self) -> None:
        record = next(item for item in load_page_records(self.output) if item.pdf_page == 20)
        source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        text_hash = hashlib.sha256(record.text.encode("utf-8")).hexdigest()
        suggestion = {
            "source_pdf_sha256": source_hash,
            "primary_model_id": self.model,
            "pages": [
                {
                    "pdf_page": 20,
                    "image_sha256": "image-20",
                    "ocr_text_sha256": text_hash,
                    "suggestion": "Source image may show an opening quote where OCR reads へ.",
                }
            ],
        }
        (self.output / "audit" / "ocr-review-suggestions.json").write_text(
            json.dumps(suggestion, ensure_ascii=False), encoding="utf-8"
        )

        plan = initialize(self.output, self.source)
        self.assertEqual(len(plan["sample_pages"]), 3)
        self.assertEqual(plan["additional_review_pages"], [20])
        self.assertIn(20, plan["required_pages"])
        self.assertIn("opening quote", plan["page_bindings"]["20"]["agent_suggestion"])

        confirm_page(
            self.output,
            20,
            "fixture-human",
            matches_image=True,
            confirm_page=20,
        )

    def test_only_explicit_human_confirmation_writes_review_and_correction_overlay(self) -> None:
        plan = initialize(self.output, self.source)
        with self.assertRaisesRegex(ValueError, "explicit confirmation required"):
            confirm_page(
                self.output,
                6,
                "reviewer",
                matches_image=False,
                corrected_text="人工核定原文。",
            )
        sample_page = plan["sample_pages"][0]
        if sample_page != 6:
            confirm_page(
                self.output,
                sample_page,
                "reviewer",
                matches_image=True,
                confirm_page=sample_page,
            )
        confirm_page(
            self.output,
            6,
            "reviewer",
            matches_image=False,
            corrected_text="人工核定原文。",
            confirm_page=6,
        )
        page_six = next(record for record in load_page_records(self.output) if record.pdf_page == 6)
        self.assertEqual(page_six.text, "")
        self.assertEqual(page_six.effective_text, "人工核定原文。")
        ledger = read_json_object(self.output / "audit" / "ocr-human-review.json")
        self.assertIn(6, [item["pdf_page"] for item in ledger["human_reviews"]])

    def test_calibration_stays_disabled_until_ten_human_labels(self) -> None:
        plan = initialize(self.output, self.source)
        for page in plan["required_pages"]:
            record = next(item for item in load_page_records(self.output) if item.pdf_page == page)
            if record.text:
                confirm_page(
                    self.output,
                    page,
                    "reviewer",
                    matches_image=True,
                    confirm_page=page,
                )
            else:
                confirm_page(
                    self.output,
                    page,
                    "reviewer",
                    matches_image=False,
                    corrected_text="人工核定原文。",
                    confirm_page=page,
                )
        calibration = calibrate(self.output)
        self.assertEqual(calibration["minimum_pages"], 10)
        self.assertIsNone(calibration["confidence_threshold"])
        self.assertIsNone(calibration["disagreement_cer_threshold"])


if __name__ == "__main__":
    unittest.main()
