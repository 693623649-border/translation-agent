"""Translate a delivered DOCX in place, preserving its layout and styles.

The publication pipeline builds its DOCX from Markdown, but a book that was
already delivered has no Markdown left, and re-deriving one would lose the
layout, the heading styles and the figures.  This module instead walks the
existing document and replaces only the text:

    source.docx ──► paragraphs ──► [needs Chinese?] ──► translate ──► write back
                       │                                   │
                       └── style / run formatting kept ◄────┘

Text is written into the paragraph's first run and any remaining runs are
cleared, so the paragraph style, indentation and alignment survive.  Paragraphs
are single-run in the books this was built for, so inline formatting is not
usually at risk; the report says how many multi-run paragraphs were flattened.

The source file is never modified: the translation is written next to it with a
``_中文`` suffix.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from book_pipeline import detect_language
from kb_translation import translate_texts

VERSION = 1
CHINESE_SUFFIX = "_中文"
CHINESE = "zh"

# Headings, folios and ornaments that carry no prose.
_NON_PROSE_RE = re.compile(r"^[\s\d\-–—·・.．、,，:：;；()（）\[\]［］]+$")
# Copyright lines and plate credits carry an owner and a year, not prose.
_RIGHTS_RE = re.compile(r"^[©Ⓒ\(]?\s*(?:copyright|©|Ⓒ)", re.I)
# Letter kana only: the katakana block also holds punctuation (U+30FB middle dot,
# U+30A0 double hyphen) that must not be mistaken for evidence of Japanese.
_KANA_RE = re.compile(r"[\u3041-\u3096\u309d\u309e\u30a1-\u30fa\u30fd\u30fe]")
_HAN_RE = re.compile(r"[\u3400-\u9fff]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# A printed page's running head survived OCR as "<folio> <section word>
# <damaged title fragment> <year range>" and sits where the page break used to
# be, splitting the sentence that ran across it.  The title fragment is often
# OCR-mangled (セカイ系 read as セ力系), so it cannot be matched verbatim —
# the load-bearing signals are the folio plus the section word, and the year
# range when present.  Applied to the source document *before* translation:
# cleaning the translated wording afterwards is impossible because the
# translation paraphrases the title into its own Chinese.
_SOURCE_RUNNING_HEAD_RE = re.compile(
    r"\s*\d{1,3}\s*"
    r"(?:第\s*\d{1,2}\s*章|序章|序文|後書|あとがき)"
    r"\s*[^\d。！？\n]{0,30}?"
    r"(?:\d{4}\s*年?\s*[-—–―一]\s*\d{2,4}\s*年)?"
    r"\s*"
)
# Latin runs below this are names, years and citation fragments, not prose.
_MIN_LATIN_PROSE = 12


class DocxTranslationError(ValueError):
    """The DOCX could not be translated without losing structure."""


class Translator(Protocol):
    provider_name: str
    model: str

    def translate(self, prompt: str) -> str: ...


def _prose_mass(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


# A printed page's running head survived OCR as "<folio> <section> <title>
# <year range>" and sits where the page break used to be, splitting the
# sentence that ran across it.  It carries no prose and is stripped.  The title
# fragment is only consumed when a year range or a sentence ender closes it, so
# a page number followed by ordinary prose is not eaten along with it.
_RUNNING_HEAD_RE = re.compile(
    r"\s*\d{1,3}\s*"
    r"(?:第\s*\d{1,2}\s*章|序章|序文|後書|后记|あとがき)"
    r"(?:"
    r"\s*[^\d。！？…,，、；;：:\n]{1,26}"
    r"(?=\s*(?:\d{4}\s*年?\s*[-—–―]\s*\d{2,4}\s*年|[。！？…]))"
    r")?"
    r"(?:\s*\d{4}\s*年?\s*[-—–―]\s*\d{2,4}\s*年)?"
    r"\s*[。，、]?"
)


def strip_running_heads(
    text: str,
    section_titles: Sequence[str] = (),
) -> tuple[str, int]:
    """Remove OCR-embedded running heads; return the text and how many went.

    Three shapes exist.  Anchored heads start with a folio plus a section word
    ("11 序文", "29 第1章").  Verbatim heads repeat a heading (or its stem
    before a dash).  Paraphrased heads render the Japanese chapter title in
    different Chinese wording ("世界系这一亡灵" vs the heading's "名为世界系的
    亡灵") and are caught by sharing a distinctive character shingle with the
    heading while sitting next to a folio — the folio is the load-bearing
    signal: prose rarely has a bare 1-3 digit number glued to a short title-like
    phrase, and only the surrounding shingle decides whether that phrase
    belongs to a chapter heading.
    """

    total = 0

    def anchored(text: str) -> str:
        nonlocal total
        cleaned, count = _RUNNING_HEAD_RE.subn("", text)
        total += count
        return cleaned

    cleaned = anchored(text)
    # Paraphrased heads: a folio from the source catalogue glued to a short
    # CJK phrase that shares at least two two-character pairs with a heading.
    # The translation paraphrases the damaged Japanese title into its own
    # wording, so verbatim or shingle matching both fail; shared digrams plus
    # the catalogue folio are the surviving signal.  The phrase must close at a
    # sentence boundary so ordinary prose after a folio is not eaten.
    key_pairs: set[str] = set()
    for title in section_titles:
        normalized = re.sub(r"\s+", "", title)
        # Runs of 3+: the heading quotes a term ("名为“世界系”的亡灵") and the
        # quoted term is exactly what the running head paraphrases, so a run
        # threshold of 4 would drop 世界系 and 的亡灵 — the two digrams the
        # paraphrase actually shares.
        for run in re.findall(r"[\u3400-\u9fff]{3,}", normalized):
            key_pairs.update(run[i : i + 2] for i in range(len(run) - 1))

    def digrams_ok(phrase: str) -> bool:
        pairs = {phrase[i : i + 2] for i in range(len(phrase) - 1)}
        return len(pairs & key_pairs) >= 2

    # Shape B: folio, paraphrased title, sentence boundary (ellipsis included).
    for match in re.finditer(
        r"(?<![\d.])\d{1,3}\s+([\u3400-\u9fff]{4,14}?)(?=[。！？」）…]|$)", cleaned
    ):
        if digrams_ok(match.group(1)):
            cleaned = cleaned.replace(match.group(0), "", 1)
            total += 1
    # Shape A: paraphrased title, folio, then a new sentence starts.  The
    # phrase is only eligible right after a sentence ender (or at the paragraph
    # start) — mid-sentence phrases preceded by an ordinary comma are real
    # prose ("在第 2 章中，后来被称为世界系的 00 年代初期" is not a head), and
    # requiring the ender keeps that prose intact.
    sentence_start = r"(?:^|(?<=[。！？…\n]))"
    for match in re.finditer(
        sentence_start
        + r"([\u3400-\u9fff]{4,14})\s+(?<![\d.])\d{1,3}\s*(?=[\u3400-\u9fff]{2,}[，。！？])",
        cleaned,
    ):
        if digrams_ok(match.group(1)):
            cleaned = cleaned.replace(match.group(0), "", 1)
            total += 1
    # Removing a spliced head can orphan the punctuation around it
    # ("发萌，？现在" after dropping "13 世界这一亡灵") or leave a doubled
    # full stop ("进程。。以"); collapse both.
    cleaned = re.sub(r"[，、]\s*([？。！])", r"\1", cleaned)
    cleaned = re.sub(r"([？。！])\s*[，、]", r"\1", cleaned)
    cleaned = re.sub(r"([。！？])\s*\1+", r"\1", cleaned)
    return cleaned, total


def _section_titles(paragraphs: Sequence[Any]) -> list[str]:
    """Heading texts double as bare-title running heads after OCR."""

    return [
        _paragraph_text(paragraph).strip()
        for paragraph in paragraphs
        if (getattr(paragraph.style, "name", "") or "").startswith("Heading")
        and _paragraph_text(paragraph).strip()
    ]


def clean_source_running_heads(
    source: Path | str,
    *,
    output: Path | str | None = None,
) -> dict[str, Any]:
    """Strip Japanese running heads from the source DOCX, before translation.

    The translated wording of a running head cannot be predicted — the model
    paraphrases the damaged title fragment — so the only reliable place to
    clean them is the source, where folio plus section word is matchable.
    Writes next to the source with a ``_清洗`` suffix and never touches the
    original.
    """

    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise DocxTranslationError("clean_source_running_heads requires python-docx") from exc

    source_path = Path(source)
    document = Document(str(source_path))
    paragraphs = list(document.paragraphs)
    touched = 0
    removed = 0
    for paragraph in paragraphs:
        text = _paragraph_text(paragraph)
        if not text.strip():
            continue
        cleaned, count = _SOURCE_RUNNING_HEAD_RE.subn("", text)
        if count:
            _write_paragraph(paragraph, cleaned)
            touched += 1
            removed += count
    destination = Path(output) if output else source_path.with_name(
        f"{source_path.stem}_清洗{source_path.suffix}"
    )
    report = {
        "source": str(source_path),
        "output": str(destination),
        "paragraphs": len(paragraphs),
        "paragraphs_touched": touched,
        "heads_removed": removed,
    }
    if not removed:
        report["status"] = "nothing_to_do"
        return report
    document.save(str(destination))
    report["status"] = "passed"
    return report


def clean_running_heads(
    source: Path | str,
    *,
    output: Path | str | None = None,
) -> dict[str, Any]:
    """Strip OCR running heads from every paragraph, without calling a model.

    Separate from ``translate_docx`` because a paragraph that is already Chinese
    is not re-sent for translation, yet its embedded running heads still have to
    go.
    """

    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise DocxTranslationError("clean_running_heads requires python-docx") from exc

    source_path = Path(source)
    document = Document(str(source_path))
    paragraphs = list(document.paragraphs)
    titles = _section_titles(paragraphs)
    touched = 0
    removed = 0
    for paragraph in paragraphs:
        text = _paragraph_text(paragraph)
        if not text.strip():
            continue
        cleaned, count = strip_running_heads(text, titles)
        if count:
            _write_paragraph(paragraph, cleaned)
            touched += 1
            removed += count
    destination = Path(output) if output else source_path
    report = {
        "source": str(source_path),
        "output": str(destination),
        "paragraphs": len(paragraphs),
        "paragraphs_touched": touched,
        "heads_removed": removed,
    }
    if not touched:
        report["status"] = "nothing_to_do"
        return report
    document.save(str(destination))
    report["status"] = "passed"
    return report


def needs_translation(text: str) -> bool:
    """A paragraph is translated when it is prose that is not already Chinese.

    Script counts decide this, not only ``detect_language``: headings are short
    by nature and fall below its significance floor, and a kana-only heading
    ("あとがき", "エクリチュル") carries no Han at all.  Kana is decisive
    evidence of Japanese, so its presence always means translate; a Han-only
    short string ("漱石論集成", "柄谷行人") reads as Chinese and is left alone.
    """

    stripped = text.strip()
    if not stripped or _NON_PROSE_RE.match(stripped):
        return False
    if _RIGHTS_RE.match(stripped):
        return False
    if detect_language(stripped) == CHINESE:
        return False
    if _KANA_RE.search(stripped):
        return True
    han = len(_HAN_RE.findall(stripped))
    latin = len(_LATIN_RE.findall(stripped))
    if latin >= _MIN_LATIN_PROSE and latin > han:
        return True
    return False


def _paragraph_text(paragraph: Any) -> str:
    return "".join(run.text for run in paragraph.runs) or paragraph.text


def _write_paragraph(paragraph: Any, text: str) -> bool:
    """Replace a paragraph's text in its first run; return True if flattened."""

    runs = list(paragraph.runs)
    if not runs:
        paragraph.add_run(text)
        return False
    runs[0].text = text
    flattened = len(runs) > 1
    for run in runs[1:]:
        run.text = ""
    return flattened


def translate_docx(
    source: Path | str,
    translator: Translator,
    *,
    output: Path | str | None = None,
    batch_chars: int = 8000,
    concurrency: int = 8,
    dry_run: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Translate every non-Chinese paragraph of a DOCX into Chinese."""

    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise DocxTranslationError("translate_docx requires python-docx") from exc

    source_path = Path(source)
    document = Document(str(source_path))
    paragraphs = list(document.paragraphs)
    targets = [index for index, para in enumerate(paragraphs) if needs_translation(_paragraph_text(para))]
    report: dict[str, Any] = {
        "source": str(source_path),
        "paragraphs": len(paragraphs),
        "non_empty": sum(1 for para in paragraphs if _paragraph_text(para).strip()),
        "translate_count": len(targets),
        "multi_run_paragraphs": sum(1 for para in paragraphs if len(para.runs) > 1),
        "characters": sum(len(_paragraph_text(paragraphs[index])) for index in targets),
    }
    if dry_run or not targets:
        report["status"] = "dry_run" if dry_run else "nothing_to_do"
        report["written"] = False
        return report
    if progress is not None:
        progress({"phase": "translate", "pending": len(targets)})
    translated = translate_texts(
        [_paragraph_text(paragraphs[index]) for index in targets],
        translator,
        batch_chars=batch_chars,
        concurrency=concurrency,
    )
    flattened = 0
    unchanged = 0
    stripped_heads = 0
    titles = _section_titles(paragraphs)
    for index, text in zip(targets, translated, strict=True):
        text, removed = strip_running_heads(text, titles)
        stripped_heads += removed
        if text.strip() == _paragraph_text(paragraphs[index]).strip():
            unchanged += 1
            continue
        flattened += int(_write_paragraph(paragraphs[index], text))
    destination = Path(output) if output else source_path.with_name(
        f"{source_path.stem}{CHINESE_SUFFIX}{source_path.suffix}"
    )
    document.save(str(destination))
    report.update(
        {
            "status": "passed",
            "written": True,
            "output": str(destination),
            "provider": getattr(translator, "provider_name", ""),
            "model": getattr(translator, "model", ""),
            "flattened_paragraphs": flattened,
            "unchanged_paragraphs": unchanged,
        }
    )
    return report


def verify_translated_docx(path: Path | str) -> dict[str, Any]:
    """Report how much of a DOCX is still non-Chinese."""

    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise DocxTranslationError("verify_translated_docx requires python-docx") from exc

    document = Document(str(path))
    paragraphs = [para for para in document.paragraphs if _paragraph_text(para).strip()]
    pending = [para for para in paragraphs if needs_translation(_paragraph_text(para))]
    return {
        "path": str(path),
        "paragraphs": len(paragraphs),
        "pending": len(pending),
        "pending_samples": [_paragraph_text(para)[:60] for para in pending[:5]],
    }


def languages_in(paragraph_texts: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for text in paragraph_texts:
        if not text.strip():
            continue
        language = detect_language(text)
        counts[language] = counts.get(language, 0) + 1
    return counts
