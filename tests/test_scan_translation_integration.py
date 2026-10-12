"""Exercise the real paragraph adapter through checkpointed page translation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from book_pipeline import ChatTranslator, PageRecord, PageStore, translate_non_chinese_pages
from publication_verifier import _check_checkpoints


class RecordingClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.prompts = []

    def chat_text(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return next(self.outputs)


class ScanTranslationIntegrationTests(unittest.TestCase):
    def test_unreviewed_draft_cannot_be_formally_released(self):
        record = PageRecord(1, '彼が話した。', language='ja', ocr_model='test')
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_translation([record], directory, RecordingClient(['他说话了。']))
            store = PageStore(directory)
            saved = store.load(1)
            saved.translation_prompt_version += '-unreviewed-draft'
            store.save(saved)
            context = SimpleNamespace(output_dir=directory, source_pdf=None, require_translation=True,
                                      expected_translation_fingerprint=None,
                                      manifest=[{'pdf_page': 1, 'end_pdf_page': 1}])
            check = _check_checkpoints(context)
            self.assertTrue(any(i['code'] == 'checkpoint_translation_stale_or_missing'
                                for i in check['issues']))

    def run_translation(self, records, directory, client):
        translate_non_chinese_pages(
            records, directory, ChatTranslator(client), target_language="简体中文",
            force=False, concurrency=1, translation_provider="test",
            translation_model="test-model",
        )

    def test_neighbor_correction_invalidates_context_dependent_translation_and_release(self):
        records = [PageRecord(1, "前の人物は兄だ。", language="ja", ocr_model="test"),
                   PageRecord(2, "彼が話した。", language="ja", ocr_model="test")]
        client = RecordingClient(["前文人物是哥哥。", "哥哥说话了。", "前文人物是弟弟。", "弟弟说话了。"])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_translation(records, directory, client)
            context = SimpleNamespace(output_dir=directory, source_pdf=None, require_translation=True,
                                      expected_translation_fingerprint=None,
                                      manifest=[{"pdf_page": 1, "end_pdf_page": 2}])
            self.assertEqual(_check_checkpoints(context)["issues"], [])
            previous_hash = records[1].translation_input_sha256
            records[0].text = "前の人物は弟だ。"
            PageStore(directory).save(records[0])
            issues = _check_checkpoints(context)["issues"]
            stale = next(issue for issue in issues if issue["code"] == "checkpoint_translation_stale_or_missing")
            self.assertIn(2, stale["evidence"]["pages"])
            self.run_translation(records, directory, client)
            self.assertEqual(len(client.prompts), 4)
            self.assertEqual(records[1].translated_text, "弟弟说话了。")
            self.assertNotEqual(records[1].translation_input_sha256, previous_hash)
            self.assertEqual(_check_checkpoints(context)["issues"], [])

    def test_geometry_body_enters_prompt_raw_source_retained_and_resume_skips(self):
        lines = [
            {"text": "本文甲全文を保存する。", "box": [500, 130, 530, 1100]},
            {"text": "本文乙全文を保存する。", "box": [450, 130, 480, 1100]},
            {"text": "17", "box": [800, 1300, 820, 1320]},
        ]
        raw = "\n".join(line["text"] for line in lines)
        record = PageRecord(17, raw, language="ja", notes=json.dumps({
            "engine": "paddleocr-local", "reading_direction": "vertical", "lines": lines,
        }))
        client = RecordingClient(["完整保留正文甲。完整保留正文乙。", "完整保留正文甲。完整保留正文乙。17"])
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            self.run_translation([record], directory, client)
            source = client.prompts[0].split("<source>\n", 1)[1].split("\n</source>", 1)[0]
            self.assertNotIn("17", source)
            self.assertIn("本文甲全文を保存する。本文乙全文を保存する。", source)
            self.assertEqual(raw, record.text)
            event = json.loads((directory / "audit/performance/translation-requests.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(event["source_sha256"], hashlib.sha256(raw.encode()).hexdigest())
            self.assertEqual(event["prepared_source_sha256"], hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(event["requests"], 1)
            self.assertEqual(event["status"], "passed_structural_checks")
            self.run_translation([record], directory, client)
            self.assertEqual(len(client.prompts), 1)
            # Same raw OCR, but corrected geometry now puts the numeral in body.
            # It must invalidate the prepared input without changing model identity.
            old_fingerprint = record.translation_fingerprint
            lines[-1]["box"] = [400, 1000, 420, 1020]
            record.notes = json.dumps({"engine": "paddleocr-local", "reading_direction": "vertical", "lines": lines})
            PageStore(directory).save(record)
            self.run_translation([record], directory, client)
            self.assertEqual(len(client.prompts), 2)
            self.assertIn("17", client.prompts[1].split("<source>\n", 1)[1].split("\n</source>", 1)[0])
            self.assertEqual(record.translation_fingerprint, old_fingerprint)
            self.assertEqual(record.text, raw)

    def test_cross_page_quotes_supply_context_without_merging_page_outputs(self):
        records = [PageRecord(17, "彼は「言葉を", language="ja"),
                   PageRecord(18, "続けた」と書いた。", language="ja")]
        client = RecordingClient(["他说「话语", "继续了」并写下。"])
        with tempfile.TemporaryDirectory() as temp:
            self.run_translation(records, Path(temp), client)
            self.assertIn("<context_after>\n続けた」と書いた。", client.prompts[0])
            self.assertIn("<context_before>\n彼は「言葉を", client.prompts[1])
            self.assertEqual(records[0].translated_text, "他说「话语")
            self.assertEqual(records[1].translated_text, "继续了」并写下。")

    def test_unresolved_source_quotes_block_before_any_paid_request(self):
        client = RecordingClient([])
        record = PageRecord(17, "彼は「欠落した引用。", language="ja")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "OCR source needs image review"):
                self.run_translation([record], directory, client)
            self.assertEqual(client.prompts, [])
            self.assertEqual(record.translated_text, "")
            report = json.loads((directory / "audit/ocr-structure-issues.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "blocked_before_translation")
            self.assertIn("17", report["pages"])

    def test_failed_repair_is_bounded_and_recorded_without_checkpoint_translation(self):
        client = RecordingClient(["引号丢失。", "引号仍然丢失。"])
        record = PageRecord(17, "「原文の引用」。", language="ja")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "1 failed page"):
                self.run_translation([record], directory, client)
            summary = json.loads((directory / "audit/performance/translation-run-summary.json").read_text(encoding="utf-8"))
            event = json.loads((directory / "audit/performance/translation-requests.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(len(client.prompts), 2)
            self.assertEqual(summary["requests"], 2)
            self.assertEqual(summary["repairs"], 1)
            self.assertEqual(summary["failed_pages"], [17])
            self.assertTrue(summary["structural_checks_only"])
            self.assertEqual(event["status"], "failed")
            self.assertEqual(len(event["request_seconds"]), 2)
            self.assertEqual(record.translated_text, "")


if __name__ == "__main__":
    unittest.main()
