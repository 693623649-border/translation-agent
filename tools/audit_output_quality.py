#!/usr/bin/env python3
"""Scan published book outputs for truncation / reading-order / splice defects.

Defect classes detected (per paragraph unless noted):

- dash_fragment   paragraph opens with “——“ / “— (a continuation fragment, the
                  signature of scrambled OCR line order being translated verbatim)
- dash_seam       consecutive paragraphs where one ends with “——”/“—” and the next
                  resumes with a dash: a hard mid-sentence splice
- bracket_span    paragraph ends with an unclosed 《（“「 and the next paragraph
                  carries the closing glyph: text was cut inside a title/quote
- folio_glue      “——<digits>” or sentence-boundary digits glued to CJK: a printed
                  page number (folio) left inside the prose
- running_head    a recurring bare-line section heading embedded inside a paragraph
- head_tail       paragraph tail fuzzy-matches a section heading (absorbed page head)
- no_terminal     long paragraph whose last glyph is not terminal punctuation
- quote_imbalance paragraph-level “/” mismatch (soft signal)

Usage: python3 tools/audit_output_quality.py [--outputs outputs] [--json PATH]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
from collections import Counter
from pathlib import Path

FRAGMENT_OPEN_RE = re.compile(r"^[“\"]*——")
DASH_TAIL_RE = re.compile(r"[“”]*——[“”]*$")
TERMINAL_RE = re.compile(r"[。！？…”’」』）】:；]$")
FOLIO_DASH_RE = re.compile(r"——\d{1,4}")
FOLIO_BOUNDARY_RE = re.compile(r"(?<=[。！？”…])\d{1,3}(?=[一-龥])")
YEAR_RE = re.compile(r"(19|20)\d{2}")
CJK_RE = re.compile(r"[一-龥]")
PAIRS = {"《": "》", "（": "）", "“": "”", "「": "」", "『": "』"}
CLOSERS = {v: k for k, v in PAIRS.items()}

DEFECT_KEYS = (
    "dash_fragment",
    "dash_seam",
    "bracket_span",
    "folio_glue",
    "running_head",
    "no_terminal",
    "quote_imbalance",
)
MAX_SAMPLES = 3


def _keep(samples, hit):
    if len(samples) < MAX_SAMPLES:
        samples.append(hit)


def _mostly_cjk(text: str, threshold: float = 0.6) -> bool:
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return False
    cjk = sum(1 for ch in chars if CJK_RE.match(ch))
    return cjk / len(chars) >= threshold


def audit_paragraphs(paras, title_keys=()):
    """Return (counts, samples) of defect hits over a list of paragraph strings."""
    counts = {k: 0 for k in DEFECT_KEYS}
    samples = {k: [] for k in DEFECT_KEYS}

    for i, para in enumerate(paras):
        stripped = para.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("<", "!", "|")) or re.fullmatch(r"<[^>]+>", stripped):
            continue
        prev = paras[i - 1].strip() if i else ""
        nxt = paras[i + 1].strip() if i + 1 < len(paras) else ""

        if FRAGMENT_OPEN_RE.match(stripped):
            counts["dash_fragment"] += 1
            _keep(samples["dash_fragment"], stripped[:48])
        if prev and DASH_TAIL_RE.search(prev) and (
            stripped.startswith("——") or FRAGMENT_OPEN_RE.match(stripped)
        ):
            counts["dash_seam"] += 1
            _keep(samples["dash_seam"], f"…{prev[-32:]} ⏎ {stripped[:32]}…")

        open_counts = Counter()
        for ch in stripped:
            if ch in PAIRS:
                open_counts[ch] += 1
            elif ch in CLOSERS:
                open_counts[CLOSERS[ch]] -= 1
        for open_ch, count in open_counts.items():
            if count > 0 and nxt and PAIRS[open_ch] in nxt:
                counts["bracket_span"] += 1
                _keep(samples["bracket_span"], f"…{stripped[-28:]} ⏎ {nxt[:28]}…")
                break

        if FOLIO_DASH_RE.search(stripped):
            counts["folio_glue"] += 1
            _keep(samples["folio_glue"], stripped[:48])
        elif _mostly_cjk(stripped):
            for m in FOLIO_BOUNDARY_RE.finditer(stripped):
                if not YEAR_RE.search(stripped[max(0, m.start() - 3) : m.end() + 3]):
                    counts["folio_glue"] += 1
                    _keep(samples["folio_glue"], stripped[:48])
                    break

        for key in title_keys:
            idx = stripped.find(key)
            if 0 < idx and CJK_RE.match(stripped[idx - 1]):
                counts["running_head"] += 1
                _keep(
                    samples["running_head"],
                    f"…{stripped[max(0, idx - 10) : idx + len(key) + 4]}…",
                )
                break

        if (
            len(stripped) >= 40
            and _mostly_cjk(stripped)
            and not TERMINAL_RE.search(stripped)
        ):
            counts["no_terminal"] += 1
            _keep(samples["no_terminal"], stripped[-36:])

        if (
            len(stripped) > 20
            and _mostly_cjk(stripped)
            and stripped.count("“") != stripped.count("”")
        ):
            counts["quote_imbalance"] += 1

    return counts, samples


def collect_headings(lines):
    """Bare-line section headings: standalone CJK-ish lines of 4-20 chars, recurring."""
    headings = Counter()
    for line in lines:
        s = line.strip()
        if re.fullmatch(r"[一-龥A-Za-z0-9·]{4,20}", s):
            headings[s] += 1
    return {h for h, n in headings.items() if n >= 2}


def tail_fuzzy_heading(paras, headings):
    hits = []
    for para in paras:
        s = para.strip()
        if not s or s.startswith("#") or s in headings or len(s) < 12:
            continue
        tail = s[-24:]
        for h in headings:
            if h in s:
                continue  # exact embedding counted separately
            if difflib.SequenceMatcher(None, tail, h).ratio() >= 0.72 and abs(len(tail) - len(h)) <= 6:
                hits.append(f"…{tail} ≈ {h}")
                break
    return hits[:MAX_SAMPLES]


def audit_chapter_file(path, headings):
    text = path.read_text(encoding="utf-8")
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    counts, samples = audit_paragraphs(paras, title_keys=headings)
    fuzzy = tail_fuzzy_heading(paras, headings)
    if fuzzy:
        counts["head_tail"] = len(fuzzy)
        samples["head_tail"] = fuzzy
    return counts, samples


def audit_pages_dir(pages_dir):
    flagged = []
    if not pages_dir.is_dir():
        return flagged
    for path in sorted(pages_dir.glob("page_*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        translated = record.get("translated_text") or ""
        paras = [p for p in re.split(r"\n\s*\n", translated) if p.strip()]
        counts, _ = audit_paragraphs(paras)
        n_frag = sum(1 for p in paras if FRAGMENT_OPEN_RE.match(p.strip()))
        if n_frag >= 2 or counts["dash_seam"]:
            flagged.append(
                {
                    "page": record.get("pdf_page") or path.stem,
                    "dash_fragments": n_frag,
                    "dash_seams": counts["dash_seam"],
                    "proofread": bool((record.get("proofread_text") or "").strip()),
                }
            )
    return flagged


def audit_book(book_dir):
    result = {
        "book": book_dir.name,
        "chapters": {},
        "pages_flagged": [],
        "kb_rows_flagged": 0,
        "totals": Counter(),
    }
    chapters_dir = book_dir / "chapters"
    if not chapters_dir.is_dir():
        return result

    md_files = sorted(chapters_dir.glob("*.md"))
    all_lines = []
    for path in md_files:
        all_lines.extend(path.read_text(encoding="utf-8").splitlines())
    headings = collect_headings(all_lines)

    for path in md_files:
        counts, samples = audit_chapter_file(path, headings)
        result["chapters"][path.name] = {"counts": counts, "samples": {k: v for k, v in samples.items() if v}}
        for k, v in counts.items():
            result["totals"][k] += v

    result["pages_flagged"] = audit_pages_dir(book_dir / "pages")

    kb_path = book_dir / "knowledge_base.jsonl"
    if kb_path.is_file():
        for line in kb_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            paras = [p for p in re.split(r"\n\s*\n", row.get("content") or "") if p.strip()]
            counts, _ = audit_paragraphs(paras)
            if any(counts[k] for k in ("dash_fragment", "dash_seam", "bracket_span", "folio_glue")):
                result["kb_rows_flagged"] += 1

    return result


WEIGHTS = {
    "dash_seam": 3,
    "dash_fragment": 3,
    "bracket_span": 2,
    "folio_glue": 1,
    "running_head": 1,
    "head_tail": 1,
    "no_terminal": 0.5,
    "quote_imbalance": 0,
}


def severity(r):
    return sum(r["totals"].get(k, 0) * w for k, w in WEIGHTS.items())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    root = Path(args.outputs)
    reports = []
    for book_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        reports.append(audit_book(book_dir))

    reports.sort(key=lambda r: (-severity(r), r["book"]))

    header = f"{'book':<26} {'seam':>5} {'frag':>5} {'brk':>5} {'folio':>6} {'head':>5} {'tail':>5} {'noterm':>7} {'pgs':>5} {'kb':>4}"
    print(header)
    print("-" * len(header))
    for r in reports:
        t = r["totals"]
        n_tail = sum(1 for c in r["chapters"].values() if c["counts"].get("head_tail"))
        print(
            f"{r['book'][:26]:<26} {t['dash_seam']:>5} {t['dash_fragment']:>5} "
            f"{t['bracket_span']:>5} {t['folio_glue']:>6} {t['running_head']:>5} {n_tail:>5} "
            f"{t['no_terminal']:>7} {len(r['pages_flagged']):>5} {r['kb_rows_flagged']:>4}"
        )

    worst = [r for r in reports if r["totals"]["dash_seam"] or r["totals"]["dash_fragment"]]
    if worst:
        print("\n== dash-truncation hotspots (the screenshot defect) ==")
        for r in worst[:6]:
            hot = sorted(
                ((n, c) for n, c in r["chapters"].items() if c["counts"]["dash_seam"] or c["counts"]["dash_fragment"]),
                key=lambda kv: kv[1]["counts"]["dash_seam"] + kv[1]["counts"]["dash_fragment"],
                reverse=True,
            )[:4]
            print(f"  {r['book']}:")
            for n, c in hot:
                print(f"    {n}: seams={c['counts']['dash_seam']} fragments={c['counts']['dash_fragment']}")
            pages = [str(p["page"]) for p in r["pages_flagged"]][:16]
            if pages:
                proofread = sum(1 for p in r["pages_flagged"] if p["proofread"])
                print(f"    flagged pdf pages: {', '.join(pages)} (proofread coverage: {proofread}/{len(r['pages_flagged'])})")
            for n, c in hot[:2]:
                for s in c["samples"].get("dash_seam", [])[:1]:
                    print(f"    e.g. {n}: {s}")

    if args.json_path:
        payload = [
            {
                "book": r["book"],
                "totals": dict(r["totals"]),
                "pages_flagged": r["pages_flagged"],
                "kb_rows_flagged": r["kb_rows_flagged"],
                "chapters": r["chapters"],
            }
            for r in reports
        ]
        Path(args.json_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.json_path}")


if __name__ == "__main__":
    main()
