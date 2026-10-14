"""Unit tests for the local Xiaomi-OCR-0 pipeline backend.

The backend shells out to ``tools/local_paddleocr_import.py --backend
xiaomi-ocr`` (vLLM Docker), so these tests cover the deterministic pieces —
checkpoint identity, pending page selection, backend resolution with the
auto chain, and argument plumbing — without Docker.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from ingestion.book_pipeline import (  # noqa: E402
    PageRecord,
    build_parser as _book_parser,
    resolve_ocr_backend_name,
    local_ocr_pending_pages,
    xiaomi_local_model_id,
    resolve_expected_ocr_model_exact,
    resolve_expected_ocr_model_prefix,
)
from local_paddleocr_import import _parse_args  # noqa: E402
from orchestration.pipeline_profiles import ModelProfile  # noqa: E402
_xiaomi_module_path = Path(__file__).resolve().parents[1] / "deploy" / "xiaomi-ocr" / "batch_ocr.py"
_xiaomi_spec = importlib.util.spec_from_file_location("xiaomi_ocr_batch", _xiaomi_module_path)
assert _xiaomi_spec and _xiaomi_spec.loader
_xiaomi_module = importlib.util.module_from_spec(_xiaomi_spec)
_xiaomi_spec.loader.exec_module(_xiaomi_module)
select_ocr_candidate = _xiaomi_module.select_ocr_candidate


def _record(page: int, model: str) -> PageRecord:
    return PageRecord(
        pdf_page=page,
        text="正文",
        ocr_model=model,
    )


class XiaomiOcrLocalBackendTests(unittest.TestCase):
    def test_model_id_is_stable_and_variant_sensitive(self) -> None:
        common = dict(
            task="text",
            dpi=200,
            max_image_side=3000,
        )
        first = xiaomi_local_model_id(**common)
        second = xiaomi_local_model_id(**common)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("xiaomi-ocr-local/Xiaomi-OCR-0-text-"))
        document = xiaomi_local_model_id(**{**common, "task": "document"})
        self.assertNotEqual(first, document)
        self.assertIn("-document-", document)
        vertical = xiaomi_local_model_id(**{**common, "reading_direction": "vertical"})
        self.assertNotEqual(first, vertical)
        self.assertTrue(vertical.endswith("-vertical-v3"))
        self.assertEqual(
            xiaomi_local_model_id(**{**common, "dpi": 300}),
            first.replace("dpi200", "dpi300"),
        )
        with self.assertRaises(ValueError):
            xiaomi_local_model_id(**{**common, "task": "kie"})

    def test_pending_pages_respect_cache_identity_and_force(self) -> None:
        model = xiaomi_local_model_id(
            task="text", dpi=200, max_image_side=3000
        )
        records = [
            _record(1, model),
            _record(2, "paddleocr-local/PP-OCRv5-server-det-paddle_fp32"),
        ]
        requested = {1, 2, 3, 4}
        self.assertEqual(
            local_ocr_pending_pages(records, requested=requested, model_id=model),
            [2, 3, 4],
        )
        self.assertEqual(
            local_ocr_pending_pages(
                records, requested=requested, model_id=model, force=True
            ),
            [1, 2, 3, 4],
        )

    def test_xiaomi_identity_is_not_a_primary_backend(self) -> None:
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
        # Paddle remains the only primary even when Xiaomi is available.
        with patch("ingestion.book_pipeline.paddle_local_available", return_value=True):
            backend, reason = resolve_ocr_backend_name(args, remote_profile)
            self.assertEqual(backend, "paddleocr-local")
        # Xiaomi never replaces a missing Paddle primary.
        with patch("ingestion.book_pipeline.paddle_local_available", return_value=False):
            with self.assertRaisesRegex(ValueError, "Xiaomi cannot replace"):
                resolve_ocr_backend_name(args, remote_profile, strict=True)
            backend, reason = resolve_ocr_backend_name(args, remote_profile)
            self.assertEqual(backend, "paddleocr-local")
            self.assertIn("primary OCR cache identity", reason)
        # Direct Xiaomi use is forbidden; the shared Docker importer invokes
        # it only for one-time page-level fallback.
        explicit = _book_parser().parse_args(["--ocr-backend", "xiaomi-ocr-local"])
        with self.assertRaisesRegex(ValueError, "only run once as the scanned-page fallback"):
            resolve_ocr_backend_name(explicit, remote_profile)

    def test_empty_or_truncated_xiaomi_candidates_fail_closed(self) -> None:
        text, notes = select_ocr_candidate({"text": "", "finish_reason": "stop"}, None)
        self.assertEqual(text, "")
        self.assertEqual(notes["status"], "failed")
        self.assertTrue(notes["hard_failure"])
        text, notes = select_ocr_candidate(
            {"text": "正文被截断", "finish_reason": "length"},
            {"text": "重试仍被截断", "finish_reason": "length"},
        )
        self.assertEqual(text, "重试仍被截断")
        self.assertEqual(notes["status"], "failed")
        self.assertTrue(notes["hard_failure"])
        self.assertIn("retry_finish_reason", notes)

    def test_clean_retry_is_adopted_and_untruncated(self) -> None:
        text, notes = select_ocr_candidate(
            {"text": "正文不完整", "finish_reason": "length"},
            {"text": "完整正文。", "finish_reason": "stop"},
        )
        self.assertEqual(text, "完整正文。")
        self.assertEqual(notes["status"], "passed")
        self.assertTrue(notes["retry_adopted"])

    def test_import_tool_backend_arguments(self) -> None:
        args = _parse_args(["sample.pdf", "--output-dir", "."])
        self.assertEqual(args.xiaomi_task, "text")
        self.assertEqual(args.xiaomi_max_tokens, 4096)
        self.assertIsNone(args.xiaomi_gpu_mem)
        with self.assertRaises(SystemExit):
            _parse_args(["sample.pdf", "--output-dir", ".", "--backend", "xiaomi-ocr"])


if __name__ == "__main__":
    unittest.main()
