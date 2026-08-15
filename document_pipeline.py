"""Unified product CLI over the Graph and semantic document adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from product_contracts import APP_VERSION, CONTRACT_SCHEMA_VERSION, RunSpec
from run_execution_service import (
    SEMANTIC_CACHE_DIRNAME,
    execute_runspec,
    plan_runspec,
)


def _json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _result_is_blocked(payload: Mapping[str, Any]) -> bool:
    return bool(
        payload.get("release_blocked") is True
        or payload.get("status") in {"blocked", "failed"}
    )


def _load_spec(path: Path) -> RunSpec:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("RunSpec JSON root must be an object")
    return RunSpec.from_dict(payload)


def _spec_from_args(args: argparse.Namespace) -> RunSpec:
    if getattr(args, "spec", None):
        return _load_spec(args.spec)
    source = getattr(args, "source", None)
    return RunSpec(
        source=source,
        source_mode=getattr(args, "source_mode", "scanned-pdf"),
        output_dir=getattr(args, "output_dir", "outputs/book"),
        phase=getattr(args, "phase", "all"),
        title=getattr(args, "title", None),
        author=getattr(args, "author", None),
        target_language=getattr(args, "target_language", "简体中文"),
        config=getattr(args, "config", None),
        recipe=getattr(args, "recipe", None),
        targets=tuple(getattr(args, "target", ()) or ()),
        translate=not getattr(args, "no_translate", False),
        verify=not getattr(args, "no_verify", False),
        options={
            "toc_source": getattr(args, "toc_source", "pipeline"),
            "text_pdf_sort": getattr(args, "text_pdf_sort", False),
            "text_pdf_reflow": getattr(args, "text_pdf_reflow", False),
            "translation_profile": getattr(args, "translation_profile", None),
            "glossary": str(args.glossary) if getattr(args, "glossary", None) else None,
        },
    )


def plan_spec(spec: RunSpec) -> dict[str, Any]:
    """Compatibility wrapper around the single product compiler."""

    return plan_runspec(spec).to_dict()


def run_pdf_spec(spec: RunSpec) -> dict[str, Any]:
    if spec.source_mode == "epub":
        raise ValueError("run_pdf_spec requires scanned-pdf or text-pdf source_mode")
    return execute_runspec(spec).to_dict()


def run_spec(spec: RunSpec) -> dict[str, Any]:
    """Execute a RunSpec through the same service used by product clients."""

    return execute_runspec(spec).to_dict()


def ingest_spec(spec: RunSpec) -> dict[str, Any]:
    if spec.source is None:
        raise ValueError("ingest requires a source file")
    source = Path(spec.source).expanduser().resolve()
    output = Path(spec.output_dir).expanduser().resolve()
    if spec.source_mode == "epub":
        from epub_semantic_import import import_epub

        result = import_epub(source, output)
    elif spec.source_mode == "text-pdf":
        from born_digital_pdf_import import import_born_digital_pdf

        result = import_born_digital_pdf(source, output)
    else:
        compile_spec = RunSpec.from_dict(
            {
                **spec.to_dict(),
                "phase": "compile",
                "targets": ["chapters.semantic"],
            }
        )
        return run_pdf_spec(compile_spec)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        **result,
    }


def translate_spec(spec: RunSpec, *, prepare_only: bool = False) -> int:
    output = Path(spec.output_dir).expanduser().resolve()
    units = output / "semantic" / "translation-units.jsonl"
    if not units.is_file():
        raise FileNotFoundError(f"semantic translation units not found: {units}")
    target = output / "semantic" / (
        "prepared.jsonl" if prepare_only else "translations.jsonl"
    )
    from semantic_translation_runner import main as semantic_main

    argv = [
        "prepare" if prepare_only else "run",
        str(units),
        "-o",
        str(target),
        "--target-language",
        spec.target_language,
        "--cache-dir",
        str(output / SEMANTIC_CACHE_DIRNAME),
    ]
    if spec.config:
        argv.extend(["--config", str(spec.config)])
    profile = spec.options.get("translation_profile")
    if profile:
        argv.extend(["--translation-profile", str(profile)])
    glossary = spec.options.get("glossary")
    if glossary:
        argv.extend(["--glossary", str(glossary)])
    return semantic_main(argv)


def apply_spec(spec: RunSpec, translations: Path | None = None) -> dict[str, Any]:
    output = Path(spec.output_dir).expanduser().resolve()
    source = translations or output / "semantic" / "translations.jsonl"
    if spec.source_mode == "epub":
        from epub_semantic_import import apply_translations
    elif spec.source_mode == "text-pdf":
        from born_digital_pdf_import import apply_translations
    else:
        raise ValueError("semantic apply supports epub or text-pdf sources")
    from semantic_translation_runner import load_glossary

    glossary_path = spec.options.get("glossary")
    glossary = (
        load_glossary(Path(str(glossary_path)).expanduser())
        if glossary_path
        else {}
    )
    result = apply_translations(
        output,
        source,
        target_language=spec.target_language,
        glossary=glossary,
    )
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        **result,
    }


def publish_spec(spec: RunSpec, formats: tuple[str, ...]) -> dict[str, Any]:
    if not formats:
        formats = ("epub", "docx")
    invalid = sorted(set(formats) - {"epub", "docx"})
    if invalid:
        raise ValueError(f"unsupported publication formats: {invalid}")
    from translation_agent_api import GraphRunRequest, RunRequest, run_graph

    results: list[dict[str, Any]] = []
    for format_name in formats:
        artifact = f"publication.{format_name}"
        graph_request = GraphRunRequest(
            pipeline=RunRequest(
                output_dir=spec.output_dir,
                phase=format_name,
                title=spec.title,
                author=spec.author,
                target_language=spec.target_language,
                verify_publication=False,
            ),
            targets=(artifact,),
        )
        run = run_graph(graph_request)
        results.append(
            {
                "format": format_name,
                "run_id": run.run_id,
                "executed": list(run.executed),
                "skipped": list(run.skipped),
            }
        )
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "status": "passed",
        "release_ready": False,
        "publication_status": "draft",
        "reason": (
            "the standalone publish command does not execute a release verifier; "
            "use the unified run command with publication.epub_report (or another "
            "Graph publication report target) for a release-ready artifact"
        ),
        "formats": results,
    }


def _review_error_report(
    output_dir: Path,
    *,
    mode: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "mode": mode,
        "output_dir": str(output_dir.expanduser().resolve()),
        "status": "blocked",
        "release_blocked": True,
        "reconstruction": None,
        "review_policy": None,
        "issue_set": None,
        "decision_log": None,
        "summary": None,
        "error": {
            "code": "semantic_review_unavailable",
            "type": type(error).__name__,
            "message": str(error),
        },
    }


def _review_report_from_resolution(
    context: Any,
    resolution: Any,
    *,
    mode: str,
) -> dict[str, Any]:
    resolved = resolution.to_dict()
    report: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "mode": mode,
        "output_dir": str(context.output_dir),
        "status": resolution.status,
        "release_blocked": resolution.release_blocked,
        "reconstruction": {
            "path": str(context.reconstruction_path),
            "sha256": context.reconstruction_sha256,
        },
        "review_policy": resolved["review_policy"],
        "issue_set": resolved["issue_set"],
        "decision_log": resolved["decision_log"],
        "summary": resolved["summary"],
        "error": None,
    }
    if mode == "review":
        report["issues"] = resolved["issues"]
        report["resolution"] = resolved
    return report


def review_status(
    output_dir: Path,
    *,
    include_issues: bool = False,
) -> dict[str, Any]:
    """Return current semantic review status without changing product state."""

    if type(include_issues) is not bool:
        raise ValueError("include_issues must be a boolean")
    output = output_dir.expanduser().resolve()
    try:
        import semantic_review_policy

        context = semantic_review_policy.collect_reconstruction_review(output)
        if not (
            context.decision_log_path.exists()
            or context.decision_log_path.is_symlink()
        ):
            raise FileNotFoundError(
                f"semantic review decision log is missing: "
                f"{context.decision_log_path}; run review first"
            )
        decision_lock = context.decision_log_path.with_name(
            context.decision_log_path.name + ".lock"
        )
        if not (decision_lock.exists() or decision_lock.is_symlink()):
            raise FileNotFoundError(
                f"semantic review decision lock is missing: {decision_lock}; "
                "run review first"
            )
        current, resolution = semantic_review_policy.resolve_semantic_review(
            output,
            expected_reconstruction_sha256=context.reconstruction_sha256,
        )
        report = _review_report_from_resolution(
            current,
            resolution,
            mode="status",
        )
        if include_issues:
            report["issues"] = resolution.to_dict()["issues"]
        return report
    except (OSError, ValueError) as exc:
        return _review_error_report(output, mode="status", error=exc)


def review_report(
    output_dir: Path,
    *,
    issue_id: str | None = None,
    reviewer: str | None = None,
    decision: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """View the complete review or append one policy-authorized decision."""

    fields = (issue_id, reviewer, decision, reason)
    if any(value is not None for value in fields) and not all(
        value is not None for value in fields
    ):
        raise ValueError(
            "recording a review decision requires --issue-id, --reviewer, "
            "--decision, and --reason together"
        )
    output = output_dir.expanduser().resolve()
    try:
        import semantic_review_policy

        if all(value is not None for value in fields):
            artifact = semantic_review_policy.record_semantic_review_decision(
                output,
                issue_id=str(issue_id),
                reviewer=str(reviewer),
                decision=str(decision),
                reason=str(reason),
            )
            action = "decision-recorded"
        else:
            artifact = semantic_review_policy.refresh_semantic_review(
                output,
                create_decision_log=True,
            )
            action = "viewed"
        context = semantic_review_policy.collect_reconstruction_review(
            output,
            expected_reconstruction_sha256=(
                artifact.resolution.reconstruction_sha256
            ),
        )
        report = _review_report_from_resolution(
            context,
            artifact.resolution,
            mode="review",
        )
        report["action"] = action
        report["review_audit"] = {
            "path": str(artifact.audit_path),
            "sha256": artifact.audit_sha256,
            "snapshot_path": (
                str(artifact.snapshot_path)
                if artifact.snapshot_path is not None
                else None
            ),
        }
        return report
    except (OSError, ValueError) as exc:
        report = _review_error_report(output, mode="review", error=exc)
        report["action"] = "blocked"
        report["issues"] = []
        report["resolution"] = None
        report["review_audit"] = None
        return report


def _add_spec_arguments(parser: argparse.ArgumentParser, *, source: bool = True) -> None:
    parser.add_argument("--spec", type=Path, help="versioned RunSpec JSON")
    if source:
        parser.add_argument("source", nargs="?")
    parser.add_argument("-o", "--output-dir", default="outputs/book")
    parser.add_argument(
        "--source-mode",
        choices=("scanned-pdf", "text-pdf", "epub"),
        default="scanned-pdf",
    )
    parser.add_argument("--phase", default="all")
    parser.add_argument("--title")
    parser.add_argument("--author")
    parser.add_argument("--target-language", default="简体中文")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--recipe", type=Path)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--translation-profile")
    parser.add_argument("--glossary", type=Path)
    parser.add_argument("--toc-source", choices=("pipeline", "outline"), default="pipeline")
    parser.add_argument("--text-pdf-sort", action="store_true")
    parser.add_argument("--text-pdf-reflow", action="store_true")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--no-verify", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translation-agent",
        description="Local-first semantic translation and publication pipeline.",
    )
    parser.add_argument("--version", action="version", version=APP_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run", "ingest"):
        child = subparsers.add_parser(name)
        _add_spec_arguments(child)
    translate = subparsers.add_parser("translate")
    _add_spec_arguments(translate, source=False)
    translate.add_argument("--prepare-only", action="store_true")
    apply_parser = subparsers.add_parser("apply")
    _add_spec_arguments(apply_parser, source=False)
    apply_parser.add_argument("--translations", type=Path)
    publish = subparsers.add_parser("publish")
    _add_spec_arguments(publish, source=False)
    publish.add_argument("--format", action="append", default=[])
    status = subparsers.add_parser("status")
    status.add_argument("-o", "--output-dir", default="outputs/book")
    review = subparsers.add_parser("review")
    review.add_argument("-o", "--output-dir", default="outputs/book")
    review.add_argument("--issue-id")
    review.add_argument("--reviewer")
    review.add_argument(
        "--decision",
        choices=("accepted", "rejected", "replaced"),
    )
    review.add_argument("--reason")
    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("doctor_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Delegate before the outer parser consumes options.  ``argparse.REMAINDER``
    # otherwise requires an awkward ``--`` sentinel and then forwards that
    # sentinel to the inner parser, making valid doctor flags unusable.
    if raw_argv and raw_argv[0] == "doctor":
        from doctor import main as doctor_main

        return doctor_main(raw_argv[1:])
    args = build_parser().parse_args(raw_argv)
    if args.command == "status":
        report = review_status(Path(args.output_dir))
        _json(report)
        return 1 if report["release_blocked"] else 0
    if args.command == "review":
        report = review_report(
            Path(args.output_dir),
            issue_id=args.issue_id,
            reviewer=args.reviewer,
            decision=args.decision,
            reason=args.reason,
        )
        _json(report)
        return 1 if report["release_blocked"] else 0

    spec = _spec_from_args(args)
    if args.command == "plan":
        _json(plan_spec(spec))
        return 0
    if args.command == "run":
        _json(run_spec(spec))
        return 0
    if args.command == "ingest":
        imported = ingest_spec(spec)
        _json(imported)
        return 1 if _result_is_blocked(imported) else 0
    if args.command == "translate":
        return translate_spec(spec, prepare_only=args.prepare_only)
    if args.command == "apply":
        _json(apply_spec(spec, args.translations))
        return 0
    if args.command == "publish":
        _json(publish_spec(spec, tuple(args.format)))
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def cli(argv: list[str] | None = None) -> int:
    """Console boundary with concise, traceback-free operational errors."""

    try:
        return main(argv)
    except (OSError, ValueError) as exc:
        print(
            f"[translation-agent-error] {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
