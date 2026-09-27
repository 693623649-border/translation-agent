#!/usr/bin/env python3
"""Structural self-check for the data-pipeline graphs.

Builds both built-in DAGs (scanned-book and born-digital EPUB) without
executing any model or file stage, then validates:

- the full-graph topological plan resolves (no cycles, no dangling deps);
- every artifact is provided by at most one node;
- context-seeded values (e.g. ``pipeline.argv``) satisfy the requires that
  reference them;
- optional-node variants assemble and plan cleanly (proofread, outline TOC,
  text-pdf, EPUB translation modes, injected verifier);
- recipe TOMLs reference only known core node names.

Usage: python3 tools/selfcheck_pipeline_nodes.py [--json]
Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import traceback
import zipfile
from pathlib import Path

from pipeline_graph.book import (
    BookGraphOptions,
    NODE_PROOFREAD,
    NODE_TEXT_EXTRACT,
    NODE_TOC_OUTLINE,
    KNOWN_NODE_NAMES,
    prepare_book_graph,
)
from pipeline_graph.epub import (
    EpubGraphOptions,
    NODE_APPLY,
    NODE_TRANSLATE,
    NODE_TRANSLATIONS_IMPORT,
    NODE_VERIFY_EPUB,
    prepare_epub_graph,
)
from pipeline_graph.recipe import load_recipe

RECIPES_DIR = Path("recipes")
EPUB_ALL_NODES = 11


def _plan_full(prepared) -> tuple[str, ...]:
    """All-node plan: structural validation independent of run targets."""

    return tuple(node.name for node in prepared.graph.plan(
        available=prepared.context.values
    ))


def _check_graph(prepared, label: str) -> dict[str, object]:
    """Validate one prepared graph and describe its nodes."""
    findings: dict[str, object] = {"graph": label, "issues": [], "nodes": []}
    issues: list[str] = findings["issues"]  # type: ignore[assignment]

    try:
        full_plan = _plan_full(prepared)
        default_plan = tuple(node.name for node in prepared.plan())
    except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
        issues.append(f"plan() failed: {exc!r}")
        return findings

    available = set(prepared.context.values)
    providers: dict[str, list[str]] = {}
    for node in prepared.graph.nodes:
        for value in node.provides:
            providers.setdefault(value, []).append(node.name)
        if not callable(node.handler):
            issues.append(f"{node.name}: handler is not callable")

    for value, owners in sorted(providers.items()):
        if len(owners) > 1:
            issues.append(f"artifact {value!r} provided by multiple nodes: {owners}")

    provided = set(providers) | available
    for node in prepared.graph.nodes:
        missing = set(node.requires) - provided
        if missing:
            issues.append(f"{node.name}: unprovided requires {sorted(missing)}")

    if len(set(full_plan)) != len(full_plan):
        issues.append("full plan repeats a node (cycle?)")
    graph_names = {node.name for node in prepared.graph.nodes}
    if set(full_plan) != graph_names:
        issues.append(
            f"full plan does not cover graph: missing {sorted(graph_names - set(full_plan))}"
        )

    disabled = getattr(prepared.options, "disabled_nodes", frozenset())
    node_rows = []
    for node in prepared.graph.nodes:
        node_rows.append(
            {
                "name": node.name,
                "requires": sorted(node.requires),
                "provides": sorted(node.provides),
                "version": node.version,
                "in_default_run": node.name in default_plan,
                "description": node.description,
            }
        )
    findings["nodes"] = node_rows
    findings["full_plan"] = list(full_plan)
    findings["default_plan"] = list(default_plan)
    if disabled:
        findings["disabled"] = sorted(disabled)
    return findings


def _book_graph(source: Path, output: Path, **options):
    base = BookGraphOptions(load_dotenv=False)
    from dataclasses import replace

    return prepare_book_graph(
        [str(source), "-o", str(output)], options=replace(base, **options)
    )


def _check_book_variants(root: Path) -> list[str]:
    issues: list[str] = []
    source = root / "source.pdf"
    cases = {
        "include_proofread": {"include_proofread": True},
        "toc_source=outline": {"toc_source": "outline"},
        "source_mode=text-pdf": {"source_mode": "text-pdf"},
        "disable-epub+docx": {"disabled_nodes": frozenset({"core.publish.epub", "core.publish.docx"})},
    }
    expect = {
        "include_proofread": NODE_PROOFREAD,
        "toc_source=outline": NODE_TOC_OUTLINE,
        "source_mode=text-pdf": NODE_TEXT_EXTRACT,
    }
    for label, kwargs in cases.items():
        try:
            prepared = _book_graph(source, root / f"book-{label}", **kwargs)
            names = set(_plan_full(prepared))
            if label in expect and expect[label] not in names:
                issues.append(f"book variant {label}: {expect[label]} not assembled")
            if label == "source_mode=text-pdf" and "core.pages.ocr" in names:
                issues.append("book variant text-pdf: OCR node should be excluded")
            if label == "disable-epub+docx":
                for gone in ("core.publish.epub", "core.publish.docx"):
                    if gone in names:
                        issues.append(f"book variant disable: {gone} still assembled")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"book variant {label}: prepare failed: {exc!r}")
    return issues


def _check_epub_variants(root: Path) -> list[str]:
    issues: list[str] = []
    source = root / "source.epub"
    translations = root / "translations.jsonl"
    translations.write_text(
        '{"unit_id": "u1", "target": "你好"}\n', encoding="utf-8"
    )

    modes = {
        "run": EpubGraphOptions(
            translation_mode="run",
            translation_request=lambda text: text,
            translation_request_fingerprint="selfcheck",
        ),
        "apply": EpubGraphOptions(
            translation_mode="apply",
            translations_path=translations,
        ),
    }
    expect = {
        "run": {NODE_TRANSLATE},
        "apply": {NODE_TRANSLATIONS_IMPORT, NODE_APPLY},
    }
    for label, options in modes.items():
        try:
            prepared = prepare_epub_graph(
                source, root / f"epub-{label}", options=options
            )
            names = set(_plan_full(prepared))
            for node in expect[label]:
                if node not in names:
                    issues.append(f"epub variant {label}: {node} not assembled")
        except Exception as exc:  # noqa: BLE001
            issues.append(f"epub variant {label}: prepare failed: {exc!r}")

    try:
        prepared = prepare_epub_graph(
            source,
            root / "epub-report",
            options=EpubGraphOptions(
                target_artifacts=frozenset({"publication.epub_report"})
            ),
        )
        names = set(_plan_full(prepared))
        if NODE_VERIFY_EPUB not in names:
            issues.append("epub: release gate absent when report artifact targeted")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"epub report-target plan failed: {exc!r}")
    return issues


def _recipe_findings() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if not RECIPES_DIR.is_dir():
        return [{"recipe": None, "issues": ["recipes/ directory not found"]}]
    for path in sorted(RECIPES_DIR.glob("*.toml")):
        entry: dict[str, object] = {
            "recipe": path.name,
            "issues": [],
            "enable": [],
            "disable": [],
            "targets": [],
        }
        try:
            recipe = load_recipe(path)
            entry["enable"] = list(recipe.enable)
            entry["disable"] = list(recipe.disable)
            entry["targets"] = list(recipe.targets)
            unknown = [n for n in recipe.enable if n not in KNOWN_NODE_NAMES]
            if unknown:
                entry["issues"].append(f"enables unknown nodes: {unknown}")
        except Exception as exc:  # noqa: BLE001
            entry["issues"].append(f"load_recipe failed: {exc!r}")
        results.append(entry)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    findings: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source_pdf = root / "source.pdf"
        source_pdf.write_bytes(b"%PDF-1.4 dummy")
        source_epub = root / "source.epub"
        with zipfile.ZipFile(source_epub, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", "<container/>")
            archive.writestr("OEBPS/content.xhtml", "<html><body>ok</body></html>")

        try:
            findings["book"] = _check_graph(
                _book_graph(source_pdf, root / "book-default"),
                "book (scanned-pdf default)",
            )
            findings["book"]["issues"].extend(_check_book_variants(root))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            findings["book"] = {"issues": [traceback.format_exc(limit=4)]}

        try:
            findings["epub"] = _check_graph(
                prepare_epub_graph(source_epub, root / "epub-default"),
                "epub (semantic, mode=none)",
            )
            findings["epub"]["issues"].extend(_check_epub_variants(root))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            findings["epub"] = {"issues": [traceback.format_exc(limit=4)]}

    findings["recipes"] = _recipe_findings()

    issues_total = sum(
        len(findings[section].get("issues", []))  # type: ignore[union-attr]
        for section in ("book", "epub")
    ) + sum(
        len(entry.get("issues", []))  # type: ignore[union-attr]
        for entry in findings["recipes"]  # type: ignore[union-attr]
    )

    if args.json:
        print(json.dumps(findings, ensure_ascii=False, indent=2))
    else:
        for section in ("book", "epub"):
            block = findings[section]
            print(f"== {block.get('graph', section)} ==")  # type: ignore[union-attr]
            for node in block.get("nodes", []):  # type: ignore[union-attr]
                marker = "*" if node["in_default_run"] else " "
                req = ",".join(node["requires"]) or "-"
                prov = ",".join(node["provides"]) or "-"
                print(f" {marker} {node['name']}  [{req}] -> [{prov}]")
            print(f"    default-run closure: {len(block.get('default_plan', []))} nodes (* above)")  # type: ignore[union-attr]
            for issue in block.get("issues", []):  # type: ignore[union-attr]
                print(f"  !! {issue}")
        print("== recipes ==")
        for entry in findings["recipes"]:  # type: ignore[union-attr]
            status = "OK" if not entry.get("issues") else "FAIL"
            print(f"  [{status}] {entry.get('recipe')}")
            for issue in entry.get("issues", []):  # type: ignore[union-attr]
                print(f"    !! {issue}")
        print(
            f"\nselfcheck: {'PASSED' if issues_total == 0 else f'{issues_total} ISSUE(S)'}"
        )
    return 0 if issues_total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
