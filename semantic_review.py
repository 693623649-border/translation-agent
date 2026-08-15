"""Fail-closed human-review decisions for canonical document semantics.

The decision JSONL is the only append-only source of truth.  A derived
``semantic-review.json`` can be rebuilt after every append; each rebuild is
also stored under a content-addressed name so a verifier can retain the exact
resolution bytes used by a publication run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from semantic_ir import (
    REVIEW_DECISIONS,
    SEMANTIC_SCHEMA_VERSION,
    ReviewDecision,
    SemanticContractError,
)

try:  # pragma: no cover - platform-specific import.
    import fcntl
except ImportError:  # pragma: no cover - Windows.
    fcntl = None

try:  # pragma: no cover - platform-specific import.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX.
    msvcrt = None


SEMANTIC_REVIEW_SCHEMA_VERSION = 1
REVIEW_POLICY_VERSION = "semantic-review-policy-v1"
ISSUE_ID_PREFIX = "issue-"
_ISSUE_ID = re.compile(r"issue-[0-9a-f]{64}")
_ISSUE_CODE = re.compile(r"[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RESOLVING_DECISIONS = frozenset({"accepted", "replaced"})
_DECISION_ORDER = ("accepted", "replaced")
_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "reconstruction_sha256",
        "issue_id",
        "subject_id",
        "unit_id",
        "source_sha256",
        "reviewer",
        "decision",
        "timestamp",
        "reason",
        "replacement_markdown",
    }
)


class SemanticReviewError(SemanticContractError):
    """Raised when review evidence cannot be resolved without ambiguity."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SemanticReviewError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticReviewError(f"{field_name} must be a non-empty string")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SemanticReviewError("review identity is not canonical JSON") from exc
    return text.encode("utf-8")


def canonical_review_evidence_sha256(value: object) -> str:
    """Hash a canonical locator/evidence object for use in issue identity."""

    return _sha256_bytes(
        b"semantic-review-evidence-v1\0" + _canonical_json(value)
    )


def stable_review_issue_id(
    *,
    reconstruction_sha256: str,
    code: str,
    subject_id: str | None = None,
    unit_id: str | None,
    source_sha256: str,
    evidence_sha256: str | None = None,
) -> str:
    """Create the stable ID for one exact reviewable semantic finding."""

    # The reconstruction hash is validated here but deliberately excluded from
    # the ID.  Importers can therefore write the issue ID into reconstruction
    # bytes without creating a self-referential hash cycle; decisions bind the
    # final reconstruction hash separately.
    _sha256(reconstruction_sha256, "reconstruction_sha256")
    source = _sha256(source_sha256, "source_sha256")
    issue_code = _required_string(code, "issue code")
    if _ISSUE_CODE.fullmatch(issue_code) is None:
        raise SemanticReviewError("issue code must be a stable lowercase token")
    if unit_id is not None:
        _required_string(unit_id, "issue unit_id")
    subject = subject_id if subject_id is not None else unit_id
    _required_string(subject, "issue subject_id")
    if unit_id is not None and subject != unit_id:
        raise SemanticReviewError(
            "a unit-level issue subject_id must equal its unit_id"
        )
    evidence = (
        _sha256(evidence_sha256, "evidence_sha256")
        if evidence_sha256 is not None
        else None
    )
    identity = {
        "schema_version": SEMANTIC_REVIEW_SCHEMA_VERSION,
        "code": issue_code,
        "subject_id": subject,
        "unit_id": unit_id,
        "source_sha256": source,
        "evidence_sha256": evidence,
    }
    return ISSUE_ID_PREFIX + _sha256_bytes(
        b"semantic-review-issue-v1\0" + _canonical_json(identity)
    )


# A short alias is convenient for adapters constructing issues.
make_review_issue_id = stable_review_issue_id


