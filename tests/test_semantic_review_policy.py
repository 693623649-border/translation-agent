from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from semantic_ir import ReviewDecision
from semantic_review import (
    append_semantic_review_decision,
    initialize_review_decision_log,
)
from semantic_review_policy import (
    SemanticReviewPolicyError,
    collect_reconstruction_review,
    record_semantic_review_decision,
    refresh_semantic_review,
    resolve_semantic_review,
    validate_semantic_review,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issue(
    code: str,
    *,
    blocking: bool = True,
    evidence: object | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "message": f"Finding for {code}",
        "blocking": blocking,
        "source_page": "pdf-0001-0001",
        "note_label": None,
        "evidence": evidence if evidence is not None else {},
    }


def _write_reconstruction(
    output: Path,
    *,
    chapter_issues: list[dict[str, object]] | None = None,
    root_issues: list[dict[str, object]] | None = None,
    contract_mode: str = "born-digital-pdf-text-layer",
) -> Path:
    chapter_issues = list(chapter_issues or [])
    root_issues = list(root_issues or [])
    chapter_blocking = sum(issue["blocking"] is True for issue in chapter_issues)
    root_blocking = sum(issue["blocking"] is True for issue in root_issues)
    issue_count = len(chapter_issues) + len(root_issues)
    blocking_count = chapter_blocking + root_blocking
    blocked = blocking_count > 0
    audit = {
        "schema_version": 1,
        "status": "blocked" if blocked else "passed",
        "release_blocked": blocked,
        "generated_by": "test",
        "contract_mode": contract_mode,
        "importer_version": "test-v1",
        "source": {"path": "/source/book.pdf", "sha256": _sha("document")},
        "summary": {
            "chapter_count": 1,
            "footnote_count": 0,
            "issue_count": issue_count,
            "blocking_issue_count": blocking_count,
            "release_blocked": blocked,
        },
        "issues": root_issues,
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "filename": "chapter-1.md",
                "markdown_sha256": _sha("chapter source"),
                "footnote_count": 0,
                "issues": chapter_issues,
                "release_blocked": bool(chapter_blocking),
            }
        ],
    }
    path = output / "audit" / "semantic-reconstruction.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class SemanticReviewPolicyTests(unittest.TestCase):
    def root(self, temporary: str) -> Path:
        return Path(temporary).resolve() / "output"

    def test_complete_blocker_extraction_is_policy_owned_and_evidence_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(
                output,
                chapter_issues=[
                    _issue(
                        "pdf_visible_superscript_unresolved",
                        evidence={"page": 1, "marker": "1"},
                    ),
                    _issue(
                        "pdf_visible_superscript_unresolved",
                        evidence={"page": 1, "marker": "2"},
                    ),
                    _issue("unknown_future_finding", evidence={"page": 1}),
                ],
                root_issues=[_issue("pdf_text_layer_too_sparse")],
            )

            context = collect_reconstruction_review(output)

        self.assertEqual(len(context.issues), 4)
        superscripts = [
            issue
            for issue in context.issues
            if issue.code == "pdf_visible_superscript_unresolved"
        ]
        self.assertEqual(len({issue.issue_id for issue in superscripts}), 2)
        self.assertTrue(all(issue.allowed_decisions == ("accepted",) for issue in superscripts))
        unknown = next(issue for issue in context.issues if issue.code == "unknown_future_finding")
        root = next(issue for issue in context.issues if issue.code == "pdf_text_layer_too_sparse")
        self.assertFalse(unknown.reviewable)
        self.assertFalse(root.reviewable)

    def test_summary_and_chapter_flags_are_strictly_reconciled(self) -> None:
        mutations = {
            "issue_count": lambda audit: audit["summary"].__setitem__("issue_count", 0),
            "blocking_count": lambda audit: audit["summary"].__setitem__("blocking_issue_count", 0),
            "chapter_count": lambda audit: audit["summary"].__setitem__("chapter_count", 2),
            "chapter_flag": lambda audit: audit["chapters"][0].__setitem__("release_blocked", False),
            "top_status": lambda audit: audit.__setitem__("status", "passed"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                output = self.root(temporary)
                path = _write_reconstruction(
                    output,
                    chapter_issues=[_issue("pdf_visible_superscript_unresolved")],
                )
                audit = json.loads(path.read_text(encoding="utf-8"))
                mutate(audit)
                path.write_text(json.dumps(audit) + "\n", encoding="utf-8")
                with self.assertRaises(SemanticReviewPolicyError):
                    collect_reconstruction_review(output)

    def test_expected_reconstruction_hash_rejects_stale_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(output)
            current = collect_reconstruction_review(output)

            with self.assertRaisesRegex(SemanticReviewPolicyError, "expected bytes"):
                collect_reconstruction_review(
                    output,
                    expected_reconstruction_sha256="0" * 64,
                )
            self.assertEqual(
                collect_reconstruction_review(
                    output,
                    expected_reconstruction_sha256=current.reconstruction_sha256,
                ).reconstruction_sha256,
                current.reconstruction_sha256,
            )

    def test_accepted_reason_and_replacement_policy_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(
                output,
                chapter_issues=[_issue("pdf_visible_superscript_unresolved")],
            )
            context = collect_reconstruction_review(output)
            issue = context.issues[0]

            with self.assertRaisesRegex(SemanticReviewPolicyError, "accept_as_text"):
                record_semantic_review_decision(
                    output,
                    issue_id=issue.issue_id,
                    reviewer="reviewer@example.test",
                    decision="accepted",
                    reason="looks fine",
                )
            with self.assertRaisesRegex(SemanticReviewPolicyError, "replacement"):
                record_semantic_review_decision(
                    output,
                    issue_id=issue.issue_id,
                    reviewer="reviewer@example.test",
                    decision="replaced",
                    reason="corrected",
                    replacement_markdown="Replacement",
                )

            artifact = record_semantic_review_decision(
                output,
                issue_id=issue.issue_id,
                reviewer="reviewer@example.test",
                decision="accepted",
                reason="accept_as_text",
                timestamp="2026-08-15T12:00:00Z",
            )
            validated = validate_semantic_review(output)

        self.assertEqual(artifact.resolution.status, "passed")
        self.assertEqual(validated.audit_sha256, artifact.audit_sha256)

    def test_direct_bad_reason_or_replacement_log_cannot_bypass_policy(self) -> None:
        for decision_name, reason, replacement in (
            ("accepted", "not-the-policy-reason", None),
            ("replaced", "corrected", "Replacement"),
        ):
            with self.subTest(decision=decision_name), tempfile.TemporaryDirectory() as temporary:
                output = self.root(temporary)
                _write_reconstruction(
                    output,
                    chapter_issues=[_issue("pdf_visible_superscript_unresolved")],
                )
                context = collect_reconstruction_review(output)
                issue = context.issues[0]
                initialize_review_decision_log(context.decision_log_path)
                append_semantic_review_decision(
                    context.decision_log_path,
                    ReviewDecision(
                        schema_version=1,
                        reconstruction_sha256=context.reconstruction_sha256,
                        issue_id=issue.issue_id,
                        subject_id=issue.subject_id,
                        unit_id=issue.unit_id,
                        source_sha256=issue.source_sha256,
                        reviewer="reviewer@example.test",
                        decision=decision_name,
                        timestamp="2026-08-15T12:00:00Z",
                        reason=reason,
                        replacement_markdown=replacement,
                    ),
                )
                with self.assertRaises(SemanticReviewPolicyError):
                    resolve_semantic_review(output)

    def test_validate_is_read_only_and_detects_stale_or_corrupt_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(output)
            artifact = refresh_semantic_review(output, create_decision_log=True)
            audit_entries_before = {
                path.name: (path.stat().st_ino, path.stat().st_size, path.read_bytes())
                for path in (output / "audit").iterdir()
                if path.is_file()
            }
            before = {
                path: path.read_bytes()
                for path in (
                    artifact.audit_path,
                    artifact.snapshot_path,
                    output / "audit" / "review-decisions.jsonl",
                )
                if path is not None
            }

            validated = validate_semantic_review(output)
            self.assertEqual(validated.audit_sha256, artifact.audit_sha256)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertEqual(
                audit_entries_before,
                {
                    path.name: (path.stat().st_ino, path.stat().st_size, path.read_bytes())
                    for path in (output / "audit").iterdir()
                    if path.is_file()
                },
            )

            artifact.audit_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(SemanticReviewPolicyError, "stale"):
                validate_semantic_review(output)

    def test_missing_corrupt_and_symlinked_evidence_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            path = _write_reconstruction(output)
            path.unlink()
            with self.assertRaises(SemanticReviewPolicyError):
                collect_reconstruction_review(output)

        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            path = _write_reconstruction(output)
            path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
            with self.assertRaisesRegex(SemanticReviewPolicyError, "invalid JSON"):
                collect_reconstruction_review(output)

        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            path = _write_reconstruction(output)
            real = output / "real.json"
            path.replace(real)
            path.symlink_to(real)
            with self.assertRaises(SemanticReviewPolicyError):
                collect_reconstruction_review(output)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            real_output = root / "real-output"
            _write_reconstruction(real_output)
            alias = root / "alias-output"
            alias.symlink_to(real_output, target_is_directory=True)
            with self.assertRaisesRegex(SemanticReviewPolicyError, "symlink"):
                collect_reconstruction_review(alias)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staged = root / "staged"
            _write_reconstruction(staged)
            output = root / "output"
            output.mkdir()
            (output / "audit").symlink_to(staged / "audit", target_is_directory=True)
            with self.assertRaisesRegex(SemanticReviewPolicyError, "audit path"):
                collect_reconstruction_review(output)

    def test_missing_or_symlinked_decision_log_and_review_audit_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(output)
            with self.assertRaises(SemanticReviewPolicyError):
                resolve_semantic_review(output)

            external = output / "external-log"
            external.write_bytes(b"")
            decision_log = output / "audit" / "review-decisions.jsonl"
            decision_log.symlink_to(external)
            with self.assertRaises(SemanticReviewPolicyError):
                resolve_semantic_review(output)

        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            _write_reconstruction(output)
            artifact = refresh_semantic_review(output, create_decision_log=True)
            actual = artifact.audit_path.with_name("actual-review.json")
            artifact.audit_path.replace(actual)
            artifact.audit_path.symlink_to(actual)
            with self.assertRaises(SemanticReviewPolicyError):
                validate_semantic_review(output)

    def test_reconstruction_change_during_read_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = self.root(temporary)
            reconstruction = _write_reconstruction(output)
            original_read = os.read
            changed = False

            def racing_read(descriptor: int, count: int) -> bytes:
                nonlocal changed
                data = original_read(descriptor, count)
                if data and not changed:
                    changed = True
                    reconstruction.write_bytes(reconstruction.read_bytes() + b" ")
                return data

            with mock.patch("semantic_review_policy.os.read", side_effect=racing_read):
                with self.assertRaisesRegex(SemanticReviewPolicyError, "changed"):
                    collect_reconstruction_review(output)


if __name__ == "__main__":
    unittest.main()
