"""Conservative geometry reconstruction; all exclusions are explicit and auditable."""
from __future__ import annotations
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path


def require(value, message):
    if not value:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def evidence(items, expected, label):
    require(isinstance(items, list) and all(isinstance(x, dict) and type(x.get("pdf_page")) is int and isinstance(x.get("note"), str) and x["note"].strip() for x in items), f"{label}: page and note required")
    require(len(items) == len(expected) and {x["pdf_page"] for x in items} == set(expected), f"{label}: exact page coverage required")


def load_ocr(workspace, manifest):
    from book_pipeline import load_page_records
    records = load_page_records(workspace)
    result, issues, low = {}, [], []
    for record in records:
        page = record.pdf_page
        try:
            notes = json.loads(record.notes)
            lines = notes["lines"]
            require(notes.get("ordering_version") == 3 and notes.get("reading_direction") == "horizontal", "horizontal v3 geometry required")
            require(isinstance(lines, list), "lines must be a list")
            for index, line in enumerate(lines):
                box = line.get("box")
                require(isinstance(line.get("text"), str) and isinstance(box, list) and len(box) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in box) and box[2] > box[0] and box[3] > box[1], "invalid line coordinates/text")
                require(type(line.get("score")) in (int, float) and 0 <= line["score"] <= 1, "invalid confidence")
                if line["score"] < .94:
                    low.append(dict(pdf_page=page, line_index=index))
            image = Path(manifest.get("page_images", {}).get(str(page), "__missing__"))
            require(image.is_file(), "retained page image missing")
            job_path = image.parent.parent / "local_paddleocr_job.json"
            require(job_path.is_file() and read(job_path).get("source_pdf", {}).get("sha256") == manifest["source_sha256"], "OCR job source provenance mismatch")
            result[page] = dict(lines=lines, raw_text=record.text, image=str(image), image_sha256=sha(image), job_sha256=sha(job_path))
        except (ValueError, KeyError, TypeError) as exc:
            issues.append(f"Page {page}: {exc}")
    if len(records) != len(result) or set(result) != set(range(1, manifest["page_count"] + 1)):
        issues.append("Full OCR page coverage required")
    return result, digest(result), issues, low


def validate_plan(plan, count):
    sections, excluded = plan.get("sections", []), plan.get("excluded_pages", [])
    require(sections and all(isinstance(s, dict) and isinstance(s.get("id"), str) and s["id"] and isinstance(s.get("title"), str) and s["title"].strip() and not re.search(r"[\n\r\x00]", s["title"]) and isinstance(s.get("pdf_pages"), list) and s["pdf_pages"] for s in sections), "Explicit nonempty sections with single-line titles required")
    require(len({s["id"] for s in sections}) == len(sections), "Duplicate section ids")
    require(all(e.get("role") and e.get("reason") for e in excluded), "Excluded pages need role and source reason")
    pages = [p for s in sections for p in s["pdf_pages"]] + [e.get("pdf_page") for e in excluded]
    require(all(type(p) is int for p in pages) and len(pages) == count and set(pages) == set(range(1, count + 1)), "Every PDF page must occur exactly once in sections/exclusions")
    overrides = plan.get("page_overrides", [])
    included = {p for s in sections for p in s["pdf_pages"]}
    require(len(overrides) == len(included) and {o.get("pdf_page") for o in overrides} == included, "Each body page needs explicit geometry decisions")
    for override in overrides:
        require("note_boundary_y" in override and override.get("note"), "Every body page needs reviewed note_boundary_y (null for none) and note")
    budget = plan.get("chunk_chars", 2400)
    require(type(budget) is int and 200 <= budget <= 20000, "chunk_chars must be 200..20000")
    return {o["pdf_page"]: o for o in overrides}, budget


