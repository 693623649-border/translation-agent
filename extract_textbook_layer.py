"""Import a born-digital PDF text layer into ``book_pipeline`` checkpoints.

This is deliberately a page-text importer, not a book-specific TOC generator.
After importing, use ``book_pipeline.py --phase toc`` (or ``--toc-json``) to
create the book's structure.  The former hard-coded Japanese title table was a
one-book migration aid and is no longer applied to unrelated PDFs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import fitz

from book_pipeline import (
    PageRecord,
    PageStore,
    detect_language,
    save_page_record,
)


TEXT_LAYER_MODEL = "text-layer/pymupdf-v1"


@dataclass(frozen=True)
class HeadingSpec:
    title: str
    level: int
    aliases: tuple[str, ...] = ()
    replacement: str | None = None
    pdf_page: int | None = None


@dataclass(frozen=True)
class HeadingIssue:
    title: str
    matches: tuple[str, ...]


def _normalized_heading(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _parse_heading_item(title: str, raw: Any) -> HeadingSpec:
    if isinstance(raw, int):
        level = raw
        aliases: Sequence[Any] = ()
    elif isinstance(raw, dict):
        level = raw.get("level")
        aliases = raw.get("aliases", ())
    else:
        raise ValueError(
            f"Heading {title!r} must map to a Markdown level or an object."
        )
    if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 6:
        raise ValueError(f"Heading {title!r} level must be an integer from 1 to 6.")
    if not isinstance(aliases, (list, tuple)) or not all(
        isinstance(alias, str) and alias.strip() for alias in aliases
    ):
        raise ValueError(f"Heading {title!r} aliases must be an array of strings.")
    replacement = raw.get("replacement") if isinstance(raw, dict) else None
    if replacement is not None and (
        not isinstance(replacement, str) or not replacement.strip()
    ):
        raise ValueError(f"Heading {title!r} replacement must be a non-empty string.")
    pdf_page = raw.get("pdf_page") if isinstance(raw, dict) else None
    if pdf_page is not None and (
        not isinstance(pdf_page, int) or isinstance(pdf_page, bool) or pdf_page < 1
    ):
        raise ValueError(f"Heading {title!r} pdf_page must be a positive integer.")
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Heading title cannot be empty.")
    return HeadingSpec(
        title=clean_title,
        level=level,
        aliases=tuple(alias.strip() for alias in aliases),
        replacement=replacement.strip() if replacement is not None else None,
        pdf_page=pdf_page,
    )


def load_heading_specs(path: Path) -> list[HeadingSpec]:
    """Load either a title mapping or an array of heading objects."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    specs: list[HeadingSpec] = []
    if isinstance(payload, dict):
        for title, raw in payload.items():
            specs.append(_parse_heading_item(str(title), raw))
    elif isinstance(payload, list):
        for position, raw in enumerate(payload, start=1):
            if not isinstance(raw, dict):
                raise ValueError(f"Heading item {position} must be an object.")
            title = raw.get("title")
            if not isinstance(title, str):
                raise ValueError(f"Heading item {position} requires a string title.")
            specs.append(_parse_heading_item(title, raw))
    else:
        raise ValueError("Headings JSON must be an object mapping or an array.")
    if not specs:
        raise ValueError("Headings JSON contains no headings.")

    owners: dict[str, str] = {}
    for spec in specs:
        for candidate in (spec.title, *spec.aliases):
            normalized = _normalized_heading(candidate)
            owner = owners.get(normalized)
            if owner is not None and owner != spec.title:
                raise ValueError(
                    f"Heading candidate {candidate!r} is shared by {owner!r} "
                    f"and {spec.title!r}."
                )
            owners[normalized] = spec.title
    return specs


def _strip_expected_leading_page_number(
    text: str,
    *,
    pdf_page: int,
    offset: int | None,
) -> str:
    if offset is None:
        return text.strip()
    expected = pdf_page - offset
    lines = text.splitlines()
    first_nonblank = next(
        (index for index, line in enumerate(lines) if line.strip()),
        None,
    )
    if first_nonblank is None:
        return ""
    candidate = lines[first_nonblank].strip()
    if re.fullmatch(r"\d+", candidate) and int(candidate) == expected:
        del lines[first_nonblank]
    return "\n".join(lines).strip()


