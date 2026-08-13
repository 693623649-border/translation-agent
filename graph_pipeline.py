"""CLI for the dependency-graph execution engine.

All existing ``book_pipeline.py`` options remain valid.  Graph-only switches
are stripped before each built-in node delegates to the legacy phase code.
"""

from __future__ import annotations

import argparse
import json
import sys

from pipeline_graph import GraphError
from pipeline_graph.book import (
    BookGraphConfigurationError,
    BookGraphOptions,
    NODE_PROOFREAD,
    NODE_TEXT_EXTRACT,
    NODE_TOC_OUTLINE,
    prepare_book_graph,
)
from pipeline_graph.recipe import Recipe, RecipeError, load_recipe


def build_graph_control_parser() -> argparse.ArgumentParser:
    # Abbreviation must be disabled: legacy ``--force`` must not be consumed
    # as an ambiguous prefix of ``--force-node`` / ``--force-graph``.
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument(
        "--recipe",
        help="Strict data-only Graph Recipe TOML (separate from model Profile TOML).",
    )
    parser.add_argument(
        "--target",
        action="append",
        default=[],
        help="Run the dependency closure for this artifact (repeatable).",
    )
    parser.add_argument(
        "--enable-node",
        action="append",
        default=[],
        help="Enable an optional built-in node, or a plugin node declared by --recipe.",
    )
    parser.add_argument(
        "--disable-node",
        action="append",
        default=[],
        help="Remove a built-in node; planning fails if a selected target still needs it.",
    )
    parser.add_argument(
        "--include-proofread",
        action="store_true",
        help="Insert OCR proofreading before translation/TOC/compile.",
    )
    parser.add_argument(
        "--toc-source",
        choices=("pipeline", "outline"),
        default="pipeline",
        help="Use existing manual/LLM TOC behavior or the PDF's embedded outline.",
    )
    parser.add_argument(
        "--source-mode",
        choices=("scanned-pdf", "text-pdf"),
        default=None,
        help=(
            "Explicitly select vision OCR or a complete embedded PDF text layer; "
            "the graph never guesses from PDF contents."
        ),
    )
    parser.add_argument(
        "--text-pdf-sort",
        action="store_true",
        help="Use PyMuPDF visual-position sorting in text-pdf mode.",
    )
    parser.add_argument(
        "--text-pdf-reflow",
        action="store_true",
        help="Join visual line wraps inside paragraphs in text-pdf mode.",
    )
    parser.add_argument(
        "--text-pdf-strip-leading-page-number-offset",
        type=int,
        metavar="N",
        help=(
            "In text-pdf mode, remove a leading numeric line only when it equals "
            "PDF page minus N."
        ),
    )
    parser.add_argument(
        "--allow-plugin",
        action="append",
        default=[],
        help="Explicitly allow one installed translation_agent.graph_nodes entry point.",
    )
    parser.add_argument(
        "--force-node",
        action="append",
        default=[],
        help="Ignore the graph cache for one named node (repeatable).",
    )
    parser.add_argument(
        "--force-graph",
        action="store_true",
        help="Ignore every graph-node cache; page stages still use their own checkpoints unless --force is also set.",
    )
    parser.add_argument(
        "--adopt-existing-output",
        action="store_true",
        help="Bind a complete unbound legacy checkpoint directory to the supplied PDF after explicit confirmation.",
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Print the resolved DAG without executing it.",
    )
    parser.add_argument(
        "--graph-help",
        action="store_true",
        help="Show graph-only options; use --help for the inherited pipeline options.",
    )
    return parser


def _print_graph_help() -> None:
    print("Graph execution options (all book_pipeline.py options are also accepted):")
    print(build_graph_control_parser().format_help())
    print("\nLegacy pipeline options:")
    # Import lazily so normal planning remains cheap.
    from book_pipeline import build_parser

    print(build_parser().format_help())


def _plan_payload(prepared: object) -> dict:
    plan = prepared.plan()
    return {
        "targets": sorted(prepared.targets),
        "nodes": [
            {
                "name": node.name,
                "version": node.version,
                "requires": sorted(node.requires),
                "provides": sorted(node.provides),
                "resources": sorted(node.resources),
                "cache": node.cache,
                "description": node.description,
            }
            for node in plan
        ],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    controls, pipeline_argv = build_graph_control_parser().parse_known_args(argv)
    if controls.graph_help:
        _print_graph_help()
        return 0
    if not pipeline_argv:
        _print_graph_help()
        return 2

    recipe = load_recipe(controls.recipe) if controls.recipe else None
    enabled = set(controls.enable_node)
    disabled = set(controls.disable_node)
    if recipe is not None and (enabled or disabled or controls.target):
        recipe = Recipe(
            id=recipe.id,
            targets=tuple(controls.target) or recipe.targets,
            enable=tuple(dict.fromkeys((*recipe.enable, *enabled))),
            disable=tuple(dict.fromkeys((*recipe.disable, *disabled))),
            required_plugins=recipe.required_plugins,
        )
    elif recipe is None:
        unsupported = enabled - {NODE_PROOFREAD, NODE_TOC_OUTLINE}
        if unsupported:
            raise BookGraphConfigurationError(
                "only proofreading and outline TOC are optional without a Recipe; "
                "external nodes require required_plugins: "
                f"{sorted(unsupported)}"
            )
    include_proofread = controls.include_proofread or NODE_PROOFREAD in enabled
    force_nodes = set(controls.force_node)
    recipe_selects_text_pdf = bool(
        recipe is not None and NODE_TEXT_EXTRACT in recipe.enable
    )
    if controls.source_mode == "scanned-pdf" and recipe_selects_text_pdf:
        raise BookGraphConfigurationError(
            "explicit --source-mode scanned-pdf conflicts with Recipe enabling "
            "core.pages.text_extract"
        )
    source_mode = controls.source_mode or (
        "text-pdf" if recipe_selects_text_pdf else "scanned-pdf"
    )
    options = BookGraphOptions(
        include_proofread=include_proofread,
        toc_source=(
            "outline" if NODE_TOC_OUTLINE in enabled else controls.toc_source
        ),
        disabled_nodes=frozenset(disabled),
        target_artifacts=frozenset(controls.target),
        force_nodes=frozenset(force_nodes),
        force_all=controls.force_graph,
        adopt_existing_output=controls.adopt_existing_output,
        source_mode=source_mode,
        text_pdf_sort=controls.text_pdf_sort,
        text_pdf_reflow=controls.text_pdf_reflow,
        text_pdf_strip_leading_page_number_offset=(
            controls.text_pdf_strip_leading_page_number_offset
        ),
    )
    prepared = prepare_book_graph(
        pipeline_argv,
        options=options,
        recipe=recipe,
        plugin_allowlist=controls.allow_plugin,
    )
    if controls.plan:
        print(json.dumps(_plan_payload(prepared), ensure_ascii=False, indent=2))
        return 0

    result = prepared.execute()
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": result.run_id,
                "plan": list(result.plan),
                "executed": list(result.executed),
                "skipped": list(result.skipped),
                "state": str(result.state_path),
                "events": str(result.events_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BookGraphConfigurationError, GraphError, RecipeError, OSError, ValueError) as exc:
        print(f"[graph-error] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
