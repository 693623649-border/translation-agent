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
    for index, text in zip(targets, translated, strict=True):
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
