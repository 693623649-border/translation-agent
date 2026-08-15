"""Read-only validation for SHA-bound JSON evidence graphs.

Evidence reports often outlive the temporary files used to create them.  A
consumer must therefore distinguish two cases instead of silently accepting a
digest next to a stale path:

* a live reference has a non-empty path and its bytes match the declared SHA;
* a historical reference has ``path: null`` and an explicit
  ``historical``/``superseded`` lifecycle marker.

The validator follows SHA-bound JSON references, checks source-document
identity across the graph, and verifies ground-truth bindings.  It never
writes to the evidence files it inspects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_HISTORICAL_MARKERS = (
    "historical",
    "superseded",
    "archived",
    "retired",
    "obsolete",
)
_STATUS_KEYS = frozenset(
    {
        "artifact_status",
        "lifecycle",
        "lifecycle_status",
        "mode",
        "review_status",
        "status",
    }
)

# A named path is only considered a contract reference when its matching
# digest key is present (or vice versa).  Unrelated report fields named
# ``path`` are handled only by the explicit ``{path, sha256}`` object form.
_NAMED_REFERENCE_PAIRS = (
    ("source_pdf", "source_pdf_sha256", "source"),
    ("pdf_path", "pdf_sha256", "source"),
    ("ground_truth", "ground_truth_sha256", "ground_truth"),
    ("ground_truth_path", "ground_truth_sha256", "ground_truth"),
    (
        "ground_truth_artifact",
        "ground_truth_artifact_sha256",
        "ground_truth",
    ),
    ("frozen_ground_truth", "frozen_ground_truth_sha256", "ground_truth"),
    ("base_report", "base_report_sha256", "evidence"),
    ("source_path", "source_sha256", "evidence"),
)


def sha256_file(path: Path) -> str:
    """Return the SHA-256 digest of *path* without mutating it."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class EvidenceContractIssue:
    code: str
    message: str
    artifact: str
    pointer: str = ""
    expected: str | None = None
    actual: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "artifact": self.artifact,
        }
        if self.pointer:
            value["pointer"] = self.pointer
        if self.expected is not None:
            value["expected"] = self.expected
        if self.actual is not None:
            value["actual"] = self.actual
        return value


@dataclass
class EvidenceContractReport:
    roots: list[str]
    project_root: str
    recursive: bool = True
    expected_source: str | None = None
    expected_source_sha256: str | None = None
    artifacts_checked: list[str] = field(default_factory=list)
    referenced_json_not_traversed: list[str] = field(default_factory=list)
    references_declared: int = 0
    live_references_checked: int = 0
    historical_references: int = 0
    issues: list[EvidenceContractIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        issues = sorted(
            self.issues,
            key=lambda item: (item.artifact, item.pointer, item.code),
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": self.ok,
            "roots": self.roots,
            "project_root": self.project_root,
            "scope": "recursive_graph" if self.recursive else "root_bindings_only",
            "recursive": self.recursive,
            "expected_source": self.expected_source,
            "expected_source_sha256": self.expected_source_sha256,
            "artifacts_checked": sorted(self.artifacts_checked),
            "artifact_count": len(self.artifacts_checked),
            "referenced_json_not_traversed": sorted(
                set(self.referenced_json_not_traversed)
            ),
            "references_declared": self.references_declared,
            "live_references_checked": self.live_references_checked,
            "historical_references": self.historical_references,
            "issue_count": len(issues),
            "issues": [item.to_dict() for item in issues],
        }


@dataclass(frozen=True)
class _Reference:
    artifact: Path
    pointer: str
    kind: str
    declared_path: str | None
    expected_sha256: str | None
    historical: bool
    declared_schema_version: str | None = None


@dataclass(frozen=True)
class _GraphEdge:
    parent: Path
    child: Path
    pointer: str
    kind: str
    parent_source_sha256: str | None
    declared_schema_version: str | None


def _pointer(parent: str, key: str | int) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}"


def _is_historical(mapping: Mapping[str, Any], inherited: bool = False) -> bool:
    if inherited:
        return True
    for key in _STATUS_KEYS:
        value = mapping.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.casefold()
        if any(marker in normalized for marker in _HISTORICAL_MARKERS):
            return True
    return False


