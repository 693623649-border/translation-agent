"""Source-to-knowledge-base ingestion with a strict Chinese Word fidelity gate.

This module is a *stdout-clean* JSON gateway that adds the missing third entry
point to this repository's data pipeline.  The README's pipeline overview has
three ways into the knowledge base — scanned/OCR PDF, text PDF/EPUB through
semantic import, and pasted articles through ``txt_article_kb`` — and every one
of them ends at ``knowledge_base.jsonl``.  What none of them offers is a single
command that turns an arbitrary *source file* into both

* a canonical five-field ``knowledge_base.jsonl`` corpus, and
* a reader-facing Chinese ``.docx``,

while *proving*, character by character, that the Word text is exactly the
source text.  ``translation-agent-kb derive-docx`` is the closest existing
command, but it reads a DOCX and writes only a corpus; it never checks that
what reached the page is what the source said.

Why fidelity needs its own gate
-------------------------------
The repository already compares source Markdown with DOCX text in
``publication_verifier._check_docx`` (issue ``docx_chapter_text_mismatch``).
That check is exact but *binary*: it reports two SHA-256 digests and two
character counts, so an operator learns that 3 characters vanished but not
which ones.  It also derives its expectation from ``chapters/*.md`` inside a
pipeline output directory, which a standalone source file does not have.

This module therefore keeps that normalization chain — the same
``_docx_markdown_body`` → ``_normalize_wrapped_markdown_for_docx`` →
``_markdown_visible_text`` → ``_canonical_visible_text`` sequence — so the
verdict cannot drift from the release gate, and adds the three things it lacks:

``missing`` / ``extra``
    An ordered character alignment (``difflib.SequenceMatcher`` with autojunk
    disabled) over the canonical texts, reporting exact counts, a similarity
    ratio and a bounded list of located edit hunks.  A passage that was
    reworded shows up as a paired delete/insert at one offset instead of two
    unrelated count deltas.

``linewrap``
    A package-level inspection of ``word/document.xml``.  Nothing in this
    repository currently counts a plain ``<w:br/>`` — the soft line break that
    a bad Markdown→Word conversion leaves behind mid-sentence.  Every such
    break is counted, located, and classified by whether it splits a sentence.

``trace``
    ``publication_verifier.TRACE_PATTERNS`` re-applied to the Word text, so
    model preambles, ``TRANSLATION_FAILED`` placeholders, source page markers
    and replacement characters cannot pass as body prose.

Every issue code is one the operator can act on, and every count is
recomputed from the documents at hand — no historical number is reused.

Commands
--------
``ingest``
    Source file → knowledge base + Word; see :func:`ingest_source`.
``verify-word``
    Re-run the fidelity gate on rows that already exist; see
    :func:`verify_word`.
``status``
    Corpus health for one workspace; see :func:`corpus_status`.

Every command prints exactly one JSON object on stdout and exits 0 on success,
or prints ``{"error": {...}}`` and exits 1.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import hashlib
import io
import json
import os
import re
import sys
import unicodedata
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_KNOWLEDGE_BASE = "knowledge_base.jsonl"
META_SIDECAR = "knowledge_base.meta.jsonl"
INGEST_REPORT = "audit/kb-ingest-report.json"
FIDELITY_REPORT = "audit/word-fidelity-report.json"

#: Paragraph chunk ceiling, matching ``book_pipeline``/``txt_article_kb``.
CHUNK_CHARS = 4000

#: Above this many edit ops the hunk list is truncated (counts stay exact).
MAX_HUNKS = 40

SOURCE_SUFFIXES = frozenset({".txt", ".text", ".md", ".markdown", ".docx", ".epub", ".pdf"})

# ── project .env (never overrides an already-exported variable) ───────────────

_ENV_LOADED = False


def load_project_env() -> None:
    """Export unset keys from the project ``.env`` into ``os.environ``.

    The language gate and the optional embedding registration read keys from the
    environment.  A CLI launched from a shell that never sourced ``.env`` would
    silently degrade to lexical-only retrieval, so the same loader every other
    entrypoint uses is applied here.  Existing variables always win and a
    malformed line is skipped rather than fatal.

    Returns
    -------
    None
        The process environment is updated in place, once per process.
    """

    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    path = PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


# ── stdout guard ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _stdout_to_stderr() -> Iterable[None]:
    """Redirect Python-level stdout writes onto stderr for the duration.

    Importing the pipeline modules can emit warnings.  Those must never share
    stdout with this module's one-JSON-document contract, so the block's text is
    replayed on stderr afterwards.
    """

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield
    text = buffer.getvalue()
    if text:
        sys.stderr.write(text)


def _load_pipeline() -> tuple[Any, Any]:
    """Import ``book_pipeline`` and ``publication_verifier`` behind the guard.

    Returns
    -------
    tuple
        ``(book_pipeline, publication_verifier)``.
    """

    with _stdout_to_stderr():
        import book_pipeline
        import publication_verifier

    return book_pipeline, publication_verifier


# =============================================================================
# section: import-fidelity helpers
# =============================================================================


def _import_fidelity_helpers() -> dict[str, Any]:
    """Return ``book_pipeline``/``publication_verifier`` helpers by exact name.

    Importing a private helper by name fails loudly here, at call time, instead
    of silently drifting if an upstream rename lands.  The names are the frozen
    normalization chain the release gate itself uses, so a rename would break
    ``docx_chapter_text_mismatch`` too and must be handled deliberately.

    Returns
    -------
    dict
        The requested callables.

    Raises
    ------
    IngestError
        If an expected helper is missing from the upstream module.
    """

    book_pipeline, publication_verifier = _load_pipeline()
    wanted = {
        "docx_markdown_body": (publication_verifier, "_docx_markdown_body"),
        "markdown_visible_text": (publication_verifier, "_markdown_visible_text"),
        "canonical_visible_text": (publication_verifier, "_canonical_visible_text"),
        "normalize_wrapped_markdown": (
            book_pipeline,
            "_normalize_wrapped_markdown_for_docx",
        ),
        "split_text": (book_pipeline, "split_text"),
        "detect_language": (book_pipeline, "detect_language"),
    }
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for alias, (module, attribute) in wanted.items():
        value = getattr(module, attribute, None)
        if value is None:
            missing.append(f"{module.__name__}.{attribute}")
            continue
        resolved[alias] = value
    if missing:
        raise IngestError(
            "上游保真工具函数缺失，无法在不变更验收契约的前提下继续："
            + "、".join(missing)
        )
    return resolved


class IngestError(RuntimeError):
    """Raised when a source file cannot be ingested or a gate cannot run."""


# =============================================================================
# section: markdown chapter model
# =============================================================================


class Chapter(dict):
    """One chapter: ``{"title", "body", "level"}`` (a dict for JSON output)."""


_ATX_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERED_HEADING = re.compile(
    r"^(?:第[一二三四五六七八九十百千零〇0-9]+[章节節篇部卷讲講]"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|\d+(?:\.\d+)*[、.．]?\s+\S"
    r"|(?:序|前言|緒論|绪论|导论|導論|引言|后记|後記|结语|結語|结论|結論|附录|附錄|"
    r"索引|参考文献|參考文獻|译后记|譯後記|跋|凡例|摘要|余论|餘論)"
    r")"
)
_PUNCTUATION = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "（": "(", "）": ")", "《": "<", "》": ">",
    "“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "…": ".",
    "「": '"', "」": '"', "『": '"', "』": '"', "．": ".", "　": " ",
}


def normalize_for_match(value: str) -> str:
    """Fold text for comparison: NFKC, CJK punctuation→ASCII, whitespace, case.

    Traditional characters are deliberately *not* converted to simplified: the
    Word document must reproduce the character set the source actually prints,
    and silently accepting the other set would hide exactly the corruption this
    gate exists to catch.

    Parameters
    ----------
    value : str
        Raw text.

    Returns
    -------
    str
        The folded text.
    """

    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(_PUNCTUATION.get(char, char) for char in folded)
    return re.sub(r"\s+", " ", folded).strip().casefold()


def slugify(value: str, *, limit: int = 48) -> str:
    """Return a stable, filesystem- and ID-safe slug.

    Parameters
    ----------
    value : str
        Source label.
    limit : int, optional
        Maximum slug length.

    Returns
    -------
    str
        The slug, never empty.
    """

    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff-]+", "-", text.strip())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:limit] or "sec"


def parse_markdown_chapters(text: str) -> list[Chapter]:
    """Split Markdown into chapters at its shallowest ATX heading level.

    Using the *shallowest* level means ``# 书`` / ``## 章`` nests correctly, and
    a file whose only headings are ``##`` still splits into chapters instead of
    collapsing into one blob.

    Parameters
    ----------
    text : str
        Markdown source.

    Returns
    -------
    list of Chapter
        Chapters in document order; a single ``全文`` chapter when no heading
        exists.
    """

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*```", line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _ATX_HEADING.match(line.strip())
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    if not headings:
        return [Chapter(title="全文", body=text.strip(), level=0)]
    shallowest = min(level for _, level, _ in headings)
    chapters: list[Chapter] = []
    preamble = "\n".join(lines[: headings[0][0]]).strip()
    for position, (index, level, title) in enumerate(headings):
        if level != shallowest:
            continue
        end = len(lines)
        for later_index, later_level, _ in headings:
            if later_index > index and later_level == shallowest:
                end = later_index
                break
        body = "\n".join(lines[index + 1 : end]).strip()
        if position == 0 and preamble:
            body = f"{preamble}\n\n{body}".strip()
        chapters.append(Chapter(title=title, body=body, level=level))
    return [chapter for chapter in chapters if chapter["body"]] or [
        Chapter(title="全文", body=text.strip(), level=0)
    ]


def parse_plaintext_chapters(text: str) -> list[Chapter]:
    """Split plain text at standalone numbered/section headings.

    A heading must be a *whole* short line, so a sentence that merely starts
    with ``一、`` keeps its place in the preceding paragraph.

    Parameters
    ----------
    text : str
        Plain text source.

    Returns
    -------
    list of Chapter
        Chapters in document order; ``全文`` when nothing matches.
    """

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 60:
            continue
        if _NUMBERED_HEADING.match(stripped) and not stripped.endswith(
            ("。", "，", "；", "：", ".", ",", ";", ":")
        ):
            headings.append((index, stripped))
    if not headings:
        return [Chapter(title="全文", body=text.strip(), level=0)]
    chapters: list[Chapter] = []
    preamble = "\n".join(lines[: headings[0][0]]).strip()
    for position, (index, title) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body = "\n".join(lines[index + 1 : end]).strip()
        if position == 0 and preamble:
            body = f"{preamble}\n\n{body}".strip()
        if body:
            chapters.append(Chapter(title=title, body=body, level=1))
    return chapters or [Chapter(title="全文", body=text.strip(), level=0)]


def parse_docx_chapters(path: Path) -> list[Chapter]:
    """Read a DOCX into chapters using its ``Heading N`` structure.

    Parameters
    ----------
    path : pathlib.Path
        Source ``.docx``.

    Returns
    -------
    list of Chapter
        Chapters in body order.

    Raises
    ------
    IngestError
        If ``python-docx`` is unavailable or the file has no headings.
    """

    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise IngestError("读取 DOCX 需要 python-docx") from exc

    document = Document(str(path))
    chapters: list[Chapter] = []
    current: Chapter | None = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        style = str(getattr(getattr(paragraph, "style", None), "name", "") or "")
        if style.startswith("Heading ") and text:
            current = Chapter(title=text, body="", level=int(style.split()[-1] or 1))
            chapters.append(current)
            continue
        if current is not None and text:
            current["body"] = (current["body"] + "\n\n" + text).strip()
    chapters = [chapter for chapter in chapters if chapter["body"]]
    if not chapters:
        raise IngestError(
            f"DOCX 没有可识别的标题章节：{path}；"
            "请先补 Heading 结构，或改用语义导入（epub_semantic_import / docx_semantic_migration）"
        )
    return chapters


class _HtmlTextExtractor(HTMLParser):
    """Collect visible text with block-level elements turned into paragraphs.

    Heading text is collected separately as well as in the flow: the caller uses
    the headings to *name* a chapter and must be able to remove them from the
    body, otherwise the title would be written twice.
    """

    _BLOCK = frozenset(
        {
            "p", "div", "section", "article", "li", "blockquote", "tr",
            "h1", "h2", "h3", "h4", "h5", "h6", "br",
        }
    )
    _HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._heading_depth = 0
        self._heading_buffer: list[str] = []
        self._headings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head", "title"}:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
            if tag in self._HEADINGS:
                self._heading_depth += 1
                self._heading_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head", "title"}:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")
            if tag in self._HEADINGS and self._heading_depth:
                self._heading_depth -= 1
                text = " ".join("".join(self._heading_buffer).split())
                if text:
                    self._headings.append(text)
                self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if data.strip():
            self.parts.append(data)
            if self._heading_depth:
                self._heading_buffer.append(data)

    def text(self) -> str:
        """Return the collected text with blank-line paragraph separation."""

        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{2,}", "\n\n", raw)
        return raw.strip()

    def heading_texts(self) -> list[str]:
        """Return every heading's text, in document order."""

        return list(self._headings)