def annotate_unique_headings(
    page_texts: list[str],
    specs: Sequence[HeadingSpec],
) -> tuple[list[str], list[HeadingIssue]]:
    """Mark only exact, standalone heading lines occurring once in the PDF."""

    candidate_to_spec: dict[str, HeadingSpec] = {}
    for spec in specs:
        for candidate in (spec.title, *spec.aliases):
            candidate_to_spec[_normalized_heading(candidate)] = spec

    locations: dict[HeadingSpec, list[tuple[int, int]]] = {
        spec: [] for spec in specs
    }
    split_pages = [text.splitlines() for text in page_texts]
    for page_index, lines in enumerate(split_pages):
        for line_index, line in enumerate(lines):
            spec = candidate_to_spec.get(_normalized_heading(line.strip()))
            if spec is not None and (
                spec.pdf_page is None or spec.pdf_page == page_index + 1
            ):
                locations[spec].append((page_index, line_index))

    issues: list[HeadingIssue] = []
    for spec in specs:
        matches = locations[spec]
        if len(matches) != 1:
            issues.append(
                HeadingIssue(
                    title=spec.title,
                    matches=tuple(
                        f"PDF {page_index + 1}, line {line_index + 1}"
                        for page_index, line_index in matches
                    ),
                )
            )
            continue
        page_index, line_index = matches[0]
        original = split_pages[page_index][line_index].strip()
        rendered = spec.replacement or original
        split_pages[page_index][line_index] = f"{'#' * spec.level} {rendered}"
    return ["\n".join(lines).strip() for lines in split_pages], issues


def reflow_logical_text(text: str) -> str:
    """Join visual wraps inside paragraphs while retaining structural breaks."""

    output_blocks: list[str] = []

    def flush_paragraph(lines: list[str]) -> None:
        if not lines:
            return
        joined = lines[0].strip()
        for line in lines[1:]:
            following = line.strip()
            if not following:
                continue
            if joined.endswith("-"):
                joined = joined[:-1] + following
            else:
                joined += " " + following
        if joined:
            output_blocks.append(joined)

    for raw_block in re.split(r"\n\s*\n+", text.strip()):
        paragraph_lines: list[str] = []
        for raw_line in raw_block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if re.match(r"^#{1,6}\s+", line):
                flush_paragraph(paragraph_lines)
                paragraph_lines = []
                output_blocks.append(line)
            else:
                paragraph_lines.append(line)
        flush_paragraph(paragraph_lines)
    return "\n\n".join(output_blocks).strip()


def extract_page_texts(
    pdf_path: Path,
    *,
    sort: bool = False,
    strip_leading_page_number_offset: int | None = None,
) -> list[str]:
    """Pre-scan every page and reject PDFs without a complete text layer."""

    if strip_leading_page_number_offset is not None and (
        strip_leading_page_number_offset < 0
    ):
        raise ValueError("Leading page-number offset cannot be negative.")
    page_texts: list[str] = []
    failures: list[str] = []
    with fitz.open(pdf_path) as document:
        if document.page_count < 1:
            raise ValueError("Input PDF contains no pages.")
        for page_index, page in enumerate(document):
            text = page.get_text("text", sort=sort).strip()
            if not text:
                content = "image-only" if page.get_images(full=True) else "empty-text"
                failures.append(f"PDF {page_index + 1} ({content})")
                continue
            page_texts.append(
                _strip_expected_leading_page_number(
                    text,
                    pdf_page=page_index + 1,
                    offset=strip_leading_page_number_offset,
                )
            )
    if failures:
        preview = ", ".join(failures[:20])
        suffix = "..." if len(failures) > 20 else ""
        raise ValueError(
            "The PDF does not have a complete embedded text layer; no checkpoints "
            f"were written. Missing pages: {preview}{suffix}. Use vision OCR instead."
        )
    return page_texts


def _preflight_existing_pages(
    output_dir: Path,
    page_texts: Sequence[str],
    *,
    force: bool,
) -> dict[int, PageRecord]:
    store = PageStore(output_dir)
    existing = {record.pdf_page: record for record in store.load_all()}
    extras = sorted(page for page in existing if page > len(page_texts) or page < 1)
    if extras:
        raise ValueError(
            "Output contains page checkpoints outside the input PDF range: "
            f"{extras[:20]}. Use a fresh output directory."
        )
    differences = [
        page
        for page, text in enumerate(page_texts, start=1)
        if page in existing and existing[page].text != text
    ]
    if differences and not force:
        raise ValueError(
            "Existing page text differs from this extraction on pages "
            f"{differences[:20]}. Re-run with --force to replace those pages "
            "and invalidate their model overlays, or use a fresh output directory."
        )
    return existing


