"""Central, fail-closed policy for semantic reconstruction review.

The raw reconstruction audit remains immutable.  This module derives the
complete blocking issue set from those exact bytes, assigns policy-owned
review permissions, and delegates append-only decision resolution to
``semantic_review``.  Callers cannot omit blockers or grant an unknown issue
code review authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping

from semantic_ir import ReviewDecision
from semantic_review import (
    REVIEW_POLICY_VERSION,
    ReviewIssue,
    SemanticReviewArtifact,
    SemanticReviewError,
    SemanticReviewResolution,
    append_semantic_review_decision,
    canonical_review_evidence_sha256,
    initialize_review_decision_log,
    review_issue_set_sha256,
    review_policy_fingerprint,
    resolve_review_decisions,
    write_semantic_review_audit,
)


DECISION_LOG_RELATIVE = Path("audit/review-decisions.jsonl")
REVIEW_AUDIT_RELATIVE = Path("audit/semantic-review.json")

# Version 1 intentionally supports only judgments that do not change source
# bytes.  Structural findings stay blocked until replacement materialization
# and validation are implemented as a separate, explicit contract.
_ACCEPTABLE_AS_TEXT = frozenset(
    {
        "pdf_visible_superscript_unresolved",
    }
)
_PDF_TEXT_CONTRACT = "born-digital-pdf-text-layer"
ACCEPT_AS_TEXT_REASON = "accept_as_text"


class SemanticReviewPolicyError(SemanticReviewError):
    """Raised when a raw audit and the central review policy disagree."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha256(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise SemanticReviewPolicyError(f"{field} must be lowercase SHA-256 hex")
    return value


def _require_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticReviewPolicyError(f"{field} must be a non-empty string")
    return value


