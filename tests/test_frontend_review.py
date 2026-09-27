from __future__ import annotations

import copy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from application_service import ApplicationService, REVIEW_ACCEPT_REASON
from frontend_runtime import FrontendSettings
from product_contracts import RunSpec


def _review_report(*, reviewable: bool = True) -> dict:
    issue_id = "issue-" + "1" * 64
    allowed = ["accepted"] if reviewable else []
    resolution = "unresolved" if reviewable else "not_reviewable"
    return {
        "status": "blocked",
        "release_blocked": True,
        "action": "viewed",
        "error": None,
        "reconstruction": {
            "path": "/bounded/task/output/audit/semantic-reconstruction.json",
            "sha256": "2" * 64,
        },
        "review_policy": {"version": "policy-v1", "fingerprint": "3" * 64},
        "issue_set": {"count": 1, "sha256": "4" * 64},
        "decision_log": {"record_count": 0, "sha256": "5" * 64},
        "summary": {
            "issue_count": 1,
            "blocking_issue_count": 1,
            "resolved_issue_count": 0,
        },
        "issues": [
            {
                "issue_id": issue_id,
                "code": (
                    "pdf_visible_superscript_unresolved"
                    if reviewable
                    else "semantic_structure_invalid"
                ),
                "message": "受限的简短问题说明",
                "subject_id": "chapter-1",
                "unit_id": "unit-1",
                "evidence_sha256": "6" * 64,
                "reviewable": reviewable,
                "allowed_decisions": allowed,
                "resolution": resolution,
                "release_blocked": True,
                "effective_decision": None,
            }
        ],
    }


class _ReviewService:
    def __init__(self, *, reviewable: bool = True) -> None:
        self.report = _review_report(reviewable=reviewable)
        self.recorded: list[tuple[str, str, str]] = []
        spec = types.SimpleNamespace(title="测试任务")
        self.job = types.SimpleNamespace(
            id="a" * 32,
            status="succeeded",
            source_path=Path("book.pdf"),
            spec=spec,
        )

    def list_jobs(self, *, limit: int) -> list[object]:
        self.limit = limit
        return [self.job]

    def review_report(self, job_id: str) -> dict:
        self.refreshed_job_id = job_id
        return copy.deepcopy(self.report)

    def review_status(self, job_id: str) -> dict:
        self.requested_job_id = job_id
        return copy.deepcopy(self.report)

    def accept_review_issue_as_text(
        self,
        job_id: str,
        *,
        issue_id: str,
        reviewer: str,
    ) -> dict:
        self.recorded.append((job_id, issue_id, reviewer))
        issue = self.report["issues"][0]
        issue["resolution"] = "accepted"
        issue["release_blocked"] = False
        issue["effective_decision"] = {
            "decision": "accepted",
            "reviewer": reviewer,
            "reason": REVIEW_ACCEPT_REASON,
        }
        self.report["status"] = "passed"
        self.report["release_blocked"] = False
        self.report["summary"]["blocking_issue_count"] = 0
        self.report["summary"]["resolved_issue_count"] = 1
        response = copy.deepcopy(self.report)
        response["action"] = "decision-recorded"
        return response


class FrontendReviewServiceTests(unittest.TestCase):
    def _service_with_job(
        self,
        root: Path,
        *,
        output: Path | None = None,
    ) -> tuple[ApplicationService, str, Path]:
        settings = FrontendSettings(
            database=root / "jobs.sqlite3",
            jobs_root=root / "jobs",
            source_roots=(root,),
        )
        service = ApplicationService(settings)
        job_id = "a" * 32
        workspace = settings.jobs_root / job_id
        source = workspace / "input" / "book.pdf"
        expected_output = workspace / "output"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"pdf")
        expected_output.mkdir()
        spec = RunSpec(
            source=source,
            source_mode="text-pdf",
            output_dir=output or expected_output,
            translate=False,
            verify=False,
        )
        service.registry.create(job_id, workspace, spec)
        return service, job_id, expected_output.resolve()

    def test_facade_delegates_view_status_and_fixed_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, job_id, output = self._service_with_job(Path(directory))
            viewed = {"status": "blocked"}
            status = {"status": "passed"}
            accepted = {"action": "decision-recorded"}
            with (
                patch("document_pipeline.review_report", side_effect=[viewed, accepted]) as report,
                patch("document_pipeline.review_status", return_value=status) as review_status,
            ):
                self.assertIs(service.review_report(job_id), viewed)
                self.assertIs(service.review_status(job_id), status)
                self.assertIs(
                    service.accept_review_issue_as_text(
                        job_id,
                        issue_id="issue-" + "1" * 64,
                        reviewer="editor@example.test",
                    ),
                    accepted,
                )

        report.assert_any_call(output)
        review_status.assert_called_once_with(output, include_issues=True)
        report.assert_called_with(
            output,
            issue_id="issue-" + "1" * 64,
            reviewer="editor@example.test",
            decision="accepted",
            reason="accept_as_text",
        )

    def test_facade_rejects_review_output_outside_uuid_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            service, job_id, _output = self._service_with_job(root, output=outside)
            with (
                patch("document_pipeline.review_report") as report,
                self.assertRaisesRegex(ValueError, "UUID workspace"),
            ):
                service.review_report(job_id)
        report.assert_not_called()


