"""English PDF -> Chinese reader edition, using the repository publishers."""
from __future__ import annotations

import contextlib
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import book_pipeline as book
from publication_verifier import verify_publication
from rag_apparatus import annotate_apparatus
import rag_knowledge_base


MANIFEST = "english-pdf-ingest.json"
PLAN = "selection-plan.json"
STAGES = {"extract", "ocr", "toc", "translate", "plan", "draft", "publish", "verify", "register"}
ROLES = {"selection", "editorial", "structure"}
SOURCE_EDITOR_LABEL = re.compile(r"\[\s*Ed(?=\.|\s|\]|\||$)\.?(?:\s*[J|\]])?", re.I)


class GateError(ValueError):
    """A reviewed source or publication prerequisite has not been met."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise GateError(message)


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1 << 20), b""):
            digest.update(part)
    return digest.hexdigest()


def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"JSON root must be an object: {path}")
    return value


def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _single_line(value: Any, field: str) -> str:
    require(isinstance(value, str) and value.strip(), f"{field} must be a nonempty string")
    result = value.strip()
    require(not any(char in result for char in "\r\n\x00"), f"{field} must be one line")
    return result


def _workspace(args: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    workspace = Path(_single_line(args.get("workspace"), "workspace")).expanduser().resolve()
    path = workspace / MANIFEST
    require(path.is_file(), "Run en_pdf_prepare before this stage")
    manifest = read(path)
    source = Path(str(manifest.get("source") or "")).resolve()
    require(source.is_file() and sha(source) == manifest.get("source_sha256"), "Source PDF changed or is missing")
    return workspace, manifest


def _source(manifest: dict[str, Any]) -> Path:
    return Path(str(manifest["source"])).resolve()


def _config(args: dict[str, Any], manifest: dict[str, Any]) -> Path:
    value = args.get("config") or manifest.get("config") or ROOT / "pipeline.toml"
    path = Path(value).expanduser().resolve()
    require(path.is_file(), f"Pipeline TOML not found: {path}")
    return path


def _pages(value: Any, count: int, *, required: bool) -> list[int]:
    if value is None and not required:
        return list(range(1, count + 1))
    require(isinstance(value, list) and value, "pages must be a nonempty list")
    require(all(type(page) is int and 1 <= page <= count for page in value), "pages must be valid one-based PDF page numbers")
    require(len(value) == len(set(value)), "pages must be unique")
    return value


def _ranges(pages: list[int]) -> list[tuple[int, int]]:
    ordered = sorted(pages)
    groups: list[tuple[int, int]] = []
    for page in ordered:
        if groups and page == groups[-1][1] + 1:
            groups[-1] = (groups[-1][0], page)
        else:
            groups.append((page, page))
    return groups


def prepare(args: dict[str, Any]) -> dict[str, Any]:
    source = Path(_single_line(args.get("source"), "source")).expanduser().resolve()
    workspace = Path(_single_line(args.get("workspace"), "workspace")).expanduser().resolve()
    title = _single_line(args.get("title"), "title")
    author = _single_line(args.get("author"), "author")
    require(source.is_file() and source.suffix.casefold() == ".pdf", "An existing source PDF is required")
    with pymupdf.open(source) as pdf:
        count = len(pdf)
        require(count > 0, "Source PDF has no pages")
        samples = sorted({1, (count + 1) // 2, count})
        text_layer = {str(page): len(pdf[page - 1].get_text().strip()) for page in samples}
    config = str(_config(args, {})) if args.get("config") else None
    identity = {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": sha(source),
        "page_count": count,
        "title": title,
        "author": author,
        "config": config,
    }
    target = workspace / MANIFEST
    if target.exists():
        old = read(target)
        require(all(old.get(key) == value for key, value in identity.items()), "Workspace is bound to another source or book identity")
    else:
        require(not workspace.exists() or not any(workspace.iterdir()), "Refusing a nonempty unrelated workspace")
        write(target, identity)
    return {"ok": True, "status": "prepared", "workspace": str(workspace),
            "source_sha256": identity["source_sha256"], "page_count": count,
            "text_layer_sample_characters": text_layer}


def _plan(workspace: Path, manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    path = workspace / PLAN
    require(path.is_file(), "Selection plan is missing; run stage=plan")
    plan = read(path)
    toc_path = workspace / "toc.json"
    require(toc_path.is_file(), "Reviewed TOC is missing")
    toc = book.load_toc(toc_path)
    require(plan.get("source_sha256") == manifest["source_sha256"], "Selection plan source checksum is stale")
    require(plan.get("toc_sha256") == sha(toc_path), "Selection plan TOC checksum is stale")
    _validate_plan(plan, toc, manifest, workspace)
    return plan, toc


def _validate_plan(
    plan: dict[str, Any], toc: dict[str, Any], manifest: dict[str, Any],
    workspace: Path | None = None,
) -> None:
    require(plan.get("schema_version") == 1, "Selection plan schema_version must be 1")
    require(plan.get("source_sha256") == manifest["source_sha256"], "Selection plan source checksum differs")
    entries = plan.get("entries")
    require(isinstance(entries, list) and entries, "Selection plan entries are required")
    by_id: dict[str, dict[str, Any]] = {}
    for item in entries:
        require(isinstance(item, dict), "Each plan entry must be an object")
        entry_id = _single_line(item.get("id"), "plan entry id")
        require(entry_id not in by_id, f"Duplicate plan entry: {entry_id}")
        require(item.get("role") in ROLES, f"Invalid role for {entry_id}")
        if item["role"] == "selection":
            title = _single_line(item.get("reader_title"), f"Chinese reader_title for {entry_id}")
            require(bool(re.search(r"[\u3400-\u9fff]", title)), f"reader_title must be Chinese: {entry_id}")
            _single_line(item.get("source_review"), f"source_review for {entry_id}")
        by_id[entry_id] = item
    toc_entries = toc["entries"]
    toc_ids = {item["id"] for item in toc_entries}
    require(set(by_id) == toc_ids, "Plan must classify every TOC entry exactly once")
    require(any(item["role"] == "selection" for item in by_id.values()), "Plan has no selected text")
    if plan.get("publication_title"):
        title = _single_line(plan["publication_title"], "publication_title")
        require(bool(re.search(r"[\u3400-\u9fff]", title)), "publication_title must be Chinese")
    overrides = plan.get("reviewed_overrides") or []
    require(isinstance(overrides, list) and len(overrides) == len(set(overrides)), "reviewed_overrides must be unique IDs")
    require(set(overrides).issubset({key for key, value in by_id.items() if value["role"] == "selection"}),
            "reviewed_overrides may name selected entries only")
    if workspace is not None:
        for entry_id in overrides:
            require((workspace / "reviewed_chapters" / f"{entry_id}.md").is_file(),
                    f"Reviewed chapter override missing: {entry_id}")
    clips = plan.get("clips") or []
    require(isinstance(clips, list), "Plan clips must be a list")
    clips_by_page: dict[int, dict[str, Any]] = {}
    for clip in clips:
        require(isinstance(clip, dict), "Each clip must be an object")
        page = clip.get("pdf_page")
        require(type(page) is int and 1 <= page <= manifest["page_count"], "Clip page is invalid")
        require(page not in clips_by_page, f"Only one reviewed clip may target PDF page {page}")
        clips_by_page[page] = clip
        require(clip.get("keep_from") or clip.get("keep_before"), f"Clip {page} has no boundary anchor")
        _single_line(clip.get("translated_sha256"), f"translation checksum for clip {page}")
        _single_line(clip.get("source_review"), f"source review for clip {page}")
    paragraph = plan.get("paragraph_layout")
    if paragraph is not None:
        require(isinstance(paragraph, dict), "paragraph_layout must be an object")
        path = Path(_single_line(paragraph.get("path"), "paragraph layout path")).expanduser().resolve()
        require(path.is_file() and sha(path) == paragraph.get("sha256"), "Paragraph layout audit is missing or stale")
    note_reviews = plan.get("note_reviews") or []
    require(isinstance(note_reviews, list), "note_reviews must be a list")
    reviewed_pages: set[int] = set()
    for note_review in note_reviews:
        require(isinstance(note_review, dict), "Each note review must be an object")
        page = note_review.get("pdf_page")
        require(type(page) is int and 1 <= page <= manifest["page_count"], "Note review PDF page is invalid")
        require(page not in reviewed_pages, f"Duplicate note review for PDF page {page}")
        reviewed_pages.add(page)
        _single_line(note_review.get("source_review"), f"note source review for PDF page {page}")
    policy = plan.get("inline_editor_note_policy", "separate")
    require(policy in {"separate", "preserve_marked"}, "inline_editor_note_policy is invalid")
    if policy == "preserve_marked":
        _single_line(plan.get("inline_editor_note_review"), "inline editor note review")
    starts: dict[int, list[str]] = {}
    for toc_entry in toc_entries:
        if by_id[toc_entry["id"]]["role"] == "selection" and toc_entry.get("pdf_page"):
            starts.setdefault(int(toc_entry["pdf_page"]), []).append(toc_entry["id"])
    for page, ids in starts.items():
        if len(ids) > 1:
            for entry_id in ids:
                require(entry_id in overrides,
                        f"Shared selection page {page} needs reviewed override for {entry_id}")
    shared = plan.get("shared_selection_pages") or []
    require(isinstance(shared, list), "shared_selection_pages must be a list")
    for item in shared:
        require(isinstance(item, dict), "Each shared selection page must be an object")
        page = item.get("pdf_page")
        ids = item.get("selection_ids")
        require(type(page) is int and 1 <= page <= manifest["page_count"], "Shared selection page is invalid")
        require(isinstance(ids, list) and len(ids) >= 2 and len(ids) == len(set(ids)), "Shared selection IDs are invalid")
        _single_line(item.get("source_review"), f"source review for shared PDF page {page}")
        for entry_id in ids:
            require(entry_id in overrides and by_id.get(entry_id, {}).get("role") == "selection",
                    f"Shared PDF page {page} needs a selected, reviewed override for {entry_id}")
    for position, toc_entry in enumerate(toc_entries):
        entry_id = toc_entry["id"]
        if by_id[entry_id]["role"] != "selection":
            continue
        page = toc_entry.get("pdf_page")
        if page is None:
            continue
        for other_index, other in enumerate(toc_entries):
            if other["id"] == entry_id or other.get("pdf_page") != page:
                continue
            if by_id[other["id"]]["role"] != "editorial":
                continue
            required_anchor = "keep_from" if other_index < position else "keep_before"
            require(entry_id in overrides or clips_by_page.get(page, {}).get(required_anchor),
                    f"Selection/editorial shared PDF page {page} needs {required_anchor} or reviewed override")
        next_entry = next(
            (other for other in toc_entries[position + 1:]
             if other.get("pdf_page") is not None
             and int(other["pdf_page"]) >= int(page)
             and int(other["level"]) <= int(toc_entry["level"])),
            None,
        )
        if next_entry is not None:
            end_page = int(next_entry["pdf_page"])
            next_role = by_id[next_entry["id"]]["role"]
            if next_role == "editorial" and end_page != int(page):
                require(entry_id in overrides or clips_by_page.get(end_page, {}).get("keep_before"),
                        f"Selection ending on editorial PDF page {end_page} needs keep_before or reviewed override")


def set_plan(args: dict[str, Any], workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    source = Path(_single_line(args.get("plan_file"), "plan_file")).expanduser().resolve()
    require(source.is_file(), "Reviewed plan_file is missing")
    plan = read(source)
    toc_path = workspace / "toc.json"
    require(toc_path.is_file(), "Run the TOC stage or provide a reviewed toc.json first")
    require(plan.get("toc_sha256") == sha(toc_path), "Plan TOC checksum differs")
    _validate_plan(plan, book.load_toc(toc_path), manifest, workspace)
    write(workspace / PLAN, plan)
    roles = {item["id"]: item["role"] for item in plan["entries"]}
    return {"ok": True, "status": "planned", "workspace": str(workspace),
            "plan_sha256": sha(workspace / PLAN),
            "roles": {role: sum(value == role for value in roles.values()) for role in sorted(ROLES)},
            "clips": len(plan.get("clips") or [])}


def extract(args: dict[str, Any], workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    pages = _pages(args.get("pages"), manifest["page_count"], required=True)
    existing = {record.pdf_page: record for record in book.load_page_records(workspace)}
    written: list[int] = []
    reused: list[int] = []
    needs_ocr: list[int] = []
    with pymupdf.open(_source(manifest)) as pdf:
        for page_number in pages:
            page = pdf[page_number - 1]
            text = page.get_text(sort=False).strip()
            if len(text) < 10:
                needs_ocr.append(page_number)
                continue
            if page_number in existing:
                require(existing[page_number].text == text, f"Page {page_number} checkpoint differs; use a fresh workspace or review it")
                reused.append(page_number)
                continue
            blocks = [
                {"bbox": [round(float(value), 2) for value in block[:4]], "text": str(block[4])}
                for block in page.get_text("blocks")
                if len(block) >= 5 and str(block[4]).strip()
            ]
            record = book.PageRecord(
                pdf_page=page_number,
                text=text,
                language="en",
                notes=json.dumps({"engine": "embedded-pdf-text", "pdf_page": page_number,
                                  "reading_order": "source-stream", "blocks": blocks}, ensure_ascii=False),
                ocr_model="embedded-text-pymupdf-v1",
            )
            book.save_page_record(workspace, record)
            written.append(page_number)
    return {"ok": True, "status": "extracted", "workspace": str(workspace),
            "written_pages": written, "reused_pages": reused, "needs_ocr_pages": needs_ocr}


def _run_pipeline(workspace: Path, manifest: dict[str, Any], args: dict[str, Any], stage: str) -> dict[str, Any]:
    source = _source(manifest)
    if stage == "toc" and args.get("toc_file"):
        toc_source = Path(_single_line(args["toc_file"], "toc_file")).expanduser().resolve()
        require(toc_source.is_file(), "Reviewed TOC file is missing")
        toc = book.load_toc(toc_source)
        require(all(
            item.get("pdf_page") is None or 1 <= int(item["pdf_page"]) <= manifest["page_count"]
            for item in toc["entries"]
        ), "Reviewed TOC has PDF pages outside the source")
        target = workspace / "toc.json"
        if target.is_file() and read(target) != toc:
            backup = workspace / "audit" / "toc-history" / f"{sha(target)}.json"
            backup.parent.mkdir(parents=True, exist_ok=True)
            if not backup.exists():
                shutil.copy2(target, backup)
        write(target, toc)
        return {"ok": True, "status": "completed", "stage": "toc",
                "workspace": str(workspace), "method": "reviewed-file",
                "entries": len(toc["entries"]), "toc_sha256": sha(target)}
    command = [
        sys.executable, str(ROOT / "book_pipeline.py"), str(source),
        "--output-dir", str(workspace), "--phase", stage,
        "--config", str(_config(args, manifest)),
    ]
    if stage == "toc":
        pages = args.get("toc_pages")
        require(pages is not None, "toc_pages must name the reviewed PDF TOC pages")
        values = _pages(pages, manifest["page_count"], required=True)
        command.extend(["--toc-pages", ",".join(str(page) for page in values)])
        intervals: list[tuple[int, int]] = [(1, manifest["page_count"])]
    else:
        required = stage == "ocr"
        values = _pages(args.get("pages"), manifest["page_count"], required=required)
        intervals = _ranges(values)
        if stage == "translate":
            command.extend(["--translate-non-chinese", "--translation-source-language", "en"])
        if stage == "ocr":
            require(len(values) <= 100, "OCR accepts at most 100 exact pages per call; resume in batches")
            command.extend(["--ocr-reading-direction", "horizontal"])
    seconds = args.get("timeout_seconds", 1800)
    require(type(seconds) is int and 1 <= seconds <= 3600, "timeout_seconds must be 1–3600")
    for start, end in intervals:
        invocation = list(command)
        if stage != "toc":
            invocation.extend(["--start-page", str(start), "--end-page", str(end)])
        try:
            completed = subprocess.run(
                invocation, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=seconds,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GateError(f"{stage} timed out on exact PDF pages {start}–{end}; inspect the backend job before retrying") from exc
        require(completed.returncode == 0, f"{stage} failed on exact PDF pages {start}–{end} (exit {completed.returncode})")
    return {"ok": True, "status": "completed", "stage": stage, "workspace": str(workspace),
            "page_ranges": [list(interval) for interval in intervals]}


def _role_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in plan["entries"]}


def _selected_toc(toc: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    roles = _role_map(plan)
    result = {key: value for key, value in toc.items() if key != "entries"}
    result["entries"] = []
    for original in toc["entries"]:
        item = dict(original)
        role = roles[item["id"]]["role"]
        if role == "selection":
            item["source_title"] = item.get("source_title") or item["title"]
            item["title"] = roles[item["id"]]["reader_title"]
            item["kind"] = "section"
        elif item["kind"] in {"section", "subsection"} or role == "editorial":
            item["kind"] = "other"
        result["entries"].append(item)
    return result


def _compiled_records(
    workspace: Path, plan: dict[str, Any],
) -> tuple[list[book.PageRecord], list[dict[str, Any]]]:
    records = book.load_page_records(workspace)
    by_page = {record.pdf_page: record for record in records}
    audit: list[dict[str, Any]] = []
    for clip in plan.get("clips") or []:
        page = int(clip["pdf_page"])
        record = by_page.get(page)
        require(record is not None and record.translation_is_fresh, f"Clip page {page} has no fresh Chinese translation")
        before = record.translated_text
        require(text_sha(before) == clip["translated_sha256"], f"Clip page {page} translation changed since review")
        after = before
        removed_prefix = removed_suffix = ""
        if clip.get("keep_from"):
            anchor = str(clip["keep_from"])
            require(after.count(anchor) == 1, f"Clip page {page} keep_from is not unique")
            offset = after.index(anchor)
            removed_prefix, after = after[:offset], after[offset:]
        if clip.get("keep_before"):
            anchor = str(clip["keep_before"])
            require(after.count(anchor) == 1, f"Clip page {page} keep_before is not unique")
            offset = after.index(anchor)
            removed_suffix, after = after[offset:], after[:offset]
        require(after.strip(), f"Clip removed all translated prose on page {page}")
        by_page[page] = replace(record, translated_text=after.strip())
        audit.append({
            "pdf_page": page, "before_sha256": text_sha(before),
            "after_sha256": text_sha(after.strip()),
            "removed_prefix": removed_prefix, "removed_suffix": removed_suffix,
            "source_review": clip["source_review"],
        })
    return [by_page[record.pdf_page] for record in records], audit


def _candidate_key(
    manifest: dict[str, Any], plan_path: Path, records: list[book.PageRecord],
    reviewed_dir: Path,
) -> str:
    payload = {
        "source": manifest["source_sha256"], "plan": sha(plan_path),
        "publisher": {
            path.name: sha(path) for path in (
                ROOT / "book_pipeline.py", ROOT / "publication_semantics.py",
                ROOT / "publication_verifier.py", Path(__file__),
            )
        },
        "pages": [
            [record.pdf_page, record.text_sha256, text_sha(record.translated_text)]
            for record in records
        ],
        "reviewed": [
            [path.name, sha(path)] for path in sorted(reviewed_dir.glob("*.md"))
        ] if reviewed_dir.is_dir() else [],
    }
    return text_sha(json.dumps(payload, sort_keys=True, ensure_ascii=False))[:16]


def _candidate(
    workspace: Path, manifest: dict[str, Any], plan: dict[str, Any],
    toc: dict[str, Any],
) -> tuple[Path, list[book.PageRecord], dict[str, Any]]:
    records, clips = _compiled_records(workspace, plan)
    require(len(records) == manifest["page_count"], "Every PDF page needs an OCR/text checkpoint before publication")
    selected_toc = _selected_toc(toc, plan)
    reviewed = workspace / "reviewed_chapters"
    for entry_id in plan.get("reviewed_overrides") or []:
        require((reviewed / f"{entry_id}.md").is_file(), f"Reviewed chapter override missing: {entry_id}")
    key = _candidate_key(manifest, workspace / PLAN, records, reviewed)
    candidate = workspace / "editions" / key
    candidate.mkdir(parents=True, exist_ok=True)
    source_pages = workspace / "pages"
    target_pages = candidate / "pages"
    target_pages.mkdir(parents=True, exist_ok=True)
    for path in source_pages.iterdir():
        if not path.is_file():
            continue
        target = target_pages / path.name
        if not target.exists():
            shutil.copy2(path, target)
    if reviewed.is_dir():
        target_reviewed = candidate / "reviewed_chapters"
        target_reviewed.mkdir(parents=True, exist_ok=True)
        for path in reviewed.glob("*.md"):
            target = target_reviewed / path.name
            if not target.exists():
                shutil.copy2(path, target)
    write(candidate / "toc.json", selected_toc)
    write(candidate / "audit" / "selection-plan.json", plan)
    write(candidate / "audit" / "editorial-clips.json", {"source_sha256": manifest["source_sha256"], "items": clips})
    if plan.get("paragraph_layout"):
        source_layout = Path(plan["paragraph_layout"]["path"]).expanduser().resolve()
        shutil.copy2(source_layout, candidate / "audit" / "selection-paragraph-layout.json")
    editorial = [
        {"id": entry["id"], "title": entry["title"], "pdf_page": entry.get("pdf_page"),
         "printed_page": entry.get("printed_page"), "role": "editorial"}
        for entry in toc["entries"] if _role_map(plan)[entry["id"]]["role"] == "editorial"
    ]
    write(candidate / "audit" / "editorial-material-index.json",
          {"source_sha256": manifest["source_sha256"], "items": editorial})
    state = read(workspace / MANIFEST)
    state["active_candidate"] = str(candidate.relative_to(workspace))
    write(workspace / MANIFEST, state)
    return candidate, records, selected_toc


def _publication_title(manifest: dict[str, Any], plan: dict[str, Any]) -> str:
    value = plan.get("publication_title") or manifest["title"]
    return _single_line(value, "publication_title")


def _artifact_name(title: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", title).strip(" .")
    require(bool(clean), "Publication title cannot form a file name")
    return clean[:120]


def _compile(
    workspace: Path, manifest: dict[str, Any],
) -> tuple[Path, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    plan, toc = _plan(workspace, manifest)
    candidate, records, selected_toc = _candidate(workspace, manifest, plan, toc)
    chapter_manifest, rows = book.compile_chapters(
        _source(manifest), candidate, records, selected_toc,
        granularity="section", publication_title=_publication_title(manifest, plan),
        require_translation=True, expected_translation_identity=None,
    )
    selected_pages = {
        page for item in chapter_manifest
        for page in range(int(item["pdf_page"]), int(item["end_pdf_page"]) + 1)
    }
    by_page = {record.pdf_page: record for record in records}
    census = [
        {
            "pdf_page": page,
            "source_editor_labels": len(SOURCE_EDITOR_LABEL.findall(by_page[page].text)),
            "translated_editor_labels": (
                by_page[page].translated_text.count("[编者注]")
                + by_page[page].translated_text.count("[编者]")
            ),
        }
        for page in sorted(selected_pages) if page in by_page
    ]
    mismatches = [
        item for item in census
        if item["source_editor_labels"] != item["translated_editor_labels"]
    ]
    write(candidate / "audit" / "editorial-note-census.json", {
        "source_sha256": manifest["source_sha256"],
        "items": census, "mismatches": mismatches,
    })
    reviewed_pages = {item["pdf_page"] for item in plan.get("note_reviews") or []}
    unreviewed = [item["pdf_page"] for item in mismatches if item["pdf_page"] not in reviewed_pages]
    require(not unreviewed, f"Editor note count differs without source review on PDF pages {unreviewed[:20]}")
    inline_labels = sum(row["content"].count("[编者注]") for row in rows)
    write(candidate / "audit" / "editorial-note-reader-gate.json", {
        "inline_editor_labels_in_kb": inline_labels,
        "policy": plan.get("inline_editor_note_policy", "separate"),
    })
    require(
        inline_labels == 0 or plan.get("inline_editor_note_policy") == "preserve_marked",
        f"{inline_labels} inline editor-note labels remain in KB content; review source notes or choose preserve_marked with evidence",
    )
    book.write_knowledge_base(candidate / "knowledge_base.jsonl", rows)
    return candidate, chapter_manifest, rows, selected_toc, plan


def draft(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    candidate, chapters, rows, _toc, plan = _compile(workspace, manifest)
    ids = [item["id"] for item in chapters]
    report = verify_publication(
        candidate, source_pdf=_source(manifest), book_title=_publication_title(manifest, plan),
        expected_language="zh-CN", require_translation=True, chapter_ids=ids,
        report_path=candidate / "audit" / "chapter-report.json",
    )
    ok = report["summary"]["failed"] == 0
    return {"ok": ok, "status": "draft" if ok else "failed",
            "candidate": str(candidate), "chapters": len(chapters), "kb_chunks": len(rows),
            "chapter_gate": report["status"],
            "failed_checks": [item["id"] for item in report["checks"] if item["status"] == "failed"]}


def publish(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    existing_plan, _toc = _plan(workspace, manifest)
    current_records, _clips = _compiled_records(workspace, existing_plan)
    existing_key = _candidate_key(manifest, workspace / PLAN, current_records, workspace / "reviewed_chapters")
    existing = workspace / "editions" / existing_key
    if (existing / "audit" / "publication-build.json").is_file():
        try:
            build, _plan_data = _build_current(workspace, manifest, existing)
        except (GateError, FileNotFoundError):
            pass
        else:
            state = read(workspace / MANIFEST)
            state["active_candidate"] = str(existing.relative_to(workspace))
            write(workspace / MANIFEST, state)
            return {
                "ok": True, "status": "built", "candidate": str(existing),
                "chapters": build["chapter_count"], "kb_chunks": build["chunk_count"],
                "editorial_items": len(read(existing / "audit" / "editorial-material-index.json")["items"]),
                "reused": True,
            }
    candidate, chapters, rows, toc, plan = _compile(workspace, manifest)
    title = _publication_title(manifest, plan)
    stem = _artifact_name(title)
    chapter_dir = candidate / "chapters"
    book.build_epub(candidate / f"{stem}.epub", chapter_dir, chapters, book_title=title, language="zh-CN")
    book.build_docx(candidate / f"{stem}.docx", chapter_dir, chapters, book_title=title, author=manifest["author"])
    book.build_bookmarked_pdf(_source(manifest), candidate / f"{stem}_带目录.pdf", toc)
    rag_knowledge_base.initialize_rag_manifest(candidate / "knowledge_base.jsonl")
    annotate_apparatus(candidate / "knowledge_base.jsonl")
    files = [
        "chapters.json", "knowledge_base.jsonl",
        f"{stem}.docx", f"{stem}.epub", f"{stem}_带目录.pdf",
    ]
    files.extend(str(path.relative_to(candidate)) for path in sorted(chapter_dir.glob("*.md")))
    write(candidate / "audit" / "publication-build.json", {
        "source_sha256": manifest["source_sha256"], "plan_sha256": sha(workspace / PLAN),
        "artifacts": {name: sha(candidate / name) for name in files},
        "chapter_count": len(chapters), "chunk_count": len(rows),
    })
    return {"ok": True, "status": "built", "candidate": str(candidate),
            "chapters": len(chapters), "kb_chunks": len(rows),
            "editorial_items": len(read(candidate / "audit" / "editorial-material-index.json")["items"])}


def _active_candidate(workspace: Path, manifest: dict[str, Any]) -> Path:
    relative = manifest.get("active_candidate")
    require(isinstance(relative, str) and relative, "No built reader edition is active")
    candidate = (workspace / relative).resolve()
    require(candidate.is_relative_to(workspace) and candidate.is_dir(), "Active candidate path is invalid")
    return candidate


def _build_current(workspace: Path, manifest: dict[str, Any], candidate: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    plan, _toc = _plan(workspace, manifest)
    records, _clips = _compiled_records(workspace, plan)
    require(
        candidate.name == _candidate_key(manifest, workspace / PLAN, records, workspace / "reviewed_chapters"),
        "Candidate no longer matches current page translations or reviewed chapters",
    )
    build = read(candidate / "audit" / "publication-build.json")
    require(build.get("source_sha256") == manifest["source_sha256"], "Candidate source identity is stale")
    require(build.get("plan_sha256") == sha(workspace / PLAN), "Candidate plan is stale")
    for name, checksum in build.get("artifacts", {}).items():
        path = candidate / name
        require(path.is_file() and sha(path) == checksum, f"Candidate artifact changed: {name}")
    return build, plan


def _receipt_current(workspace: Path, manifest: dict[str, Any], candidate: Path) -> bool:
    receipt_path = candidate / "audit" / "release-receipt.json"
    if not receipt_path.is_file():
        return False
    try:
        _build_current(workspace, manifest, candidate)
        receipt = read(receipt_path)
        report_path = candidate / "audit" / "release-report.json"
        if receipt.get("report_sha256") != sha(report_path):
            return False
        for name, checksum in receipt.get("artifacts", {}).items():
            if sha(candidate / name) != checksum:
                return False
        return read(report_path).get("release_ready") is True
    except (OSError, ValueError, KeyError):
        return False


def verify(workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    candidate = _active_candidate(workspace, manifest)
    build, plan = _build_current(workspace, manifest, candidate)
    report_path = candidate / "audit" / "release-report.json"
    report = verify_publication(
        candidate, source_pdf=_source(manifest), book_title=_publication_title(manifest, plan),
        expected_language="zh-CN", require_translation=True, report_path=report_path,
    )
    ready = bool(
        report.get("release_ready") is True
        and report.get("status") == "passed"
        and report.get("summary", {}).get("failed") == 0
        and report.get("summary", {}).get("skipped") == 0
    )
    if ready:
        write(candidate / "audit" / "release-receipt.json", {
            "source_sha256": manifest["source_sha256"],
            "plan_sha256": sha(workspace / PLAN),
            "report_sha256": sha(report_path),
            "artifacts": build["artifacts"],
        })
    failures = [
        {"id": item["id"], "codes": sorted({issue.get("code") for issue in item.get("issues", []) if issue.get("code")})}
        for item in report.get("checks", []) if item.get("status") == "failed"
    ]
    return {"ok": ready, "status": "passed" if ready else "failed",
            "candidate": str(candidate), "release_ready": ready,
            "summary": report.get("summary", {}), "failed_checks": failures,
            "report": str(report_path)}


def _layout_review(path: Path, candidate: Path, report: dict[str, Any]) -> dict[str, Any]:
    require(path.is_file(), "A Word layout review JSON is required before registration")
    review = read(path)
    docx_files = list(candidate.glob("*.docx"))
    require(len(docx_files) == 1, "Candidate must contain one Word artifact")
    require(review.get("docx_sha256") == sha(docx_files[0]), "Layout review is stale for this Word artifact")
    _single_line(review.get("reviewer"), "layout reviewer")
    checked = review.get("checked_pages")
    total = int(report.get("summary", {}).get("docx_render_pages") or 0)
    require(isinstance(checked, list) and checked, "Layout review must name inspected rendered pages")
    require(all(type(page) is int and 1 <= page <= total for page in checked), "Layout review has invalid rendered page numbers")
    require(len(set(checked)) == len(checked), "Layout review page numbers must be unique")
    _single_line(review.get("note"), "layout review note")
    return review


def register(args: dict[str, Any], workspace: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    candidate = _active_candidate(workspace, manifest)
    require(_receipt_current(workspace, manifest, candidate), "Full publication verification is missing or stale")
    report = read(candidate / "audit" / "release-report.json")
    review_path = Path(_single_line(args.get("layout_review_file"), "layout_review_file")).expanduser().resolve()
    review = _layout_review(review_path, candidate, report)
    mode = args.get("embedding_mode") or "auto"
    require(mode in {"off", "auto", "on"}, "embedding_mode must be off, auto, or on")
    book.load_env_file(ROOT / ".env")
    key_available = bool(os.getenv("ZHIPU_API_KEY"))
    require(mode != "on" or key_available, "ZHIPU_API_KEY is required for embedding_mode=on")
    lexical_only = mode == "off" or (mode == "auto" and not key_available)
    command = [sys.executable, str(ROOT / "knowledge_base_cli.py"), "register", str(candidate)]
    if lexical_only:
        command.append("--lexical-only")
    seconds = args.get("timeout_seconds", 1800)
    require(type(seconds) is int and 1 <= seconds <= 3600, "timeout_seconds must be 1–3600")
    try:
        completed = subprocess.run(
            command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=seconds, check=False,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError("Embedding registration timed out; candidate remains available for retry") from exc
    require(completed.returncode == 0, f"Single-book KB registration failed (exit {completed.returncode}); candidate remains available")
    rag = read(candidate / "knowledge_base.rag.json")
    kb = candidate / "knowledge_base.jsonl"
    require(rag.get("documents", {}).get("sha256") == sha(kb), "RAG manifest is stale after registration")
    embedding = rag.get("retrieval", {}).get("embedding", {})
    require(mode != "on" or embedding.get("status") == "ready", "Required vector index was not built")
    write(candidate / "audit" / "layout-review.json", review)
    files = {
        name: sha(candidate / name) for name in
        ("knowledge_base.jsonl", "knowledge_base.rag.json", "knowledge_base.apparatus.json")
    }
    vectors = candidate / "knowledge_base.vectors.jsonl"
    if vectors.is_file():
        files[vectors.name] = sha(vectors)
    files["audit/layout-review.json"] = sha(candidate / "audit" / "layout-review.json")
    write(candidate / "audit" / "registration-receipt.json", {
        "source_sha256": manifest["source_sha256"], "plan_sha256": sha(workspace / PLAN),
        "embedding_mode": mode, "files": files,
    })
    return {"ok": True, "status": "registered", "candidate": str(candidate),
            "kb_chunks": report["summary"]["kb_chunks"], "embedding_status": embedding.get("status"),
            "lexical_status": rag.get("retrieval", {}).get("lexical", {}).get("status")}


def status(args: dict[str, Any]) -> dict[str, Any]:
    workspace, manifest = _workspace(args)
    records = book.load_page_records(workspace)
    candidate: Path | None = None
    if manifest.get("active_candidate"):
        try:
            candidate = _active_candidate(workspace, manifest)
        except GateError:
            candidate = None
    plan_path = workspace / PLAN
    toc_path = workspace / "toc.json"
    plan_current = False
    if plan_path.is_file() and toc_path.is_file():
        plan = read(plan_path)
        plan_current = (
            plan.get("source_sha256") == manifest["source_sha256"]
            and plan.get("toc_sha256") == sha(toc_path)
        )
    verified = bool(candidate and _receipt_current(workspace, manifest, candidate))
    registered = False
    if verified and candidate:
        receipt_path = candidate / "audit" / "registration-receipt.json"
        if receipt_path.is_file():
            receipt = read(receipt_path)
            registered = all(
                (candidate / name).is_file() and sha(candidate / name) == digest
                for name, digest in receipt.get("files", {}).items()
            )
    return {
        "ok": True, "status": "inspected", "workspace": str(workspace),
        "source_current": True, "source_sha256": manifest["source_sha256"],
        "page_count": manifest["page_count"], "checkpoint_pages": len(records),
        "translated_pages": sum(record.translation_is_fresh for record in records),
        "missing_pages": [page for page in range(1, manifest["page_count"] + 1)
                          if page not in {record.pdf_page for record in records}][:100],
        "toc_exists": toc_path.is_file(), "plan_current": plan_current,
        "candidate": str(candidate) if candidate else None,
        "verification_current": verified, "registration_current": registered,
        "release_ready": verified and registered,
    }


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    require(isinstance(payload, dict), "Request must be a JSON object")
    args = payload.get("args") or {}
    require(isinstance(args, dict), "args must be a JSON object")
    command = payload.get("command")
    if command == "prepare":
        return prepare(args)
    if command == "status":
        return status(args)
    require(command == "run", "Unknown command")
    workspace, manifest = _workspace(args)
    stage = args.get("stage")
    require(stage in STAGES, "Unknown stage")
    if stage == "extract":
        return extract(args, workspace, manifest)
    if stage in {"ocr", "toc", "translate"}:
        return _run_pipeline(workspace, manifest, args, stage)
    if stage == "plan":
        return set_plan(args, workspace, manifest)
    if stage == "draft":
        return draft(workspace, manifest)
    if stage == "publish":
        return publish(workspace, manifest)
    if stage == "verify":
        return verify(workspace, manifest)
    return register(args, workspace, manifest)


def main() -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(json.load(sys.stdin))
    except Exception as exc:
        result = {
            "ok": False,
            "status": "blocked" if isinstance(exc, (GateError, FileNotFoundError, KeyError)) else "failed",
            "error": str(exc) if isinstance(exc, (GateError, FileNotFoundError, KeyError)) else type(exc).__name__,
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
