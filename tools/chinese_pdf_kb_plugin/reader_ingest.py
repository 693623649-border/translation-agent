"""Staged Chinese scanned-PDF body edition, with checksum-bound quality gates."""
from __future__ import annotations
import contextlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.chinese_pdf_kb_plugin.reconstruction import digest, evidence, jsonl, load_ocr, read, reconstruct, require, sha, write

MANIFEST = "chinese-pdf-ingest.json"


def prepare(args):
    import fitz
    source, workspace = Path(args["source"]).resolve(), Path(args["workspace"]).resolve()
    title, author = args["title"].strip(), args["author"].strip()
    require(title and author and not any(c in title+author for c in '\n\r\x00'), "Explicit single-line title and author required")
    require(source.is_file() and source.suffix.lower() == ".pdf", "Source PDF required")
    with fitz.open(source) as pdf:
        count = len(pdf)
    require(count, "Empty source PDF")
    identity = dict(source=str(source), source_sha256=sha(source), page_count=count, title=title, author=author)
    target = workspace / MANIFEST
    if target.exists():
        require(all(read(target).get(k) == v for k, v in identity.items()), "Workspace identity differs")
    else:
        require(not workspace.exists() or not any(workspace.iterdir()), "Refusing nonempty unrelated workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        write(target, dict(schema_version=1, page_images={}, **identity))
    return dict(status="prepared", workspace=str(workspace), **identity)


def hashes(workspace, names):
    return {name: sha(workspace / name) if (workspace / name).is_file() else None for name in names}


def docx_name(manifest):
    from tools.kb_ingest_plugin.kb_ingest import slugify
    return slugify(manifest["title"]) + ".docx"


RECON_FILES = ["source.md", "正文段落.jsonl", "archive/非正文材料.jsonl", "archive/页下注释.jsonl", "audit/cleaning-ledger.jsonl", "reconstruction-plan.json", "source-review.json"]


def snapshot(workspace):
    manifest = read(workspace / MANIFEST)
    records, ocr_sha, issues, low = load_ocr(workspace, manifest)
    source = Path(manifest["source"])
    current = source.is_file() and sha(source) == manifest["source_sha256"]
    base = dict(source_sha256=sha(source) if source.is_file() else None, identity={k:manifest[k] for k in ("title","author","page_count")}, ocr_sha256=ocr_sha, files=hashes(workspace, RECON_FILES))
    fingerprint = digest(base)
    reconstructed = current and not issues and manifest.get("reconstructed", {}).get("fingerprint") == fingerprint
    published_files = hashes(workspace, ["knowledge_base.jsonl", "chapters.json", docx_name(manifest),"knowledge_base.meta.jsonl"])
    published_files.update({str(p.relative_to(workspace)):sha(p) for p in sorted((workspace/"chapters").glob("*.md"))})
    artifact = digest(dict(reconstructed=fingerprint, published=published_files))
    published = reconstructed and all(published_files.values()) and manifest.get("published", {}).get("artifact_sha256") == artifact
    receipt = manifest.get("verified", {})
    render = Path(receipt.get("rendered_pdf", "__missing__"))
    report = workspace / "audit/chinese-word-verification.json"
    verified = published and receipt.get("artifact_sha256") == artifact and render.is_file() and sha(render) == receipt.get("rendered_pdf_sha256") and report.is_file() and sha(report) == receipt.get("report_sha256")
    sidecars = hashes(workspace, ["knowledge_base.rag.json", "knowledge_base.vectors.jsonl", "layout-review.json"])
    registered = verified and all(sidecars.values()) and manifest.get("registered", {}).get("files") == sidecars and manifest.get("registered", {}).get("artifact_sha256") == artifact
    geometry = [dict(pdf_page=page, image=data["image"], image_sha256=data["image_sha256"], line_count=len(data["lines"]), lines=[dict(line_index=i, text=line["text"], box=line["box"], score=line["score"]) for i, line in enumerate(data["lines"])]) for page, data in records.items()]
    return dict(status="inspected", workspace=str(workspace), source_current=current, source_sha256=manifest["source_sha256"], page_count=manifest["page_count"], ocr_sha256=ocr_sha, ocr_pages=sorted(records), issues=issues, low_confidence_lines=low, reconstructed=bool(reconstructed), published=bool(published), verification_current=bool(verified), registration_current=bool(registered), publication_ready=bool(registered), fingerprint=fingerprint, artifact_sha256=artifact, verified=receipt, page_images=manifest.get("page_images", {}), geometry_review=geometry), manifest, records


def execute(command, timeout):
    require(timeout > 0, "Stage deadline exhausted")
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", shell=False, start_new_session=os.name != "nt", env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    try:
        output, _ = process.communicate(timeout=timeout)
    except BaseException:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)
        tail,_=process.communicate(timeout=5)
        if tail:print(tail,file=sys.stderr)
        raise
    print(output, file=sys.stderr)
    require(process.returncode == 0, f"Stage command failed: {process.returncode}; inspect stderr")