def reconstruct(workspace, manifest, plan, review, plan_sha, records, ocr_sha, low):
    from docx_translation import needs_translation
    overrides, budget = validate_plan(plan, manifest["page_count"])
    for key, value in dict(source_sha256=manifest["source_sha256"], plan_sha256=plan_sha, ocr_sha256=ocr_sha).items():
        require(review.get(key) == value, f"Source review {key} is stale")
    require(review.get("reviewer") and review.get("note"), "Source reviewer/note required")
    evidence(review.get("checked_pages"), range(1, manifest["page_count"] + 1), "Source review")
    checked = review.get("low_confidence_lines", [])
    require(all(x.get("note") for x in checked) and len(checked) == len(low) and {(x.get("pdf_page"), x.get("line_index")) for x in checked} == {(x["pdf_page"], x["line_index"]) for x in low}, "Every low-confidence line needs a source review note")
    corrections = {}
    for item in plan.get("corrections", []):
        key = (item.get("pdf_page"), item.get("line_index"))
        require(key not in corrections and key[0] in records and type(key[1]) is int and 0 <= key[1] < len(records[key[0]]["lines"]), "Invalid/duplicate correction")
        require(item.get("reason") and item.get("before") == records[key[0]]["lines"][key[1]]["text"] and isinstance(item.get("after"), str) and item.get("evidence_image_sha256") == records[key[0]]["image_sha256"], "Correction requires exact original line and current image evidence")
        require(key[0] in overrides, "Corrections belong to body pages; excluded source remains raw")
        require(len(re.findall(r"…+",item["after"])) <= len(re.findall(r"…+",item["before"])), "Introduced ellipsis may hide omitted source text")
        corrections[key] = item
    paragraphs, ledger, archive = [], [], []
    for section in plan["sections"]:
        section_rows = []
        last_indent = None
        last_width = None
        for page in section["pdf_pages"]:
            data, override = records[page], overrides[page]
            from PIL import Image
            with Image.open(data["image"]) as image:
                width, height = image.size
            boundary, header = override["note_boundary_y"], override.get("header_bottom_y", 0)
            require(type(header) in (int, float) and 0 <= header < height and (boundary is None or type(boundary) in (int, float) and header <= boundary <= height), "Invalid header/note boundary")
            sets = {}
            for key in ("exclude_line_indices", "diagram_line_indices", "heading_line_indices", "paragraph_start_line_indices", "opening_title_line_indices"):
                values = override.get(key, [])
                require(isinstance(values, list) and len(values) == len(set(values)) and all(type(i) is int and 0 <= i < len(data["lines"]) for i in values), f"Invalid {key}")
                sets[key] = set(values)
            require(not (sets["diagram_line_indices"] & sets["heading_line_indices"]), "Conflicting line kinds")
            body = []
            for index, line in enumerate(data["lines"]):
                x, y, right, bottom = line["box"]
                role = "body"
                if index in sets["exclude_line_indices"]: role = "excluded-noise"
                elif bottom <= header: role = "header"
                elif boundary is not None and y >= boundary: role = "note"
                elif index in sets["opening_title_line_indices"]:
                    require(page == section["pdf_pages"][0] and line["text"] in section.get("opening_title_aliases", []), "Opening title exclusion requires exact reviewed alias at chapter start")
                    role = "opening-title"
                edited = corrections.get((page, index))
                text = edited["after"] if edited else line["text"]
                entry = dict(pdf_page=page, line_index=index, role=role, raw_text=line["text"], text=text, raw_page_sha256=digest(data), image_sha256=data["image_sha256"], decision_note=override["note"], correction=edited)
                ledger.append(entry)
                if role != "body":
                    archive.append(entry)
                    continue
                require(text.strip(), "Empty body line requires explicit exclusion decision")
                kind = "diagram" if index in sets["diagram_line_indices"] else "heading" if index in sets["heading_line_indices"] else "body"
                body.append(dict(text=text, x=x, y=y, right=right, bottom=bottom, height=bottom-y, kind=kind, forced=index in sets["paragraph_start_line_indices"], refs=[dict(pdf_page=page, line_index=index)]))
            body.sort(key=lambda r: (r["y"], r["x"]))
            require(body, f"Included page {page} has no body lines; classify explicitly as excluded")
            median = statistics.median(r["height"] for r in body)
            merged = []
            for row in body:
                if merged and row["kind"] == merged[-1]["kind"] == "body" and not row["forced"] and abs((row["y"]+row["bottom"]-merged[-1]["y"]-merged[-1]["bottom"])/2) < median*.4:
                    prev = merged[-1]
                    pieces = prev.setdefault("pieces", [(prev["x"], prev["right"], prev["text"])])
                    pieces.append((row["x"], row["right"], row["text"]))
                    pieces.sort()
                    require(all(b[0] >= a[1] - median*.3 for a, b in zip(pieces, pieces[1:])), "Overlapping OCR fragments need source repair")
                    prev["text"] = ""
                    for _, _, part in pieces: prev["text"] = join(prev["text"], part)
                    prev["x"], prev["right"] = pieces[0][0], max(p[1] for p in pieces)
                    prev["refs"] += row["refs"]
                else: merged.append(row)
            baseline = override.get("column_left", min(r["x"] for r in merged))
            for i, row in enumerate(merged):
                previous = merged[i-1] if i else None
                following = merged[i+1] if i+1 < len(merged) else None
                continuing_indent = (previous is not None and abs(row["x"]-previous["x"])<median*.6 and previous["right"]>=row["right"]-median*1.25) or (previous is None and last_indent is not None and abs((row["x"]-baseline)/median-last_indent)<.6 and last_width>=row["right"]-row["x"]-median*1.25)
                first_indent = following is not None and row["x"] - following["x"] > median*1.15 and not continuing_indent
                entering_indent = row["x"] - baseline > median*1.35 and (previous["x"]-baseline < median*.6 if previous is not None else last_indent is None or last_indent<.6)
                gap = previous and row["y"] - previous["bottom"] > median*1.4
                start = row["forced"] or first_indent or entering_indent or gap or row["kind"] != "body" or not section_rows or section_rows[-1]["kind"] != "body"
                if start:
                    section_rows.append(dict(section_id=section["id"], chapter_title=section["title"], text=row["text"], kind=row["kind"], source_lines=row["refs"]))
                else:
                    section_rows[-1]["text"] = join(section_rows[-1]["text"], row["text"])
                    section_rows[-1]["source_lines"] += row["refs"]
                last_indent=(row["x"]-baseline)/median
                last_width=row["right"]-row["x"]
        paragraphs += section_rows
    for excluded in plan["excluded_pages"]:
        page = excluded["pdf_page"]
        for index, line in enumerate(records[page]["lines"]):
            entry = dict(pdf_page=page, line_index=index, role=excluded["role"], reason=excluded["reason"], text=line["text"], raw_page_sha256=digest(records[page]))
            ledger.append(entry); archive.append(entry)
        archive.append(dict(pdf_page=page, role=excluded["role"], reason=excluded["reason"], raw_text=records[page]["raw_text"], raw_page_sha256=digest(records[page])))
    expected={(page,i) for page,data in records.items() for i in range(len(data["lines"]))}
    require(len(ledger)==len(expected) and {(r["pdf_page"],r["line_index"]) for r in ledger}==expected, "OCR line coverage mismatch")
    body_keys=Counter((r["pdf_page"],r["line_index"]) for r in ledger if r["role"]=="body")
    consumed=Counter((r["pdf_page"],r["line_index"]) for p in paragraphs for r in p["source_lines"])
    require(consumed==body_keys, "Body line consumed more than once or silently lost")
    for paragraph in paragraphs:
        text = paragraph["text"]
        require(len(text) <= budget, "Paragraph exceeds chunk budget; raise reviewed budget without splitting original paragraph")
        require(not re.search(r"[□�]|\[\s*\]|<\/?(?:think|analysis)>|作为(?:一个)?AI|以下是.*(?:整理|识别)结果", text, re.I), "Unknown glyph, empty note marker or model trace remains")
        require(not needs_translation(text), "Foreign prose paragraph requires reviewed handling")
        require(not text.startswith(("#", "```")), "Markdown control prefix requires source-specific adapter")
        paragraph["paragraph_id"]=digest(dict(source=manifest["source_sha256"],section=paragraph["section_id"],lines=paragraph["source_lines"]))
        paragraph["pdf_pages"]=list(dict.fromkeys(r["pdf_page"] for r in paragraph["source_lines"]))
    markdown = []
    for section in plan["sections"]:
        markdown.append("# " + section["title"])
        markdown.extend(("## " if p["kind"] == "heading" else "") + p["text"] for p in paragraphs if p["section_id"] == section["id"])
    (workspace / "source.md").write_text("\n\n".join(markdown) + "\n", encoding="utf-8")
    jsonl(workspace / "正文段落.jsonl", paragraphs)
    jsonl(workspace / "archive/非正文材料.jsonl", [line for line in archive if line["role"] != "note"])
    jsonl(workspace / "archive/页下注释.jsonl", [line for line in archive if line["role"] == "note"])
    jsonl(workspace / "audit/cleaning-ledger.jsonl", ledger)
    return dict(paragraphs=len(paragraphs), lines=len(ledger), archived_lines=len(archive), chunk_chars=budget)


def join(left, right):
    return left + (" " if re.search(r"[A-Za-z0-9]$", left) and re.match(r"[A-Za-z0-9]", right) else "") + right
