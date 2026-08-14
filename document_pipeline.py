"""Unified product CLI over the Graph and semantic document adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from product_contracts import APP_VERSION, CONTRACT_SCHEMA_VERSION, RunSpec


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


def _graph_request(spec: RunSpec):
    if spec.source_mode not in {"scanned-pdf", "text-pdf"}:
        raise ValueError("Graph PDF request requires scanned-pdf or text-pdf source_mode")
    if spec.source is None and spec.phase not in {"epub", "docx", "verify", "status"}:
        raise ValueError(f"phase {spec.phase!r} requires a PDF source")
    from translation_agent_api import GraphRunRequest, RunRequest

    options = dict(spec.options)
    request = RunRequest(
        input_pdf=spec.source,
        output_dir=spec.output_dir,
        phase=spec.phase,
        config=spec.config,
        translation_profile=options.get("translation_profile"),
        title=spec.title,
        author=spec.author,
        target_language=spec.target_language,
        translate_non_chinese=spec.translate,
        verify_publication=spec.verify,
    )
    return GraphRunRequest(
        pipeline=request,
        recipe=spec.recipe,
        targets=spec.targets,
        source_mode=spec.source_mode,
        toc_source=str(options.get("toc_source") or "pipeline"),
        text_pdf_sort=bool(options.get("text_pdf_sort")),
        text_pdf_reflow=bool(options.get("text_pdf_reflow")),
    )


def plan_spec(spec: RunSpec) -> dict[str, Any]:
    if spec.source_mode == "epub":
        nodes = [
            "core.source.epub.inspect",
            "core.source.epub.import",
        ]
        if spec.translate:
            nodes.extend(
                [
                    "core.semantic.translate.prepare",
                    "core.semantic.translate.run",
                    "core.semantic.verify",
                    "core.semantic.apply",
                ]
            )
        nodes.extend(
            [
                "core.chapters.load",
                "core.reconstruct.semantic",
                "core.publication.sanitize",
                "core.publish.requested",
            ]
        )
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "source_mode": spec.source_mode,
            "targets": list(spec.targets),
            "nodes": nodes,
            "release_profile": "draft-until-epub-native-verifier",
        }
    from translation_agent_api import prepare_graph

    prepared = prepare_graph(_graph_request(spec))
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "source_mode": spec.source_mode,
        "targets": sorted(prepared.targets),
        "nodes": [
            {
                "name": node.name,
                "version": node.version,
                "requires": sorted(node.requires),
                "provides": sorted(node.provides),
                "cache": node.cache,
            }
            for node in prepared.plan()
        ],
    }


def run_pdf_spec(spec: RunSpec) -> dict[str, Any]:
    from translation_agent_api import run_graph

    result = run_graph(_graph_request(spec))
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "status": "passed",
        "run_id": result.run_id,
        "plan": list(result.plan),
        "executed": list(result.executed),
        "skipped": list(result.skipped),
        "state": str(result.state_path),
        "events": str(result.events_path),
    }


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
        str(output / ".translation-cache"),
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
            "EPUB and standalone semantic adapters do not yet have a compatible "
            "native release verifier; artifacts remain drafts until they pass a "
            "Graph publication verification profile"
        ),
        "formats": results,
    }


def review_status(output_dir: Path) -> dict[str, Any]:
    audit_dir = output_dir.expanduser().resolve() / "audit"
    audits: list[dict[str, Any]] = []
    blocked = False
    if audit_dir.is_dir():
        for path in sorted(audit_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            is_blocked = bool(
                payload.get("release_blocked") is True
                or payload.get("status") in {"blocked", "failed"}
            )
            blocked = blocked or is_blocked
            audits.append(
                {
                    "path": str(path),
                    "status": payload.get("status"),
                    "release_blocked": is_blocked,
                }
            )
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "status": "blocked" if blocked else "passed",
        "release_blocked": blocked,
        "audits": audits,
    }


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
    for name in ("review", "status"):
        child = subparsers.add_parser(name)
        child.add_argument("-o", "--output-dir", default="outputs/book")
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
    if args.command in {"review", "status"}:
        report = review_status(Path(args.output_dir))
        _json(report)
        return 1 if report["release_blocked"] else 0

    spec = _spec_from_args(args)
    if args.command == "plan":
        _json(plan_spec(spec))
        return 0
    if args.command == "run":
        if spec.source_mode == "epub":
            if spec.verify:
                raise ValueError(
                    "EPUB-native release verification is not available; "
                    "rerun with --no-verify to explicitly authorize draft artifacts"
                )
            imported = ingest_spec(spec)
            _json(imported)
            if _result_is_blocked(imported):
                raise ValueError(
                    "EPUB semantic ingest is blocked; review the reconstruction "
                    "audit before translation or publication"
                )
            if spec.translate:
                exit_code = translate_spec(spec)
                if exit_code:
                    return exit_code
                _json(apply_spec(spec))
            _json(publish_spec(spec, ("epub", "docx")))
            return 0
        _json(run_pdf_spec(spec))
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
