"""Reconstruct the two supplied Chinese Schmitt scans from fresh line geometry.

The source OCR remains untouched. Page-bottom notes and excluded apparatus are
archived separately; indentation controls paragraph boundaries. Two verified
three-page reversals in the Concept PDF are restored explicitly, never by a
global text/title heuristic. Publisher and canonical five-field corpus are
provided by the existing kb_ingest backend.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# These physical page mappings were checked against the supplied scans.
# Section leaves are excluded only where they contain no prose.
SECTIONS = {
    "政治的神学": [
        ("教会的可见性——经院学思考", 15, 26),
        ("政治的神学｜第2版序（1933）", 29, 31),
        ("政治的神学｜一、主权的定义", 32, 40),
        ("政治的神学｜二、主权问题作为法律形式和决断问题", 41, 56),
        ("政治的神学｜三、政治的神学", 57, 70),
        ("政治的神学｜四、论反对革命的国家哲学（德·迈斯特、波纳德、柯特）", 71, 81),
        ("罗马天主教与政治形式", 85, 125),
        ("政治的神学续篇｜给读者阅读方向的提示", 129, 131),
        ("政治的神学续篇｜引言", 132, 134),
        ("政治的神学续篇｜一、关于彻底终结神学的传说", 135, 159),
        ("政治的神学续篇｜二、已成传说的文献", 160, 199),
        ("政治的神学续篇｜三、传说的结论命题", 200, 211),
        ("政治的神学续篇｜跋：问题的当前状况——现代的正当性", 212, 227),
        ("价值的僭政｜引言", 231, 247),
        ("价值的僭政｜1959年自印文本", 248, 261),
    ],
    "政治的概念": [
        ("政治的概念｜重版序（1963）", 14, 27),
        ("政治的概念｜一、国家的和政治的", 28, 36),
        ("政治的概念｜二、划分敌友是政治的标准", 37, 40),
        ("政治的概念｜三、战争是敌对性的显现形式", 41, 51),
        ("政治的概念｜四、国家是政治的统一体，因多元论而出问题", 52, 60),
        ("政治的概念｜五、决断战争和敌人", 61, 70),
        ("政治的概念｜六、世界并非政治的统一体，而是政治的多样体", 71, 76),
        ("政治的概念｜七、政治理论的人类学始基", 77, 92),
        ("政治的概念｜八、伦理与经济的两极导致的非政治化", 93, 103),
        ("政治的概念｜1932年版跋", 104, 105),
        ("政治的概念｜增补附论", 106, 125),
        ("中立化与非政治化的时代", 128, 129),
        ("中立化与非政治化的时代｜一、嬗变的中心领域的阶段后果", 130, 137),
        ("中立化与非政治化的时代｜二、中立化和非政治化阶段", 138, 144),
        ("游击队理论｜前言", 148, 148),
        ("游击队理论｜引论", 149, 176),
        ("游击队理论｜理论的发展", 177, 210),
        ("游击队理论｜晚近阶段的视角和概念", 211, 235),
        ("附录：与施米特谈游击队理论", 236, 286),
    ],
}
PAGE_ORDER = {"政治的概念": {63: 65, 65: 63, 71: 73, 73: 71}}
RULE_CACHES = {}
# Exact transcriptions checked against the supplied source-image crops.
# Only the reconstructed layer changes; the fresh OCR checkpoint stays raw.
VERIFIED_REPLACEMENTS = {
    ("theology",74): [("δoγuaTiKç","δογματικῶς")],
    ("theology",155): [("□号","口号")],
    ("theology",215): [("语Gesetz翻译希腊词vóuoç。","语 Gesetz 翻译希腊词 νόμος。")],
    ("concept",42): [("πoλÉuoç","πολέμιος"),("非x9ó。","非ἐχθρός。"),
                       ("αγαπāτε τouç εx9oous","ἀγαπᾶτε τοὺς ἐχθροὺς"),("u@u）","ὑμῶν）")],
    ("concept",270): [("后人已无法完.全理.了。……把政.简.为所","后人已无法完全理解了。……把政治简括为所")],
    ("concept",267): [("性足以使他生存方面与迥异。","性足以使他在生存方面与我迥异。")],
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def note_separator(image_path):
    """Locate the isolated horizontal footnote rule in the actual source image.

    Long prose baselines are rejected by ink above/below the thin rule. The
    vertical gutter lies outside the tested horizontal start/width interval.
    """
    cache_path=image_path.parent.parent/"body_layout_rules.json"
    if cache_path not in RULE_CACHES:
        RULE_CACHES[cache_path]=json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    cache=RULE_CACHES[cache_path]
    signature=[2,image_path.stat().st_size,image_path.stat().st_mtime_ns]
    saved=cache.get(image_path.name)
    if saved and saved["signature"]==signature:
        return tuple(saved["result"])
    def save_result(value):
        cache[image_path.name]=dict(signature=signature,result=value)
        write_json(cache_path,cache)
        return value
    with Image.open(image_path) as image:
        gray = np.array(image.convert("L"))
    h, w = gray.shape
    dark = gray < 135
    lo, hi = int(w * .10), int(w * .82)
    for y in range(int(h * .115), int(h * .94)):
        row = dark[y, lo:hi]
        if row.sum() < w * .065:
            continue
        changes = np.flatnonzero(np.diff(np.r_[False, row, False].astype(np.int8)))
        for start, end in zip(changes[::2], changes[1::2]):
            length, x = end - start, start + lo
            if not (w * .065 <= length <= w * .42 and w * .10 <= x <= w * .31):
                continue
            pad = max(7, round(h * .0035))
            a = dark[max(0, y-pad):y-3, x:x+length]
            b = dark[y+4:min(h, y+pad), x:x+length]
            if a.sum() + b.sum() <= length * .5:
                return save_result((y, w, h))
    return save_result((None, w, h))


def merge_text(left, right):
    if not left:
        return right
    space = " " if re.search(r"[A-Za-z0-9]$", left) and re.match(r"[A-Za-z0-9]", right) else ""
    return left + space + right


def clean_reference_marks(text, has_notes):
    if not has_notes:
        return text, []
    # Four-digit dates, numbered prose lists and explanatory bracketed text
    # remain intact. Only page-note reference shapes are removed.
    pattern = r"\[\s*\]|\[\s*\d{1,2}\s*\]|(?<!\d)\d{1,2}\](?!\d)|\[\d{1,2}(?=[\u3400-\u9fff，。；])"
    marks = re.findall(pattern, text)
    return re.sub(pattern, "", text), marks


def page_lines(record, image_path):
    notes = json.loads(record["notes"])
    cut, w, h = note_separator(image_path)
    rows, header, footnotes, noise = [], [], [], []
    for index, item in enumerate(notes["lines"]):
        x1, y1, x2, y2 = map(float, item["box"])
        line = dict(text=item["text"], score=item["score"], x=x1, y=y1, right=x2, bottom=y2,
                    height=y2-y1, pdf_page=record["pdf_page"], line_index=index)
        if "theology" in str(image_path) and record["pdf_page"]==49 and index==14 and item["text"]=="2" and item["score"]<.1:
            noise.append(line)
        elif y2 <= h * .105:
            header.append(line)
        elif cut is not None and y1 > cut:
            footnotes.append(line)
        elif re.search(r"[\u3400-\u9fffA-Za-z0-9]", item["text"]) or (item["score"]>.85 and re.fullmatch(r"[。？！，、；：‘’“”《》（）\[\]…—]+",item["text"])):
            rows.append(line)
    rows.sort(key=lambda x: (x["y"], x["x"]))
    # Join separately detected pieces on the same baseline, including inline
    # Latin names. Keep their original line IDs for lossless provenance.
    merged = []
    median_h = statistics.median(r["height"] for r in rows) if rows else 1
    for row in rows:
        row["line_refs"] = [row["line_index"]]
        row["pieces"] = [(row["x"],row["text"])]
        if merged and abs((row["y"]+row["bottom"])/2 - (merged[-1]["y"]+merged[-1]["bottom"])/2) < median_h*.45:
            prev = merged[-1]
            prev["pieces"] += row["pieces"]
            prev["pieces"].sort(key=lambda p:p[0])
            prev["text"] = ""
            for _,part in prev["pieces"]:
                prev["text"] = merge_text(prev["text"],part)
            prev["x"] = min(prev["x"],row["x"])
            prev["right"] = max(prev["right"], row["right"])
            prev["line_refs"] += row["line_refs"]
            prev["score"] = min(prev["score"], row["score"])
        else:
            merged.append(row)
    for row in merged:
        row["text"], row["removed_marks"] = clean_reference_marks(row["text"], bool(footnotes))
    slug="theology" if "theology" in str(image_path) else "concept"
    edits=[]
    for row in merged:
        for before,after in VERIFIED_REPLACEMENTS.get((slug,record["pdf_page"]),[]):
            if before in row["text"]:
                edits.append(dict(line_refs=row["line_refs"],before=before,after=after,evidence_image=str(image_path),image_sha256=file_sha(image_path)))
                row["text"]=row["text"].replace(before,after)
    return merged, dict(pdf_page=record["pdf_page"], separator_y=cut, image_width=w, image_height=h,
                        header_lines=header, note_lines=footnotes,noise_lines=noise,verified_source_edits=edits)


def annotate_paragraph_starts(rows):
    """Use indentation relative to each page's own text column, not sentences."""
    if not rows:
        return []
    long = [r for r in rows if len(r["text"]) > 15]
    baseline = min((r["x"] for r in long), default=min(r["x"] for r in rows))
    right = statistics.median(sorted(r["right"] for r in long)[len(long)//2:]) if long else max(r["right"] for r in rows)
    body_h = statistics.median(r["height"] for r in long) if long else statistics.median(r["height"] for r in rows)
    char = body_h * .95
    differences = [rows[i]["y"]-rows[i-1]["y"] for i in range(1,len(rows)) if body_h < rows[i]["y"]-rows[i-1]["y"] < body_h*3]
    step = statistics.median(differences) if differences else body_h*1.8
    for i, row in enumerate(rows):
        prev, nxt = (rows[i-1] if i else None), (rows[i+1] if i+1 < len(rows) else None)
        indent = row["x"]-baseline
        centered = row["x"] > baseline+char*2 and row["right"] < right-char*.8
        large_heading = row["height"] > body_h*1.35 and 3 < len(row["text"]) < 65 and centered and not re.search(r"[。；？！]",row["text"])
        numbered_heading = bool(re.match(r"^(?:[一二三四五六七八九十]+[、，]|\d{1,2}[.．、])", row["text"])) and len(row["text"]) < 36 and not re.search(r"[。；？！]", row["text"])
        isolated = nxt is None or nxt["y"]-row["y"] > step*1.38
        row["kind"] = "heading" if large_heading or (numbered_heading and isolated) else "body"
        if row["text"] in {"在罗马", "您忠实的"}:
            row["kind"] = "body"
        first_indent = nxt is not None and row["x"]-nxt["x"] > char*1.15
        entering_indent = indent > char*1.35 and (prev is None or prev["x"]-baseline < char*.6)
        gap = prev is not None and row["y"]-prev["y"] > step*1.45
        row["paragraph_start"] = row["kind"] == "heading" or first_indent or entering_indent or gap
        row["column_left"] = baseline
        row["column_right"] = right
        row["character_width"] = char
        row["line_step"] = step
    return rows


def reconstruct_section(title, pages, records, images):
    paragraphs, audits, archive = [], [], []
    for page in pages:
        rows, audit = page_lines(records[page], images / f"page_{page:04d}.jpg")
        annotate_paragraph_starts(rows)
        is_concept = "concept" in str(images)
        for row in rows:
            relative_y=row["y"]/audit["image_height"]
            if is_concept and ((page==33 and .63<relative_y<.80) or (page==88 and relative_y<.42)):
                row["kind"]="diagram"
                row["paragraph_start"]=True
                if page==88:
                    row["text"]=re.sub(r"^([1-5])(.+)([1-5])$",r"\1 \2 \3",row["text"])
            if not is_concept and page==256 and row["text"]=="价值的僭政":
                row["kind"]="heading"
                row["paragraph_start"]=True
            if audit["note_lines"]:
                # A superscript marker sometimes loses both brackets in OCR.
                # Only the distinctive sentence-stop + reference + connective
                # shape is handled, leaving prose quantities untouched.
                row["text"],count=re.subn(r"(?<=[。；！？])([1-9]\d?)(?=[这那对但因我他她它在至所并而其如即])","",row["text"])
                if count: row["removed_marks"].append("unbracketed superscript")
        audit["body_lines"] = len(rows)
        audit["removed_reference_marks"] = [x for r in rows for x in r["removed_marks"]]
        audit["low_confidence_body_lines"] = [dict(line_index=r["line_index"], score=r["score"], text=r["text"]) for r in rows if r["score"] < .94]
        audit["source_empty"] = not rows
        audits.append(audit)
        if audit["note_lines"]:
            archive.append(dict(pdf_page=page, kind="page_footnotes", separator_y=audit["separator_y"],
                                text="\n".join(r["text"] for r in audit["note_lines"]),
                                line_refs=[r["line_index"] for r in audit["note_lines"]]))
        for row in rows:
            # The externally verified section title supplies navigation; the
            # matching scan heading is kept in evidence, not duplicated.
            normalize = lambda s: re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", s)
            source_title = normalize(title.split("｜")[-1])
            row_title = normalize(row["text"])
            matching_heading = (row["kind"] == "heading" or row["y"] < audit["image_height"]*.30) and len(row_title) >= 2 and (row_title in source_title or source_title in row_title)
            metadata = bool(re.fullmatch(r"[\[［（(]?\d{4}[\]］）)]?", row["text"].strip())) or (len(row["text"]) < 35 and re.search(r"(?:译|著)$",row["text"]) and row["x"] > row["column_left"]+row["character_width"]*2 and row["y"] < audit["image_height"]*.45)
            if not paragraphs and (matching_heading or metadata):
                audit.setdefault("opening_headings", []).append(row["text"])
                continue
            if row["kind"] in {"heading","diagram"} or not paragraphs or row["paragraph_start"] or paragraphs[-1]["kind"] in {"heading","diagram"}:
                paragraphs.append(dict(text=row["text"], kind=row["kind"], pdf_pages=[page], source_lines=[]))
            else:
                paragraphs[-1]["text"] = merge_text(paragraphs[-1]["text"], row["text"])
                if page not in paragraphs[-1]["pdf_pages"]:
                    paragraphs[-1]["pdf_pages"].append(page)
            paragraphs[-1]["source_lines"] += [dict(pdf_page=page, line_index=n) for n in row["line_refs"]]
    for p in paragraphs:
        # Preserve balanced dates/glosses. Orphan square brackets arising from
        # damaged note markers are removed only in paragraphs on note-bearing
        # pages, and the change is recorded separately.
        if not any(a["note_lines"] and a["pdf_page"] in p["pdf_pages"] for a in audits):
            continue
        stack,remove=[],set()
        for i,c in enumerate(p["text"]):
            if c=="[":stack.append(i)
            elif c=="]":
                if stack:stack.pop()
                else:remove.add(i)
        remove.update(stack)
        if remove:
            p["removed_orphan_note_brackets"]=[dict(offset=i,character=p["text"][i]) for i in sorted(remove)]
            p["text"]="".join(c for i,c in enumerate(p["text"]) if i not in remove)
    return paragraphs, audits, archive


def build(title, publish=False):
    from tools.kb_ingest_plugin.kb_ingest import ingest_source
    slug = "theology" if title == "政治的神学" else "concept"
    job = ROOT / "deploy/paddleocr/io" / f"schmitt-20260929-{slug}-v3"
    workspace = ROOT / "outputs" / (title+"_正文重建版")
    source = next((ROOT / "book/卡尔施密特").glob(title+"*.pdf"))
    manifest = json.loads((job / "local_paddleocr_job.json").read_text(encoding="utf-8"))
    if manifest["source_pdf"]["sha256"] != file_sha(source):
        raise ValueError("OCR input SHA differs from the supplied source")
    records = {}
    for path in sorted((workspace / "pages").glob("page_*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        records[value["pdf_page"]] = value
    if len(records) != manifest["source_pdf"]["page_count"]:
        raise ValueError("Fresh OCR must cover every PDF page")
    text, paragraphs, audits, notes = [], [], [], []
    included = set()
    for sequence, (heading, start, end) in enumerate(SECTIONS[title],1):
        pages = [PAGE_ORDER.get(title, {}).get(p,p) for p in range(start,end+1)]
        section, page_audits, page_notes = reconstruct_section(heading,pages,records,job/"images")
        if not section:
            raise ValueError(f"Section contains no body: {heading}")
        text.append("# "+heading+"\n\n"+"\n\n".join(("## " if p["kind"]=="heading" else "")+p["text"] for p in section))
        for p in section:
            p.update(paragraph_id=f"s{sequence:02d}-p{sum(x.get('section_order')==sequence for x in paragraphs)+1:04d}", section_order=sequence, chapter_title=heading)
            paragraphs.append(p)
        audits += page_audits
        notes += page_notes
        included.update(pages)
    excluded=[]
    for n in sorted(set(records)-included):
        if n<=8 if title=="政治的神学" else n<=7:
            role="cover_copyright_contents"
        elif n in (range(9,13) if title=="政治的神学" else range(8,12)):
            role="editor_preface"
        elif n in (range(262,266) if title=="政治的神学" else range(287,293)):
            role="name_equivalence_table"
        elif n==manifest["source_pdf"]["page_count"]:
            role="download_site_notice"
        else:
            role="title_leaf_or_blank"
        excluded.append(dict(pdf_page=n,kind=role,text=records[n]["text"]))
    source_md=workspace/(title+"_正文.md")
    source_md.write_text("\n\n".join(text)+"\n",encoding="utf-8",newline="\n")
    write_jsonl(workspace/"正文段落.jsonl",paragraphs)
    write_jsonl(workspace/"archive/页下注释.jsonl",notes)
    write_jsonl(workspace/"archive/非正文材料.jsonl",excluded)
    write_jsonl(workspace/"audit/source-page-layout.jsonl",audits)
    write_json(workspace/"audit/verified-source-corrections.json",dict(
        source_sha256=file_sha(source),line_edits=[dict(pdf_page=a["pdf_page"],**e) for a in audits for e in a["verified_source_edits"]],
        removed_noise=[dict(pdf_page=a["pdf_page"],line_index=r["line_index"],text=r["text"],reason="source-image check: low-confidence non-text artifact") for a in audits for r in a["noise_lines"]],
        source_page_order_fixes=PAGE_ORDER.get(title,{})))
    report=dict(source_pdf=str(source),source_sha256=file_sha(source),ocr_job=str(job),source_pages=len(records),
                section_count=len(SECTIONS[title]),paragraph_count=sum(p["kind"]=="body" for p in paragraphs),
                heading_count=sum(p["kind"]=="heading" for p in paragraphs),body_characters=sum(len(p["text"]) for p in paragraphs),
                included_pdf_pages=sorted(included),excluded_pages=[dict(pdf_page=e["pdf_page"],kind=e["kind"]) for e in excluded],
                page_order_fixes=PAGE_ORDER.get(title,{}),notes_pages=len(notes),notes_characters=sum(len(n["text"]) for n in notes),
                removed_reference_marks=sum(len(a["removed_reference_marks"]) for a in audits),
                empty_body_pages=[a["pdf_page"] for a in audits if a["source_empty"]],
                low_confidence_lines=sum(len(a["low_confidence_body_lines"]) for a in audits),
                paragraph_policy="source indentation, line spacing, cross-page continuity; page-bottom rule separates all footnotes",
                fidelity_scope="publication equals reconstructed source; not a claim of glyph-by-glyph proofreading")
    if publish:
        result=ingest_source(source_md,output_dir=workspace,title=title,author="卡尔·施米特",chunk_chars=2400)
        report["publication"]=dict(status=result["status"],ok=result["ok"],chapter_count=result.get("chapter_count"),chunk_count=result.get("chunk_count"),fidelity=result.get("docx_fidelity"),error=result.get("error"))
        if not result["ok"]:
            write_json(workspace/"audit/body-rebuild-report.json",report)
            raise ValueError(f"Publication failed: {result.get('error')}")
    write_json(workspace/"audit/body-rebuild-report.json",report)
    print(json.dumps({k:v for k,v in report.items() if k not in {"publication","excluded_pages","included_pdf_pages"}},ensure_ascii=False))
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--book",choices=list(SECTIONS),required=True)
    parser.add_argument("--publish",action="store_true")
    args=parser.parse_args()
    build(args.book,args.publish)
