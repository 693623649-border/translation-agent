from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestration.document_pipeline as document_pipeline
from orchestration.product_contracts import RunSpec


class DocumentPipelineTests(unittest.TestCase):
    def test_pdf_run_quality_report_preserves_verifier_failure_details(self) -> None:
        class FakeRun:
            run_id = "run-test"
            plan = ("core.publication.verify",)
            executed = ("core.publication.verify",)
            skipped = ()
            state_path = Path("state.json")
            events_path = Path("events.jsonl")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            audit = output / "audit"
            audit.mkdir()
            source_pdf = output / "book.pdf"
            source_pdf.write_bytes(b"book-source")
            source_hash = __import__("hashlib").sha256(b"book-source").hexdigest()
            (audit / "release-report.json").write_text(json.dumps({
                "status": "failed", "release_ready": False, "mode": "full",
                "source": {"path": str(source_pdf), "sha256": source_hash},
                "summary": {"failed": 1},
                "checks": [{"id": "ocr.human_review", "status": "failed",
                            "summary": "needs image review",
                            "metrics": {"confirmed_review_count": 0}}],
                "issues": [{"code": "ocr_human_review_missing", "message": "missing human review"}],
            }), encoding="utf-8")
            spec = RunSpec(source=source_pdf, source_mode="scanned-pdf", output_dir=output)
            with patch("frontend.translation_agent_api.run_graph", return_value=FakeRun()):
                result = document_pipeline.run_pdf_spec(spec)
            report = Path(result["quality_report"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "passed")  # graph execution completed
        self.assertEqual(result["quality_status"], "failed")
        self.assertFalse(result["release_ready"])
        self.assertIn("状态：**failed**", report)
        self.assertIn("ocr.human_review", report)
        self.assertIn("ocr_human_review_missing", report)
        self.assertIn("confirmed_review_count=0", report)

    def test_partial_pdf_run_ignores_old_release_report(self) -> None:
        class FakeRun:
            run_id = "run-partial"
            plan = ("core.pages.ocr",)
            executed = ("core.pages.ocr",)
            skipped = ()
            state_path = Path("state.json")
            events_path = Path("events.jsonl")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            source_pdf = output / "book.pdf"
            source_pdf.write_bytes(b"new-source")
            audit = output / "audit"
            audit.mkdir()
            (audit / "release-report.json").write_text(json.dumps({
                "status": "passed", "release_ready": True, "mode": "full",
                "checks": [{"id": "old.check", "status": "passed"}],
            }), encoding="utf-8")
            spec = RunSpec(source=source_pdf, source_mode="scanned-pdf",
                           output_dir=output, phase="ocr", verify=False)
            with patch("frontend.translation_agent_api.run_graph", return_value=FakeRun()):
                result = document_pipeline.run_pdf_spec(spec)
            report = Path(result["quality_report"]).read_text(encoding="utf-8")

        self.assertFalse(result["release_ready"])
        self.assertEqual(result["quality_status"], "unverified")
        self.assertNotIn("old.check", report)
        self.assertIn("verification not current", report)

    def test_epub_plan_exposes_semantic_quality_gate(self) -> None:
        plan = document_pipeline.plan_spec(
            RunSpec(
                source="book.epub",
                source_mode="epub",
                output_dir="outputs/book",
                targets=("publication.epub",),
            )
        )

        self.assertEqual(plan["schema_version"], 1)
        self.assertIn("core.semantic.verify", plan["nodes"])
        self.assertLess(
            plan["nodes"].index("core.semantic.verify"),
            plan["nodes"].index("core.semantic.apply"),
        )
        self.assertEqual(
            plan["release_profile"],
            "draft-until-epub-native-verifier",
        )

    def test_cli_accepts_versioned_spec_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spec_path = Path(directory) / "run.json"
            spec_path.write_text(
                json.dumps(
                    RunSpec(
                        source="book.epub",
                        source_mode="epub",
                        output_dir=Path(directory) / "output",
                        translate=False,
                    ).to_dict()
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = document_pipeline.main(["plan", "--spec", str(spec_path)])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["source_mode"], "epub")

    def test_review_is_blocked_by_any_failed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            audit = output / "audit"
            audit.mkdir()
            (audit / "semantic-reconstruction.json").write_text(
                json.dumps({"status": "blocked", "release_blocked": True}),
                encoding="utf-8",
            )

            report = document_pipeline.review_status(output)

        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["release_blocked"])

    def test_publish_plan_is_explicitly_draft_without_release_report(self) -> None:
        class FakeRun:
            run_id = "run-test"
            executed = ("core.publish.epub",)
            skipped = ()

        from unittest.mock import patch

        with patch("frontend.translation_agent_api.run_graph", return_value=FakeRun()):
            report = document_pipeline.publish_spec(
                RunSpec(
                    source_mode="epub",
                    output_dir="outputs/book",
                    translate=False,
                ),
                ("epub",),
            )

        self.assertFalse(report["release_ready"])
        self.assertEqual(report["publication_status"], "draft")

    def test_doctor_flags_are_delegated_without_double_dash(self) -> None:
        with patch("orchestration.doctor.main", return_value=0) as doctor_main:
            code = document_pipeline.main(["doctor", "--json", "--render"])

        self.assertEqual(code, 0)
        doctor_main.assert_called_once_with(["--json", "--render"])

    def test_epub_run_requires_explicit_draft_authorization(self) -> None:
        with patch("orchestration.document_pipeline.ingest_spec") as ingest:
            with self.assertRaisesRegex(ValueError, "--no-verify"):
                document_pipeline.main(
                    [
                        "run",
                        "book.epub",
                        "--source-mode",
                        "epub",
                        "--no-translate",
                    ]
                )

        ingest.assert_not_called()

    def test_apply_preserves_language_and_glossary_contract(self) -> None:
        spec = RunSpec(
            source_mode="epub",
            output_dir="outputs/book",
            target_language="中文",
            options={"glossary": "terms.json"},
            verify=False,
        )
        with patch(
            "translation.semantic_translation_runner.load_glossary",
            return_value={"state": "国家"},
        ) as load_glossary, patch(
            "ingestion.epub_semantic_import.apply_translations",
            return_value={"status": "passed"},
        ) as apply_translations:
            report = document_pipeline.apply_spec(
                spec,
                Path("translated.jsonl"),
            )

        load_glossary.assert_called_once_with(Path("terms.json"))
        apply_translations.assert_called_once_with(
            Path("outputs/book").resolve(),
            Path("translated.jsonl"),
            target_language="中文",
            glossary={"state": "国家"},
        )
        self.assertEqual(report["status"], "passed")

    def test_console_boundary_reports_expected_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = document_pipeline.cli(
                [
                    "run",
                    "book.epub",
                    "--source-mode",
                    "epub",
                    "--no-translate",
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn("[translation-agent-error] ValueError", stderr.getvalue())
        self.assertIn("--no-verify", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_epub_run_stops_when_semantic_ingest_is_blocked(self) -> None:
        with patch(
            "orchestration.document_pipeline.ingest_spec",
            return_value={"status": "blocked", "release_blocked": True},
        ), patch("orchestration.document_pipeline.publish_spec") as publish:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "semantic ingest is blocked"):
                    document_pipeline.main(
                        [
                            "run",
                            "book.epub",
                            "--source-mode",
                            "epub",
                            "--no-translate",
                            "--no-verify",
                        ]
                    )

        publish.assert_not_called()

    def test_standalone_ingest_returns_nonzero_for_blocked_result(self) -> None:
        with patch(
            "orchestration.document_pipeline.ingest_spec",
            return_value={"status": "blocked", "release_blocked": True},
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = document_pipeline.main(
                    [
                        "ingest",
                        "book.pdf",
                        "--source-mode",
                        "text-pdf",
                    ]
                )

        self.assertEqual(code, 1)
        self.assertTrue(json.loads(stdout.getvalue())["release_blocked"])


if __name__ == "__main__":
    unittest.main()
