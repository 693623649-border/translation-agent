import json
import tempfile
import unittest
from pathlib import Path

from ingestion.ingestion_quality import write_ingestion_quality_report


class IngestionQualityReportTests(unittest.TestCase):
    def test_unrelated_metadata_is_not_a_passed_check_and_nested_source_is_readable(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            audit = workspace/'audit/metadata.json'
            audit.parent.mkdir()
            audit.write_text('{"provider":"paddleocr"}', encoding='utf-8')
            report = write_ingestion_quality_report(workspace)
            self.assertIn('状态：**unverified**',report.read_text(encoding='utf-8'))
            report = write_ingestion_quality_report(workspace, {'status':'blocked','source':{'path':'book.epub','sha256':'abc'}})
            text=report.read_text(encoding='utf-8')
            self.assertIn('来源：`book.epub`',text)
            self.assertIn('来源 SHA256：`abc`',text)

    def test_human_counts_and_nested_issue_page_scope_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            result={'status':'failed','checks':[{'id':'ocr.human_review','status':'failed','summary':'待核图',
                'metrics':{'confirmed_review_count':1,'required_review_pages':[2,3],'sample_page_count':2}}],
                'errors':[{'code':'missing_review','message':'未核验','evidence':{'pages':[3]}}]}
            text=write_ingestion_quality_report(Path(root),result).read_text(encoding='utf-8')
            self.assertIn('当前有效人类确认：1 页',text)
            self.assertIn('页：[3]',text)

    def test_passed_report_reuses_audit_and_records_release(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            audit = workspace / "audit" / "semantic-reconstruction.json"
            audit.parent.mkdir()
            audit.write_text(json.dumps({"status": "passed", "summary": {"issue_count": 0}}), encoding="utf-8")
            path = write_ingestion_quality_report(
                workspace,
                {"status": "passed", "release_ready": True, "source": "book.epub", "source_sha256": "abc"},
                evidence=(str(audit),),
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("正式发布：**允许**", text)
            self.assertIn("semantic-reconstruction.json", text)

    def test_blocked_report_cannot_claim_release(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_ingestion_quality_report(
                Path(root), {"status": "blocked", "release_ready": True}, status="blocked"
            )
            self.assertIn("正式发布：**禁止**", path.read_text(encoding="utf-8"))

    def test_existing_audit_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            audit = workspace / "audit" / "release-report.json"
            audit.parent.mkdir()
            audit.write_text(json.dumps({"status": "failed", "release_blocked": True, "summary": {"issue_count": 2}}), encoding="utf-8")
            path = write_ingestion_quality_report(workspace, {}, evidence=(str(audit),))
            text = path.read_text(encoding="utf-8")
            self.assertIn("状态：**blocked**", text)
            self.assertIn("问题数 `2`", text)

    def test_incremental_report_has_separate_name(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            whole = write_ingestion_quality_report(workspace, {"status": "passed"})
            incremental = write_ingestion_quality_report(
                workspace, {"status": "passed", "mode": "chapters"}
            )
            self.assertEqual(whole.name, "quality-report.md")
            self.assertEqual(incremental.name, "quality-report-chapters.md")
            self.assertTrue(whole.is_file())

    def test_current_result_is_authoritative_over_old_failed_audit(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Path(root)
            audit = workspace / "audit" / "old-release.json"
            audit.parent.mkdir()
            audit.write_text(json.dumps({"status": "failed", "release_blocked": True}), encoding="utf-8")
            path = write_ingestion_quality_report(
                workspace,
                {"status": "passed", "release_ready": True},
                evidence=(),
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("状态：**passed**", text)

    def test_missing_evidence_is_unverified(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_ingestion_quality_report(Path(root), {})
            text = path.read_text(encoding="utf-8")
            self.assertIn("状态：**unverified**", text)
            self.assertIn("正式发布：**禁止**", text)


if __name__ == "__main__":
    unittest.main()
