from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from evidence_contract import main, sha256_file, validate_evidence_contract


class EvidenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "audit" / "evidence"
        self.evidence.mkdir(parents=True)
        self.source = self.root / "book" / "source.pdf"
        self.source.parent.mkdir()
        self.source.write_bytes(b"immutable source bytes")
        self.source_sha = sha256_file(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _write(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _valid_graph(self) -> tuple[Path, Path, Path]:
        anchor = self.evidence / "anchor.json"
        self._write(
            anchor,
            {
                "artifact_kind": "manual_anchor_evidence",
                "source": {
                    "pdf_path": "book/source.pdf",
                    "pdf_sha256": self.source_sha,
                },
            },
        )
        ground_truth = self.evidence / "ground-truth.json"
        self._write(
            ground_truth,
            {
                "schema_version": "1.0-frozen",
                "artifact_kind": "frozen_manual_ground_truth",
                "source_pdf": "book/source.pdf",
                "source_pdf_sha256": self.source_sha,
                "method": {
                    "anchor_evidence": [
                        {"path": "anchor.json", "sha256": self._digest(anchor)}
                    ]
                },
            },
        )
        report = self.evidence.parent / "review.json"
        self._write(
            report,
            {
                "artifact_kind": "current_review",
                "source_pdf_sha256": self.source_sha,
                "ground_truth": {
                    "path": "evidence/ground-truth.json",
                    "sha256": self._digest(ground_truth),
                    "schema_version": "1.0-frozen",
                    "review_status": "frozen",
                },
            },
        )
        return report, ground_truth, anchor

    def test_validates_live_recursive_graph_and_source_identity(self) -> None:
        report_path, _ground_truth, _anchor = self._valid_graph()

        report = validate_evidence_contract(
            [report_path],
            project_root=self.root,
            expected_source=self.source,
        )

        self.assertTrue(report.ok, report.to_dict())
        self.assertEqual(len(report.artifacts_checked), 3)
        self.assertEqual(report.expected_source_sha256, self.source_sha)
        self.assertEqual(report.live_references_checked, 4)

    def test_live_ground_truth_hash_drift_fails_closed(self) -> None:
        report_path, ground_truth, _anchor = self._valid_graph()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["ground_truth"]["sha256"] = "0" * 64
        self._write(report_path, payload)

        report = validate_evidence_contract(
            [report_path], project_root=self.root, expected_source=self.source
        )

        self.assertFalse(report.ok)
        self.assertIn(
            "reference_sha256_mismatch",
            {issue.code for issue in report.issues},
        )
        self.assertTrue(ground_truth.is_file())

    def test_ground_truth_must_bind_to_the_same_source_bytes(self) -> None:
        report_path, ground_truth, _anchor = self._valid_graph()
        payload = json.loads(ground_truth.read_text(encoding="utf-8"))
        payload["source_pdf_sha256"] = "1" * 64
        # Keep the consumer binding correct for the mutated GT bytes so that
        # this test isolates source identity rather than the outer SHA edge.
        self._write(ground_truth, payload)
        report_payload = json.loads(report_path.read_text(encoding="utf-8"))
        report_payload["ground_truth"]["sha256"] = self._digest(ground_truth)
        self._write(report_path, report_payload)

        report = validate_evidence_contract(
            [report_path], project_root=self.root, expected_source=self.source
        )

        codes = {issue.code for issue in report.issues}
        self.assertIn("source_sha256_mismatch", codes)
        self.assertIn("ground_truth_source_sha256_mismatch", codes)

    def test_historical_sha_only_reference_requires_explicit_status(self) -> None:
        historical = self.evidence / "historical.json"
        self._write(
            historical,
            {
                "mode": "historical-dry-run-superseded",
                "ground_truth": {
                    "path": None,
                    "sha256": "2" * 64,
                    "review_status": "historical-superseded",
                },
            },
        )

        accepted = validate_evidence_contract(
            [historical], project_root=self.root
        )
        self.assertTrue(accepted.ok, accepted.to_dict())
        self.assertEqual(accepted.historical_references, 1)

        payload = json.loads(historical.read_text(encoding="utf-8"))
        payload.pop("mode")
        payload["ground_truth"].pop("review_status")
        self._write(historical, payload)
        rejected = validate_evidence_contract([historical], project_root=self.root)
        self.assertIn(
            "reference_path_missing",
            {issue.code for issue in rejected.issues},
        )

    def test_historical_label_never_excuses_a_stale_live_path(self) -> None:
        old_ground_truth = self.evidence / "old-ground-truth.json"
        self._write(old_ground_truth, {"version": "new live bytes"})
        historical = self.evidence / "historical.json"
        self._write(
            historical,
            {
                "artifact_status": "historical superseded evidence",
                "ground_truth": {
                    "path": "old-ground-truth.json",
                    "sha256": "3" * 64,
                    "review_status": "historical-superseded",
                },
            },
        )

        report = validate_evidence_contract([historical], project_root=self.root)

        self.assertIn(
            "reference_sha256_mismatch",
            {issue.code for issue in report.issues},
        )

    def test_historical_missing_live_path_must_be_normalized_to_null(self) -> None:
        historical = self.evidence / "historical.json"
        self._write(
            historical,
            {
                "mode": "historical-superseded",
                "ground_truth": {
                    "path": "/tmp/no-longer-retained-ground-truth.json",
                    "sha256": "4" * 64,
                },
            },
        )

        report = validate_evidence_contract([historical], project_root=self.root)

        self.assertIn(
            "historical_live_path_not_found",
            {issue.code for issue in report.issues},
        )

    def test_recursive_validation_catches_stale_reference_inside_anchor(self) -> None:
        report_path, _ground_truth, anchor = self._valid_graph()
        anchor_payload = json.loads(anchor.read_text(encoding="utf-8"))
        anchor_payload["source"]["ground_truth_path"] = "/tmp/stale-gt.json"
        anchor_payload["source"]["ground_truth_sha256"] = "5" * 64
        self._write(anchor, anchor_payload)
        # Rebind both parent edges after changing the anchor bytes.
        ground_truth = self.evidence / "ground-truth.json"
        gt_payload = json.loads(ground_truth.read_text(encoding="utf-8"))
        gt_payload["method"]["anchor_evidence"][0]["sha256"] = self._digest(anchor)
        self._write(ground_truth, gt_payload)
        review_payload = json.loads(report_path.read_text(encoding="utf-8"))
        review_payload["ground_truth"]["sha256"] = self._digest(ground_truth)
        self._write(report_path, review_payload)

        report = validate_evidence_contract(
            [report_path], project_root=self.root, expected_source=self.source
        )

        stale_issues = [
            issue for issue in report.issues if issue.code == "reference_not_found"
        ]
        self.assertEqual(len(stale_issues), 1, report.to_dict())
        self.assertEqual(Path(stale_issues[0].artifact), anchor)

    def test_conflicting_ground_truth_declarations_are_rejected(self) -> None:
        report_path, _ground_truth, _anchor = self._valid_graph()
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        payload["ground_truth_sha256"] = "6" * 64
        self._write(report_path, payload)

        report = validate_evidence_contract([report_path], project_root=self.root)

        self.assertIn(
            "ground_truth_sha256_conflict",
            {issue.code for issue in report.issues},
        )

    def test_ground_truth_path_without_digest_is_rejected(self) -> None:
        report_path = self.evidence / "review.json"
        self._write(
            report_path,
            {"ground_truth": {"path": "ground-truth.json"}},
        )

        report = validate_evidence_contract([report_path], project_root=self.root)

        self.assertIn(
            "reference_sha256_missing",
            {issue.code for issue in report.issues},
        )

    def test_root_bindings_only_scope_discloses_untraversed_json(self) -> None:
        report_path, ground_truth, _anchor = self._valid_graph()

        report = validate_evidence_contract(
            [report_path],
            project_root=self.root,
            expected_source=self.source,
            recursive=False,
        )

        self.assertTrue(report.ok, report.to_dict())
        self.assertFalse(report.recursive)
        self.assertEqual(report.artifacts_checked, [str(report_path)])
        self.assertEqual(
            report.referenced_json_not_traversed,
            [str(ground_truth)],
        )
        self.assertEqual(report.to_dict()["scope"], "root_bindings_only")

    def test_validation_is_read_only_and_cli_returns_machine_readable_status(self) -> None:
        report_path, ground_truth, anchor = self._valid_graph()
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (report_path, ground_truth, anchor, self.source)
        }
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            exit_code = main(
                [
                    str(report_path),
                    "--project-root",
                    str(self.root),
                    "--source",
                    str(self.source),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout.getvalue())["ok"])
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (report_path, ground_truth, anchor, self.source)
        }
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