def ranges(pages, count):
    require(isinstance(pages, list) and 0 < len(pages) <= 100 and len(pages) == len(set(pages)) and all(type(p) is int and 1 <= p <= count for p in pages), "OCR requires exact unique PDF pages (1..100 per call)")
    result = []
    for page in sorted(pages):
        if result and result[-1][1] + 1 == page: result[-1][1] = page
        else: result.append([page, page])
    return result


def corpus_gate(workspace):
    import hashlib
    from tools.kb_ingest_plugin.kb_ingest import slugify
    paragraphs = [json.loads(line) for line in (workspace / "正文段落.jsonl").read_text(encoding="utf-8").splitlines()]
    chunks = [json.loads(line) for line in (workspace / "knowledge_base.jsonl").read_text(encoding="utf-8").splitlines()]
    require(chunks and all(set(c) == {"id", "title", "chapter_id", "chapter_order", "content"} for c in chunks), "Canonical corpus must have exactly five fields")
    require(len({c["id"] for c in chunks}) == len(chunks) and all(isinstance(c["id"], str) and c["id"] and isinstance(c["chapter_id"], str) and c["chapter_id"] and type(c["chapter_order"]) is int and c["chapter_order"] > 0 and isinstance(c["title"], str) and isinstance(c["content"], str) for c in chunks), "Invalid corpus field types or duplicate ids")
    plan = read(workspace / "reconstruction-plan.json")
    title = read(workspace / MANIFEST)["title"]
    chapter_ids = {section["id"]: f"01_{slugify(title)}:{slugify(section['title'])}" for section in plan["sections"]}
    require(len(set(chapter_ids.values())) == len(chapter_ids), "Chapter titles collide after slug normalization")
    counts = {}
    for order, chunk in enumerate(chunks, 1):
        chapter = chunk["chapter_id"]
        counts[chapter] = counts.get(chapter, 0) + 1
        expected_id = hashlib.sha1(f"source.md:{chapter}:{counts[chapter]}".encode()).hexdigest()
        require(chunk["id"] == expected_id and chunk["chapter_order"] == order, "Corpus identity/order differs from publisher contract")
    for paragraph in paragraphs:
        require(any(paragraph["text"] in c["content"] and c["chapter_id"] == chapter_ids[paragraph["section_id"]] and c["title"] == f"[{title}] {paragraph['chapter_title']}" for c in chunks), "Publisher split, changed or misplaced an original paragraph")


