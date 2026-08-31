"""Unit tests for the local PaddleOCR pipeline backend.

The backend shells out to ``tools/local_paddleocr_import.py`` (Docker GPU),
so these tests cover the deterministic pieces — checkpoint identity, pending
page selection, segment batching, and argument plumbing — without Docker.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from book_pipeline import (  # noqa: E402
    PageRecord,
    build_parser as _book_parser,
    resolve_ocr_backend_name,
    save_page_record,
    contiguous_page_segments,
    paddle_local_model_id,
    paddle_local_pending_pages,
    resolve_expected_ocr_model_exact,
    resolve_expected_ocr_model_prefix,
)
from local_paddleocr_import import _parse_args  # noqa: E402
from pipeline_profiles import ModelProfile  # noqa: E402


def _record(page: int, model: str) -> PageRecord:
    return PageRecord(
        pdf_page=page,
        text="正文",
        ocr_model=model,
    )


class PaddleOcrLocalBackendTests(unittest.TestCase):
    def test_model_id_is_stable_and_variant_sensitive(self) -> None:
        common = dict(
            det_variant="server",
            rec_variant="server",
            det_mode="paddle_fp32",
            rec_mode="paddle_fp16",
            rec_batch=16,
            det_len=736,
            dpi=200,
            max_image_side=3000,
        )
        first = paddle_local_model_id(**common)
        second = paddle_local_model_id(**common)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("paddleocr-local/PP-OCRv5-server-det-"))
        mobile = paddle_local_model_id(**{**common, "det_variant": "mobile"})
        self.assertNotEqual(first, mobile)
        self.assertEqual(
            paddle_local_model_id(**{**common, "dpi": 300}),
            first.replace("dpi200", "dpi300"),
        )

    def test_pending_pages_respect_cache_identity_and_force(self) -> None:
        model = paddle_local_model_id(
            det_variant="server",
            rec_variant="server",
            det_mode="paddle_fp32",
            rec_mode="paddle_fp16",
            rec_batch=16,
            det_len=736,
            dpi=200,
            max_image_side=3000,
        )
        records = [
            _record(1, model),
            _record(2, "coding-plan/glm-4.6v-vision-mcp/horizontal-v2"),
        ]
        requested = {1, 2, 3, 4}
        self.assertEqual(
            paddle_local_pending_pages(records, requested=requested, model_id=model),
            [2, 3, 4],
        )
        self.assertEqual(
            paddle_local_pending_pages(
                records, requested=requested, model_id=model, force=True
            ),
            [1, 2, 3, 4],
        )

    def test_contiguous_page_segments(self) -> None:
        self.assertEqual(contiguous_page_segments([1, 2, 3]), [[1, 2, 3]])
        self.assertEqual(
            contiguous_page_segments([1, 2, 5, 6, 9]), [[1, 2], [5, 6], [9]]
        )
        self.assertEqual(contiguous_page_segments([3, 1, 2]), [[1, 2, 3]])

    def test_profile_resolvers_emit_paddle_identity(self) -> None:
        profile = ModelProfile(
            name="paddleocr_local",
            adapter="paddleocr-local",
            provider="paddleocr",
            base_url="",
            model="PP-OCRv5-server",
            credential_env="",
            timeout=600,
            concurrency=1,
            thinking="disabled",
        )
        args = _book_parser().parse_args([])

        self.assertEqual(
            resolve_expected_ocr_model_prefix(args, profile), "paddleocr-local/"
        )
        exact = resolve_expected_ocr_model_exact(args, profile)
        self.assertTrue(exact.startswith("paddleocr-local/PP-OCRv5-server-det-"))

    def test_auto_backend_prefers_local_then_falls_back(self) -> None:
        args = _book_parser().parse_args([])
        self.assertEqual(args.ocr_backend, "auto")
        remote_profile = ModelProfile(
            name="glm_vision",
            adapter="coding-plan-mcp",
            provider="zhipu",
            base_url="https://example.invalid",
            model="glm-4.6v",
            credential_env="GLM_CODING_API_KEY",
            timeout=180,
            concurrency=4,
            thinking="omit",
        )
        with patch("book_pipeline.paddle_local_available", return_value=True):
            backend, reason = resolve_ocr_backend_name(args, remote_profile)
            self.assertEqual(backend, "paddleocr-local")
            self.assertIn("local", reason)
        with patch("book_pipeline.paddle_local_available", return_value=False):
            backend, reason = resolve_ocr_backend_name(args, remote_profile)
            self.assertEqual(backend, "coding-plan-mcp")
            self.assertIn("unavailable", reason)
        explicit = _book_parser().parse_args(["--ocr-backend", "coding-plan-mcp"])
        with patch("book_pipeline.paddle_local_available", return_value=True):
            backend, reason = resolve_ocr_backend_name(explicit, remote_profile)
            self.assertEqual(backend, "coding-plan-mcp")
            self.assertEqual(reason, "explicit --ocr-backend")

    def test_cloud_rerun_protects_local_paddle_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "book"
            record = PageRecord(
                pdf_page=1,
                text="本地识别正文",
                ocr_model="paddleocr-local/PP-OCRv5-server-det-paddle_fp32-server-rec-paddle_fp16-b16-det736-dpi200-max3000-v1",
            )
            save_page_record(output, record)
            # A cloud coding-plan rerun without --force must keep the local
            # page untouched (it must not be scheduled as pending).
            from book_pipeline import ocr_pdf

            class _StubBackend:
                def ocr_image(self, path):
                    raise AssertionError("local checkpoint must not be re-OCR'd")

                def close(self):
                    pass

            result = ocr_pdf(
                Path(directory) / "nonexistent.pdf",
                output,
                _StubBackend(),
                start_page=1,
                end_page=1,
                concurrency=1,
                dpi=200,
                max_image_side=3000,
                jpeg_quality=90,
                keep_page_images=False,
                force=False,
                cache_model_prefix="coding-plan/",
            )
            self.assertEqual(result[0].ocr_model, record.ocr_model)

    def test_tool_range_args_defaults_and_validation(self) -> None:
        args = _parse_args(["sample.pdf", "--output-dir", "."])
        self.assertEqual(args.start_page, 1)
        self.assertIsNone(args.end_page)
        ranged = _parse_args(
            [
                "sample.pdf",
                "--output-dir",
                ".",
                "--start-page",
                "198",
                "--end-page",
                "198",
            ]
        )
        self.assertEqual((ranged.start_page, ranged.end_page), (198, 198))


if __name__ == "__main__":
    unittest.main()