def _drop_heading_paragraphs(
    paragraphs: Sequence[str],
    headings: Sequence[str],
) -> list[str]:
    """Remove paragraphs that merely repeat a document heading.

    Only a *leading run* of heading-equal paragraphs is dropped, and each
    heading is consumed once. A sentence in the middle of the prose that happens
    to equal a heading is therefore preserved, and a heading that legitimately
    introduces a subsection deeper in the body is not silently deleted.

    Parameters
    ----------
    paragraphs : sequence of str
        Paragraphs extracted from one EPUB document.
    headings : sequence of str
        Heading texts found in the same document.

    Returns
    -------
    list of str
        Paragraphs with the consumed leading headings removed.
    """

    remaining = list(paragraphs)
    pending = list(headings)
    while remaining and pending:
        head = remaining[0].strip()
        match = next((item for item in pending if item == head), None)
        if match is None:
            break
        pending.remove(match)
        remaining.pop(0)
    return remaining


def _split_epub_paragraphs(html_text: str) -> list[str]:
    """Convert extracted HTML text into paragraphs, preserving blank lines."""

    cleaned = html_text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", cleaned)
    paragraphs = [" ".join(block.split()) for block in blocks]
    return [paragraph for paragraph in paragraphs if paragraph]


def parse_epub_chapters(path: Path) -> list[Chapter]:
    """Read an EPUB in spine order, splitting on heading elements.

    The project's own EPUB front door is ``epub_semantic_import``; it is the
    richer path and should be preferred for publication-grade work.  This reader
    is the standalone fallback for the common case, and it is deliberately
    stdlib-only so the plugin has no new dependency.  It fails closed rather than
    guessing when reading order cannot be established.

    Parameters
    ----------
    path : pathlib.Path
        Source ``.epub``.

    Returns
    -------
    list of Chapter
        Chapters in spine order.

    Raises
    ------
    IngestError
        If the archive, the OPF package or the spine cannot be read.
    """

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            container = archive.read("META-INF/container.xml").decode(
                "utf-8", errors="replace"
            )
            rootfile = re.search(r'full-path="([^"]+)"', container)
            if not rootfile:
                raise IngestError(f"EPUB 缺少 OPF 根文件声明：{path}")
            opf_name = rootfile.group(1)
            opf = archive.read(opf_name).decode("utf-8", errors="replace")
            base = opf_name.rsplit("/", 1)[0] if "/" in opf_name else ""

            manifest: dict[str, str] = {}
            for match in re.finditer(r"<item\b[^>]*>", opf, flags=re.I):
                tag = match.group(0)
                item_id = re.search(r'id="([^"]+)"', tag)
                href = re.search(r'href="([^"]+)"', tag)
                if item_id and href:
                    manifest[item_id.group(1)] = href.group(1)

            spine_ids = re.findall(r'<itemref\b[^>]*idref="([^"]+)"', opf, flags=re.I)
            if not spine_ids:
                raise IngestError(
                    f"EPUB 缺少可用的 spine 阅读顺序：{path}；"
                    "请改用 epub_semantic_import.py 的语义导入"
                )

            chapters: list[Chapter] = []
            for item_id in spine_ids:
                href = manifest.get(item_id)
                if not href:
                    continue
                member = f"{base}/{href}" if base else href
                member = member.replace("//", "/")
                member = member.split("#", 1)[0]
                if member not in names:
                    continue
                payload = archive.read(member).decode("utf-8", errors="replace")
                extractor = _HtmlTextExtractor()
                extractor.feed(payload)
                paragraphs = _split_epub_paragraphs(extractor.text())
                # The document's own headings name the chapter and are written
                # back out as the chapter's H1, so leaving them in the body
                # would duplicate the title — and the publisher's running-title
                # stripper would then delete a prose line that merely resembles
                # it, losing real text.
                body_paragraphs = _drop_heading_paragraphs(
                    paragraphs, extractor.heading_texts()
                )
                if not body_paragraphs:
                    continue
                title_match = re.search(
                    r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", payload, flags=re.I | re.S
                )
                if title_match:
                    title = re.sub(r"<[^>]+>", "", title_match.group(1))
                    title = " ".join(title.split())
                else:
                    title = body_paragraphs[0][:60]
                chapters.append(
                    Chapter(
                        title=title or f"第{len(chapters) + 1}节",
                        body="\n\n".join(body_paragraphs),
                        level=1,
                    )
                )
    except zipfile.BadZipFile as exc:
        raise IngestError(f"EPUB 不是合法的 ZIP 容器：{path}") from exc
    except KeyError as exc:
        raise IngestError(f"EPUB 缺少必需成员 {exc}：{path}") from exc

    if not chapters:
        raise IngestError(f"EPUB 未解析出任何正文：{path}")
    return chapters


# =============================================================================
# section: source adapters
# =============================================================================


def read_source(path: Path) -> tuple[list[Chapter], str]:
    """Dispatch one source file to its adapter.

    Parameters
    ----------
    path : pathlib.Path
        Existing source file.

    Returns
    -------
    tuple
        ``(chapters, adapter_name)``.

    Raises
    ------
    IngestError
        On an unsupported suffix, a missing file, or an adapter failure.
    """

    suffix = path.suffix.lower()
    if suffix not in SOURCE_SUFFIXES:
        raise IngestError(
            f"不支持的源文件类型 {suffix or '<无后缀>'}：{path}；"
            f"支持 {'、'.join(sorted(SOURCE_SUFFIXES))}"
        )
    if suffix in {".md", ".markdown"}:
        return parse_markdown_chapters(
            path.read_text(encoding="utf-8-sig")
        ), "markdown-headings"
    if suffix in {".txt", ".text"}:
        return parse_plaintext_chapters(
            path.read_text(encoding="utf-8-sig")
        ), "plaintext-headings"
    if suffix == ".docx":
        return parse_docx_chapters(path), "docx-headings"
    if suffix == ".epub":
        return parse_epub_chapters(path), "epub-spine"
    return read_pdf_source(path), "pdf-text-layer"


