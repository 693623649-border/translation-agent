"""Staged, evidence-bound entry point for vertical Japanese scanned books."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
MANIFEST = "vertical-ingest.json"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def prepare(args):
    import fitz
    source = Path(args["source"]).resolve()
    workspace = Path(args["workspace"]).resolve()
    title, author = args["title"].strip(), args["author"].strip()
    require(title and author, "Explicit title and author are required")
    require(source.is_file() and source.suffix.lower() == ".pdf", "Source must be a PDF")
    with fitz.open(source) as document:
        count = len(document)
    require(count > 0, "Source PDF is empty")
    identity = dict(source=str(source), source_sha256=sha(source), page_count=count, title=title, author=author)
    target = workspace / MANIFEST
    if target.exists():
        old = read(target)
        require(all(old.get(k) == v for k, v in identity.items()), "Existing workspace belongs to another source/title/author")
    else:
        require(not workspace.exists() or not any(workspace.iterdir()), "Refusing nonempty unrelated workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        write(target, dict(schema_version=1, **identity))
    return dict(status="prepared", workspace=str(workspace), **identity)


def snapshot(workspace):
    from book_pipeline import load_page_records
    from docx_translation import needs_translation
    manifest = read(workspace / MANIFEST)
    source = Path(manifest["source"])
    records = load_page_records(workspace)
    issues = []
    source_current = source.is_file() and sha(source) == manifest["source_sha256"]
    if not source_current:
        issues.append("Source PDF changed or is missing")
    expected = set(range(1, manifest["page_count"] + 1))
    actual = {r.pdf_page for r in records}
    if actual != expected or len(records) != len(actual):
        issues.append("OCR page coverage is incomplete or invalid")
    geometry, required, fresh, nonempty, foreign, ellipsis = [], [], [], [], [], []
    for record in records:
        try:
            notes = json.loads(record.notes)
        except (ValueError, TypeError):
            notes = {}
        lines = notes.get("lines")
        valid_lines = isinstance(lines, list) and all(isinstance(line, dict) and isinstance(line.get("box"), list) and len(line["box"]) >= 4 for line in lines)
        if notes.get("ordering_version") == 3 and notes.get("reading_direction") == "vertical" and valid_lines and (lines or record.text.strip() == "[空白页]"):
            geometry.append(record.pdf_page)
        if not record.effective_text.strip():
            issues.append(f"Empty OCR source page {record.pdf_page}; rerun or explicitly review blank source")
        if record.effective_text.strip() == "[无法辨认]":
            issues.append(f"Unreadable source page {record.pdf_page} needs source review/repair")
        if record.effective_text.strip() and record.effective_text.strip() != "[空白页]":
            required.append(record.pdf_page)
            if record.translated_text.strip():
                nonempty.append(record.pdf_page)
            if record.translation_is_fresh and record.translation_prompt_version.strip() and record.translation_fingerprint.strip() and record.translation_target_language.lower() in {"zh", "zh-cn", "chinese", "中文", "简体中文"}:
                fresh.append(record.pdf_page)
            if any(needs_translation(p) for p in re.split(r"\n+", record.translated_text)):
                foreign.append(record.pdf_page)
            if len(re.findall(r"…+", record.translated_text)) > len(re.findall(r"…+", record.effective_text)):
                ellipsis.append(record.pdf_page)
    paths = sorted((workspace / "pages").glob("*.json"))
    page_hash = digest([{ "pdf_page": r.pdf_page, "text": r.text, "effective_text": r.effective_text, "notes": r.notes, "ocr_model": r.ocr_model} for r in records])
    full_page_hash = digest({p.name: sha(p) for p in paths})
    # Office lock files (~$name.docx) appear whenever an editor opens an
    # artifact and survive a crash; they are not deliverables, so they must not
    # move the artifact digest and invalidate a verification bound to it.
    artifact_paths = [p for p in workspace.rglob("*") if p.is_file() and not p.name.startswith("~$") and (p.suffix.lower() in {".docx", ".epub"} or (p.suffix.lower() == ".pdf" and p.parent == workspace) or p.name in {"knowledge_base.jsonl", "chapters.json", "toc.json"} or p.parent.name == "chapters")]
    artifacts = digest(dict(source=manifest["source_sha256"], pages=full_page_hash, files={str(p.relative_to(workspace)): sha(p) for p in sorted(artifact_paths)}))
    receipt = manifest.get("verified", {})
    report = workspace / "audit" / "release-report.json"
    rendered = Path(receipt.get("rendered_pdf", "__missing__"))
    verification_current = bool(source_current and receipt.get("artifact_sha256") == artifacts and report.is_file() and receipt.get("report_sha256") == sha(report) and rendered.is_file() and receipt.get("rendered_pdf_sha256") == sha(rendered))
    registered = manifest.get("registered", {})
    layout = workspace / "layout-review.json"
    sidecars = registration_files(workspace)
    registration_current = bool(verification_current and registered.get("artifact_sha256") == artifacts and layout.is_file() and registered.get("layout_review_sha256") == sha(layout) and sidecars and registered.get("sidecars") == sidecars)
    toc_path = workspace / "toc.json"
    content_required = None
    if toc_path.is_file():
        entries = read(toc_path).get("entries", [])
        starts = [e.get("pdf_page") for e in entries if isinstance(e, dict)]
        if starts and all(type(page) is int for page in starts):
            content_required = [page for page in required if page >= min(starts)]
    return dict(status="inspected", publication_ready=registration_current, verification_current=verification_current, registration_current=registration_current, source_translation_pages=required, content_required_translation_pages=content_required, translation_complete=set(required) <= set(fresh) and not foreign and not ellipsis, geometry_complete=set(geometry) == expected, verified=manifest.get("verified"), workspace=str(workspace), source_current=source_current, source_sha256=manifest["source_sha256"], page_records_sha256=page_hash, artifact_sha256=artifacts, toc_sha256=sha(workspace / "toc.json") if (workspace / "toc.json").exists() else None, page_count=manifest["page_count"], ocr_pages=sorted(actual), geometry_pages=geometry, required_translation_pages=required, fresh_translation_pages=fresh, nonempty_translation_pages=nonempty, foreign_translation_pages=foreign, extra_ellipsis_pages=ellipsis, issues=issues), manifest, records


def registration_files(workspace):
    paths = [workspace / "knowledge_base.rag.json", workspace / "knowledge_base.vectors.jsonl"]
    if not all(path.is_file() for path in paths):
        return {}
    return {path.name: sha(path) for path in paths}


def ranges(pages, count):
    require(isinstance(pages, list) and pages and all(type(p) is int and 1 <= p <= count for p in pages), "pages must be an explicit nonempty list of PDF page integers")
    require(len(pages) <= 100, "Limit each call to at most 100 pages")
    result = []
    for page in sorted(set(pages)):
        if result and result[-1][1] + 1 == page:
            result[-1][1] = page
        else:
            result.append([page, page])
    return result


def evidence(items, expected, label):
    require(isinstance(items, list), f"{label} evidence list required")
    require(all(isinstance(x, dict) and type(x.get("pdf_page")) is int and isinstance(x.get("note"), str) and x["note"].strip() for x in items), f"{label} requires page numbers and notes")
    require({x["pdf_page"] for x in items} == set(expected) and len(items) == len(expected), f"{label} evidence must cover exactly {sorted(expected)}")


def source_gate(workspace, state, manifest, records, toc_path, review_path):
    require(not state["issues"], "; ".join(state["issues"]))
    require(set(state["geometry_pages"]) == set(state["ocr_pages"]), "Missing vertical v3 OCR geometry; rerun inference")
    toc = read(toc_path)
    entries = toc.get("entries", [])
    require(entries and toc.get("book_title") == manifest["title"] and toc.get("author") == manifest["author"], "TOC title/author must match explicit publication metadata")
    require(all(isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"].strip() and type(e.get("index")) is int and e["index"] > 0 and e.get("level") == 1 and e.get("kind") == "chapter" for e in entries), "TOC entries require explicit id, index, level=1 and kind=chapter")
    require(len({e["id"] for e in entries}) == len(entries) and len({e["index"] for e in entries}) == len(entries), "TOC ids and indices must be unique")
    require(type(toc.get("page_offset")) is int and toc.get("printed_pages_per_pdf_page", 1) == 1, "Explicit single-page mapping required; spreads need reviewed pipeline adaptation")
    require(all(isinstance(e, dict) and type(e.get("pdf_page")) is int and 1 <= e["pdf_page"] <= manifest["page_count"] and e.get("title") and e.get("source_title") and e.get("kind", "chapter") == "chapter" and e.get("level", 1) == 1 for e in entries), "Use explicit flat chapter entries with source_title and pdf_page; complex TOCs require pipeline adaptation")
    starts = [e["pdf_page"] for e in entries]
    require(starts == sorted(set(starts)), "Chapter starts must be unique and ordered")
    review = read(review_path)
    for key, expected in dict(source_sha256=manifest["source_sha256"], toc_sha256=sha(toc_path), page_records_sha256=state["page_records_sha256"], title=manifest["title"], author=manifest["author"]).items():
        require(review.get(key) == expected, f"Source review {key} is stale or mismatched")
    require(review.get("reviewer") and review.get("note"), "Source reviewer and note required")
    evidence(review.get("content_starts"), starts, "Content starts")
    excluded = set(range(1, starts[0])) | {r.pdf_page for r in records if r.effective_text.strip() == "[空白页]"}
    evidence(review.get("excluded_pages"), excluded, "Excluded nonprose pages")
    required = set(state["required_translation_pages"]) - excluded
    require(required <= set(state["fresh_translation_pages"]), "Missing, empty, stale or non-Chinese-target translations")
    require(not (required & set(state["foreign_translation_pages"])), "Untranslated foreign paragraphs remain")
    require(not (required & set(state["extra_ellipsis_pages"])), "Introduced ellipsis may hide omitted text")
    require(not (workspace / "reviewed_chapters").exists(), "Chapter overrides require independent review; unsupported by this adapter")
    return review


def execute(command, timeout):
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", shell=False, start_new_session=os.name != "nt", env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
        raise
    print(output, file=sys.stderr)
    require(process.returncode == 0, f"Stage command failed (exit {process.returncode}); inspect stderr and checkpoints")


def run(args):
    workspace = Path(args["workspace"]).resolve()
    state, manifest, records = snapshot(workspace)
    require(state["source_current"], "Source PDF changed or missing")
    stage = args["stage"]
    require(stage in {"ocr", "translate", "compile", "verify", "register"}, "Unknown stage")
    timeout = args.get("timeout_seconds", 600)
    require(type(timeout) is int and 1 <= timeout <= 3600, "timeout_seconds must be 1..3600")
    base = [sys.executable, str(ROOT / "book_pipeline.py"), manifest["source"], "-o", str(workspace), "--title", manifest["title"], "--author", manifest["author"], "--ocr-profile", "paddleocr_local", "--ocr-reading-direction", "vertical"]
    config = Path(args.get("config") or manifest.get("config") or ROOT / "pipeline.toml").resolve()
    require(config.is_file(), "Pipeline config does not exist")
    base += ["--config", str(config)]
    commands = []
    if stage in {"ocr", "translate"}:
        for start, end in ranges(args.get("pages"), manifest["page_count"]):
            command = base + ["--phase", stage, "--start-page", str(start), "--end-page", str(end)]
            if stage == "translate":
                require(set(range(start, end + 1)) <= set(state["geometry_pages"]), "Translation requires vertical geometry for selected pages")
                command += ["--translate-non-chinese", "--translation-source-language", "ja"]
            commands.append(command)
    else:
        toc_path = Path(args.get("toc_file") or workspace / "toc.json").resolve()
        review_path = Path(args.get("source_review_file") or workspace / "source-review.json").resolve()
        source_gate(workspace, state, manifest, records, toc_path, review_path)
        if stage != "compile":
            require((workspace / "toc.json").exists() and sha(toc_path) == sha(workspace / "toc.json"), "Verification/registration must use the published TOC")
        if stage == "compile":
            toc_bytes, review_bytes = toc_path.read_bytes(), review_path.read_bytes()
            (workspace / "toc.json").write_bytes(toc_bytes)
            (workspace / "source-review.json").write_bytes(review_bytes)
            commands.append(base + ["--phase", "compile", "--granularity", "all", "--require-complete-ocr", "--no-rag-embed"])
        elif stage == "verify":
            commands.append(base + ["--phase", "verify"])
        else:
            report_path = workspace / "audit" / "release-report.json"
            receipt = manifest.get("verified", {})
            require(receipt.get("artifact_sha256") == state["artifact_sha256"] and report_path.exists() and receipt.get("report_sha256") == sha(report_path), "Fresh successful verification receipt required")
            layout = read(args["layout_review_file"])
            require(layout.get("artifact_sha256") == state["artifact_sha256"] and layout.get("reviewer") and layout.get("note"), "Layout review must bind current artifacts and name reviewer")
            rendered = Path(layout["rendered_pdf"])
            require(rendered.is_file() and str(rendered.resolve()) == receipt.get("rendered_pdf") and layout.get("rendered_pdf_sha256") == receipt.get("rendered_pdf_sha256") == sha(rendered), "Rendered PDF must match the retained verification render")
            import fitz
            with fitz.open(rendered) as document:
                evidence(layout.get("pages"), range(1, len(document) + 1), "Rendered layout")
            commands.append([sys.executable, str(ROOT / "knowledge_base_cli.py"), "register", str(workspace)])
    log = workspace / "vertical-attempts.jsonl"
    manifest["config"] = str(config)
    write(workspace / MANIFEST, manifest)
    started = time.monotonic()
    attempt = dict(stage=stage, pages=args.get("pages"), started_at=time.time(), status="running")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(attempt) + "\n")
    try:
        for command in commands:
            remaining = timeout - (time.monotonic() - started)
            require(remaining > 0, "Stage time budget exhausted; resume explicit remaining pages")
            execute(command, remaining)
        if stage in {"compile", "verify"}:
            report = workspace / "audit" / "release-report.json"
            require(report.exists() and read(report).get("release_ready") is True and read(report).get("mode") == "full" and read(report).get("docx_render_required") is True, "Full publication verification including Word rendering did not pass")
            current, _, _ = snapshot(workspace)
            docx_files = list(workspace.glob("*.docx"))
            require(len(docx_files) == 1, "Exactly one Word artifact required")
            render_dir = workspace / "audit" / "vertical-word-render"
            render_command = ["powershell", "-NoProfile", "-File", str(ROOT / "skills/docx-publication-finisher/scripts/render_docx_with_word.ps1"), "-DocxPath", str(docx_files[0]), "-OutputDirectory", str(render_dir), "-Force"]
            remaining = timeout - (time.monotonic() - started)
            require(remaining > 0, "Time budget exhausted before retaining layout render")
            execute(render_command, remaining)
            rendered = render_dir / (docx_files[0].stem + ".pdf")
            require(rendered.is_file(), "Retained Word render is missing")
            manifest["verified"] = dict(artifact_sha256=current["artifact_sha256"], report_sha256=sha(report), rendered_pdf=str(rendered.resolve()), rendered_pdf_sha256=sha(rendered), docx_sha256=sha(docx_files[0]))
            write(workspace / MANIFEST, manifest)
        if stage == "register":
            sidecars = registration_files(workspace)
            require(sidecars, "Registration did not produce RAG manifest and vectors")
            write(workspace / "layout-review.json", layout)
            manifest["registered"] = dict(artifact_sha256=state["artifact_sha256"], layout_review_sha256=sha(workspace / "layout-review.json"), sidecars=sidecars)
            write(workspace / MANIFEST, manifest)
        attempt["status"] = "passed"
    except Exception:
        attempt["status"] = "failed"
        raise
    finally:
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(attempt) + "\n")
    return dict(status="passed", stage=stage, workspace=str(workspace), verified=manifest.get("verified"))


def dispatch(payload):
    args = payload.get("args", {})
    command = payload.get("command")
    if command == "prepare":
        return prepare(args)
    if command == "status":
        return snapshot(Path(args["workspace"]).resolve())[0]
    if command == "run":
        return run(args)
    raise ValueError("Unknown command")


def main():
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(json.load(sys.stdin))
    except Exception as exc:
        result = dict(status="blocked" if isinstance(exc, (ValueError, KeyError, FileNotFoundError)) else "failed", error=str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"prepared", "passed", "inspected"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