def extract_text_layer(
    pdf: str | Path,
    output_dir: str | Path,
    *,
    force: bool = False,
    sort: bool = False,
    reflow: bool = False,
    headings_json: str | Path | None = None,
    strip_leading_page_number_offset: int | None = None,
) -> tuple[int, int, list[HeadingIssue]]:
    """Extract pages, preserving fresh overlays when source text is unchanged."""

    pdf_path = Path(pdf).expanduser().resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Input must be an existing PDF: {pdf_path}")
    destination = Path(output_dir).expanduser().resolve()
    page_texts = extract_page_texts(
        pdf_path,
        sort=sort,
        strip_leading_page_number_offset=strip_leading_page_number_offset,
    )
    issues: list[HeadingIssue] = []
    if headings_json is not None:
        specs = load_heading_specs(Path(headings_json).expanduser().resolve())
        page_texts, issues = annotate_unique_headings(page_texts, specs)
    if reflow:
        page_texts = [reflow_logical_text(text) for text in page_texts]

    # All source- and heading-level validation happens before the first write.
    existing = _preflight_existing_pages(destination, page_texts, force=force)
    written = 0
    preserved = 0
    option_notes = [
        "embedded-text-layer",
        "extractor=PyMuPDF",
        f"sort={str(sort).lower()}",
        f"reflow={str(reflow).lower()}",
    ]
    if strip_leading_page_number_offset is not None:
        option_notes.append(
            f"leading-page-number-offset={strip_leading_page_number_offset}"
        )
    if headings_json is not None:
        option_notes.append("headings=exact-unique-lines")
    notes = "; ".join(option_notes)
    for pdf_page, text in enumerate(page_texts, start=1):
        cached = existing.get(pdf_page)
        if cached is not None and cached.text == text:
            # Do not rewrite an identical source: PageStore.save(new record)
            # would correctly but needlessly discard fresh translations.
            preserved += 1
            continue
        save_page_record(
            destination,
            PageRecord(
                pdf_page=pdf_page,
                text=text,
                language=detect_language(text),
                notes=notes,
                ocr_model=TEXT_LAYER_MODEL,
            ),
        )
        written += 1
    return written, preserved, issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Import a complete embedded PDF text layer as book_pipeline page "
            "checkpoints without calling an OCR model."
        )
    )
    parser.add_argument("input", help="Born-digital PDF input.")
    parser.add_argument(
        "legacy_output",
        nargs="?",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("-o", "--output-dir", default=None, help="Pipeline output directory.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace pages whose extracted source text differs; fresh overlays on those pages are invalidated.",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help=(
            "Use PyMuPDF visual-position sorting. The default preserves the PDF's "
            "logical content-stream order and usually retains paragraph wrapping better."
        ),
    )
    parser.add_argument(
        "--reflow",
        action="store_true",
        help=(
            "Join single newlines inside logical text blocks, removing a trailing "
            "line-break hyphen or otherwise inserting one space. Blank-line paragraph "
            "breaks and Markdown headings are retained. Disabled by default."
        ),
    )
    parser.add_argument(
        "--headings-json",
        default=None,
        help="Optional exact-title mapping/array used to mark unique standalone heading lines.",
    )
    parser.add_argument(
        "--strip-leading-page-number-offset",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Remove the first standalone numeric line only when it exactly equals "
            "PDF page minus N. Disabled by default."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output_dir and args.legacy_output:
        parser.error("Use either -o/--output-dir or the legacy second positional output, not both.")
    output_dir = args.output_dir or args.legacy_output
    if not output_dir:
        parser.error("-o/--output-dir is required.")
    if args.legacy_output:
        print(
            "[warning] The positional output argument is deprecated; use -o/--output-dir. "
            "This generic importer no longer creates a hard-coded toc.json.",
            file=sys.stderr,
        )
    try:
        written, preserved, issues = extract_text_layer(
            args.input,
            output_dir,
            force=args.force,
            sort=args.sort,
            reflow=args.reflow,
            headings_json=args.headings_json,
            strip_leading_page_number_offset=args.strip_leading_page_number_offset,
        )
    except (OSError, ValueError, json.JSONDecodeError, fitz.FileDataError) as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for issue in issues:
        locations = ", ".join(issue.matches) if issue.matches else "not found"
        print(
            f"[heading-unmatched] title={issue.title!r} matches={len(issue.matches)} "
            f"locations={locations}",
            file=sys.stderr,
        )
    print(
        f"[done] text-layer pages={written + preserved} written={written} "
        f"preserved={preserved} heading_issues={len(issues)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