def _walk_mappings(
    value: Any,
    *,
    pointer: str = "",
    inherited_historical: bool = False,
) -> Iterable[tuple[Mapping[str, Any], str, bool]]:
    if isinstance(value, Mapping):
        historical = _is_historical(value, inherited_historical)
        yield value, pointer, historical
        for key, child in value.items():
            yield from _walk_mappings(
                child,
                pointer=_pointer(pointer, key),
                inherited_historical=historical,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_mappings(
                child,
                pointer=_pointer(pointer, index),
                inherited_historical=inherited_historical,
            )


def _reference_kind(pointer: str) -> str:
    return "ground_truth" if "ground_truth" in pointer.casefold() else "evidence"


def _looks_like_generic_reference(pointer: str) -> bool:
    normalized = pointer.casefold()
    return any(
        marker in normalized
        for marker in (
            "ground_truth",
            "anchor_evidence",
            "anchor_inputs",
            "binding",
            "evidence_input",
            "reference",
        )
    )


def _coerce_path(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _coerce_sha256(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def _extract_references(payload: Mapping[str, Any], artifact: Path) -> list[_Reference]:
    references: list[_Reference] = []
    for mapping, pointer, historical in _walk_mappings(payload):
        if (
            "path" in mapping or "sha256" in mapping
        ) and (
            "path" in mapping
            and "sha256" in mapping
            or _looks_like_generic_reference(pointer)
        ):
            references.append(
                _Reference(
                    artifact=artifact,
                    pointer=pointer or "/",
                    kind=_reference_kind(pointer),
                    declared_path=_coerce_path(mapping.get("path")),
                    expected_sha256=_coerce_sha256(mapping.get("sha256")),
                    historical=historical,
                    declared_schema_version=(
                        str(mapping["schema_version"])
                        if mapping.get("schema_version") is not None
                        else None
                    ),
                )
            )

        for path_key, sha_key, kind in _NAMED_REFERENCE_PAIRS:
            if path_key == "ground_truth" and isinstance(
                mapping.get(path_key), Mapping
            ):
                continue
            if path_key not in mapping and sha_key not in mapping:
                continue
            # A source digest may deliberately be an identity-only declaration.
            if path_key not in mapping and kind == "source":
                continue
            # A top-level ground_truth_sha256 may be bound by a nested
            # frozen_ground_truth reference.  It is checked separately below.
            if path_key not in mapping and kind == "ground_truth":
                continue
            references.append(
                _Reference(
                    artifact=artifact,
                    pointer=_pointer(pointer, path_key),
                    kind=kind,
                    declared_path=_coerce_path(mapping.get(path_key)),
                    expected_sha256=_coerce_sha256(mapping.get(sha_key)),
                    historical=historical,
                )
            )
    return references


def _source_sha_declarations(payload: Mapping[str, Any]) -> list[tuple[str, str]]:
    declarations: list[tuple[str, str]] = []
    for mapping, pointer, _historical in _walk_mappings(payload):
        for key in ("source_pdf_sha256", "pdf_sha256"):
            value = mapping.get(key)
            if value is not None:
                declarations.append((_pointer(pointer, key), str(value).strip()))
    return declarations


def _ground_truth_sha_declarations(
    payload: Mapping[str, Any],
) -> list[tuple[str, str, bool]]:
    declarations: list[tuple[str, str, bool]] = []
    keys = {
        "ground_truth_sha256",
        "ground_truth_artifact_sha256",
        "frozen_ground_truth_sha256",
    }
    for mapping, pointer, historical in _walk_mappings(payload):
        for key in keys:
            value = mapping.get(key)
            if value is not None:
                declarations.append(
                    (_pointer(pointer, key), str(value).strip(), historical)
                )
        ground_truth = mapping.get("ground_truth")
        if isinstance(ground_truth, Mapping) and ground_truth.get("sha256") is not None:
            declarations.append(
                (
                    _pointer(_pointer(pointer, "ground_truth"), "sha256"),
                    str(ground_truth["sha256"]).strip(),
                    _is_historical(ground_truth, historical),
                )
            )
    return declarations


def _discover_project_root(artifacts: Sequence[Path]) -> Path:
    for artifact in artifacts:
        start = artifact if artifact.is_dir() else artifact.parent
        for candidate in (start, *start.parents):
            if (candidate / ".git").exists():
                return candidate.resolve()
    return Path.cwd().resolve()


def _resolve_reference(
    declared_path: str,
    *,
    artifact: Path,
    project_root: Path,
) -> tuple[Path | None, tuple[Path, ...]]:
    raw = Path(declared_path).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve(strict=False)
        return (candidate if candidate.is_file() else None), (candidate,)

    candidates: list[Path] = []
    for candidate in (artifact.parent / raw, project_root / raw):
        resolved = candidate.resolve(strict=False)
        if resolved not in candidates:
            candidates.append(resolved)
    existing = [candidate for candidate in candidates if candidate.is_file()]
    if len(existing) == 1:
        return existing[0], tuple(candidates)
    if len(existing) > 1:
        canonical = {candidate.resolve(strict=True) for candidate in existing}
        if len(canonical) == 1:
            return existing[0], tuple(candidates)
        return None, tuple(existing)
    return None, tuple(candidates)


def _add_issue(
    report: EvidenceContractReport,
    *,
    code: str,
    message: str,
    artifact: Path,
    pointer: str = "",
    expected: str | None = None,
    actual: str | None = None,
) -> None:
    report.issues.append(
        EvidenceContractIssue(
            code=code,
            message=message,
            artifact=str(artifact),
            pointer=pointer,
            expected=expected,
            actual=actual,
        )
    )


def validate_evidence_contract(
    artifacts: Iterable[str | Path],
    *,
    project_root: str | Path | None = None,
    expected_source: str | Path | None = None,
    recursive: bool = True,
) -> EvidenceContractReport:
    """Validate one connected set of SHA-bound JSON evidence artifacts.

    Relative reference paths are resolved against both the declaring artifact
    and ``project_root``.  If both locations exist and differ, validation fails
    as ambiguous instead of silently choosing one.

    ``expected_source`` is optional but recommended at a release gate.  When
    supplied, every source-PDF digest declared anywhere in the traversed graph
    must match those exact bytes.
    """

    roots = [Path(value).expanduser().resolve(strict=False) for value in artifacts]
    root_dir = (
        Path(project_root).expanduser().resolve(strict=False)
        if project_root is not None
        else _discover_project_root(roots)
    )
    source_path = (
        Path(expected_source).expanduser().resolve(strict=False)
        if expected_source is not None
        else None
    )
    report = EvidenceContractReport(
        roots=[str(path) for path in roots],
        project_root=str(root_dir),
        recursive=recursive,
        expected_source=str(source_path) if source_path is not None else None,
    )

    hash_cache: dict[Path, str] = {}

    def digest(path: Path) -> str:
        try:
            return hash_cache[path]
        except KeyError:
            value = sha256_file(path)
            hash_cache[path] = value
            return value

    if source_path is not None:
        if not source_path.is_file():
            _add_issue(
                report,
                code="expected_source_not_found",
                message="The expected source file does not exist.",
                artifact=source_path,
            )
        else:
            try:
                report.expected_source_sha256 = digest(source_path)
            except OSError as exc:
                _add_issue(
                    report,
                    code="expected_source_unreadable",
                    message=f"The expected source file cannot be read: {exc}",
                    artifact=source_path,
                )

    queue: deque[Path] = deque(roots)
    queued = set(roots)
    payloads: dict[Path, Mapping[str, Any]] = {}
    artifact_source_shas: dict[Path, set[str]] = {}
    edges: list[_GraphEdge] = []

    while queue:
        artifact = queue.popleft()
        if artifact in payloads:
            continue
        if not artifact.is_file():
            _add_issue(
                report,
                code="artifact_not_found",
                message="Evidence root or referenced JSON artifact does not exist.",
                artifact=artifact,
            )
            continue
        try:
            raw_payload = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            _add_issue(
                report,
                code="artifact_invalid_json",
                message=f"Evidence artifact is not valid UTF-8 JSON: {exc}",
                artifact=artifact,
            )
            continue
        if not isinstance(raw_payload, Mapping):
            _add_issue(
                report,
                code="artifact_root_not_object",
                message="Evidence artifact root must be a JSON object.",
                artifact=artifact,
            )
            continue
        payload = raw_payload
        payloads[artifact] = payload
        report.artifacts_checked.append(str(artifact))

        source_declarations = _source_sha_declarations(payload)
        valid_source_shas: set[str] = set()
        for pointer, declared_sha in source_declarations:
            if not _SHA256_RE.fullmatch(declared_sha):
                _add_issue(
                    report,
                    code="source_sha256_invalid",
                    message="Source SHA-256 must contain exactly 64 hexadecimal characters.",
                    artifact=artifact,
                    pointer=pointer,
                    actual=declared_sha,
                )
                continue
            normalized = declared_sha.lower()
            valid_source_shas.add(normalized)
            if (
                report.expected_source_sha256 is not None
                and normalized != report.expected_source_sha256
            ):
                _add_issue(
                    report,
                    code="source_sha256_mismatch",
                    message="Evidence is bound to different source bytes.",
                    artifact=artifact,
                    pointer=pointer,
                    expected=report.expected_source_sha256,
                    actual=normalized,
                )
        if len(valid_source_shas) > 1:
            _add_issue(
                report,
                code="source_sha256_conflict",
                message="One evidence artifact declares multiple source-document digests.",
                artifact=artifact,
                expected="one source SHA-256",
                actual=", ".join(sorted(valid_source_shas)),
            )
        artifact_source_shas[artifact] = valid_source_shas

        references = _extract_references(payload, artifact)
        report.references_declared += len(references)
        live_ground_truth_shas: set[str] = set()

        for reference in references:
            expected_sha = reference.expected_sha256
            if expected_sha is None:
                _add_issue(
                    report,
                    code="reference_sha256_missing",
                    message="Every live or historical reference requires a SHA-256 digest.",
                    artifact=artifact,
                    pointer=reference.pointer,
                )
                continue
            if not _SHA256_RE.fullmatch(expected_sha):
                _add_issue(
                    report,
                    code="reference_sha256_invalid",
                    message="Reference SHA-256 must contain exactly 64 hexadecimal characters.",
                    artifact=artifact,
                    pointer=reference.pointer,
                    actual=expected_sha,
                )
                continue
            expected_sha = expected_sha.lower()

            if reference.declared_path is None:
                if reference.historical:
                    report.historical_references += 1
                else:
                    _add_issue(
                        report,
                        code="reference_path_missing",
                        message=(
                            "A digest without live bytes must use path: null and an "
                            "explicit historical/superseded status."
                        ),
                        artifact=artifact,
                        pointer=reference.pointer,
                        expected=expected_sha,
                    )
                continue

            resolved, candidates = _resolve_reference(
                reference.declared_path,
                artifact=artifact,
                project_root=root_dir,
            )
            if resolved is None:
                existing_candidates = [path for path in candidates if path.is_file()]
                if len(existing_candidates) > 1:
                    _add_issue(
                        report,
                        code="reference_path_ambiguous",
                        message=(
                            "Relative reference resolves to multiple live files; use an "
                            "unambiguous sibling or project-root path."
                        ),
                        artifact=artifact,
                        pointer=reference.pointer,
                        actual=", ".join(str(path) for path in candidates),
                    )
                else:
                    code = (
                        "historical_live_path_not_found"
                        if reference.historical
                        else "reference_not_found"
                    )
                    message = (
                        "Historical bytes are unavailable but the record still advertises "
                        "a live path; replace it with path: null and keep the superseded digest."
                        if reference.historical
                        else "Referenced evidence bytes do not exist."
                    )
                    _add_issue(
                        report,
                        code=code,
                        message=message,
                        artifact=artifact,
                        pointer=reference.pointer,
                        expected=expected_sha,
                        actual=", ".join(str(path) for path in candidates),
                    )
                continue

            try:
                actual_sha = digest(resolved)
            except OSError as exc:
                _add_issue(
                    report,
                    code="reference_unreadable",
                    message=f"Referenced evidence bytes cannot be read: {exc}",
                    artifact=artifact,
                    pointer=reference.pointer,
                    expected=expected_sha,
                    actual=str(resolved),
                )
                continue
            report.live_references_checked += 1
            if actual_sha != expected_sha:
                _add_issue(
                    report,
                    code="reference_sha256_mismatch",
                    message=(
                        "Live referenced bytes do not match the declared digest; "
                        "historical status never disables this check."
                    ),
                    artifact=artifact,
                    pointer=reference.pointer,
                    expected=expected_sha,
                    actual=actual_sha,
                )
                continue

            if reference.kind == "ground_truth":
                live_ground_truth_shas.add(expected_sha)

            if resolved.suffix.casefold() == ".json" and not recursive:
                report.referenced_json_not_traversed.append(str(resolved))

            if recursive and resolved.suffix.casefold() == ".json":
                parent_source_sha = (
                    next(iter(valid_source_shas))
                    if len(valid_source_shas) == 1
                    else report.expected_source_sha256
                )
                edges.append(
                    _GraphEdge(
                        parent=artifact,
                        child=resolved,
                        pointer=reference.pointer,
                        kind=reference.kind,
                        parent_source_sha256=parent_source_sha,
                        declared_schema_version=reference.declared_schema_version,
                    )
                )
                if resolved not in queued:
                    queue.append(resolved)
                    queued.add(resolved)

        ground_truth_declarations = _ground_truth_sha_declarations(payload)
        valid_ground_truth_shas = {
            value.lower()
            for _pointer_value, value, historical in ground_truth_declarations
            if _SHA256_RE.fullmatch(value) and not historical
        }
        for pointer, declared_sha, _historical in ground_truth_declarations:
            if not _SHA256_RE.fullmatch(declared_sha):
                _add_issue(
                    report,
                    code="ground_truth_sha256_invalid",
                    message="Ground-truth SHA-256 must contain 64 hexadecimal characters.",
                    artifact=artifact,
                    pointer=pointer,
                    actual=declared_sha,
                )
        if len(valid_ground_truth_shas) > 1:
            _add_issue(
                report,
                code="ground_truth_sha256_conflict",
                message="One artifact declares incompatible ground-truth digests.",
                artifact=artifact,
                expected="one ground-truth SHA-256",
                actual=", ".join(sorted(valid_ground_truth_shas)),
            )
        for pointer, declared_sha, historical in ground_truth_declarations:
            normalized = declared_sha.lower()
            if not _SHA256_RE.fullmatch(declared_sha) or historical:
                continue
            # Object-form ground_truth references and named references are
            # already validated above.  This catches otherwise-unbound fields
            # such as a lone top-level ground_truth_sha256.
            if normalized not in live_ground_truth_shas:
                matching_reference = any(
                    reference.kind == "ground_truth"
                    and reference.expected_sha256 is not None
                    and reference.expected_sha256.lower() == normalized
                    and reference.declared_path is not None
                    for reference in references
                )
                if not matching_reference:
                    _add_issue(
                        report,
                        code="ground_truth_sha256_unbound",
                        message=(
                            "A current ground-truth digest must be paired with live "
                            "SHA-verified bytes in the same artifact."
                        ),
                        artifact=artifact,
                        pointer=pointer,
                        expected=normalized,
                    )

    for edge in edges:
        child_payload = payloads.get(edge.child)
        if child_payload is None:
            continue
        child_source_shas = artifact_source_shas.get(edge.child, set())
        if edge.parent_source_sha256 is not None:
            if edge.kind == "ground_truth" and not child_source_shas:
                _add_issue(
                    report,
                    code="ground_truth_source_sha256_missing",
                    message="Ground truth must declare the source-document SHA-256.",
                    artifact=edge.parent,
                    pointer=edge.pointer,
                    expected=edge.parent_source_sha256,
                )
            elif (
                child_source_shas
                and edge.parent_source_sha256 not in child_source_shas
            ):
                _add_issue(
                    report,
                    code=(
                        "ground_truth_source_sha256_mismatch"
                        if edge.kind == "ground_truth"
                        else "referenced_artifact_source_sha256_mismatch"
                    ),
                    message="Referenced JSON evidence is bound to a different source document.",
                    artifact=edge.parent,
                    pointer=edge.pointer,
                    expected=edge.parent_source_sha256,
                    actual=", ".join(sorted(child_source_shas)),
                )
        if edge.kind == "ground_truth" and edge.declared_schema_version is not None:
            actual_schema = child_payload.get("schema_version")
            if str(actual_schema) != edge.declared_schema_version:
                _add_issue(
                    report,
                    code="ground_truth_schema_version_mismatch",
                    message="Ground-truth schema version differs from the binding record.",
                    artifact=edge.parent,
                    pointer=edge.pointer,
                    expected=edge.declared_schema_version,
                    actual=str(actual_schema),
                )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only validation of SHA-bound evidence JSON, historical "
            "references, ground truth, and source identity"
        )
    )
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument(
        "--project-root",
        type=Path,
        help="base for project-relative evidence paths; auto-detected from .git",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="expected immutable source document; all declared source SHAs must match",
    )
    parser.add_argument(
        "--no-recursive",
        "--current-bindings-only",
        dest="no_recursive",
        action="store_true",
        help=(
            "validate only bindings declared by the root artifacts; referenced "
            "JSON is listed as untraversed and its embedded provenance is not endorsed"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = validate_evidence_contract(
        args.artifacts,
        project_root=args.project_root,
        expected_source=args.source,
        recursive=not args.no_recursive,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised through main tests.
    raise SystemExit(main())