def _require_count(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise SemanticReviewPolicyError(f"{field} must be a non-negative integer")
    return value


def _safe_output_dir(output_dir: Path | str) -> Path:
    output = Path(output_dir).expanduser().absolute()
    for entry in (output, *output.parents):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            raise SemanticReviewPolicyError(
                f"output directory is missing: {output}"
            )
        if stat.S_ISLNK(metadata.st_mode):
            raise SemanticReviewPolicyError(
                f"output directory has a symlink component: {entry}"
            )
        if entry == output and not stat.S_ISDIR(metadata.st_mode):
            raise SemanticReviewPolicyError(
                f"output directory is not a directory: {output}"
            )
    audit = output / "audit"
    try:
        metadata = audit.lstat()
    except OSError as exc:
        raise SemanticReviewPolicyError(f"audit directory is missing: {audit}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SemanticReviewPolicyError(f"audit path is not a regular directory: {audit}")
    return output


def _read_regular_bytes(path: Path, *, label: str) -> bytes:
    for parent in (path.parent, *path.parent.parents):
        try:
            metadata = parent.lstat()
        except OSError as exc:
            raise SemanticReviewPolicyError(f"{label} parent is missing: {parent}") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SemanticReviewPolicyError(f"{label} has an unsafe parent: {parent}")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SemanticReviewPolicyError(f"{label} is missing: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SemanticReviewPolicyError(f"{label} is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if identity(before) != identity(after):
        raise SemanticReviewPolicyError(f"{label} changed while being read: {path}")
    try:
        current = path.lstat()
    except OSError as exc:
        raise SemanticReviewPolicyError(f"{label} disappeared while being read: {path}") from exc
    if not stat.S_ISREG(current.st_mode) or identity(current) != identity(after):
        raise SemanticReviewPolicyError(f"{label} path changed while being read: {path}")
    return b"".join(chunks)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _read_regular_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    raw = _read_regular_bytes(path, label=label)
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid constant {value}")
            ),
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SemanticReviewPolicyError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise SemanticReviewPolicyError(f"{label} root must be an object")
    return payload, raw


@dataclass(frozen=True)
class ReconstructionReviewContext:
    output_dir: Path
    reconstruction_path: Path
    reconstruction_sha256: str
    reconstruction: Mapping[str, Any]
    issues: tuple[ReviewIssue, ...]
    issue_set_sha256: str
    policy_fingerprint: str

    @property
    def decision_log_path(self) -> Path:
        return self.output_dir / DECISION_LOG_RELATIVE

    @property
    def review_audit_path(self) -> Path:
        return self.output_dir / REVIEW_AUDIT_RELATIVE

    @property
    def decision_lock_path(self) -> Path:
        return self.decision_log_path.with_name(self.decision_log_path.name + ".lock")


def _issue_sequence(value: object, *, location: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, Mapping) for item in value
    ):
        raise SemanticReviewPolicyError(f"{location} issues must be an array")
    return list(value)


def _review_issue(
    *,
    reconstruction_sha256: str,
    issue: Mapping[str, Any],
    subject_id: str,
    subject_sha256: str,
    scope: str,
    contract_mode: str,
    root_issue: bool,
) -> ReviewIssue:
    code = _require_string(issue.get("code"), field=f"{scope} issue code")
    message = _require_string(
        issue.get("message"),
        field=f"{scope} issue message",
    )
    raw_unit_id = issue.get("unit_id")
    if raw_unit_id is not None and not isinstance(raw_unit_id, str):
        raise SemanticReviewPolicyError(f"{scope} issue unit_id is invalid")
    unit_id = raw_unit_id.strip() if isinstance(raw_unit_id, str) else None
    if unit_id == "":
        raise SemanticReviewPolicyError(f"{scope} issue unit_id is empty")
    effective_subject = unit_id or subject_id
    evidence_identity = {
        "scope": scope,
        "source_page": issue.get("source_page"),
        "note_label": issue.get("note_label"),
        "evidence": issue.get("evidence"),
    }
    allowed = (
        ("accepted",)
        if (
            not root_issue
            and contract_mode == _PDF_TEXT_CONTRACT
            and code in _ACCEPTABLE_AS_TEXT
        )
        else ()
    )
    return ReviewIssue.create(
        reconstruction_sha256=reconstruction_sha256,
        code=code,
        subject_id=effective_subject,
        unit_id=unit_id,
        source_sha256=subject_sha256,
        message=message,
        evidence_sha256=canonical_review_evidence_sha256(evidence_identity),
        reviewable=bool(allowed),
        allowed_decisions=allowed,
    )


def collect_reconstruction_review(
    output_dir: Path | str,
    *,
    expected_reconstruction_sha256: str | None = None,
) -> ReconstructionReviewContext:
    """Extract and exactly count every blocking finding in the raw audit."""

    output = _safe_output_dir(output_dir)
    reconstruction_path = output / "audit" / "semantic-reconstruction.json"
    audit, raw = _read_regular_json(
        reconstruction_path,
        label="semantic reconstruction audit",
    )
    if audit.get("schema_version") != 1:
        raise SemanticReviewPolicyError(
            "unsupported semantic reconstruction audit schema"
        )
    reconstruction_sha256 = _sha256_bytes(raw)
    if expected_reconstruction_sha256 is not None and _require_sha256(
        expected_reconstruction_sha256,
        field="expected reconstruction sha256",
    ) != reconstruction_sha256:
        raise SemanticReviewPolicyError(
            "semantic reconstruction audit does not match the expected bytes"
        )
    contract_mode = _require_string(
        audit.get("contract_mode"),
        field="reconstruction contract_mode",
    )
    source = audit.get("source")
    if not isinstance(source, Mapping):
        raise SemanticReviewPolicyError("reconstruction source identity is missing")
    source_sha256 = _require_sha256(
        source.get("sha256"),
        field="reconstruction source sha256",
    )

    all_issue_count = 0
    blocking: list[ReviewIssue] = []
    root_issues = _issue_sequence(audit.get("issues"), location="root")
    all_issue_count += len(root_issues)
    for issue in root_issues:
        blocking_value = issue.get("blocking", True)
        if type(blocking_value) is not bool:
            raise SemanticReviewPolicyError("root issue blocking must be a boolean")
        if blocking_value:
            blocking.append(
                _review_issue(
                    reconstruction_sha256=reconstruction_sha256,
                    issue=issue,
                    subject_id="document",
                    subject_sha256=source_sha256,
                    scope="root",
                    contract_mode=contract_mode,
                    root_issue=True,
                )
            )

    chapters = audit.get("chapters")
    if not isinstance(chapters, list) or not all(
        isinstance(chapter, Mapping) for chapter in chapters
    ):
        raise SemanticReviewPolicyError("reconstruction chapters must be an array")
    seen_chapter_ids: set[str] = set()
    chapter_footnote_count = 0
    for chapter in chapters:
        chapter_id = _require_string(
            chapter.get("chapter_id"),
            field="chapter_id",
        )
        if chapter_id in seen_chapter_ids:
            raise SemanticReviewPolicyError(
                f"duplicate reconstruction chapter_id: {chapter_id}"
            )
        seen_chapter_ids.add(chapter_id)
        chapter_sha256 = _require_sha256(
            chapter.get("markdown_sha256"),
            field=f"chapter {chapter_id} markdown_sha256",
        )
        chapter_issues = _issue_sequence(
            chapter.get("issues"),
            location=f"chapter {chapter_id}",
        )
        chapter_footnote_count += _require_count(
            chapter.get("footnote_count"),
            field=f"chapter {chapter_id} footnote_count",
        )
        all_issue_count += len(chapter_issues)
        chapter_blocking_count = 0
        for issue in chapter_issues:
            blocking_value = issue.get("blocking", True)
            if type(blocking_value) is not bool:
                raise SemanticReviewPolicyError(
                    f"chapter {chapter_id} issue blocking must be a boolean"
                )
            if blocking_value:
                chapter_blocking_count += 1
                blocking.append(
                    _review_issue(
                        reconstruction_sha256=reconstruction_sha256,
                        issue=issue,
                        subject_id=chapter_id,
                        subject_sha256=chapter_sha256,
                        scope=f"chapter:{chapter_id}",
                        contract_mode=contract_mode,
                        root_issue=False,
                    )
                )
        chapter_release_blocked = chapter.get("release_blocked")
        if type(chapter_release_blocked) is not bool or chapter_release_blocked != bool(
            chapter_blocking_count
        ):
            raise SemanticReviewPolicyError(
                f"chapter {chapter_id} release_blocked does not match its issues"
            )

    summary = audit.get("summary")
    if not isinstance(summary, Mapping):
        raise SemanticReviewPolicyError("reconstruction summary is missing")
    if _require_count(summary.get("chapter_count"), field="summary chapter_count") != len(
        chapters
    ):
        raise SemanticReviewPolicyError(
            "reconstruction chapter count does not match chapters"
        )
    if _require_count(summary.get("footnote_count"), field="summary footnote_count") != chapter_footnote_count:
        raise SemanticReviewPolicyError(
            "reconstruction footnote count does not match chapters"
        )
    if _require_count(summary.get("issue_count"), field="summary issue_count") != all_issue_count:
        raise SemanticReviewPolicyError(
            "reconstruction issue count does not match root and chapter issues"
        )
    if _require_count(
        summary.get("blocking_issue_count"),
        field="summary blocking_issue_count",
    ) != len(blocking):
        raise SemanticReviewPolicyError(
            "reconstruction blocking issue count does not match issue set"
        )
    expected_blocked = bool(blocking)
    summary_release_blocked = summary.get("release_blocked")
    audit_release_blocked = audit.get("release_blocked")
    if (
        type(summary_release_blocked) is not bool
        or type(audit_release_blocked) is not bool
        or summary_release_blocked is not expected_blocked
        or audit_release_blocked is not expected_blocked
        or audit.get("status") != ("blocked" if expected_blocked else "passed")
    ):
        raise SemanticReviewPolicyError(
            "reconstruction status does not match its blocking issue set"
        )

    issue_tuple = tuple(blocking)
    return ReconstructionReviewContext(
        output_dir=output,
        reconstruction_path=reconstruction_path,
        reconstruction_sha256=reconstruction_sha256,
        reconstruction=audit,
        issues=issue_tuple,
        issue_set_sha256=review_issue_set_sha256(issue_tuple),
        policy_fingerprint=review_policy_fingerprint(
            issue_tuple,
            policy_version=REVIEW_POLICY_VERSION,
        ),
    )


def _ensure_decision_log(path: Path) -> None:
    if path.exists() or path.is_symlink():
        return
    try:
        initialize_review_decision_log(path)
    except SemanticReviewError:
        # A concurrent creator either produced a valid log or the subsequent
        # locked resolver will fail closed with the precise unsafe-file error.
        if not path.exists():
            raise


def _assert_reconstruction_current(context: ReconstructionReviewContext) -> None:
    raw = _read_regular_bytes(
        context.reconstruction_path,
        label="semantic reconstruction audit",
    )
    if _sha256_bytes(raw) != context.reconstruction_sha256:
        raise SemanticReviewPolicyError(
            "semantic reconstruction audit changed during review resolution"
        )


def _validate_effective_policy(resolution: SemanticReviewResolution) -> None:
    for resolved in resolution.issues:
        decision = resolved.effective_decision
        if decision is None:
            continue
        if decision.decision == "accepted" and decision.reason != ACCEPT_AS_TEXT_REASON:
            raise SemanticReviewPolicyError(
                f"accepted issue {resolved.issue.issue_id} requires reason "
                f"{ACCEPT_AS_TEXT_REASON!r}"
            )
        if decision.decision == "replaced":
            raise SemanticReviewPolicyError(
                "replacement decisions are unsupported by review policy v1"
            )


def resolve_semantic_review(
    output_dir: Path | str,
    *,
    expected_reconstruction_sha256: str | None = None,
) -> tuple[ReconstructionReviewContext, SemanticReviewResolution]:
    """Recompute policy and decisions without writing derived artifacts."""

    context = collect_reconstruction_review(
        output_dir,
        expected_reconstruction_sha256=expected_reconstruction_sha256,
    )
    # The core lock is persistent.  Requiring it to pre-exist keeps this path
    # genuinely read-only instead of allowing the resolver's O_CREAT fallback
    # to mutate an otherwise incomplete review workspace.
    _read_regular_bytes(
        context.decision_lock_path,
        label="semantic review decision lock",
    )
    decision_log_before = _read_regular_bytes(
        context.decision_log_path,
        label="semantic review decision log",
    )
    try:
        resolution = resolve_review_decisions(
            reconstruction_sha256=context.reconstruction_sha256,
            issues=context.issues,
            decision_log_path=context.decision_log_path,
            decision_log_reference=DECISION_LOG_RELATIVE.as_posix(),
            policy_version=REVIEW_POLICY_VERSION,
            policy_fingerprint=context.policy_fingerprint,
            expected_issue_count=len(context.issues),
            expected_issue_set_sha256=context.issue_set_sha256,
        )
    except SemanticReviewPolicyError:
        raise
    except SemanticReviewError as exc:
        raise SemanticReviewPolicyError(str(exc)) from exc
    if resolution.decision_log_sha256 != _sha256_bytes(decision_log_before):
        raise SemanticReviewPolicyError(
            "semantic review decision log changed during resolution"
        )
    decision_log_after = _read_regular_bytes(
        context.decision_log_path,
        label="semantic review decision log",
    )
    if decision_log_after != decision_log_before:
        raise SemanticReviewPolicyError(
            "semantic review decision log changed during resolution"
        )
    _assert_reconstruction_current(context)
    _validate_effective_policy(resolution)
    return context, resolution


def _resolution_bytes(resolution: SemanticReviewResolution) -> bytes:
    return (
        json.dumps(
            resolution.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def refresh_semantic_review(
    output_dir: Path | str,
    *,
    create_decision_log: bool = False,
    expected_reconstruction_sha256: str | None = None,
) -> SemanticReviewArtifact:
    context = collect_reconstruction_review(
        output_dir,
        expected_reconstruction_sha256=expected_reconstruction_sha256,
    )
    if create_decision_log:
        _ensure_decision_log(context.decision_log_path)
    context, resolution = resolve_semantic_review(
        output_dir,
        expected_reconstruction_sha256=context.reconstruction_sha256,
    )
    _assert_reconstruction_current(context)
    artifact = write_semantic_review_audit(context.review_audit_path, resolution)
    _assert_reconstruction_current(context)
    written = _read_regular_bytes(
        context.review_audit_path,
        label="semantic review audit",
    )
    if _sha256_bytes(written) != artifact.audit_sha256 or written != _resolution_bytes(
        resolution
    ):
        raise SemanticReviewPolicyError(
            "semantic review audit does not match the resolved policy bytes"
        )
    return artifact


def validate_semantic_review(
    output_dir: Path | str,
    *,
    expected_reconstruction_sha256: str | None = None,
) -> SemanticReviewArtifact:
    """Read-only validation of the canonical audit against raw source evidence."""

    context, resolution = resolve_semantic_review(
        output_dir,
        expected_reconstruction_sha256=expected_reconstruction_sha256,
    )
    expected = _resolution_bytes(resolution)
    digest = _sha256_bytes(expected)
    actual = _read_regular_bytes(
        context.review_audit_path,
        label="semantic review audit",
    )
    if actual != expected:
        raise SemanticReviewPolicyError(
            "semantic review audit is stale or does not match current evidence"
        )
    snapshot_path = context.review_audit_path.with_name(
        f"{context.review_audit_path.stem}.{digest}{context.review_audit_path.suffix}"
    )
    snapshot = _read_regular_bytes(
        snapshot_path,
        label="content-addressed semantic review snapshot",
    )
    if snapshot != expected:
        raise SemanticReviewPolicyError(
            "content-addressed semantic review snapshot has conflicting bytes"
        )
    _assert_reconstruction_current(context)
    return SemanticReviewArtifact(
        resolution=resolution,
        audit_path=context.review_audit_path,
        audit_sha256=digest,
        snapshot_path=snapshot_path,
    )


def record_semantic_review_decision(
    output_dir: Path | str,
    *,
    issue_id: str,
    reviewer: str,
    decision: str,
    reason: str,
    replacement_markdown: str | None = None,
    timestamp: str | None = None,
) -> SemanticReviewArtifact:
    """Append one exact-bound decision and refresh the derived audit."""

    context = collect_reconstruction_review(output_dir)
    by_id = {issue.issue_id: issue for issue in context.issues}
    issue = by_id.get(issue_id)
    if issue is None:
        raise SemanticReviewPolicyError("issue_id is not part of current reconstruction")
    if replacement_markdown is not None or decision == "replaced":
        raise SemanticReviewPolicyError(
            "replacement decisions are unsupported by review policy v1"
        )
    if not issue.reviewable or decision not in issue.allowed_decisions:
        raise SemanticReviewPolicyError(
            f"decision {decision!r} is not allowed for issue {issue_id}"
        )
    if decision == "accepted" and reason != ACCEPT_AS_TEXT_REASON:
        raise SemanticReviewPolicyError(
            f"accepted decisions require reason {ACCEPT_AS_TEXT_REASON!r}"
        )
    _ensure_decision_log(context.decision_log_path)
    _assert_reconstruction_current(context)
    review_timestamp = timestamp or datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    row = ReviewDecision(
        schema_version=1,
        issue_id=issue.issue_id,
        subject_id=issue.subject_id,
        unit_id=issue.unit_id,
        source_sha256=issue.source_sha256,
        reconstruction_sha256=context.reconstruction_sha256,
        reviewer=reviewer,
        decision=decision,
        timestamp=review_timestamp,
        reason=reason,
        replacement_markdown=replacement_markdown,
    )
    append_semantic_review_decision(context.decision_log_path, row)
    return refresh_semantic_review(
        output_dir,
        expected_reconstruction_sha256=context.reconstruction_sha256,
    )


__all__ = [
    "ACCEPT_AS_TEXT_REASON",
    "DECISION_LOG_RELATIVE",
    "REVIEW_AUDIT_RELATIVE",
    "ReconstructionReviewContext",
    "SemanticReviewPolicyError",
    "collect_reconstruction_review",
    "record_semantic_review_decision",
    "refresh_semantic_review",
    "resolve_semantic_review",
    "validate_semantic_review",
]
