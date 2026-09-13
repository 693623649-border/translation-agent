"""Escape stray angle-bracket tokens in 视差之见 chapter Markdown.

The OCR keeps scholarly notation such as ``<www.lacan.com>`` or German terms
``<Ent-schlossenheit>`` verbatim.  Python-Markdown passes such ``<...>`` runs
through as raw inline HTML, so the DOCX renderer's ElementTree parse fails with
"mismatched tag".  Escaping the brackets as ``&lt;`` / ``&gt;`` keeps the
printed appearance while removing the HTML interpretation.  Detection runs on
the paragraph-joined text (the same normalization the DOCX builder applies),
because some tokens span a line break in the source.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import book_pipeline as legacy

WORK = Path(r"outputs/视差之见")
CHAPTERS = WORK / "chapters"

LEGIT_TAG = re.compile(
    r"</?(?:br|hr|i|b|em|strong|sup|sub|p|div|span|table|thead|tbody|tr|td|th"
    r"|a|img|ul|ol|li|blockquote|code|pre|h[1-6])(?:\s[^<>]*)?/?>",
    re.I,
)
# Runnable ``<token>`` that is not a real tag and not a Markdown autolink URL.
AUTOLINK = re.compile(r"<[a-z][a-z0-9+.-]*://[^<>\s]*>", re.I)
ANGLE_TOKEN = re.compile(r"<[^<>\n]{1,80}>")


def find_stray_tokens(text: str) -> list[str]:
    joined = legacy._normalize_wrapped_markdown_for_docx(text)
    found: list[str] = []
    for match in ANGLE_TOKEN.finditer(joined):
        token = match.group(0)
        if LEGIT_TAG.fullmatch(token) or AUTOLINK.fullmatch(token):
            continue
        # Markdown footnote markers like ``[^12]`` never contain <>; anything
        # else with an opening bracket and no tag name is suspicious.
        found.append(token)
    return found


def escape_in_source(text: str, tokens: list[str]) -> tuple[str, int]:
    """Escape each stray token where it appears (possibly split by newlines)."""

    changed = 0
    for token in dict.fromkeys(tokens):
        inner = token[1:-1]
        if token in text:
            text = text.replace(token, f"&lt;{inner}&gt;")
            changed += 1
            continue
        # The token may be split across a line break in the source, so allow
        # whitespace between every character and re-join on replacement.
        pattern = "<" + r"\s*".join(re.escape(char) for char in inner) + r"\s*>"
        text, count = re.subn(
            pattern, lambda match: f"&lt;{inner}&gt;", text, count=0
        )
        changed += count
    return text, changed


def main() -> int:
    total = 0
    for path in sorted(CHAPTERS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        tokens = find_stray_tokens(text)
        if not tokens:
            continue
        fixed, changed = escape_in_source(text, tokens)
        if changed:
            path.write_text(fixed, encoding="utf-8", newline="\n")
            total += changed
            print(f"{path.name[:44]:46s} escaped={changed} :: {tokens}")
        else:
            print(f"{path.name[:44]:46s} UNRESOLVED :: {tokens}", file=sys.stderr)
            return 1
    print(f"total escaped: {total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