def read_pdf_source(path: Path, *, minimum_characters: int = 200) -> list[Chapter]:
    """Extract a PDF's own text layer, refusing image-only scans.

    The repository's OCR path lives in ``book_pipeline.py`` behind a local
    PaddleOCR GPU container, and this plugin must never pretend to replace it.
    A scanned PDF therefore fails closed, and the error names the real command.

    Parameters
    ----------
    path : pathlib.Path
        Source ``.pdf``.
    minimum_characters : int, optional
        Below this many extracted characters the PDF counts as image-only.

    Returns
    -------
    list of Chapter
        Chapters split on printed heading patterns.

    Raises
    ------
    IngestError
        When PyMuPDF is missing, or the PDF carries no usable text layer.
    """

    try:
        import pymupdf  # type: ignore[import-not-found]
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency boundary.
            raise IngestError("读取 PDF 需要 PyMuPDF") from exc

    # PyMuPDF opens the path itself, and when it rejects a malformed file its
    # own constructor leaks the handle before raising — nothing the caller can
    # close. Reading the bytes here means this function always controls the
    # handle, so a rejected PDF cannot keep the source locked on Windows (which
    # would block the operator from moving or deleting it afterwards). The
    # source is small enough that holding it in memory is preferable to a
    # leaked lock; PyMuPDF requires a bytes-backed stream, not a file object.
    document = None
    try:
        payload = path.read_bytes()
        document = pymupdf.open(stream=payload, filetype="pdf")  # type: ignore[attr-defined]
        pages = [
            page.get_text("text") or ""  # type: ignore[attr-defined]
            for page in document
        ]
    except OSError as exc:
        raise IngestError(f"无法读取 PDF：{path}（{exc}）") from exc
    except Exception as exc:  # noqa: BLE001 - PyMuPDF raises its own types.
        raise IngestError(
            f"PDF 无法解析（{exc.__class__.__name__}: {exc}）：{path}。"
            "如果源文件已损坏，请重新导出；扫描件请改用带 OCR 的主流水线。"
        ) from exc
    finally:
        if document is not None:
            with contextlib.suppress(Exception):
                document.close()
    text = "\n\n".join(pages).strip()
    if len(text) < minimum_characters:
        raise IngestError(
            f"PDF 文字层为空或过短（{len(text)} 字符），判定为扫描件：{path}。"
            "请改用带 OCR 的主流水线：python graph_pipeline.py <pdf> -o <输出目录> "
            "--phase all --config pipeline.toml --recipe recipes/chinese-pdf-word.toml"
        )
    return parse_plaintext_chapters(text)


# =============================================================================
# section: corpus assembly
# =============================================================================


