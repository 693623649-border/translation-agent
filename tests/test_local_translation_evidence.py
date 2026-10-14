"""Rejected raw replies must remain inspectable without credentials or approval."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from ingestion.book_pipeline import LocalClient

SPEC = importlib.util.spec_from_file_location(
    "local_smoke", Path(__file__).resolve().parents[1] / "deploy/local-llm/smoke_translate.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class EvidenceTests(unittest.TestCase):
    SAMPLE = {"id": "test", "source_language": "ja",
              "source": "『怪しい隣人』は彼の作品である。"}

    def test_rejected_attempt_and_exact_raw_whitespace_survive(self):
        class Replies(LocalClient):
            def __init__(self):
                super().__init__(api_key="must-not-be-recorded")
                self.replies = iter(["\n『怪しい隣人』是他的作品。\n", "\n『可疑的邻居』是他的作品。\n"])

            def _post(self, endpoint, payload):
                return {"choices": [{"message": {"content": next(self.replies)}}]}

        client = Replies()
        original = client._post
        records = smoke.collect_samples(client, [self.SAMPLE])
        self.assertEqual(client._post, original)
        record = records[0]
        self.assertEqual(record["human_acceptance"], "pending")
        self.assertIsNone(record["pipeline_error"])
        self.assertEqual(record["pipeline_output"], "『可疑的邻居』是他的作品。")
        self.assertEqual(len(record["calls"]), 2)
        self.assertEqual(record["calls"][0]["raw_translation"], "\n『怪しい隣人』是他的作品。\n")
        self.assertFalse(record["calls"][0]["local_gate"]["passed"])
        self.assertTrue(record["calls"][1]["local_gate"]["passed"])
        self.assertNotIn("must-not-be-recorded", json.dumps(records))
        report = {"generated_at": "test", "identity": {"model": "test", "prompt_version": "test"},
                  "human_acceptance": "pending", "samples": records}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            smoke.write_evidence(path, report)
            self.assertEqual((path / "test/raw-1.txt").read_text(encoding="utf-8"),
                             record["calls"][0]["raw_translation"])
            self.assertIn("待用户判断", (path / "README.md").read_text(encoding="utf-8"))
            self.assertEqual(json.loads((path / "review.json").read_text(encoding="utf-8")), report)

    def test_service_failure_is_recorded_without_fake_translation(self):
        class Offline(LocalClient):
            def _post(self, endpoint, payload):
                raise OSError("endpoint unavailable")

        record = smoke.collect_samples(Offline(), [self.SAMPLE])[0]
        self.assertEqual(record["calls"], [])
        self.assertIsNone(record["pipeline_output"])
        self.assertIn("endpoint unavailable", record["pipeline_error"])
        self.assertEqual(record["human_acceptance"], "pending")

    def test_report_distinguishes_strict_and_local_checks(self):
        source = "「彼は来ない」、彼は言った。"
        reply = "「他不来了」，他说。"
        self.assertFalse(smoke.check_reply(source, reply, preserve_punctuation=True)["passed"])
        self.assertTrue(smoke.check_reply(source, reply, preserve_punctuation=False)["passed"])
