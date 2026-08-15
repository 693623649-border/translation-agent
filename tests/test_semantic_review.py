from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from semantic_ir import ReviewDecision, SemanticContractError, sha256_text
from semantic_review import (
    ReviewIssue,
    SemanticReviewError,
    append_semantic_review_decision,
    canonical_review_evidence_sha256,
    generate_semantic_review_audit,
    initialize_review_decision_log,
    read_review_decision_log,
    resolve_review_decisions,
    review_issue_set_sha256,
    review_policy_fingerprint,
    stable_review_issue_id,
)


class SemanticReviewTests(unittest.TestCase):
    reconstruction = "a" * 64
    source = sha256_text("source unit")
    subject = "chapter-1-u0001"

    def issue(
        self,
        *,
        reconstruction: str | None = None,
        reviewable: bool = True,
        allowed: tuple[str, ...] = ("accepted",),
        code: str = "pdf_visible_superscript_unresolved",
    ) -> ReviewIssue:
        return ReviewIssue.create(
            reconstruction_sha256=reconstruction or self.reconstruction,
            code=code,
            subject_id=self.subject,
            unit_id=self.subject,
            source_sha256=self.source,
            message="Visible superscript needs review.",
            evidence_sha256=canonical_review_evidence_sha256({"page": 7}),
            reviewable=reviewable,
            allowed_decisions=allowed if reviewable else (),
        )

    def decision(
        self,
        issue: ReviewIssue,
        *,
        decision: str = "accepted",
        reconstruction: str | None = None,
        timestamp: str = "2026-08-15T10:00:00Z",
        source_sha256: str | None = None,
        replacement: str | None = None,
    ) -> ReviewDecision:
        return ReviewDecision(
            schema_version=1,
            reconstruction_sha256=reconstruction or issue.reconstruction_sha256,
            issue_id=issue.issue_id,
            subject_id=issue.subject_id,
            unit_id=issue.unit_id,
            source_sha256=source_sha256 or issue.source_sha256,
            reviewer="reviewer@example.test",
            decision=decision,
            timestamp=timestamp,
            reason="accept_as_text" if decision == "accepted" else "corrected source",
            replacement_markdown=replacement,
        )

    def test_stable_issue_id_excludes_message_and_reconstruction_hash(self) -> None:
        first = self.issue(reconstruction="a" * 64)
        second = ReviewIssue.create(
            reconstruction_sha256="b" * 64,
            code=first.code,
            subject_id=first.subject_id,
            unit_id=first.unit_id,
            source_sha256=first.source_sha256,
            message="A newly worded explanation.",
            evidence_sha256=first.evidence_sha256,
            reviewable=True,
            allowed_decisions=("accepted",),
        )

        self.assertEqual(first.issue_id, second.issue_id)
        self.assertEqual(
            first.issue_id,
            stable_review_issue_id(
                reconstruction_sha256="c" * 64,
                code=first.code,
                subject_id=first.subject_id,
                unit_id=first.unit_id,
                source_sha256=first.source_sha256,
                evidence_sha256=first.evidence_sha256,
            ),
        )

    def test_legacy_decision_stays_constructible_but_is_not_executable(self) -> None:
        legacy = ReviewDecision(
            schema_version=1,
            issue_id="issue-1",
            unit_id="unit-1",
            source_sha256=self.source,
            reviewer="reviewer",
            decision="accepted",
            timestamp="2026-08-15T10:00:00Z",
        )
        self.assertNotIn("reason", legacy.to_dict())
        with self.assertRaisesRegex(SemanticContractError, "missing fields"):
            ReviewDecision.from_dict(
                legacy.to_dict(),
                require_reconstruction=True,
            )

    def test_timestamp_hex_and_replacement_semantics_are_strict(self) -> None:
        with self.assertRaises(SemanticContractError):
            ReviewDecision(
                schema_version=1,
                issue_id="issue-1",
                unit_id=None,
                source_sha256="A" * 64,
                reviewer="reviewer",
                decision="accepted",
                timestamp="2026-08-15 10:00:00+00:00",
            )
        with self.assertRaisesRegex(SemanticContractError, "only a replaced"):
            ReviewDecision(
                schema_version=1,
                issue_id="issue-1",
                unit_id=None,
                source_sha256=self.source,
                reviewer="reviewer",
                decision="accepted",
                timestamp="2026-08-15T10:00:00Z",
                replacement_markdown="unexpected",
            )

    def test_reviewability_and_allowed_decisions_are_explicit(self) -> None:
        raw = self.issue().to_dict()
        raw.pop("reviewable")
        raw.pop("allowed_decisions")
        parsed = ReviewIssue.from_dict(raw)
        self.assertFalse(parsed.reviewable)
        self.assertEqual(parsed.allowed_decisions, ())
        with self.assertRaisesRegex(SemanticReviewError, "explicitly declare"):
            ReviewIssue.create(
                reconstruction_sha256=self.reconstruction,
                code="reviewable_issue",
                subject_id=self.subject,
                unit_id=self.subject,
                source_sha256=self.source,
                message="Needs review",
                reviewable=True,
            )

    def test_append_is_strict_and_second_decision_conflicts_in_v1(self) -> None:
        issue = self.issue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            snapshot = append_semantic_review_decision(path, self.decision(issue))
            self.assertEqual(len(snapshot.records), 1)
            self.assertEqual(snapshot.sha256, read_review_decision_log(path).sha256)
            with self.assertRaisesRegex(SemanticReviewError, "multiple decisions"):
                append_semantic_review_decision(
                    path,
                    self.decision(
                        issue,
                        decision="rejected",
                        timestamp="2026-08-15T10:01:00Z",
                    ),
                )
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

    def test_resolution_binds_issue_set_policy_and_decision_log(self) -> None:
        issue = self.issue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            append_semantic_review_decision(path, self.decision(issue))
            resolution = resolve_review_decisions(
                reconstruction_sha256=self.reconstruction,
                issues=[issue],
                decision_log_path=path,
                expected_issue_count=1,
                expected_issue_set_sha256=review_issue_set_sha256([issue]),
                policy_fingerprint=review_policy_fingerprint([issue]),
            )

        self.assertEqual(resolution.status, "passed")
        report = resolution.to_dict()
        self.assertEqual(report["decision_log"]["record_count"], 1)
        self.assertEqual(report["issue_set"]["count"], 1)
        self.assertRegex(report["review_policy"]["fingerprint"], r"^[0-9a-f]{64}$")

    def test_rejected_and_nonreviewable_issues_remain_blocking(self) -> None:
        issue = self.issue()
        nonreviewable = self.issue(
            code="pdf_text_layer_too_sparse",
            reviewable=False,
            allowed=(),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            append_semantic_review_decision(
                path,
                self.decision(issue, decision="rejected"),
            )
            resolution = resolve_review_decisions(
                reconstruction_sha256=self.reconstruction,
                issues=[issue, nonreviewable],
                decision_log_path=path,
            )

        self.assertTrue(resolution.release_blocked)
        self.assertEqual(
            {item.resolution for item in resolution.issues},
            {"rejected", "not_reviewable"},
        )

    def test_replacement_cannot_pass_without_semantic_validator(self) -> None:
        issue = self.issue(allowed=("replaced",), code="epub_footnote_target_missing")
        replacement = self.decision(
            issue,
            decision="replaced",
            replacement="Corrected paragraph.[^n]\n\n[^n]: Note.",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            append_semantic_review_decision(path, replacement)
            with self.assertRaisesRegex(SemanticReviewError, "replacement_validator"):
                resolve_review_decisions(
                    reconstruction_sha256=self.reconstruction,
                    issues=[issue],
                    decision_log_path=path,
                )
            resolution = resolve_review_decisions(
                reconstruction_sha256=self.reconstruction,
                issues=[issue],
                decision_log_path=path,
                replacement_validator=lambda candidate, decision: bool(
                    candidate.issue_id == decision.issue_id
                    and decision.replacement_markdown
                ),
            )
        self.assertEqual(resolution.status, "passed")

    def test_current_orphan_and_stale_binding_fail_closed(self) -> None:
        issue = self.issue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            other = self.issue(code="another_review_issue")
            append_semantic_review_decision(path, self.decision(other))
            with self.assertRaisesRegex(SemanticReviewError, "orphan"):
                resolve_review_decisions(
                    reconstruction_sha256=self.reconstruction,
                    issues=[issue],
                    decision_log_path=path,
                )

            stale_path = root / "stale.jsonl"
            initialize_review_decision_log(stale_path)
            stale = self.decision(issue, source_sha256="b" * 64)
            append_semantic_review_decision(stale_path, stale)
            with self.assertRaisesRegex(SemanticReviewError, "stale decision binding"):
                resolve_review_decisions(
                    reconstruction_sha256=self.reconstruction,
                    issues=[issue],
                    decision_log_path=stale_path,
                )

    def test_historical_reconstruction_records_are_not_reused(self) -> None:
        old_issue = self.issue(reconstruction="b" * 64)
        current_issue = self.issue()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "review-decisions.jsonl"
            initialize_review_decision_log(path)
            append_semantic_review_decision(
                path,
                self.decision(old_issue, reconstruction="b" * 64),
            )
            resolution = resolve_review_decisions(
                reconstruction_sha256=self.reconstruction,
                issues=[current_issue],
                decision_log_path=path,
            )
        self.assertEqual(resolution.historical_decision_count, 1)
        self.assertEqual(resolution.status, "blocked")

    def test_canonical_audit_is_rebuilt_and_snapshots_are_content_addressed(self) -> None:
        issue = self.issue()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            log = root / "review-decisions.jsonl"
            audit = root / "audit" / "semantic-review.json"
            initialize_review_decision_log(log)
            first = generate_semantic_review_audit(
                audit,
                reconstruction_sha256=self.reconstruction,
                issues=[issue],
                decision_log_path=log,
            )
            append_semantic_review_decision(log, self.decision(issue))
            second = generate_semantic_review_audit(
                audit,
                reconstruction_sha256=self.reconstruction,
                issues=[issue],
                decision_log_path=log,
            )

            payload = json.loads(audit.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "passed")
            self.assertNotEqual(first.audit_sha256, second.audit_sha256)
            self.assertTrue(first.snapshot_path and first.snapshot_path.is_file())
            self.assertTrue(second.snapshot_path and second.snapshot_path.is_file())

    @unittest.skipIf(not hasattr(os, "symlink"), "symlinks unavailable")
    def test_symlinked_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real = root / "real"
            real.mkdir()
            link = root / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(SemanticReviewError, "symlinked directory"):
                initialize_review_decision_log(link / "review-decisions.jsonl")


if __name__ == "__main__":
    unittest.main()