def build_corpus(
    source: Path,
    chapters: Sequence[Mapping[str, Any]],
    *,
    book_title: str,
    author: str = "",
    language: str = "zh",
    chunk_chars: int = CHUNK_CHARS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build five-field corpus rows and the routing sidecar for one source.

    Row identity follows the repository's existing convention — a SHA-1 over
    source name, chapter id, chunk index and the *chapter identity*, not the
    chunk bytes — so re-ingesting an unchanged source reproduces byte-identical
    IDs and the downstream embedding cache and global index stay valid.  This is
    deliberately the same discipline as
    ``book_pipeline.build_knowledge_rows_from_manifest``.

    Parameters
    ----------
    source : pathlib.Path
        Source file the rows are derived from.
    chapters : sequence of mapping
        ``{"title", "body"}`` entries.
    book_title : str
        Display title used as the ``[书名]`` title prefix.
    author : str, optional
        Author recorded in the sidecar.
    language : str, optional
        Language recorded in the sidecar.
    chunk_chars : int, optional
        Paragraph-boundary chunk ceiling.

    Returns
    -------
    tuple
        ``(rows, sidecar_rows)``.
    """

    split_text = _import_fidelity_helpers()["split_text"]
    book_id = f"01_{slugify(book_title)}"
    rows: list[dict[str, Any]] = []
    sidecar: list[dict[str, Any]] = []
    order = 0
    for chapter in chapters:
        title = str(chapter.get("title") or "").strip() or f"第{order + 1}节"
        body = str(chapter.get("body") or "").strip()
        if not body:
            continue
        chapter_slug = slugify(title)
        chapter_id = f"{book_id}:{chapter_slug}"
        for chunk_index, chunk in enumerate(split_text(body, chunk_chars), start=1):
            order += 1
            row_id = hashlib.sha1(
                f"{source.name}:{chapter_id}:{chunk_index}".encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "id": row_id,
                    "title": f"[{book_title}] {title}",
                    "chapter_id": chapter_id,
                    "chapter_order": order,
                    "content": chunk,
                }
            )
            sidecar.append(
                {
                    "id": row_id,
                    "book_id": book_id,
                    "book_title": book_title,
                    "author": author,
                    "language": language,
                    "source_path": str(source),
                }
            )
    if not rows:
        raise IngestError(f"源文件未产生任何正文块：{source}")
    return rows, sidecar


def write_corpus(
    output_dir: Path,
    rows: Sequence[Mapping[str, Any]],
    sidecar: Sequence[Mapping[str, Any]],
) -> dict[str, Path]:
    """Write the corpus, sidecar and RAG manifest; annotate apparatus.

    ``initialize_rag_manifest`` also materialises
    ``knowledge_base.apparatus.json`` through ``rag_apparatus``, so structural
    apparatus chunks receive their retrieval demotion without a second command.

    Parameters
    ----------
    output_dir : pathlib.Path
        Workspace directory.
    rows : sequence of mapping
        Five-field corpus rows.
    sidecar : sequence of mapping
        Metadata sidecar rows.

    Returns
    -------
    dict
        Resolved artifact paths.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = output_dir / DEFAULT_KNOWLEDGE_BASE
    with corpus_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    meta_path = output_dir / META_SIDECAR
    with meta_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in sidecar:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with _stdout_to_stderr():
        from rag_knowledge_base import initialize_rag_manifest

        initialize_rag_manifest(corpus_path)
    return {
        "corpus": corpus_path,
        "meta": meta_path,
        "apparatus": output_dir / "knowledge_base.apparatus.json",
        "manifest": output_dir / "knowledge_base.rag.json",
    }


def language_gate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Classify every chunk for untranslated foreign prose.

    Parameters
    ----------
    rows : sequence of mapping
        Corpus rows.

    Returns
    -------
    dict
        ``{"chunk_count", "pending_count", "languages", "pending", "exempt"}``.
    """

    with _stdout_to_stderr():
        from kb_translation import classify_row

    pending: list[dict[str, Any]] = []
    exempt: dict[str, int] = {}
    for row in rows:
        verdict = classify_row(str(row.get("title") or ""), str(row.get("content") or ""))
        if verdict["needs_translation"]:
            pending.append(
                {
                    "id": row.get("id"),
                    "title": str(row.get("title") or ""),
                    "language": verdict["language"],
                }
            )
        else:
            reason = str(verdict["reason"])
            exempt[reason] = exempt.get(reason, 0) + 1
    return {
        "chunk_count": len(rows),
        "pending_count": len(pending),
        "languages": sorted({str(item["language"]) for item in pending}),
        "pending": pending[:10],
        "exempt": exempt,
    }


def build_chapter_markdown(chapters: Sequence[Mapping[str, Any]]) -> str:
    """Render chapters as ``# title`` Markdown for the Word publisher.

    Parameters
    ----------
    chapters : sequence of mapping
        ``{"title", "body"}`` entries.

    Returns
    -------
    str
        Markdown with one H1 per chapter.
    """

    blocks: list[str] = []
    for chapter in chapters:
        title = str(chapter.get("title") or "").strip()
        body = str(chapter.get("body") or "").strip()
        if not title or not body:
            continue
        blocks.append(f"# {title}\n\n{body}")
    return "\n\n".join(blocks).rstrip() + "\n"


def publish_docx(
    output_dir: Path,
    chapters: Sequence[Mapping[str, Any]],
    *,
    book_title: str,
    author: str = "",
) -> Path:
    """Publish the reader-facing Chinese Word document.

    Layout is delegated to ``book_pipeline.build_docx`` so the Word file carries
    the same A4 geometry, SimSun body style, Heading 1 page breaks, page-number
    footer and real-footnote contract as every other artifact this project
    releases.  The chapter Markdown is written as the audit source of truth that
    the fidelity gate reads back.

    Parameters
    ----------
    output_dir : pathlib.Path
        Workspace directory.
    chapters : sequence of mapping
        ``{"title", "body"}`` entries.
    book_title : str
        Title printed on the cover page and in the document properties.
    author : str, optional
        Author line, when known.

    Returns
    -------
    pathlib.Path
        The published ``.docx``.
    """

    book_pipeline, _verifier = _load_pipeline()
    chapter_dir = output_dir / "chapters"
    chapter_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    for sequence, chapter in enumerate(chapters, start=1):
        title = str(chapter.get("title") or "").strip()
        body = str(chapter.get("body") or "").strip()
        if not title or not body:
            continue
        filename = f"{sequence:03d}_{slugify(title, limit=40)}.md"
        # ``build_docx`` reads the chapter file back from disk and applies
        # ``strip_publication_metadata`` itself, so the fidelity gate must
        # reproduce that exact step rather than compare against the raw body.
        # The raw file stays on disk as the audit source of truth.
        (chapter_dir / filename).write_text(
            f"# {title}\n\n{body}\n", encoding="utf-8", newline="\n"
        )
        source_markdown = f"# {title}\n\n{body}\n"
        published = book_pipeline.strip_publication_metadata(
            source_markdown,
            publication_title=book_title,
            chapter_title=title,
        )
        manifest.append(
            {
                "id": f"src-{sequence:04d}",
                "filename": filename,
                "title": title,
                "display_title": title,
                "sequence": sequence,
                "pdf_page": sequence,
                "end_pdf_page": sequence,
                "published_markdown": published,
            }
        )
    if not manifest:
        raise IngestError("没有任何章节可发布为 Word")

    docx_path = output_dir / f"{slugify(book_title)}.docx"
    with _stdout_to_stderr():
        book_pipeline.build_docx(
            docx_path,
            chapter_dir,
            manifest,
            book_title=book_title,
            author=author or None,
        )
    (output_dir / "chapters.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return docx_path


# =============================================================================
# section: DOCX package inspection
# =============================================================================

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _local_name(tag: Any) -> str:
    """Return an element's local name, namespace-agnostic."""

    text = str(tag)
    return text.rsplit("}", 1)[-1]


def inspect_docx_package(path: Path) -> dict[str, Any]:
    """Read ``word/document.xml`` for text runs, paragraphs and line breaks.

    ``python-docx`` exposes paragraph text but not the difference between an
    author's hard line break and a conversion artefact, so the package is parsed
    directly.  Body order is preserved and each paragraph records its style,
    its explicit page-break count, and every non-page ``<w:br/>``/``<w:cr>``.

    Parameters
    ----------
    path : pathlib.Path
        Existing ``.docx``.

    Returns
    -------
    dict
        ``{"path", "content_sha256", "paragraphs", "line_breaks",
        "explicit_page_breaks", "tables", "core_title"}``.

    Raises
    ------
    IngestError
        If the package is not a readable DOCX.
    """

    import xml.etree.ElementTree as ET
    from html import unescape

    try:
        with zipfile.ZipFile(path) as archive:
            payload = archive.read("word/document.xml")
            try:
                core = archive.read("docProps/core.xml").decode("utf-8", "replace")
            except KeyError:
                core = ""
    except (zipfile.BadZipFile, KeyError) as exc:
        raise IngestError(f"无法读取 DOCX 包：{path}（{exc}）") from exc

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise IngestError(f"DOCX 的 word/document.xml 不是合法 XML：{path}（{exc}）") from exc

    body = root.find(f"{{{_W_NS}}}body")
    if body is None:
        raise IngestError(f"DOCX 缺少 w:body：{path}")

    paragraphs: list[dict[str, Any]] = []
    line_breaks: list[dict[str, Any]] = []
    page_breaks = 0
    table_count = 0

    def paragraph_text(element: Any) -> str:
        chunks: list[str] = []
        for node in element.iter():
            name = _local_name(node.tag)
            if name == "t":
                chunks.append(node.text or "")
            elif name in {"br", "cr"} and _break_kind(node) != "page":
                chunks.append("\n")
            elif name == "tab":
                chunks.append("\t")
        return unescape("".join(chunks))

    def _break_kind(node: Any) -> str:
        for key, value in node.attrib.items():
            if _local_name(key) == "type":
                return str(value)
        return "textWrapping"

    for index, element in enumerate(body):
        name = _local_name(element.tag)
        if name == "p":
            style = ""
            style_node = element.find(f".//{{{_W_NS}}}pStyle")
            if style_node is not None:
                for key, value in style_node.attrib.items():
                    if _local_name(key) == "val":
                        style = str(value)
            text = paragraph_text(element)
            breaks = 0
            own_page_break = False
            for node in element.iter():
                node_name = _local_name(node.tag)
                if node_name in {"br", "cr"}:
                    if _break_kind(node) == "page":
                        page_breaks += 1
                        own_page_break = True
                    else:
                        breaks += 1
            if breaks:
                line_breaks.append(
                    {
                        "paragraph_index": len(paragraphs),
                        "style": style,
                        "count": breaks,
                        "text": text[:200],
                    }
                )
            paragraphs.append(
                {
                    "index": len(paragraphs),
                    "body_index": index,
                    "style": style,
                    "text": text,
                    "line_break_count": breaks,
                    "page_break_count": 1 if own_page_break else 0,
                }
            )
        elif name == "tbl":
            table_count += 1

    title = ""
    title_match = re.search(r"<dc:title>(.*?)</dc:title>", core, flags=re.S)
    if title_match:
        title = unescape(title_match.group(1)).strip()

    return {
        "path": str(path),
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "paragraphs": paragraphs,
        "line_breaks": line_breaks,
        "explicit_page_breaks": page_breaks,
        "tables": table_count,
        "core_title": title,
        "cover_end": _cover_end_index(paragraphs),
    }


def style_key(value: Any) -> str:
    """Normalize a ``w:pStyle`` id or a ``python-docx`` style name.

    OOXML stores style ids without the spaces a display name carries
    (``CodexBookTitle`` vs ``Codex Book Title``), and the two APIs disagree
    about which one they report.  Comparing on a space- and case-folded key
    makes both views identify the same style.

    Parameters
    ----------
    value : Any
        Raw style id or name.

    Returns
    -------
    str
        Lowercase alphanumeric key, empty for a missing style.
    """

    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


#: Normalized keys of the styles ``build_docx`` uses only for the cover page.
_TITLE_STYLE_KEYS = frozenset({"title", "codexbooktitle"})

#: Normalized key of the chapter-heading style.
_HEADING1_STYLE_KEYS = frozenset({"heading1"})


def _cover_end_index(paragraphs: Sequence[Mapping[str, Any]]) -> int:
    """Return the first paragraph index past the generated cover page.

    ``build_docx`` writes a book-title paragraph in a title style, an optional
    author line in ``Normal``, and then an empty paragraph carrying an explicit
    page break.  The author line and the page-break paragraph are both plain
    ``Normal`` with empty-or-text content, so the boundary cannot be found from
    styles alone; the explicit page break the publisher always emits is the
    reliable marker.  Requiring it also means a document *without* a cover page
    is compared in full rather than losing its opening.

    Parameters
    ----------
    paragraphs : sequence of mapping
        Paragraphs from :func:`inspect_docx_package`.

    Returns
    -------
    int
        Index of the first body paragraph; 0 when no cover page is present.
    """

    last_cover = -1
    for index, paragraph in enumerate(paragraphs):
        if style_key(paragraph.get("style")) in _TITLE_STYLE_KEYS:
            last_cover = index
    if last_cover < 0:
        return 0
    for index in range(last_cover + 1, len(paragraphs)):
        if int(paragraphs[index].get("page_break_count") or 0) > 0:
            return index + 1
    return 0


def cover_prefix(paragraphs: Sequence[Mapping[str, Any]]) -> str:
    """Return the visible cover-page text that precedes the source body.

    Parameters
    ----------
    paragraphs : sequence of mapping
        Paragraphs from :func:`inspect_docx_package`.

    Returns
    -------
    str
        Newline-joined cover text, excluding the blank page-break paragraph.
    """

    cover_end = _cover_end_index(paragraphs)
    return "\n".join(
        str(paragraph.get("text") or "")
        for paragraph in paragraphs[:cover_end]
        if str(paragraph.get("text") or "").strip()
    )


# =============================================================================
# section: fidelity gate
# =============================================================================


def _canonical_from_markdown(markdown_text: str) -> str:
    """Normalize Markdown exactly as the Word publisher will render it.

    The chain mirrors ``publication_verifier._check_docx`` so a Word document
    that passes here cannot fail ``docx_chapter_text_mismatch`` for a
    normalization reason.

    Parameters
    ----------
    markdown_text : str
        Chapter Markdown.

    Returns
    -------
    str
        Canonical visible text.
    """

    helpers = _import_fidelity_helpers()
    body = helpers["docx_markdown_body"](markdown_text)
    normalized = helpers["normalize_wrapped_markdown"](body)
    visible = helpers["markdown_visible_text"](normalized)
    return helpers["canonical_visible_text"](visible)


def _docx_body_only(paragraphs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Return the source-bearing paragraphs, excluding the cover page.

    Parameters
    ----------
    paragraphs : sequence of mapping
        Paragraphs from :func:`inspect_docx_package`.

    Returns
    -------
    list of mapping
        Paragraphs from the first body-text paragraph onward.
    """

    return list(paragraphs[_cover_end_index(paragraphs) :])


def _heading_texts(paragraphs: Sequence[Mapping[str, Any]]) -> list[str]:
    """Return Heading 1 texts in body order, excluding the cover page."""

    return [
        str(paragraph.get("text") or "").strip()
        for paragraph in _docx_body_only(paragraphs)
        if style_key(paragraph.get("style")) in _HEADING1_STYLE_KEYS
    ]


_TERMINAL = "。！？!?…：:；;”』」』）)】"

#: Characters prose never begins a *paragraph* with.  A paragraph that starts
#: with one of these was cut out of the middle of a sentence, which is what an
#: unmerged OCR hard wrap looks like after conversion.  The set is deliberately
#: restricted to closing and joining marks: a paragraph legitimately begins with
#: an opening quote, a bullet, a digit or a CJK character.
_CONTINUATION_START = "，。、；：！？）》」』”’】,.);:!?]}"


def _splits_sentence(previous: str, following: str) -> bool:
    """Return whether a paragraph boundary lands mid-sentence.

    The test is intentionally narrow so it cannot fire on the shapes a book is
    legitimately made of — poetry, lists, dialogue, headings and short
    fragments.  It reports only the one arrangement prose cannot produce: the
    previous block does not end a sentence and the next block *begins* with a
    closing or joining mark.

    Parameters
    ----------
    previous : str
        Text of the paragraph carrying the break.
    following : str
        Text of the next non-empty paragraph.

    Returns
    -------
    bool
        True when the boundary looks like a conversion artefact.
    """

    left = previous.rstrip()
    right = following.lstrip()
    if not left or not right:
        return False
    if left.endswith(tuple(_TERMINAL)):
        return False
    if left.endswith(("-", "—", "…", "/")):
        return False
    return right[0] in _CONTINUATION_START


def check_line_wrapping(
    paragraphs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Count and locate every intra-paragraph line break in the Word body.

    Parameters
    ----------
    paragraphs : sequence of mapping
        Paragraphs from :func:`inspect_docx_package`.

    Returns
    -------
    dict
        Metrics plus an ``issues`` list.  Issue codes are
        ``docx_line_break_present`` and ``docx_sentence_split``.
    """

    issues: list[dict[str, Any]] = []
    body = _docx_body_only(paragraphs)
    flagged = [item for item in body if int(item.get("line_break_count") or 0) > 0]
    sentence_splits: list[dict[str, Any]] = []
    for position, item in enumerate(body):
        previous = str(item.get("text") or "")
        if not previous.strip():
            continue
        if previous.rstrip().endswith(tuple(_TERMINAL)):
            continue
        following = ""
        for later in body[position + 1 :]:
            candidate = str(later.get("text") or "").strip()
            if candidate:
                following = candidate
                break
        if _splits_sentence(previous, following):
            sentence_splits.append(
                {
                    "paragraph_index": item.get("index"),
                    "style": item.get("style"),
                    "text": previous[:120],
                    "next_text": following[:120],
                }
            )

    if flagged:
        issues.append(
            {
                "code": "docx_line_break_present",
                "message": (
                    f"Word 正文有 {len(flagged)} 个段落含段内换行（w:br/w:cr），"
                    "通常来自 OCR 硬换行或 Markdown 转换残留。"
                ),
                "evidence": {
                    "paragraph_count": len(flagged),
                    "break_count": sum(
                        int(item.get("line_break_count") or 0) for item in flagged
                    ),
                    "paragraphs": flagged[:MAX_HUNKS],
                },
            }
        )
    if sentence_splits:
        issues.append(
            {
                "code": "docx_sentence_split",
                "message": (
                    f"有 {len(sentence_splits)} 处段落在句子中间被切断，"
                    "下一段以标点开头，正文可读性受损。"
                ),
                "evidence": {
                    "count": len(sentence_splits),
                    "samples": sentence_splits[:MAX_HUNKS],
                },
            }
        )
    return {
        "metrics": {
            "body_paragraph_count": len(body),
            "paragraphs_with_line_breaks": len(flagged),
            "line_break_count": sum(
                int(item.get("line_break_count") or 0) for item in flagged
            ),
            "sentence_split_count": len(sentence_splits),
        },
        "issues": issues,
    }


def diff_texts(expected: str, actual: str) -> dict[str, Any]:
    """Align two canonical texts and locate every missing and extra character.

    An ordered alignment is used rather than a character multiset: a multiset
    hides reordering, and it cannot say *where* the text diverged.  Hunks are
    paired so a reworded passage reads as one substitution instead of an
    unrelated deletion plus insertion.  ``missing`` and ``extra`` counts are
    always exact even when the hunk list is truncated.

    Parameters
    ----------
    expected : str
        Canonical source text.
    actual : str
        Canonical Word text.

    Returns
    -------
    dict
        ``{"missing", "extra", "distance", "ratio", "hunks", "truncated"}``.
    """

    if expected == actual:
        return {
            "missing": 0,
            "extra": 0,
            "distance": 0,
            "ratio": 1.0,
            "hunks": [],
            "truncated": False,
        }
    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    missing = 0
    extra = 0
    hunks: list[dict[str, Any]] = []
    truncated = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        removed = expected[i1:i2]
        added = actual[j1:j2]
        missing += len(removed)
        extra += len(added)
        if len(hunks) < MAX_HUNKS:
            hunks.append(
                {
                    "kind": tag,
                    "expected_offset": i1,
                    "actual_offset": j1,
                    "missing_text": removed[:120],
                    "extra_text": added[:120],
                    "missing_length": len(removed),
                    "extra_length": len(added),
                    "context": expected[max(0, i1 - 30) : i2 + 30],
                }
            )
        else:
            truncated = True
    return {
        "missing": missing,
        "extra": extra,
        "distance": missing + extra,
        "ratio": round(matcher.ratio(), 6),
        "hunks": hunks,
        "truncated": truncated,
    }


def check_trace(text: str) -> dict[str, Any]:
    """Re-apply the release gate's trace patterns to the Word text.

    Parameters
    ----------
    text : str
        Canonical Word text.

    Returns
    -------
    dict
        ``{"metrics": {...}, "issues": [...]}``.
    """

    _book, verifier = _load_pipeline()
    issues: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for code, pattern in verifier.TRACE_PATTERNS:
        matches = list(pattern.finditer(text))
        counts[str(code)] = len(matches)
        if not matches:
            continue
        snippets = [
            re.sub(r"\s+", " ", match.group(0)).strip()[:160]
            for match in matches[:8]
        ]
        issues.append(
            {
                "code": f"docx_trace_{code}",
                "message": f"Word 正文检出 {len(matches)} 处发布痕迹或异常文本（{code}）。",
                "evidence": {"count": len(matches), "snippets": snippets},
            }
        )
    return {"metrics": {"trace_counts": counts}, "issues": issues}


def build_published_markdown(chapters: Sequence[Mapping[str, Any]]) -> str:
    """Return the text the publisher actually renders, per chapter.

    Each chapter carries ``published_markdown`` when it has been through
    :func:`publish_docx`, which is the exact string ``build_docx`` handed to
    ``_append_markdown_to_docx``.  Falling back to the raw chapter body keeps
    :func:`verify_word_document` usable on chapters that were never published
    (a caller comparing a hand-written expectation).

    Parameters
    ----------
    chapters : sequence of mapping
        ``{"title", "body"[, "published_markdown"]}`` entries.

    Returns
    -------
    str
        Markdown with one H1 per chapter.
    """

    blocks: list[str] = []
    for chapter in chapters:
        if not str(chapter.get("body") or "").strip():
            continue
        published = chapter.get("published_markdown")
        if isinstance(published, str) and published.strip():
            blocks.append(published.rstrip())
            continue
        title = str(chapter.get("title") or "").strip()
        blocks.append(f"# {title}\n\n{str(chapter.get('body') or '').strip()}")
    return "\n\n".join(blocks).rstrip() + "\n"


_SOURCE_PAGE_COMMENT = re.compile(r"<!--\s*(?:PDF_PAGE|pdf_page)\s*:\s*\d{1,6}\s*-->", re.I)
_SOURCE_STANDALONE_NUMBER = re.compile(r"^[-—–\s]*\d{1,3}[-—–\s]*$")


def detect_source_page_markers(
    chapters: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Find source page furniture in the chapter bodies before publication.

    A source file exported from a PDF pipeline can carry ``<!-- PDF_PAGE: n -->``
    comments and standalone printed page numbers.  Markdown rendering drops the
    comments, and the publisher's metadata stripper removes the numbers, so
    neither can reach the Word document — which is exactly why an operator must
    be *told* about them instead of discovering the loss as an unexplained
    character count.  Reporting them keeps the ledger complete without turning
    a correct removal into a failure.

    Parameters
    ----------
    chapters : sequence of mapping
        ``{"title", "body"}`` entries.

    Returns
    -------
    dict
        ``{"page_comments", "standalone_numbers", "count", "chapters"}``.
    """

    page_comments: list[dict[str, Any]] = []
    standalone_numbers: list[dict[str, Any]] = []
    for chapter in chapters:
        title = str(chapter.get("title") or "")
        body = str(chapter.get("body") or "")
        for match in _SOURCE_PAGE_COMMENT.finditer(body):
            page_comments.append(
                {"chapter": title, "marker": match.group(0).strip()}
            )
        lines = body.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or not _SOURCE_STANDALONE_NUMBER.fullmatch(stripped):
                continue
            # A lone short number bracketed by blank lines is page furniture;
            # a number inside a table or list keeps its context.
            before = lines[index - 1].strip() if index > 0 else ""
            after = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if before or after:
                continue
            standalone_numbers.append(
                {"chapter": title, "line": index + 1, "text": stripped}
            )
    listed = sorted({item["chapter"] for item in page_comments + standalone_numbers})
    return {
        "page_comments": page_comments[:50],
        "standalone_numbers": standalone_numbers[:50],
        "count": len(page_comments) + len(standalone_numbers),
        "chapters": listed,
    }


def report_publisher_stripping(
    chapters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Locate source text the publisher's metadata stripper removed.

    ``build_docx`` calls ``strip_publication_metadata``, which deletes source
    page markers and OCR running titles.  That is correct for a pipeline whose
    chapters already passed the sanitize stage, but a hand-supplied source file
    can legitimately contain such lines.  Reporting them separately turns an
    unexplained "missing characters" verdict into an actionable one: the
    operator sees the removed text and can either clean the source or accept
    the removal deliberately.

    Parameters
    ----------
    chapters : sequence of mapping
        ``{"title", "body"[, "published_markdown"]}`` entries.

    Returns
    -------
    list of dict
        One entry per chapter whose rendered text lost non-whitespace source
        text, with the removed fragments.
    """

    removed: list[dict[str, Any]] = []
    for chapter in chapters:
        published = chapter.get("published_markdown")
        if not isinstance(published, str):
            continue
        title = str(chapter.get("title") or "").strip()
        body = str(chapter.get("body") or "").strip()
        if not title or not body:
            continue
        canonical_source = _canonical_from_markdown(f"# {title}\n\n{body}\n")
        canonical_published = _canonical_from_markdown(published)
        if canonical_source == canonical_published:
            continue
        removed.append(
            {
                "chapter": title,
                "difference": diff_texts(canonical_source, canonical_published),
                "removed_snippets": [
                    hunk["missing_text"]
                    for hunk in diff_texts(canonical_source, canonical_published)["hunks"]
                    if hunk["missing_text"].strip()
                ][:10],
            }
        )
    return removed


def strip_whitespace(value: str) -> str:
    """Remove every whitespace character from a canonical text.

    Word cannot lose a *character* a reader would notice without also losing
    its glyph, but a paragraph boundary that Markdown renders as two spaces and
    Word renders as one is a legitimate layout difference with no missing
    content.  Comparing the whitespace-free forms separates the two, so a
    report can say precisely which happened instead of reporting "1 character
    missing" for a collapsed blank line.

    Parameters
    ----------
    value : str
        Canonical visible text.

    Returns
    -------
    str
        The text with all whitespace removed.
    """

    return re.sub(r"\s+", "", value)


def verify_word_document(
    docx_path: Path,
    chapters: Sequence[Mapping[str, Any]],
    *,
    book_title: str = "",
    allow_line_breaks: bool = False,
) -> dict[str, Any]:
    """Prove a Word document reproduces the source text character for character.

    Parameters
    ----------
    docx_path : pathlib.Path
        Published ``.docx``.
    chapters : sequence of mapping
        The ``{"title", "body"}`` chapters that were published, not a re-read of
        the source.  Passing the in-memory model makes the comparison a true
        round trip through the publisher.
    book_title : str, optional
        Expected document title, checked against ``docProps/core.xml``.
    allow_line_breaks : bool, optional
        Downgrade the line-break findings to warnings.  Use only when the source
        genuinely is verse or a list-heavy document.

    Returns
    -------
    dict
        ``{"status", "ok", "missing_characters", "extra_characters",
        "similarity", "issues", "warnings", "metrics", "hunks"}``.

    Raises
    ------
    IngestError
        If the DOCX cannot be read.
    """

    package = inspect_docx_package(docx_path)
    paragraphs = package["paragraphs"]
    body = _docx_body_only(paragraphs)
    actual_raw = "\n".join(str(item["text"]) for item in body)
    canonical_actual = _import_fidelity_helpers()["canonical_visible_text"](actual_raw)

    markdown = build_published_markdown(chapters)
    canonical_expected = _canonical_from_markdown(markdown)

    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    stripped = report_publisher_stripping(chapters)
    if stripped:
        issues.append(
            {
                "code": "docx_source_metadata_stripped",
                "message": (
                    f"发布器在 {len(stripped)} 个章节中删除了源文件里的页码/书眉类内容。"
                    "这些行不会是印刷正文；请清理源文件后重跑，或确认删除是有意为之。"
                ),
                "evidence": {"chapters": stripped},
            }
        )

    source_markers = detect_source_page_markers(chapters)
    if source_markers["count"]:
        warnings.append(
            {
                "code": "docx_source_page_markers_present",
                "message": (
                    f"源文件含 {source_markers['count']} 处来源页码痕迹"
                    "（PDF_PAGE 注释或独立页码）；渲染时会被移除，不影响正文保真。"
                    "若要交付纯净源，请在入库前清理。"
                ),
                "evidence": source_markers,
            }
        )

    difference = diff_texts(canonical_expected, canonical_actual)
    content_difference = diff_texts(
        strip_whitespace(canonical_expected), strip_whitespace(canonical_actual)
    )
    if content_difference["missing"] or content_difference["extra"]:
        issues.append(
            {
                "code": "docx_text_mismatch",
                "message": (
                    f"Word 正文与源文件不一致：缺字 {content_difference['missing']} 个、"
                    f"多字 {content_difference['extra']} 个"
                    f"（相似度 {content_difference['ratio']}）。"
                ),
                "evidence": {
                    "missing_characters": content_difference["missing"],
                    "extra_characters": content_difference["extra"],
                    "similarity": content_difference["ratio"],
                    "expected_sha256": hashlib.sha256(
                        canonical_expected.encode("utf-8")
                    ).hexdigest(),
                    "actual_sha256": hashlib.sha256(
                        canonical_actual.encode("utf-8")
                    ).hexdigest(),
                    "hunks": content_difference["hunks"],
                },
            }
        )
    elif difference["missing"] or difference["extra"]:
        warnings.append(
            {
                "code": "docx_whitespace_only_difference",
                "message": (
                    "Word 与源文件只在空白折叠上有差异，正文字符一致："
                    f"空白差 {difference['missing'] + difference['extra']} 处。"
                ),
                "evidence": {
                    "whitespace_missing": difference["missing"],
                    "whitespace_extra": difference["extra"],
                    "total_missing": difference["missing"],
                    "total_extra": difference["extra"],
                },
            }
        )

    expected_headings = [
        str(chapter.get("title") or "").strip()
        for chapter in chapters
        if str(chapter.get("body") or "").strip()
    ]
    actual_headings = _heading_texts(paragraphs)
    if expected_headings != actual_headings:
        issues.append(
            {
                "code": "docx_heading_mismatch",
                "message": "Word 的 Heading 1 章节标题与源文件章节不一致。",
                "evidence": {
                    "expected": expected_headings[:20],
                    "actual": actual_headings[:20],
                },
            }
        )

    wrapping = check_line_wrapping(paragraphs)
    if allow_line_breaks:
        warnings.extend(wrapping["issues"])
    else:
        issues.extend(wrapping["issues"])

    trace = check_trace(canonical_actual)
    issues.extend(trace["issues"])

    if book_title:
        expected_title = slugify(book_title)
        actual_title = str(package.get("core_title") or "")
        if actual_title and official_title(actual_title) != official_title(book_title):
            warnings.append(
                {
                    "code": "docx_core_title_mismatch",
                    "message": "Word 文档属性标题与本次书名不一致。",
                    "evidence": {
                        "expected": book_title,
                        "actual": actual_title,
                        "expected_slug": expected_title,
                    },
                }
            )

    return {
        "status": "passed" if not issues else "failed",
        "ok": not issues,
        "docx": str(docx_path),
        # Content counts are the verdict: whitespace folding between Markdown
        # and Word is reported separately and never fails the gate.
        "missing_characters": content_difference["missing"],
        "extra_characters": content_difference["extra"],
        "similarity": content_difference["ratio"],
        "whitespace_only_missing": difference["missing"] - content_difference["missing"],
        "whitespace_only_extra": difference["extra"] - content_difference["extra"],
        "issues": issues,
        "warnings": warnings,
        "hunks": content_difference["hunks"],
        "hunks_truncated": content_difference["truncated"],
        "metrics": {
            "expected_characters": len(canonical_expected),
            "actual_characters": len(canonical_actual),
            "expected_content_characters": len(strip_whitespace(canonical_expected)),
            "actual_content_characters": len(strip_whitespace(canonical_actual)),
            "raw_missing_characters": difference["missing"],
            "raw_extra_characters": difference["extra"],
            "source_page_marker_count": source_markers["count"],
            "publisher_stripped_chapter_count": len(stripped),
            "paragraph_count": len(paragraphs),
            "heading_count": len(actual_headings),
            "table_count": package["tables"],
            "explicit_page_breaks": package["explicit_page_breaks"],
            "content_sha256": package["content_sha256"],
            "canonical_expected_sha256": hashlib.sha256(
                canonical_expected.encode("utf-8")
            ).hexdigest(),
            "canonical_actual_sha256": hashlib.sha256(
                canonical_actual.encode("utf-8")
            ).hexdigest(),
            "content_expected_sha256": hashlib.sha256(
                strip_whitespace(canonical_expected).encode("utf-8")
            ).hexdigest(),
            "content_actual_sha256": hashlib.sha256(
                strip_whitespace(canonical_actual).encode("utf-8")
            ).hexdigest(),
            **wrapping["metrics"],
            **trace["metrics"],
        },
    }


def official_title(value: str) -> str:
    """Fold a title for comparison, ignoring case, width and punctuation."""

    text = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff]+", "", text)


# =============================================================================
# section: commands
# =============================================================================


def resolve_workspace(value: str | Path, *, create: bool = False) -> Path:
    """Resolve a workspace name or path to an output directory.

    A bare name resolves under ``outputs/`` first, so a same-named directory in
    the working directory can never shadow a real workspace.

    Parameters
    ----------
    value : str or pathlib.Path
        Workspace name or path.
    create : bool, optional
        Allow a path that does not exist yet.

    Returns
    -------
    pathlib.Path
        The resolved directory.

    Raises
    ------
    IngestError
        When the directory does not exist and ``create`` is false.
    """

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        under_outputs = (PROJECT_ROOT / "outputs" / candidate).resolve()
        resolved = under_outputs if under_outputs.parent.is_dir() else candidate.resolve()
    if not resolved.exists() and not create:
        raise IngestError(f"工作区目录不存在：{resolved}")
    return resolved


def ingest_source(
    source: str | Path,
    *,
    output_dir: str | Path | None = None,
    title: str | None = None,
    author: str = "",
    language: str = "zh",
    allow_foreign: bool = False,
    allow_line_breaks: bool = False,
    word: bool = True,
    chunk_chars: int = CHUNK_CHARS,
) -> dict[str, Any]:
    """Ingest one source file into a knowledge base and a verified Chinese Word.

    The sequence is deliberately fail-closed:

    1. read the source through its adapter (image-only PDFs are refused, never
       guessed at);
    2. build the five-field corpus and the routing sidecar;
    3. run the language gate — every chunk that is not Chinese blocks the run
       unless ``allow_foreign`` is set, matching ``translation-agent-kb
       register``;
    4. publish the Word document through the project's own ``build_docx``;
    5. prove the Word text equals the source text, character for character.

    The corpus and the Word are only reported as written when step 5 passes;
    otherwise the Word is still on disk for inspection but ``status`` is
    ``failed`` and the exact missing/extra counts are returned.

    Parameters
    ----------
    source : str or pathlib.Path
        Source file (``.txt``, ``.md``, ``.docx``, ``.epub``, ``.pdf``).
    output_dir : str or pathlib.Path or None, optional
        Workspace directory; defaults to ``outputs/<source stem>``.
    title : str or None, optional
        Book title; defaults to the source file stem.
    author : str, optional
        Author recorded in the sidecar and printed under the title.
    language : str, optional
        Language tag recorded in the sidecar.
    allow_foreign : bool, optional
        Register untranslated foreign chunks as-is instead of failing.
    allow_line_breaks : bool, optional
        Treat intra-paragraph line breaks as warnings instead of failures.
    word : bool, optional
        Publish and verify the Word document.
    chunk_chars : int, optional
        Paragraph chunk ceiling.

    Returns
    -------
    dict
        A JSON-ready ingest report.

    Raises
    ------
    IngestError
        If the source cannot be read or produces no corpus.
    """

    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise IngestError(f"源文件不存在：{source_path}")
    book_title = (title or source_path.stem).strip()
    workspace = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else (PROJECT_ROOT / "outputs" / slugify(book_title, limit=60)).resolve()
    )

    chapters, adapter = read_source(source_path)
    rows, sidecar = build_corpus(
        source_path,
        chapters,
        book_title=book_title,
        author=author,
        language=language,
        chunk_chars=max(int(chunk_chars), 200),
    )

    gate = language_gate(rows)
    if gate["pending_count"] and not allow_foreign:
        return {
            "status": "blocked",
            "ok": False,
            "stage": "language_gate",
            "source": str(source_path),
            "adapter": adapter,
            "workspace": str(workspace),
            "book_title": book_title,
            "chapter_count": len(chapters),
            "chunk_count": len(rows),
            "language_gate": gate,
            "error": {
                "kind": "language_gate",
                "message": (
                    f"语料含 {gate['pending_count']} 个未翻译的外文块"
                    f"（语言：{'、'.join(gate['languages'])}）。"
                    "先翻译再入库，或显式传 allow_foreign。"
                ),
            },
        }

    artifacts = write_corpus(workspace, rows, sidecar)

    fidelity: dict[str, Any] | None = None
    docx_path: Path | None = None
    if word:
        docx_path = publish_docx(
            workspace, chapters, book_title=book_title, author=author
        )
        fidelity = verify_word_document(
            docx_path,
            chapters,
            book_title=book_title,
            allow_line_breaks=allow_line_breaks,
        )

    report: dict[str, Any] = {
        "status": "passed" if (fidelity is None or fidelity["ok"]) else "failed",
        "ok": fidelity is None or fidelity["ok"],
        "source": str(source_path),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "source_characters": source_path.stat().st_size,
        "adapter": adapter,
        "workspace": str(workspace),
        "book_title": book_title,
        "author": author,
        "language": language,
        "chapter_count": len(chapters),
        "chapter_titles": [
            str(chapter.get("title") or "") for chapter in chapters
        ][:200],
        "chunk_count": len(rows),
        "source_characters_total": sum(
            len(str(chapter.get("body") or "")) for chapter in chapters
        ),
        "language_gate": {
            "passed": gate["pending_count"] == 0,
            "pending_count": gate["pending_count"],
            "languages": gate["languages"],
            "exempt": gate["exempt"],
            "allowed_foreign": bool(allow_foreign and gate["pending_count"]),
        },
        "artifacts": {
            "knowledge_base": str(artifacts["corpus"]),
            "metadata_sidecar": str(artifacts["meta"]),
            "apparatus": str(artifacts["apparatus"]),
            "rag_manifest": str(artifacts["manifest"]),
            "docx": str(docx_path) if docx_path else None,
        },
        "docx_fidelity": fidelity,
        "next_steps": [
            f"translation-agent-kb status \"{workspace}\"",
            f"translation-agent-kb retrieve \"{workspace}\" \"<问题>\"",
            f"python global_knowledge_base.py sync  # 聚合进全库（需目标工作区已通过中文门）",
        ],
    }
    if not report["ok"]:
        report["error"] = {
            "kind": "word_fidelity",
            "message": (
                "Word 保真门未通过："
                f"缺字 {fidelity['missing_characters']}、多字 {fidelity['extra_characters']}；"
                "详见 docx_fidelity.issues 与 hunks。"
            ),
        }

    report_path = workspace / INGEST_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    report["report_path"] = str(report_path)
    if fidelity is not None:
        # Publish the fidelity verdict on its own path as well, so
        # ``corpus_status`` and any later audit read the same artifact instead
        # of re-deriving it from the ingest report.
        fidelity_path = workspace / FIDELITY_REPORT
        fidelity_path.parent.mkdir(parents=True, exist_ok=True)
        fidelity_path.write_text(
            json.dumps(fidelity, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        report["fidelity_report_path"] = str(fidelity_path)
    return report


def _read_chapters_from_output(workspace: Path) -> list[dict[str, Any]]:
    """Reconstruct chapters from the published ``chapters/`` Markdown.

    When ``chapters.json`` still carries ``published_markdown`` (written by
    :func:`publish_docx`), it is preferred: it is the exact string the publisher
    rendered, which removes source-page metadata from the comparison.  The raw
    ``chapters/*.md`` files remain the fallback and the audit source of truth.

    Parameters
    ----------
    workspace : pathlib.Path
        Workspace directory.

    Returns
    -------
    list of dict
        ``{"title", "body"[, "published_markdown"]}`` entries in manifest order.

    Raises
    ------
    IngestError
        If the chapter contract is missing or unreadable.
    """

    manifest_path = workspace / "chapters.json"
    chapter_dir = workspace / "chapters"
    if not manifest_path.is_file() or not chapter_dir.is_dir():
        raise IngestError(
            f"工作区缺少 chapters.json / chapters/，无法复核 Word：{workspace}；"
            "请先用 ingest 生成，或改用 docx-publication-finisher 的验收流程"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chapters: list[dict[str, Any]] = []
    for item in manifest:
        path = chapter_dir / str(item["filename"])
        if not path.is_file():
            raise IngestError(f"章节文件缺失：{path}")
        published = item.get("published_markdown")
        if isinstance(published, str) and published.strip():
            parsed = parse_markdown_chapters(published)
        else:
            parsed = parse_markdown_chapters(path.read_text(encoding="utf-8"))
        for chapter in parsed:
            entry: dict[str, Any] = {
                "title": chapter["title"],
                "body": chapter["body"],
            }
            if isinstance(published, str) and published.strip():
                entry["published_markdown"] = published
            chapters.append(entry)
    if not chapters:
        raise IngestError(f"chapters/ 中没有可复核的章节正文：{chapter_dir}")
    return chapters


def workspace_book_title(workspace: Path) -> str:
    """Recover the book title recorded by a previous ingest.

    ``verify_word`` needs the same title the document was published with,
    otherwise ``docProps/core.xml`` is compared against the directory name and
    every re-verification reports a spurious title mismatch.

    Parameters
    ----------
    workspace : pathlib.Path
        Workspace directory.

    Returns
    -------
    str
        The recorded title, or the directory name when no report exists.
    """

    report_path = workspace / INGEST_REPORT
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = {}
        title = str(report.get("book_title") or "").strip()
        if title:
            return title
    return workspace.name


def verify_word(
    workspace: str | Path,
    *,
    docx: str | Path | None = None,
    allow_line_breaks: bool = False,
) -> dict[str, Any]:
    """Re-run the character-fidelity gate on an existing workspace.

    Parameters
    ----------
    workspace : str or pathlib.Path
        Workspace name or directory.
    docx : str or pathlib.Path or None, optional
        Explicit DOCX; defaults to the single ``*.docx`` in the workspace root.
    allow_line_breaks : bool, optional
        Downgrade line-break findings to warnings.

    Returns
    -------
    dict
        The fidelity report plus its workspace and report path.

    Raises
    ------
    IngestError
        If no unambiguous DOCX or no chapter contract exists.
    """

    resolved = resolve_workspace(workspace)
    if docx is not None:
        docx_path = Path(docx).expanduser().resolve()
    else:
        candidates = sorted(resolved.glob("*.docx"))
        if not candidates:
            raise IngestError(f"工作区没有 DOCX：{resolved}")
        if len(candidates) > 1:
            raise IngestError(
                f"工作区有 {len(candidates)} 个 DOCX，无法判断交付对象："
                + "、".join(path.name for path in candidates)
            )
        docx_path = candidates[0]
    if not docx_path.is_file():
        raise IngestError(f"DOCX 不存在：{docx_path}")

    chapters = _read_chapters_from_output(resolved)
    report = verify_word_document(
        docx_path,
        chapters,
        book_title=workspace_book_title(resolved),
        allow_line_breaks=allow_line_breaks,
    )
    report["workspace"] = str(resolved)
    report_path = resolved / FIDELITY_REPORT
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    report["report_path"] = str(report_path)
    return report


def corpus_status(workspace: str | Path) -> dict[str, Any]:
    """Report one workspace's corpus, sidecar and Word state.

    Parameters
    ----------
    workspace : str or pathlib.Path
        Workspace name or directory.

    Returns
    -------
    dict
        ``{"workspace", "corpus", "sidecar", "apparatus", "docx", "gates"}``.

    Raises
    ------
    IngestError
        If the workspace has no knowledge base.
    """

    resolved = resolve_workspace(workspace)
    corpus_path = resolved / DEFAULT_KNOWLEDGE_BASE
    if not corpus_path.is_file():
        raise IngestError(f"工作区没有 {DEFAULT_KNOWLEDGE_BASE}：{resolved}")

    rows: list[dict[str, Any]] = []
    for line in corpus_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)

    gate = language_gate(rows)
    meta_path = resolved / META_SIDECAR
    meta_count = 0
    if meta_path.is_file():
        meta_count = sum(
            1 for line in meta_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    docx_candidates = sorted(resolved.glob("*.docx"))
    fidelity: dict[str, Any] | None = None
    fidelity_path = resolved / FIDELITY_REPORT
    if fidelity_path.is_file():
        try:
            fidelity = json.loads(fidelity_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            fidelity = None

    return {
        "workspace": str(resolved),
        "corpus": {
            "path": str(corpus_path),
            "chunk_count": len(rows),
            "chapter_count": len({str(row.get("chapter_id") or "") for row in rows}),
            "characters": sum(len(str(row.get("content") or "")) for row in rows),
        },
        "sidecar": {
            "path": str(meta_path),
            "exists": meta_path.is_file(),
            "row_count": meta_count,
            "coverage": round(meta_count / max(len(rows), 1), 4),
        },
        "apparatus": {
            "path": str(resolved / "knowledge_base.apparatus.json"),
            "exists": (resolved / "knowledge_base.apparatus.json").is_file(),
        },
        "docx": {
            "candidates": [path.name for path in docx_candidates],
            "count": len(docx_candidates),
        },
        "gates": {
            "chinese": {
                "passed": gate["pending_count"] == 0,
                "pending_count": gate["pending_count"],
                "languages": gate["languages"],
                "exempt": gate["exempt"],
            },
            "word_fidelity": (
                None
                if fidelity is None
                else {
                    "status": fidelity.get("status"),
                    "missing_characters": fidelity.get("missing_characters"),
                    "extra_characters": fidelity.get("extra_characters"),
                    "similarity": fidelity.get("similarity"),
                    "report": str(fidelity_path),
                }
            ),
        },
    }


# =============================================================================
# section: CLI
# =============================================================================


def _write(payload: Mapping[str, Any]) -> None:
    """Write one JSON document to stdout, UTF-8 and unescaped."""

    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns
    -------
    argparse.ArgumentParser
        Parser exposing ``ingest``, ``verify-word`` and ``status``.
    """

    parser = argparse.ArgumentParser(
        prog="kb_ingest",
        description="源文件入库 + 中文 Word 产出与逐字保真校勘（JSON 输出）",
    )
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help=(
            "从 stdin 读取 {'command': ..., 'args': {...}} 并执行；argv 传入的参数会被忽略。"
            "宿主插件用它在 Windows 上传中文 argv，避免代码页转换破坏标题与作者。"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="源文件 → 知识库 + 中文 Word（含保真门）")
    ingest.add_argument("source", help="源文件：.txt/.md/.docx/.epub/.pdf")
    ingest.add_argument("--output-dir", default=None, help="工作区目录（默认 outputs/<源文件名>）")
    ingest.add_argument("--title", default=None, help="书名（默认取源文件名）")
    ingest.add_argument("--author", default="", help="作者")
    ingest.add_argument("--language", default="zh", help="侧表记录的语言标签")
    ingest.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS)
    ingest.add_argument("--no-word", action="store_true", help="只入库，不产出 Word")
    ingest.add_argument(
        "--allow-foreign",
        action="store_true",
        help="按原样注册未译外文块（默认拒绝，与 translation-agent-kb register 同语义）",
    )
    ingest.add_argument(
        "--allow-line-breaks",
        action="store_true",
        help="把段内换行降级为警告；仅当源文件本身是诗歌或列表时使用",
    )

    verify = commands.add_parser("verify-word", help="对已有工作区重跑逐字保真门")
    verify.add_argument("workspace")
    verify.add_argument("--docx", default=None, help="显式 DOCX 路径")
    verify.add_argument("--allow-line-breaks", action="store_true")

    status = commands.add_parser("status", help="工作区语料与两道门的状态")
    status.add_argument("workspace")
    return parser


def run_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one request object and return its JSON-ready result.

    Shared by the argv CLI and the ``--stdin-json`` bridge so both entrypoints
    enforce identical validation.

    Parameters
    ----------
    payload : mapping
        ``{"command": "ingest" | "verify-word" | "status", "args": {...}}``.
        Keys under ``args`` mirror the CLI flags with underscores.

    Returns
    -------
    dict
        The command's JSON-ready result.

    Raises
    ------
    IngestError
        If the command is unknown or the request is invalid.
    """

    command = str(payload.get("command") or "")
    args: Mapping[str, Any] = payload.get("args") or {}
    if command == "ingest":
        source = str(args.get("source") or "").strip()
        if not source:
            raise IngestError("ingest 需要 source")
        return ingest_source(
            source,
            output_dir=args.get("output_dir") or None,
            title=args.get("title") or None,
            author=str(args.get("author") or ""),
            language=str(args.get("language") or "zh"),
            allow_foreign=bool(args.get("allow_foreign", False)),
            allow_line_breaks=bool(args.get("allow_line_breaks", False)),
            word=not bool(args.get("no_word", False)),
            chunk_chars=int(args.get("chunk_chars") or CHUNK_CHARS),
        )
    if command == "verify-word":
        workspace = str(args.get("workspace") or "").strip()
        if not workspace:
            raise IngestError("verify-word 需要 workspace")
        return verify_word(
            workspace,
            docx=args.get("docx") or None,
            allow_line_breaks=bool(args.get("allow_line_breaks", False)),
        )
    if command == "status":
        workspace = str(args.get("workspace") or "").strip()
        if not workspace:
            raise IngestError("status 需要 workspace")
        return corpus_status(workspace)
    raise IngestError(f"未知命令：{command or '<empty>'}")


def _stdin_request() -> dict[str, Any]:
    """Read and parse the JSON request document from stdin.

    Returns
    -------
    dict
        The parsed request object.

    Raises
    ------
    IngestError
        If stdin is empty or does not hold a JSON object.
    """

    raw = sys.stdin.read()
    if not raw.strip():
        raise IngestError("--stdin-json 需要从 stdin 读取 JSON 请求")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise IngestError("--stdin-json 请求必须是 JSON 对象")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and emit its JSON result.

    ``--stdin-json`` is detected before argparse runs: the subparser is required
    for the argv interface, but a bridge request carries its command inside the
    JSON document and would otherwise be rejected for having no positional
    command at all.

    Parameters
    ----------
    argv : sequence of str or None, optional
        Arguments excluding the program name.

    Returns
    -------
    int
        Process exit status: 0 on success, 1 on a domain error, 2 on a usage error.
    """

    raw_argv = list(sys.argv[1:] if argv is None else argv)
    load_project_env()
    if "--stdin-json" in raw_argv:
        try:
            payload = run_request(_stdin_request())
        except (IngestError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            _write({"error": {"kind": exc.__class__.__name__, "message": str(exc)}})
            return 1
        _write(payload)
        if isinstance(payload, Mapping) and payload.get("ok") is False:
            return 1
        return 0

    args = build_parser().parse_args(raw_argv)
    try:
        if args.command == "ingest":
            payload = ingest_source(
                args.source,
                output_dir=args.output_dir,
                title=args.title,
                author=args.author,
                language=args.language,
                allow_foreign=args.allow_foreign,
                allow_line_breaks=args.allow_line_breaks,
                word=not args.no_word,
                chunk_chars=args.chunk_chars,
            )
        elif args.command == "verify-word":
            payload = verify_word(
                args.workspace,
                docx=args.docx,
                allow_line_breaks=args.allow_line_breaks,
            )
        else:
            payload = corpus_status(args.workspace)
    except (IngestError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        _write({"error": {"kind": exc.__class__.__name__, "message": str(exc)}})
        return 1
    _write(payload)
    # A blocked language gate or a failed fidelity gate is a domain verdict, not
    # a crash: the JSON above is the report, and the non-zero status lets a
    # shell caller stop a pipeline without parsing it.
    if isinstance(payload, Mapping) and payload.get("ok") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