class FrontendReviewPageTests(unittest.TestCase):
    def setUp(self) -> None:
        main_module = sys.modules["__main__"]
        self._main_metadata = {
            name: (name in main_module.__dict__, getattr(main_module, name, None))
            for name in ("__file__", "__spec__", "__package__", "__loader__", "__cached__")
        }

    def tearDown(self) -> None:
        main_module = sys.modules["__main__"]
        for name, (exists, value) in self._main_metadata.items():
            if not exists:
                main_module.__dict__.pop(name, None)
            else:
                setattr(main_module, name, value)

    def _run_page(self, service: _ReviewService) -> AppTest:
        root = Path(__file__).resolve().parents[1]
        with patch("app_pages._shared.application_service", return_value=service):
            return AppTest.from_file(str(root / "app_pages" / "review.py")).run(
                timeout=20
            )

    def test_reviewable_issue_shows_only_policy_allowed_acceptance(self) -> None:
        service = _ReviewService(reviewable=True)
        app = self._run_page(service)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.header[0].value, "人工复核")
        self.assertIn(
            "确认接受当前文本",
            [button.label for button in app.button],
        )
        self.assertIn(REVIEW_ACCEPT_REASON, [item.value for item in app.text_input])
        self.assertNotIn("replaced", [button.label for button in app.button])
        self.assertEqual(service.requested_job_id, service.job.id)
        self.assertFalse(hasattr(service, "refreshed_job_id"))

    def test_nonreviewable_issue_has_no_decision_control(self) -> None:
        service = _ReviewService(reviewable=False)
        app = self._run_page(service)

        self.assertEqual(len(app.exception), 0)
        self.assertNotIn(
            "确认接受当前文本",
            [button.label for button in app.button],
        )
        self.assertTrue(any("不允许豁免" in warning.value for warning in app.warning))
        self.assertEqual(service.recorded, [])

    def test_derived_audit_refresh_is_an_explicit_action(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = _ReviewService(reviewable=True)
        with patch("app_pages._shared.application_service", return_value=service):
            app = AppTest.from_file(str(root / "app_pages" / "review.py")).run(
                timeout=20
            )
            refresh_button = next(
                index
                for index, button in enumerate(app.button)
                if button.label == "初始化/刷新派生复核审计"
            )
            app.button[refresh_button].click().run(timeout=20)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(service.refreshed_job_id, service.job.id)
        self.assertTrue(
            any("派生复核审计" in success.value for success in app.success)
        )

    def test_acceptance_requires_confirmation_and_records_bounded_fields(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = _ReviewService(reviewable=True)
        with patch("app_pages._shared.application_service", return_value=service):
            app = AppTest.from_file(str(root / "app_pages" / "review.py")).run(
                timeout=20
            )
            app.text_input[0].input(" editor@example.test ")
            accept_button = next(
                index
                for index, button in enumerate(app.button)
                if button.label == "确认接受当前文本"
            )
            app.button[accept_button].click().run(timeout=20)
            self.assertEqual(service.recorded, [])
            self.assertTrue(
                any("勾选确认" in error.value for error in app.error)
            )

            app.checkbox[0].check()
            accept_button = next(
                index
                for index, button in enumerate(app.button)
                if button.label == "确认接受当前文本"
            )
            app.button[accept_button].click().run(timeout=20)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            service.recorded,
            [
                (
                    service.job.id,
                    "issue-" + "1" * 64,
                    "editor@example.test",
                )
            ],
        )
        self.assertTrue(
            any(REVIEW_ACCEPT_REASON in success.value for success in app.success)
        )


if __name__ == "__main__":
    unittest.main()