def _canonical_allowed_decisions(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SemanticReviewError("allowed_decisions must be an array")
    if not all(isinstance(value, str) for value in values):
        raise SemanticReviewError("allowed_decisions entries must be strings")
    unknown = sorted(set(values) - _RESOLVING_DECISIONS)
    if unknown:
        raise SemanticReviewError(
            f"allowed_decisions has unsupported resolutions: {unknown}"
        )
    if len(set(values)) != len(values):
        raise SemanticReviewError("allowed_decisions has duplicates")
    canonical = tuple(value for value in _DECISION_ORDER if value in values)
    if tuple(values) != canonical:
        raise SemanticReviewError("allowed_decisions must use canonical order")
    return canonical


@dataclass(frozen=True)
class ReviewIssue:
    schema_version: int
    issue_id: str
    reconstruction_sha256: str
    code: str
    subject_id: str
    unit_id: str | None
    source_sha256: str
    message: str
    evidence_sha256: str | None = None
    reviewable: bool = False
    allowed_decisions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != SEMANTIC_REVIEW_SCHEMA_VERSION
        ):
            raise SemanticReviewError("unsupported ReviewIssue schema")
        _sha256(self.reconstruction_sha256, "issue reconstruction_sha256")
        _sha256(self.source_sha256, "issue source_sha256")
        _required_string(self.message, "issue message")
        _required_string(self.subject_id, "issue subject_id")
        if self.unit_id is not None and self.subject_id != self.unit_id:
            raise SemanticReviewError(
                "a unit-level issue subject_id must equal its unit_id"
            )
        if type(self.reviewable) is not bool:
            raise SemanticReviewError("issue reviewable must be a boolean")
        allowed = _canonical_allowed_decisions(self.allowed_decisions)
        if not self.reviewable and allowed:
            raise SemanticReviewError(
                "a non-reviewable issue cannot declare allowed_decisions"
            )
        if self.reviewable and not allowed:
            raise SemanticReviewError(
                "a reviewable issue must explicitly declare allowed_decisions"
            )
        expected = stable_review_issue_id(
            reconstruction_sha256=self.reconstruction_sha256,
            code=self.code,
            subject_id=self.subject_id,
            unit_id=self.unit_id,
            source_sha256=self.source_sha256,
            evidence_sha256=self.evidence_sha256,
        )
        if self.issue_id != expected:
            raise SemanticReviewError("issue_id does not match the canonical issue identity")

    @classmethod
    def create(
        cls,
        *,
        reconstruction_sha256: str,
        code: str,
        subject_id: str | None = None,
        unit_id: str | None,
        source_sha256: str,
        message: str,
        evidence_sha256: str | None = None,
        reviewable: bool = False,
        allowed_decisions: Sequence[str] | None = None,
    ) -> "ReviewIssue":
        if allowed_decisions is None:
            if reviewable:
                raise SemanticReviewError(
                    "reviewable issues must explicitly declare allowed_decisions"
                )
            normalized_allowed = ()
        else:
            normalized_allowed = tuple(allowed_decisions)
        issue_id = stable_review_issue_id(
            reconstruction_sha256=reconstruction_sha256,
            code=code,
            subject_id=subject_id,
            unit_id=unit_id,
            source_sha256=source_sha256,
            evidence_sha256=evidence_sha256,
        )
        return cls(
            schema_version=SEMANTIC_REVIEW_SCHEMA_VERSION,
            issue_id=issue_id,
            reconstruction_sha256=reconstruction_sha256,
            code=code,
            subject_id=subject_id if subject_id is not None else str(unit_id or ""),
            unit_id=unit_id,
            source_sha256=source_sha256,
            message=message,
            evidence_sha256=evidence_sha256,
            reviewable=reviewable,
            allowed_decisions=normalized_allowed,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "issue_id": self.issue_id,
            "reconstruction_sha256": self.reconstruction_sha256,
            "code": self.code,
            "subject_id": self.subject_id,
            "unit_id": self.unit_id,
            "source_sha256": self.source_sha256,
            "message": self.message,
            "evidence_sha256": self.evidence_sha256,
            "reviewable": self.reviewable,
            "allowed_decisions": list(self.allowed_decisions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReviewIssue":
        """Read an issue; absent review policy deliberately means no waiver."""

        if not isinstance(value, Mapping):
            raise SemanticReviewError("review issue must be an object")
        allowed_fields = {
            "schema_version",
            "issue_id",
            "reconstruction_sha256",
            "code",
            "subject_id",
            "unit_id",
            "source_sha256",
            "message",
            "evidence_sha256",
            "reviewable",
            "allowed_decisions",
        }
        unknown = sorted(set(value) - allowed_fields)
        if unknown:
            raise SemanticReviewError(f"review issue has unknown fields: {unknown}")
        required = {
            "schema_version",
            "reconstruction_sha256",
            "code",
            "subject_id",
            "unit_id",
            "source_sha256",
            "message",
        }
        missing = sorted(required - set(value))
        if missing:
            raise SemanticReviewError(f"review issue is missing fields: {missing}")
        reviewable = value.get("reviewable", False)
        if type(reviewable) is not bool:
            raise SemanticReviewError("issue reviewable must be a boolean")
        if reviewable and "allowed_decisions" not in value:
            raise SemanticReviewError(
                "a reviewable issue must explicitly declare allowed_decisions"
            )
        raw_allowed = value.get("allowed_decisions", ())
        if not isinstance(raw_allowed, Sequence) or isinstance(raw_allowed, (str, bytes)):
            raise SemanticReviewError("allowed_decisions must be an array")
        allowed = tuple(raw_allowed)
        reconstruction = _required_string(
            value.get("reconstruction_sha256"),
            "issue reconstruction_sha256",
        )
        code = _required_string(value.get("code"), "issue code")
        unit_id = value.get("unit_id")
        if unit_id is not None and not isinstance(unit_id, str):
            raise SemanticReviewError("issue unit_id must be null or a string")
        subject_id = _required_string(value.get("subject_id"), "issue subject_id")
        source = _required_string(value.get("source_sha256"), "issue source_sha256")
        evidence = value.get("evidence_sha256")
        if evidence is not None and not isinstance(evidence, str):
            raise SemanticReviewError("issue evidence_sha256 must be null or a string")
        issue_id = value.get("issue_id")
        if issue_id is None:
            issue_id = stable_review_issue_id(
                reconstruction_sha256=reconstruction,
                code=code,
                subject_id=subject_id,
                unit_id=unit_id,
                source_sha256=source,
                evidence_sha256=evidence,
            )
        return cls(
            schema_version=value.get("schema_version"),
            issue_id=issue_id,
            reconstruction_sha256=reconstruction,
            code=code,
            subject_id=subject_id,
            unit_id=unit_id,
            source_sha256=source,
            message=_required_string(value.get("message"), "issue message"),
            evidence_sha256=evidence,
            reviewable=reviewable,
            allowed_decisions=allowed,
        )


@dataclass(frozen=True)
class LoggedReviewDecision:
    line: int
    decision: ReviewDecision


@dataclass(frozen=True)
class DecisionLogSnapshot:
    path: Path
    sha256: str
    byte_count: int
    records: tuple[LoggedReviewDecision, ...]


@dataclass(frozen=True)
class ResolvedReviewIssue:
    issue: ReviewIssue
    effective_decision: ReviewDecision | None
    decision_line: int | None
    resolution: str

    @property
    def release_blocked(self) -> bool:
        return self.resolution not in _RESOLVING_DECISIONS

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.issue.to_dict(),
            "resolution": self.resolution,
            "release_blocked": self.release_blocked,
            "effective_decision": (
                self.effective_decision.to_dict()
                if self.effective_decision is not None
                else None
            ),
            "decision_line": self.decision_line,
        }


@dataclass(frozen=True)
class SemanticReviewResolution:
    reconstruction_sha256: str
    review_policy_version: str
    review_policy_fingerprint: str
    issue_set_sha256: str
    decision_log_path: str
    decision_log_sha256: str
    decision_log_byte_count: int
    decision_count: int
    historical_decision_count: int
    issues: tuple[ResolvedReviewIssue, ...]

    @property
    def release_blocked(self) -> bool:
        return any(issue.release_blocked for issue in self.issues)

    @property
    def status(self) -> str:
        return "blocked" if self.release_blocked else "passed"

    def to_dict(self) -> dict[str, Any]:
        resolutions = {name: 0 for name in (*_DECISION_ORDER, "rejected", "unresolved", "not_reviewable")}
        for issue in self.issues:
            resolutions[issue.resolution] += 1
        blocking_count = sum(issue.release_blocked for issue in self.issues)
        return {
            "schema_version": SEMANTIC_REVIEW_SCHEMA_VERSION,
            "status": self.status,
            "release_blocked": self.release_blocked,
            "upstream_reconstruction": {
                "sha256": self.reconstruction_sha256,
            },
            "review_policy": {
                "version": self.review_policy_version,
                "fingerprint": self.review_policy_fingerprint,
            },
            "issue_set": {
                "sha256": self.issue_set_sha256,
                "count": len(self.issues),
            },
            "decision_log": {
                "path": self.decision_log_path,
                "sha256": self.decision_log_sha256,
                "byte_count": self.decision_log_byte_count,
                "record_count": self.decision_count,
                "historical_record_count": self.historical_decision_count,
            },
            "summary": {
                "issue_count": len(self.issues),
                "resolved_issue_count": len(self.issues) - blocking_count,
                "blocking_issue_count": blocking_count,
                "resolutions": resolutions,
                "release_blocked": self.release_blocked,
            },
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class SemanticReviewArtifact:
    resolution: SemanticReviewResolution
    audit_path: Path
    audit_sha256: str
    snapshot_path: Path | None


class _DuplicateJsonKey(ValueError):
    pass


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _parse_decision_log(data: bytes, *, path: Path) -> tuple[LoggedReviewDecision, ...]:
    if not data:
        return ()
    if not data.endswith(b"\n"):
        raise SemanticReviewError(f"decision log must end with a newline: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SemanticReviewError(f"decision log is not UTF-8: {path}") from exc
    records: list[LoggedReviewDecision] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise SemanticReviewError(
                f"decision log contains a blank line at {line_number}"
            )
        try:
            value = json.loads(
                line,
                object_pairs_hook=_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid constant {value}")
                ),
            )
        except (_DuplicateJsonKey, json.JSONDecodeError, ValueError) as exc:
            raise SemanticReviewError(
                f"decision log line {line_number} is invalid JSON"
            ) from exc
        if not isinstance(value, Mapping):
            raise SemanticReviewError(
                f"decision log line {line_number} must be an object"
            )
        if frozenset(value) != _DECISION_FIELDS:
            missing = sorted(_DECISION_FIELDS - set(value))
            unknown = sorted(set(value) - _DECISION_FIELDS)
            raise SemanticReviewError(
                f"decision log line {line_number} has invalid schema; "
                f"missing={missing}, unknown={unknown}"
            )
        try:
            decision = ReviewDecision.from_dict(
                value,
                require_reconstruction=True,
            )
        except SemanticContractError as exc:
            raise SemanticReviewError(
                f"decision log line {line_number} is invalid: {exc}"
            ) from exc
        if _ISSUE_ID.fullmatch(decision.issue_id) is None:
            raise SemanticReviewError(
                f"decision log line {line_number} has a non-canonical issue_id"
            )
        records.append(LoggedReviewDecision(line=line_number, decision=decision))
    _validate_decision_history(records)
    return tuple(records)


def _record_identity(decision: ReviewDecision) -> bytes:
    return _canonical_json(decision.to_dict())


def _decision_key(
    decision: ReviewDecision,
) -> tuple[str, str, str, str | None, str]:
    # require_reconstruction parsing guarantees the cast in executable paths.
    assert decision.reconstruction_sha256 is not None
    return (
        decision.reconstruction_sha256,
        decision.issue_id,
        str(decision.subject_id),
        decision.unit_id,
        decision.source_sha256,
    )


def _validate_decision_history(records: Sequence[LoggedReviewDecision]) -> None:
    seen_records: dict[bytes, int] = {}
    issue_bindings: dict[tuple[str, str], tuple[str, str | None, str]] = {}
    prior_by_key: dict[
        tuple[str, str, str, str | None, str], LoggedReviewDecision
    ] = {}
    for record in records:
        decision = record.decision
        identity = _record_identity(decision)
        duplicate_line = seen_records.get(identity)
        if duplicate_line is not None:
            raise SemanticReviewError(
                f"duplicate review decision at lines {duplicate_line} and {record.line}"
            )
        seen_records[identity] = record.line
        assert decision.reconstruction_sha256 is not None
        issue_key = (decision.reconstruction_sha256, decision.issue_id)
        binding = (
            str(decision.subject_id),
            decision.unit_id,
            decision.source_sha256,
        )
        prior_binding = issue_bindings.setdefault(issue_key, binding)
        if prior_binding != binding:
            raise SemanticReviewError(
                f"conflicting issue binding at decision log line {record.line}"
            )
        key = _decision_key(decision)
        prior = prior_by_key.get(key)
        if prior is not None:
            raise SemanticReviewError(
                f"multiple decisions for one issue are unsupported in policy v1 "
                f"(lines {prior.line} and {record.line})"
            )
        prior_by_key[key] = record


def _lock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return
    if msvcrt is not None:  # pragma: no cover - Windows.
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("no supported cross-process file locking API")


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif msvcrt is not None:  # pragma: no cover - Windows.
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _safe_absolute_path(path: Path, *, label: str) -> Path:
    normalized = path.expanduser().absolute()
    for ancestor in (normalized.parent, *normalized.parent.parents):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise SemanticReviewError(
                f"refusing {label} beneath symlinked directory: {ancestor}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            raise SemanticReviewError(
                f"{label} parent is not a directory: {ancestor}"
            )
    try:
        metadata = normalized.lstat()
    except FileNotFoundError:
        return normalized
    if stat.S_ISLNK(metadata.st_mode):
        raise SemanticReviewError(f"refusing symlinked {label}: {normalized}")
    return normalized


@contextmanager
def _decision_log_guard(path: Path) -> Iterator[Path]:
    normalized = _safe_absolute_path(path, label="decision log")
    normalized.parent.mkdir(parents=True, exist_ok=True)
    normalized = _safe_absolute_path(normalized, label="decision log")
    lock_path = normalized.with_name(normalized.name + ".lock")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SemanticReviewError(f"cannot open decision log lock: {lock_path}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SemanticReviewError(f"decision log lock is not regular: {lock_path}")
    handle = os.fdopen(descriptor, "r+b", closefd=True)
    try:
        _lock(handle)
        yield normalized
    finally:
        _unlock(handle)
        handle.close()


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SemanticReviewError(f"decision log is missing or unsafe: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SemanticReviewError(f"decision log is not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _snapshot_locked(path: Path) -> DecisionLogSnapshot:
    data = _read_regular_file(path)
    records = _parse_decision_log(data, path=path)
    return DecisionLogSnapshot(
        path=path,
        sha256=_sha256_bytes(data),
        byte_count=len(data),
        records=records,
    )


def initialize_review_decision_log(path: Path) -> None:
    """Create one empty decision log without replacing an existing entry."""

    with _decision_log_guard(path) as normalized:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(normalized, flags, 0o600)
        except FileExistsError as exc:
            raise SemanticReviewError(f"decision log already exists: {normalized}") from exc
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def read_review_decision_log(path: Path) -> DecisionLogSnapshot:
    with _decision_log_guard(path) as normalized:
        return _snapshot_locked(normalized)


def append_semantic_review_decision(
    path: Path,
    decision: ReviewDecision,
) -> DecisionLogSnapshot:
    """Validate the whole log, append one bound row, fsync, and re-snapshot."""

    if decision.reconstruction_sha256 is None:
        raise SemanticReviewError(
            "semantic review decisions require reconstruction_sha256"
        )
    # Round-trip enforces the executable log's exact schema and scalar types.
    ReviewDecision.from_dict(decision.to_dict(), require_reconstruction=True)
    if _ISSUE_ID.fullmatch(decision.issue_id) is None:
        raise SemanticReviewError("semantic review decision has a non-canonical issue_id")
    payload = _canonical_json(decision.to_dict()) + b"\n"
    with _decision_log_guard(path) as normalized:
        existing = b""
        if normalized.exists():
            existing = _read_regular_file(normalized)
            records = list(_parse_decision_log(existing, path=normalized))
            records.append(
                LoggedReviewDecision(line=len(records) + 1, decision=decision)
            )
            _validate_decision_history(records)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(normalized, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise SemanticReviewError(
                    f"decision log is not a regular file: {normalized}"
                )
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive OS failure.
                    raise OSError("short write while appending review decision")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return _snapshot_locked(normalized)


def _coerce_issues(
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
) -> tuple[ReviewIssue, ...]:
    if isinstance(issues, (str, bytes)):
        raise SemanticReviewError("issues must be an array")
    normalized = tuple(
        issue if isinstance(issue, ReviewIssue) else ReviewIssue.from_dict(issue)
        for issue in issues
    )
    seen: set[str] = set()
    identities: set[tuple[str, str, str | None, str, str | None]] = set()
    for issue in normalized:
        if issue.issue_id in seen:
            raise SemanticReviewError(f"duplicate review issue: {issue.issue_id}")
        seen.add(issue.issue_id)
        identity = (
            issue.code,
            issue.subject_id,
            issue.unit_id,
            issue.source_sha256,
            issue.evidence_sha256,
        )
        if identity in identities:
            raise SemanticReviewError(
                "duplicate review issue identity with different issue_id"
            )
        identities.add(identity)
    return tuple(sorted(normalized, key=lambda issue: issue.issue_id))


def review_issue_set_sha256(
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
) -> str:
    """Digest the complete canonical issue set, including review policy flags."""

    normalized = _coerce_issues(issues)
    return _sha256_bytes(
        b"semantic-review-issue-set-v1\0"
        + _canonical_json([issue.to_dict() for issue in normalized])
    )


def review_policy_fingerprint(
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
    *,
    policy_version: str = REVIEW_POLICY_VERSION,
) -> str:
    normalized = _coerce_issues(issues)
    policy = {
        "version": _required_string(policy_version, "review policy version"),
        "issues": [
            {
                "issue_id": issue.issue_id,
                "code": issue.code,
                "reviewable": issue.reviewable,
                "allowed_decisions": list(issue.allowed_decisions),
            }
            for issue in normalized
        ],
    }
    return _sha256_bytes(
        b"semantic-review-policy-v1\0" + _canonical_json(policy)
    )


def _resolve_snapshot(
    *,
    reconstruction_sha256: str,
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
    snapshot: DecisionLogSnapshot,
    decision_log_reference: str | None,
    policy_version: str,
    policy_fingerprint: str | None,
    expected_issue_count: int | None,
    expected_issue_set_sha256: str | None,
    replacement_validator: Callable[[ReviewIssue, ReviewDecision], bool] | None,
) -> SemanticReviewResolution:
    reconstruction = _sha256(reconstruction_sha256, "reconstruction_sha256")
    normalized_issues = _coerce_issues(issues)
    issue_set_digest = review_issue_set_sha256(normalized_issues)
    if expected_issue_count is not None:
        if type(expected_issue_count) is not int or expected_issue_count < 0:
            raise SemanticReviewError("expected_issue_count must be a non-negative integer")
        if expected_issue_count != len(normalized_issues):
            raise SemanticReviewError("review issue count does not match expected source audit")
    if expected_issue_set_sha256 is not None:
        expected_digest = _sha256(
            expected_issue_set_sha256,
            "expected_issue_set_sha256",
        )
        if expected_digest != issue_set_digest:
            raise SemanticReviewError("review issue set does not match expected source audit")
    normalized_policy_version = _required_string(
        policy_version,
        "review policy version",
    )
    computed_policy_fingerprint = review_policy_fingerprint(
        normalized_issues,
        policy_version=normalized_policy_version,
    )
    if policy_fingerprint is not None:
        supplied_policy_fingerprint = _sha256(
            policy_fingerprint,
            "policy_fingerprint",
        )
        if supplied_policy_fingerprint != computed_policy_fingerprint:
            raise SemanticReviewError("review policy fingerprint does not match issue policy")
    by_id: dict[str, ReviewIssue] = {}
    for issue in normalized_issues:
        if issue.reconstruction_sha256 != reconstruction:
            raise SemanticReviewError(
                f"stale issue reconstruction binding: {issue.issue_id}"
            )
        by_id[issue.issue_id] = issue

    latest: dict[str, LoggedReviewDecision] = {}
    historical_count = 0
    for record in snapshot.records:
        decision = record.decision
        if decision.reconstruction_sha256 != reconstruction:
            historical_count += 1
            continue
        issue = by_id.get(decision.issue_id)
        if issue is None:
            raise SemanticReviewError(
                f"orphan decision for current reconstruction at line {record.line}"
            )
        if (
            decision.subject_id != issue.subject_id
            or
            decision.unit_id != issue.unit_id
            or decision.source_sha256 != issue.source_sha256
        ):
            raise SemanticReviewError(
                f"stale decision binding at line {record.line}"
            )
        if decision.decision in _RESOLVING_DECISIONS:
            if not issue.reviewable or decision.decision not in issue.allowed_decisions:
                raise SemanticReviewError(
                    f"decision {decision.decision!r} is not allowed for issue "
                    f"{issue.issue_id}"
                )
            if decision.decision == "replaced":
                if replacement_validator is None:
                    raise SemanticReviewError(
                        "replaced decisions require an explicit replacement_validator"
                    )
                try:
                    replacement_valid = replacement_validator(issue, decision)
                except Exception as exc:
                    raise SemanticReviewError(
                        f"replacement validator failed for issue {issue.issue_id}"
                    ) from exc
                if replacement_valid is not True:
                    raise SemanticReviewError(
                        f"replacement did not resolve issue {issue.issue_id}"
                    )
        latest[issue.issue_id] = record

    resolved: list[ResolvedReviewIssue] = []
    for issue in normalized_issues:
        record = latest.get(issue.issue_id)
        if record is None:
            resolution = "not_reviewable" if not issue.reviewable else "unresolved"
            decision = None
            line = None
        else:
            decision = record.decision
            line = record.line
            resolution = decision.decision
        resolved.append(
            ResolvedReviewIssue(
                issue=issue,
                effective_decision=decision,
                decision_line=line,
                resolution=resolution,
            )
        )

    reference = (
        _required_string(decision_log_reference, "decision_log_reference")
        if decision_log_reference is not None
        else snapshot.path.name
    )
    return SemanticReviewResolution(
        reconstruction_sha256=reconstruction,
        review_policy_version=normalized_policy_version,
        review_policy_fingerprint=computed_policy_fingerprint,
        issue_set_sha256=issue_set_digest,
        decision_log_path=reference,
        decision_log_sha256=snapshot.sha256,
        decision_log_byte_count=snapshot.byte_count,
        decision_count=len(snapshot.records),
        historical_decision_count=historical_count,
        issues=tuple(resolved),
    )


def resolve_review_decisions(
    *,
    reconstruction_sha256: str,
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
    decision_log_path: Path,
    decision_log_reference: str | None = None,
    policy_version: str = REVIEW_POLICY_VERSION,
    policy_fingerprint: str | None = None,
    expected_issue_count: int | None = None,
    expected_issue_set_sha256: str | None = None,
    replacement_validator: Callable[[ReviewIssue, ReviewDecision], bool] | None = None,
) -> SemanticReviewResolution:
    """Resolve the latest exact-bound decision for every current issue."""

    with _decision_log_guard(decision_log_path) as normalized:
        snapshot = _snapshot_locked(normalized)
        return _resolve_snapshot(
            reconstruction_sha256=reconstruction_sha256,
            issues=issues,
            snapshot=snapshot,
            decision_log_reference=decision_log_reference,
            policy_version=policy_version,
            policy_fingerprint=policy_fingerprint,
            expected_issue_count=expected_issue_count,
            expected_issue_set_sha256=expected_issue_set_sha256,
            replacement_validator=replacement_validator,
        )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover - platform/filesystem dependent.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_derived(path: Path, payload: bytes) -> None:
    normalized = _safe_absolute_path(path, label="review audit")
    normalized.parent.mkdir(parents=True, exist_ok=True)
    normalized = _safe_absolute_path(normalized, label="review audit")
    if normalized.is_symlink():
        raise SemanticReviewError(f"refusing symlinked review audit: {normalized}")
    if normalized.exists() and not normalized.is_file():
        raise SemanticReviewError(f"review audit is not a regular file: {normalized}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{normalized.name}.",
        suffix=".tmp",
        dir=normalized.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if normalized.is_symlink():
            raise SemanticReviewError(f"refusing symlinked review audit: {normalized}")
        os.replace(temporary, normalized)
        _fsync_directory(normalized.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_content_addressed(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SemanticReviewError(
                f"cannot inspect existing review snapshot: {path}"
            ) from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise SemanticReviewError(
                f"existing review snapshot is not a regular file: {path}"
            )
        try:
            existing = path.read_bytes()
        except OSError as exc:
            raise SemanticReviewError(
                f"cannot validate existing review snapshot: {path}"
            ) from exc
        if existing != payload:
            raise SemanticReviewError(
                f"content-addressed review snapshot has conflicting bytes: {path}"
            )
        return
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS failure.
                raise OSError("short write while persisting review snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def write_semantic_review_audit(
    path: Path,
    resolution: SemanticReviewResolution,
    *,
    create_snapshot: bool = True,
) -> SemanticReviewArtifact:
    """Atomically refresh the derived audit and optionally retain exact bytes."""

    payload = json.dumps(
        resolution.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    digest = _sha256_bytes(payload)
    normalized = _safe_absolute_path(path, label="review audit")
    snapshot = (
        normalized.with_name(f"{normalized.stem}.{digest}{normalized.suffix}")
        if create_snapshot
        else None
    )
    if snapshot is not None:
        normalized.parent.mkdir(parents=True, exist_ok=True)
        _write_content_addressed(snapshot, payload)
    _atomic_write_derived(normalized, payload)
    return SemanticReviewArtifact(
        resolution=resolution,
        audit_path=normalized,
        audit_sha256=digest,
        snapshot_path=snapshot,
    )


def generate_semantic_review_audit(
    audit_path: Path,
    *,
    reconstruction_sha256: str,
    issues: Sequence[ReviewIssue | Mapping[str, Any]],
    decision_log_path: Path,
    decision_log_reference: str | None = None,
    policy_version: str = REVIEW_POLICY_VERSION,
    policy_fingerprint: str | None = None,
    expected_issue_count: int | None = None,
    expected_issue_set_sha256: str | None = None,
    replacement_validator: Callable[[ReviewIssue, ReviewDecision], bool] | None = None,
    create_snapshot: bool = True,
) -> SemanticReviewArtifact:
    """Resolve one locked log snapshot and publish its reproducible audit."""

    with _decision_log_guard(decision_log_path) as normalized:
        snapshot = _snapshot_locked(normalized)
        resolution = _resolve_snapshot(
            reconstruction_sha256=reconstruction_sha256,
            issues=issues,
            snapshot=snapshot,
            decision_log_reference=decision_log_reference,
            policy_version=policy_version,
            policy_fingerprint=policy_fingerprint,
            expected_issue_count=expected_issue_count,
            expected_issue_set_sha256=expected_issue_set_sha256,
            replacement_validator=replacement_validator,
        )
        return write_semantic_review_audit(
            audit_path,
            resolution,
            create_snapshot=create_snapshot,
        )


__all__ = [
    "DecisionLogSnapshot",
    "ISSUE_ID_PREFIX",
    "LoggedReviewDecision",
    "ReviewIssue",
    "REVIEW_POLICY_VERSION",
    "SEMANTIC_REVIEW_SCHEMA_VERSION",
    "SemanticReviewArtifact",
    "SemanticReviewError",
    "SemanticReviewResolution",
    "append_semantic_review_decision",
    "canonical_review_evidence_sha256",
    "generate_semantic_review_audit",
    "initialize_review_decision_log",
    "make_review_issue_id",
    "read_review_decision_log",
    "review_issue_set_sha256",
    "review_policy_fingerprint",
    "resolve_review_decisions",
    "stable_review_issue_id",
    "write_semantic_review_audit",
]
