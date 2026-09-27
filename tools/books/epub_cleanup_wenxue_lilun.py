"""One-off cleanup for the 文学理论 (Yale) EPUB import work dir.

Removes z-library / WeRead source artifacts that the deterministic importer
carried through from the pirated EPUB:

- z-library watermark links ``[EPUB...](https://t.me/...)`` (pure insertions,
  text stays grammatical when removed);
- zero-width spaces leaked from the Duokan reader footnotes;
- Duokan page locators like ``[858]`` glued to CJK text;
- mangled ``qqreader-footnote`` img fragments (``note" src=.../note.png"/>``
  and ``lt="..." class="qqreader-footnote" ...``);
- inline duplicate copies of footnote definitions that the importer also
  emitted as proper end-of-chapter ``[^id]:`` blocks;
- mangled ``<aside epub:type="footnote">`` shells and ``N. [注释N：]`` list
  remnants;
- WeRead remote lecture-slide images (remote host unreachable, references
  would crash the docx image embed);
- the z-library per-chapter ``© 未经授权禁止转载`` footer;
- the z-library promotional last chapter (033) and its manifest/audit entry;
- rewrites the cover chapter to the image-only scaffold so the docx builder
  skips it;
- repairs the one Duokan footnote the importer missed (012: Jakobson note)
  by converting the mangled noteref remnant into a standard marker plus
  end-of-chapter definition.

Finally refreshes ``audit/semantic-reconstruction.json`` digests/counts so
the publication verifier's semantic gate compares against the cleaned files.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

WORK = Path(
    "outputs/文学理论 (耶鲁大学公开课) = Theory of Literature (Open Yale Courses) "
    "([美] 保罗・H・弗莱 (Paul H. Fry) 著  吕黎 译) "
    "(z-library.sk, 1lib.sk, z-lib.sk)"
)
CHAPTERS = WORK / "chapters"
ADVISORY = WORK / "chapters.json"
AUDIT = WORK / "audit" / "semantic-reconstruction.json"

ZWSP = "\u200b"
WM_RE = re.compile(r"\[EPUB\.\.\.\]\(https://t\.me/[^)]*\)")
COPYRIGHT_LINE_RE = re.compile(r"(?m)^©\s*未经授权禁止转载\s*$\n?")
WEREAD_CLEAN_RE = re.compile(r"(?m)^!\[\]\(https://res\.weread\.qq\.com/[^)]+\)\s*\n")
WEREAD_BROKEN_HEAD_RE = re.compile(
    r"(?m)^!\[\]\(https://res\.weread\.qq\.com/[^)\n]*<span style=\)\s*\n"
)
WEREAD_BROKEN_TAIL_RE = re.compile(
    r"(?m)^[0-9a-zA-Z]{3,45}\.[a-z]{2,4}\" style=\"width:100%;\"/>\s*\n"
)
WEREAD_TAIL_FRAG_RE = re.compile(r"(?m)^yPic\">\s*\n")
NOTE_PNG_TAIL_RE = re.compile(
    r'note" src="\.\./Images/note\.png"\s*/?>'
)
NOTE_PNG_ALT_RE = re.compile(
    r'lt="[^"]*" class="qqreader-footnote" src="\.\./Images/note\.png"\s*/?>'
)
# Trailing quote/gt fragment that some mangled img tags left glued to the
# start of a footnote definition line.
DEF_LINE_JUNK_RE = re.compile(
    r'(?m)^(?P<head>\[\^(?:epub-[0-9a-f]{12}-r\d+)\]:\s*)">(?=.)'
)
# Mangled ``<li class="duokan-footnote-item" id=...>`` shell glued to a
# definition line.
DEF_LINE_LI_JUNK_RE = re.compile(
    r'(?m)^(?P<head>\[\^(?:epub-[0-9a-f]{12}-r\d+)\]:\s*)'
    r'-footnote-item" id="footnote_\d+">'
)
LONE_LT_RE = re.compile(r"(?m)^<\s*\n")
ASIDE_RE = re.compile(r'(?m)^aside epub:type="footnote" id="footnote_\d+"\s*>\s*\n')
LIST_NOTE_RE = re.compile(r"(?m)^\d+\.\s*\[注释\d+：\]\(#[^)]+\)[^\n]*\n")
# Duokan printed-page locators glued to Chinese text.  Legitimate
# bibliographic years (e.g. ``S/Z [1970]`` in the reading list) are preceded
# by ASCII text, so the CJK lookbehind keeps them.
PAGE_MARKER_RE = re.compile(r"(?<=[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef])\[\d{1,4}\]")
# Duokan page locators that landed after ASCII text or citation punctuation.
# Three-digit locators only: legitimate bracketed publication years in the
# reading lists are all four digits (e.g. ``[1970]``).
ASCII_PAGE_MARKER_RE = re.compile(r"\[\d{3}\]")
# What NOTE_PNG_TAIL_RE can leave behind after it eats the ``note.png"/>``
# half of a Duokan inline note shell: ``[lt="DEF"] " class="qqreader-foot``.
QQREADER_SHELL_RE = re.compile(r'(?:"\s*)?lt="[^"]*"\s*class="qqreader-foot|"\s*class="qqreader-foot')

MARKER_RE = re.compile(r"\[\^(epub-[0-9a-f]{12}-r\d+)\]")
DEF_LINE_RE = re.compile(r"^(\[\^(epub-[0-9a-f]{12}-r\d+)\]):\s*(.*)$")

# Broken tails of Duokan EPUB open tags.  A line that begins with an ASCII
# run plus ">" is always a mangled ``<p class="content">`` shell: no
# legitimate line in this book starts that way.
LINE_HEAD_FRAG_RE = re.compile(r'(?m)^[A-Za-z"= \-]*>')
# Broken open-tag tails glued mid-sentence.  The lookbehind keeps complete
# tags such as ``<span class="italic">`` or ``</sup>`` intact because the
# characters immediately before such a tail are lowercase letters or "<".
# Longer tokens come first so a shorter one never leaves a residue.
INLINE_FRAG_RE = re.compile(
    r'(?<![<a-z])'
    r'(?:an class="bold">|pan class="italic">|class="quotation">'
    r'|class="italic">|lass="italic">|class="content">|ss="italic">'
    r'|ss="bold">|content">|ontent">|bold">|ic">|d">|an>)'
)
SOUTH_PARK_FRAG_RE = re.compile(r"South Parkspan>")
# Orphaned closer left inside a footnote definition line (ch 009).
STRAY_CLOSE_P_RE = re.compile(r"</p>")
GOOD_SPAN_TAG_RE = re.compile(r'<span class="[a-z]+">')
# A ``<span`` opener whose attribute list was mangled away (e.g. the importer
# left ``<span Reader-Response Criticism: ...``); drop the tag head only.
BROKEN_SPAN_HEAD_RE = re.compile(r"<span(?!\s*class=)")
ORPHAN_CLOSE_SPAN_RE = re.compile(r"</span>")
# Consecutive footnote definition lines must be separated by a blank line,
# otherwise the markdown footnote extension emits a stray ``</p>``.
DEF_SPACING_RE = re.compile(
    r"(?m)(\[\^epub-[0-9a-f]{12}-r\d+\]:[^\n]*\n)(?=\[\^epub-[0-9a-f]{12}-r\d+\]:)"
)

# The Word publisher's running-title heuristic strips the copyright-page
# 书名 line (it repeats the title-page title).  Drop it at source level so
# the markdown, the DOCX body, and the verifier's expected text agree.
BOOK_NAME_LINE_RE = re.compile(r"(?m)^书名：耶鲁大学公开课：文学理论\s*$\n?")
# Duokan's shadow-library licensing line is a pirated-source artifact that a
# clean publication copyright page must not carry.
ZLIB_LICENSE_LINE_RE = re.compile(r"(?m)^本书由.*授权Z-Library.*$")
# Duokan footnote definitions keep the note label as a dead anchor link
# (``[注释3：](#footnote_ref_3)``).  Word renumbers footnotes, so the baked-in
# label reads as a mismatch; drop the link prefix, keep the note text.
NOTE_LINK_PREFIX_RE = re.compile(
    r"(?m)^(?P<head>\[\^epub-[0-9a-f]{12}-r\d+\]:\s*)\[[^\]]+\]\(#[^)]*\)"
)

# Repair for the one Duokan footnote the importer missed (ch 012).
JAKOBSON_DEF = (
    "Roman Jakobson, “Two Types of Aphasia and Two Types of Language "
    "Disturbance,” in Roman Jakobson and Morris Halle, Fundamentals of "
    "Language (The Hague: Mouton, 1956), pp. 69–96."
)
NEW_FOOTNOTE_ID = "epub-" + hashlib.sha1(JAKOBSON_DEF.encode("utf-8")).hexdigest()[:12] + "-r1"
JAKOBSON_RE = re.compile(
    r'="noteref" href="#footnote_1" id="footnote_ref_1">'
    r"!\[" + re.escape(JAKOBSON_DEF) + r"\]\(\.\./Images/note\.png\)"
    + r"[ \u200b]*"
    + re.escape(JAKOBSON_DEF)
)

log_lines: list[str] = []


def log(message: str) -> None:
    log_lines.append(message)
    print(message)


def global_clean(text: str, name: str) -> tuple[str, int]:
    """Apply the whole-book artifact removals; return (text, removals)."""
    removals = 0

    def count(pattern: re.Pattern) -> None:
        nonlocal removals, text
        text, hits = pattern.subn("", text)
        removals += hits

    count(WM_RE)
    text = text.replace(ZWSP, "")
    # The complete shell must go before NOTE_PNG_TAIL_RE eats its tail.
    count(NOTE_PNG_ALT_RE)
    count(NOTE_PNG_TAIL_RE)
    count(QQREADER_SHELL_RE)
    count(WEREAD_CLEAN_RE)
    count(WEREAD_BROKEN_HEAD_RE)
    count(WEREAD_BROKEN_TAIL_RE)
    count(WEREAD_TAIL_FRAG_RE)
    # Keep the definition prefix, drop only the glued ``">`` fragment.
    text, hits = DEF_LINE_JUNK_RE.subn(r"\1", text)
    removals += hits
    text, hits = DEF_LINE_LI_JUNK_RE.subn(r"\1", text)
    removals += hits
    count(LONE_LT_RE)
    count(ASIDE_RE)
    count(LIST_NOTE_RE)
    count(COPYRIGHT_LINE_RE)
    count(ZLIB_LICENSE_LINE_RE)
    count(PAGE_MARKER_RE)
    count(ASCII_PAGE_MARKER_RE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    log(f"{name}: removed {removals} whole-book artifacts")
    return text, removals


def inline_dedup(text: str, name: str) -> tuple[str, int]:
    """Drop inline duplicate copies of end-of-chapter footnote definitions."""
    defs: dict[str, str] = {}
    for m in re.finditer(
        r"^\[\^(epub-[0-9a-f]{12}-r\d+)\]:\s*(.*)$", text, flags=re.M
    ):
        value = m.group(2).rstrip()
        # Drop the ``[注释N：](#footnote_ref_N)`` link prefix so the match
        # target is the bare definition text used in the inline copy.
        value = re.sub(r"^\[[^\]]+\]\([^)]*\)\s*", "", value)
        defs.setdefault(m.group(1), value)
    # Definition blocks live at the tail; only delete inside the body.
    first_def = re.search(r"^\[\^epub-", text, flags=re.M)
    body_end = first_def.start() if first_def else len(text)
    edits: list[tuple[int, int, str]] = []
    for marker in MARKER_RE.finditer(text[:body_end]):
        fid = marker.group(1)
        definition = defs.get(fid)
        if definition is None:
            log(f"{name}: marker without definition {fid}")
            continue
        after = text[marker.end():]
        window = after[:400]
        if window.startswith(definition):
            cut = marker.end() + len(definition)
        else:
            at = window.find(definition[:60])
            if at < 0:
                log(f"{name}: UNMATCHED marker {fid}: {window[:80]!r}")
                continue
            cut = marker.end() + at + len(definition)
        if cut > body_end:
            log(f"{name}: inline duplicate of {fid} crosses into definitions; kept")
            continue
        edits.append((marker.start(), cut, marker.group(0), fid))
    for start, cut, marker_text, fid in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + marker_text + text[cut:]
        log(f"{name}: dropped inline duplicate of {fid}")
    return text, len(edits)


def fragment_clean(text: str, name: str) -> tuple[str, int]:
    """Remove the mangled open-tag fragments the Duokan importer left behind."""
    removals = 0

    def count(pattern: re.Pattern, repl: str = "") -> None:
        nonlocal removals, text
        text, hits = pattern.subn(repl, text)
        removals += hits

    count(SOUTH_PARK_FRAG_RE, "South Park")
    count(LINE_HEAD_FRAG_RE)
    count(INLINE_FRAG_RE)
    count(STRAY_CLOSE_P_RE)
    text, hits = BROKEN_SPAN_HEAD_RE.subn("", text)
    removals += hits
    # Keep the surviving <span> openers and </span> closers balanced; the
    # importer lost whole tags, so extra openers or closers break the parse.
    good_open = len(GOOD_SPAN_TAG_RE.findall(text))
    close = text.count("</span>")
    for _ in range(max(good_open - close, 0)):
        text = GOOD_SPAN_TAG_RE.sub("", text, count=1)
        removals += 1
    for _ in range(max(close - good_open, 0)):
        text = ORPHAN_CLOSE_SPAN_RE.sub("", text, count=1)
        removals += 1
    if removals:
        log(f"{name}: removed {removals} broken tag fragment(s)")
    return text, removals


def def_line_spacing(text: str, name: str) -> tuple[str, int]:
    """Put a blank line between consecutive footnote definition lines."""
    text, hits = DEF_SPACING_RE.subn(r"\1\n", text)
    if hits:
        log(f"{name}: separated {hits} consecutive definition line(s)")
    return text, hits


def repair_jakobson_footnote(text: str, name: str) -> str:
    if JAKOBSON_RE.search(text):
        text = JAKOBSON_RE.sub(f"[^{NEW_FOOTNOTE_ID}]", text)
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n[^{NEW_FOOTNOTE_ID}]: [注释1：](#footnote_ref_1){JAKOBSON_DEF}\n"
        log(f"{name}: repaired missed Duokan footnote as {NEW_FOOTNOTE_ID}")
    return text


def main() -> int:
    if not WORK.is_dir():
        print(f"work dir missing: {WORK}", file=sys.stderr)
        return 1
    backup = CHAPTERS.with_name("chapters.orig")
    if backup.exists():
        shutil.rmtree(backup)
    shutil.copytree(CHAPTERS, backup)
    log(f"backup: {backup}")

    manifest = json.loads(ADVISORY.read_text(encoding="utf-8"))
    kept_manifest = [
        item
        for item in manifest
        if str(item.get("title")) != "获取更多电子书 | 2026"
        # The docx builder deliberately skips image-only cover scaffolds
        # (duplicated by its own title page); the manifest must agree.
        and str(item.get("source_item_id")) != "cover_page"
    ]
    dropped = [item for item in manifest if item not in kept_manifest]
    for item in dropped:
        log(
            f"manifest: dropped {item.get('id')} ({item.get('filename')}) "
            f"as {item.get('source_item_id', 'promotional')}"
        )
    for position, item in enumerate(kept_manifest, start=1):
        item["sequence"] = position
    manifest = kept_manifest
    ADVISORY.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ad_file = CHAPTERS / "033_获取更多电子书_2026.md"
    if ad_file.exists():
        ad_file.unlink()
    cover_file = CHAPTERS / "001_Cover_Image_Images_cover_jpg.md"
    if cover_file.exists():
        cover_file.unlink()
        log("manifest: dropped cover scaffold chapter file")
    kept_hrefs = {str(item.get("source_href")) for item in manifest}

    from publication_semantics import markdown_footnote_contract_sha256, parse_markdown_footnotes

    for item in manifest:
        path = CHAPTERS / item["filename"]
        name = item["filename"]
        text = path.read_text(encoding="utf-8")
        if name.startswith("001_"):
            text = "# ![Cover Image](../Images/cover.jpg)\n"
            log(f"{name}: rewritten to image-only scaffold")
        text, _ = global_clean(text, name)
        text, _ = fragment_clean(text, name)
        text, _ = inline_dedup(text, name)
        if name.startswith("012_"):
            text = repair_jakobson_footnote(text, name)
        if name.startswith("002_"):
            text, hits = BOOK_NAME_LINE_RE.subn("", text)
            if hits:
                log(f"{name}: dropped redundant 书名 line (title-page duplicate)")
        text, hits = NOTE_LINK_PREFIX_RE.subn(r"\g<head>", text)
        if hits:
            log(f"{name}: stripped {hits} Duokan 注释-label link prefix(es)")
        text, _ = def_line_spacing(text, name)
        path.write_text(text, encoding="utf-8")

        inventory = parse_markdown_footnotes(text)
        log(
            f"{name}: defs={len(inventory.definitions)} "
            f"missing={len(inventory.missing_definitions)} "
            f"unused={len(inventory.unused_definitions)} "
            f"dup_refs={len(inventory.duplicate_references)}"
        )
        item["semantic_footnote_count"] = len(inventory.definitions)

    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    audit["chapters"] = [
        entry
        for entry in audit["chapters"]
        if str(entry.get("source_href")) in kept_hrefs
    ]
    for entry in audit["chapters"]:
        path = CHAPTERS / entry["filename"]
        text = path.read_text(encoding="utf-8")
        entry["markdown_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        entry["footnote_contract_sha256"] = markdown_footnote_contract_sha256(text)
        entry["footnote_count"] = len(parse_markdown_footnotes(text).definitions)
    total_notes = sum(entry["footnote_count"] for entry in audit["chapters"])
    audit["summary"]["footnote_count"] = total_notes
    audit["summary"]["chapter_count"] = len(audit["chapters"])
    AUDIT.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"audit: refreshed {len(audit['chapters'])} chapters, footnotes={total_notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