def _run(args):
    workspace = Path(args["workspace"]).resolve()
    state, manifest, records = snapshot(workspace)
    require(state["source_current"], "Source PDF changed or missing")
    stage = args["stage"]
    require(stage in {"ocr", "reconstruct", "publish", "verify", "register"}, "Unknown stage")
    timeout = args.get("timeout_seconds", 600)
    require(type(timeout) is int and 1 <= timeout <= 3600, "timeout_seconds must be 1..3600")
    deadline = time.monotonic() + timeout
    invalidates={"ocr":["reconstructed","published","verified","registered"],"reconstruct":["reconstructed","published","verified","registered"],"publish":["published","verified","registered"],"verify":["verified","registered"],"register":["registered"]}
    for key in invalidates[stage]:manifest.pop(key,None)
    write(workspace/MANIFEST,manifest)
    if stage == "ocr":
        for start, end in ranges(args.get("pages"), manifest["page_count"]):
            job = ROOT / "deploy/paddleocr/io" / ("chinese-" + manifest["source_sha256"][:16]) / f"{start}-{end}-{uuid.uuid4().hex[:8]}"
            attempt=dict(job_dir=str(job),pdf_pages=list(range(start,end+1)),status="running")
            manifest.setdefault("ocr_jobs",[]).append(attempt)
            write(workspace/MANIFEST,manifest)
            command = [sys.executable, str(ROOT / "tools/local_paddleocr_import.py"), manifest["source"], "--output-dir", str(workspace), "--work-dir", str(job), "--start-page", str(start), "--end-page", str(end), "--reading-direction", "horizontal", "--dpi", "300", "--max-side", "3500", "--det-len", "1280", "--force-ocr"]
            try:
                execute(command, deadline - time.monotonic())
                attempt["status"]="completed"
            except BaseException:
                attempt["status"]="failed"
                write(workspace/MANIFEST,manifest)
                raise
            for page in range(start, end + 1):
                image = job / "images" / f"page_{page:04d}.jpg"
                require(image.is_file(), "OCR did not retain source image")
                manifest["page_images"][str(page)] = str(image)
            write(workspace / MANIFEST, manifest)
    elif stage == "reconstruct":
        require(not state["issues"], "; ".join(state["issues"]))
        plan_path = Path(args.get("plan_file") or workspace / "reconstruction-plan.json").resolve()
        review_path = Path(args.get("source_review_file") or workspace / "source-review.json").resolve()
        plan, review = read(plan_path), read(review_path)
        _, _, _, low = load_ocr(workspace, manifest)
        result = reconstruct(workspace, manifest, plan, review, sha(plan_path), records, state["ocr_sha256"], low)
        # Copy exact bytes: review refers to the user's file hash, not a reserialization.
        plan_bytes, review_bytes = plan_path.read_bytes(), review_path.read_bytes()
        (workspace / "reconstruction-plan.json").write_bytes(plan_bytes)
        (workspace / "source-review.json").write_bytes(review_bytes)
        state = snapshot(workspace)[0]
        manifest["reconstructed"] = dict(fingerprint=state["fingerprint"], **result)
    elif stage == "publish":
        require(state["reconstructed"], "Fresh successful reconstruction receipt required")
        from tools.kb_ingest_plugin.kb_ingest import ingest_source
        result = ingest_source(workspace / "source.md", output_dir=workspace, title=manifest["title"], author=manifest["author"], chunk_chars=read(workspace / "reconstruction-plan.json").get("chunk_chars", 2400))
        require(result.get("ok") is True, "Publisher fidelity/language gate failed")
        corpus_gate(workspace)
        manifest["published"] = dict(artifact_sha256=snapshot(workspace)[0]["artifact_sha256"])
    elif stage == "verify":
        require(state["published"], "Fresh publication receipt required")
        from tools.kb_ingest_plugin.kb_ingest import verify_word
        corpus_gate(workspace)
        result = verify_word(workspace)
        require(result.get("ok") is True, "Word character fidelity gate failed")
        docx = workspace / docx_name(manifest)
        render_dir = workspace / "audit/word-render"
        execute(["powershell", "-NoProfile", "-File", str(ROOT / "skills/docx-publication-finisher/scripts/render_docx_with_word.ps1"), "-DocxPath", str(docx), "-OutputDirectory", str(render_dir), "-Force"], deadline-time.monotonic())
        render = render_dir / (docx.stem + ".pdf")
        require(render.is_file(), "Retained Word PDF required")
        import fitz
        with fitz.open(render) as pdf:
            require(len(pdf)>0 and all(re.search(r"[^\d\s]",page.get_text()) for page in pdf), "Word render contains blank pages or missing text")
        report = workspace / "audit/chinese-word-verification.json"
        write(report, result)
        manifest["verified"] = dict(artifact_sha256=state["artifact_sha256"], report_sha256=sha(report), rendered_pdf=str(render.resolve()), rendered_pdf_sha256=sha(render))
    else:
        require(state["verification_current"], "Fresh Word verification required")
        layout_path = Path(args.get("layout_review_file") or workspace / "layout-review.json")
        layout, receipt = read(layout_path), manifest["verified"]
        require(layout.get("reviewer") and layout.get("note") and layout.get("artifact_sha256") == state["artifact_sha256"] and layout.get("rendered_pdf_sha256") == receipt["rendered_pdf_sha256"] and str(Path(layout.get("rendered_pdf", "")).resolve()) == receipt["rendered_pdf"], "Layout review must bind actual current render/artifacts")
        import fitz
        with fitz.open(receipt["rendered_pdf"]) as pdf:
            evidence(layout.get("pages"), range(1, len(pdf)+1), "Word layout review")
        execute([sys.executable, str(ROOT / "knowledge_base_cli.py"), "register", str(workspace)], deadline-time.monotonic())
        write(workspace / "layout-review.json", layout)
        files = hashes(workspace, ["knowledge_base.rag.json", "knowledge_base.vectors.jsonl", "layout-review.json"])
        require(all(files.values()), "Vector registration artifacts missing")
        manifest["registered"] = dict(artifact_sha256=state["artifact_sha256"], files=files)
    write(workspace / MANIFEST, manifest)
    return dict(status="passed", stage=stage, workspace=str(workspace), state=snapshot(workspace)[0])


def run(args):
    workspace = Path(args["workspace"]).resolve()
    require((workspace / MANIFEST).is_file(), "Prepare the workspace first")
    attempt = dict(id=uuid.uuid4().hex, stage=args.get("stage"), pages=args.get("pages"), started_at=time.time(), status="running")
    journal = workspace / "chinese-attempts.jsonl"
    def append():
        with journal.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(attempt, ensure_ascii=False) + "\n")
    append()
    try:
        result = _run(args)
        attempt["status"] = "passed"
        return result
    except BaseException as exc:
        attempt.update(status="failed", error=str(exc))
        raise
    finally:
        attempt["finished_at"] = time.time()
        append()


def dispatch(payload):
    command, args = payload.get("command"), payload.get("args", {})
    if command == "prepare": return prepare(args)
    if command == "status": return snapshot(Path(args["workspace"]).resolve())[0]
    if command == "run": return run(args)
    raise ValueError("Unknown command")


def main():
    # The geometry receipt is large and holds Chinese text; a redirected stdout
    # inherits the Windows ANSI code page and would fail mid-write after the
    # stage already succeeded. Emit the result stream as UTF-8 regardless.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = dispatch(json.load(sys.stdin))
    except Exception as exc:
        result = dict(status="blocked" if isinstance(exc, (ValueError, KeyError, FileNotFoundError)) else "failed", error=str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"passed", "prepared", "inspected"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
