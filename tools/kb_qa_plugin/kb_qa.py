"""Retrieval core for the DSH philosophy/literature reader plugin.

This module is a *stdout-clean* JSON gateway in front of two existing,
already-tested retrieval layers of this repository:

* :mod:`global_knowledge_base` — the repository-wide SQLite + FTS5 index
  (per-book cap, apparatus demotion, report-status filter); and
* :mod:`rag_knowledge_base` — the per-workspace BM25 + vector RRF retrieval
  with citation-labelled context.

It exists because both layers print human-readable text and, in the case of
``knowledge_base_cli``, import modules that emit warnings at import time. A
model-facing tool needs one deterministic JSON document on stdout and nothing
else, so this module owns:

* the stdout guard that keeps third-party import chatter off the wire,
* the two-stage query plan (cross-book discovery, then per-book close reading),
* merge/dedup/budget of the returned passages, and
* quote verification against the corpus.

Commands
--------
``status``
    Index health plus the machine-readable shelf list.
``ask``
    Two-stage retrieval; see :func:`ask`.
``verify-quote``
    Check whether a quotation actually occurs in the corpus; see
    :func:`verify_quote`.

Every command prints one JSON object on stdout and exits 0 on success, or
prints ``{"error": {...}}`` and exits 1.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_MAX_CHARS = 28_000
DEFAULT_HIT_CHARS = 4_000
META_SIDECAR = "knowledge_base.meta.jsonl"
TRUNCATION_MARK = "⋯"

# ── project .env (never overrides an already-exported variable) ───────────────

_ENV_LOADED = False


def load_project_env() -> None:
    """Export unset keys from the project ``.env`` into ``os.environ``.

    The embedding provider reads ``ZHIPU_API_KEY`` and friends from the
    environment. The CLI entrypoint is normally launched from a shell that has
    not sourced ``.env``; an unset key makes formal hybrid unavailable. Existing
    environment variables always win, and a malformed line is skipped rather
    than fatal.

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
    """Redirect Python-level writes to ``sys.stdout`` onto stderr.

    Importing the retrieval modules can emit warnings and deprecation notices;
    those must never share stdout with the JSON contract. Only ``sys.stdout`` is
    swapped, because the retrieval modules never write to file descriptor 1
    directly.
    """

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield
    text = buffer.getvalue()
    if text:
        sys.stderr.write(text)


# ── text helpers ──────────────────────────────────────────────────────────────

_PUNCTUATION = {
    "，": ",", "。": ".", "、": ",", "；": ";", "：": ":",
    "？": "?", "！": "!", "（": "(", "）": ")", "《": "<", "》": ">",
    "“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "…": ".",
    "「": '"', "」": '"', "『": '"', "』": '"', "．": ".", "　": " ",
}


def normalize_for_match(value: str) -> str:
    """Fold a passage for punctuation- and whitespace-insensitive comparison.

    Applies NFKC, maps full-width CJK punctuation onto ASCII, and collapses
    every run of whitespace to one space. It deliberately does *not* fold case
    and does *not* convert traditional to simplified characters: a quote is
    verified for citation, so a difference a reader would see on the page has to
    survive. Case-insensitive or glyph-insensitive folding here would report
    ``A.赠与的互酬`` as a verbatim match for a source that prints
    ``a.赠与的互酬``, which is precisely the false positive this verifier
    exists to prevent.

    Parameters
    ----------
    value : str
        Raw passage text.

    Returns
    -------
    str
        The folded text, case preserved.
    """

    folded = unicodedata.normalize("NFKC", value)
    folded = "".join(_PUNCTUATION.get(char, char) for char in folded)
    return re.sub(r"\s+", " ", folded).strip()


def normalize_for_search(value: str) -> str:
    """Fold a passage for occurrence *location*, case-insensitively.

    Used to find where a quotation sits inside a passage, where case carries no
    meaning. Verdicts never use this: they use :func:`normalize_for_match`, so a
    case difference is reported rather than swallowed.

    Parameters
    ----------
    value : str
        Raw passage text.

    Returns
    -------
    str
        :func:`normalize_for_match` output, casefolded.
    """

    return normalize_for_match(value).casefold()


def _plain_lines(raw: str) -> list[str]:
    """Split a passage into comparison lines without destroying offsets.

    Parameters
    ----------
    raw : str
        Passage text as stored in the corpus.

    Returns
    -------
    list[str]
        One entry per source line, punctuation-normalized for comparison; case
        is preserved so a case difference is still reported.
    """

    return [normalize_for_match(line) for line in raw.splitlines()]


def excerpt_around(content: str, needle: str, window: int = 160) -> str:
    """Return a case-preserving context window around ``needle``.

    The needle is located through the search fold, but the returned text is
    sliced from the *raw* passage. Returning the folded text instead would
    silently lowercase the evidence: a reader would be told the corpus prints
    ``a.赠与的互酬`` when it prints ``A.赠与的互酬``, which is the one thing a
    citation view must not do.

    Parameters
    ----------
    content : str
        Full chunk text.
    needle : str
        Query to locate, matched case-insensitively after NFKC folding.
    window : int, optional
        Characters of context kept before and after the match.

    Returns
    -------
    str
        The window, prefixed/suffixed with an ellipsis when the passage was cut.
    """

    target = normalize_for_search(needle)
    position = normalize_for_search(content).find(target) if target else -1
    if position < 0:
        return content[: window * 2].strip()
    start = max(0, position - window)
    end = min(len(content), position + len(target) + window)
    prefix = "…" if start else ""
    suffix = "…" if end < len(content) else ""
    return f"{prefix}{content[start:end].strip()}{suffix}"


# ── corpus access ─────────────────────────────────────────────────────────────


class CorpusError(RuntimeError):
    """Raised when the requested corpus cannot be read."""


def _load_global_module() -> Any:
    with _stdout_to_stderr():
        import global_knowledge_base  # noqa: PLC0415 - deliberate lazy import

    return global_knowledge_base


def global_db_path(override: str | Path | None = None) -> Path:
    """Resolve the repository-level SQLite index path.

    Parameters
    ----------
    override : str | pathlib.Path or None, optional
        Explicit path; wins over the environment and the project default.

    Returns
    -------
    pathlib.Path
        The resolved database path.
    """

    if override:
        return Path(override).expanduser().resolve()
    from_env = os.environ.get("DSH_KB_GLOBAL_DB")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return PROJECT_ROOT / "global_knowledge_base.sqlite3"


def outputs_root(override: str | Path | None = None) -> Path:
    """Resolve the directory holding per-book workspaces.

    Parameters
    ----------
    override : str | pathlib.Path or None, optional
        Explicit path; wins over the environment and the project default.

    Returns
    -------
    pathlib.Path
        The resolved outputs root.
    """

    if override:
        return Path(override).expanduser().resolve()
    from_env = os.environ.get("DSH_KB_OUTPUTS_ROOT")
    if from_env:
        return Path(from_env).expanduser().resolve()
    return PROJECT_ROOT / "outputs"


def _sidecar_metadata(workspace: Path) -> dict[str, dict[str, str]]:
    """Read ``knowledge_base.meta.jsonl`` for one workspace.

    Parameters
    ----------
    workspace : pathlib.Path
        Workspace directory.

    Returns
    -------
    dict
        Row id → metadata mapping; empty when the sidecar is absent or invalid.
    """

    path = workspace / META_SIDECAR
    if not path.is_file():
        return {}
    rows: dict[str, dict[str, str]] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            rows[row["id"]] = {
                str(key): str(value)
                for key, value in row.items()
                if isinstance(value, (str, int, float)) and key != "id"
            }
    return rows


def _deep_search(
    base: str | Path,
    query: str,
    *,
    top_k: int,
    max_chars: int,
    semantic: bool,
    apparatus_weight: float | None,
) -> dict[str, Any]:
    """Run one per-workspace RAG query and return a JSON-ready report.

    Parameters
    ----------
    base : str or pathlib.Path
        Workspace directory or its ``knowledge_base.jsonl``.
    query : str
        Natural-language question or keyword set.
    top_k : int
        Number of passages requested.
    max_chars : int
        Character budget handed to ``RagKnowledgeBase.retrieve_context``.
    semantic : bool
        When true, attempt hybrid (BM25 + embedding) retrieval. Requires the
        workspace manifest to hold a ready embedding index and the provider key
        to be present; failure is reported without lexical fallback.
    apparatus_weight : float or None, optional
        Override for the apparatus demotion policy (1.0 disables demotion,
        0 excludes annotated apparatus chunks).

    Returns
    -------
    dict
        ``{"hits": [...], "context": str, "diagnostics": {...}}``.

    Raises
    ------
    CorpusError
        If the workspace has no readable knowledge base.
    """

    with _stdout_to_stderr():
        from knowledge_base_cli import _knowledge_base_path  # noqa: PLC0415
        from rag_knowledge_base import (  # noqa: PLC0415
            RagEmbeddingUnavailableError,
            RagError,
            RagFormatError,
            RagKnowledgeBase,
            retrieve_hybrid_context,
        )

    path = _knowledge_base_path(base)
    if not path.is_file():
        raise CorpusError(f"没有可读的知识库：{path}")
    try:
        knowledge_base = RagKnowledgeBase.open(path)
    except RagFormatError:
        raise CorpusError(
            f"{path} 缺少 RAG 清单（knowledge_base.rag.json）；"
            "先运行 translation-agent-kb register"
        ) from None
    except (RagError, OSError, ValueError) as exc:
        raise CorpusError(f"知识库不可用：{path}: {exc}") from exc

    mode = "hybrid" if semantic else "lexical"
    provider = None
    semantic_error: str | None = None
    if semantic:
        with _stdout_to_stderr():
            from knowledge_base_cli import _provider_from_manifest  # noqa: PLC0415

            try:
                provider = _provider_from_manifest(
                    path, argparse.Namespace(
                        api_key_env="ZHIPU_API_KEY",
                        base_url=os.environ.get("ZHIPU_EMBEDDING_BASE_URL")
                        or "https://open.bigmodel.cn/api/paas/v4/",
                        model=os.environ.get("ZHIPU_EMBEDDING_MODEL") or "embedding-3",
                        dimensions=int(os.environ.get("ZHIPU_EMBEDDING_DIMENSIONS") or 2048),
                    )
                )
            except (RagError, ValueError, TypeError) as exc:
                semantic_error = f"{exc.__class__.__name__}: {exc}"
                provider = None
        if provider is None and semantic_error is None:
            semantic_error = "该工作区的向量索引未就绪"

    if semantic and (provider is None or not knowledge_base.embedding_ready):
        raise CorpusError(f"hybrid unavailable: {path}: {semantic_error or '向量索引未就绪'}")

    def _run(active_provider: Any, active_mode: str) -> Any:
        retrieve = (lambda query, **kwargs: retrieve_hybrid_context(knowledge_base, query, **kwargs)) if semantic else knowledge_base.retrieve_context
        return retrieve(
            query,
            top_k=top_k,
            max_chars=max_chars,
            embedding_provider=active_provider,
            **({} if semantic else {"mode": active_mode}),
            per_book_cap=0,
            apparatus_weight=apparatus_weight,
        )

    try:
        context = _run(provider, mode)
    except (RagError, OSError, ValueError, KeyError) as exc:
        raise CorpusError(f"{mode} unavailable: {path}: {exc}") from exc
    used_mode = context.diagnostics.get("effective_mode")
    if semantic and used_mode != "hybrid":
        raise CorpusError(f"hybrid unavailable: 检索实际模式为 {used_mode!r}")

    metadata = _sidecar_metadata(path.parent)
    hits: list[dict[str, Any]] = []
    for hit in context.hits:
        extra = metadata.get(hit.id, {})
        # Only a real book identity may override the workspace name. A single
        # workspace's sidecar sometimes carries the *chapter* id in book_title
        # (legacy rows), and letting that through would print a citation as
        # 《toc-0001》 instead of the work it came from.
        candidate_book = extra.get("book_title")
        book = candidate_book if candidate_book and candidate_book != hit.title else path.parent.name
        hits.append(
            {
                "id": hit.id,
                "workspace": path.parent.name,
                "book": book or path.parent.name,
                "author": extra.get("author"),
                "language": extra.get("language"),
                "title": hit.title,
                "content": hit.content,
                "excerpt": excerpt_around(hit.content, query),
                "score": round(float(hit.score), 6),
                "channels": hit.channels,
                "chapter_order": hit.chapter_order,
                "source_path": extra.get("source_path"),
                "stage": "deep",
            }
        )
    return {
        "hits": hits,
        "context": context.text,
        "diagnostics": {
            "requested_mode": mode,
            "used_mode": used_mode,
            "effective_mode": used_mode,
            "diagnostic_only": not semantic,
            "semantic_error": semantic_error,
            "chunk_count": knowledge_base.chunk_count,
            "embedding_ready": bool(knowledge_base.embedding_ready),
        },
    }


def resolve_workspace(name: str, root: Path) -> Path | None:
    """Resolve a workspace name to the directory holding its knowledge base.

    Model-supplied values arrive either as a bare workspace name
    (``知识库_鲁迅全集``) or as a path. Bare names are resolved against the
    outputs root first and only then against the working directory, so a
    same-named directory at the repository root can never shadow a real
    workspace.

    Parameters
    ----------
    name : str
        Workspace name or path.
    root : pathlib.Path
        Resolved outputs root.

    Returns
    -------
    pathlib.Path or None
        The workspace directory, or None when it holds no ``knowledge_base.jsonl``.
    """

    candidate = Path(name).expanduser()
    options: list[Path] = []
    if candidate.is_absolute():
        options.append(candidate)
    else:
        # The workspace name wins over a same-named directory in the cwd.
        options.extend([root / candidate, PROJECT_ROOT / candidate])
    for option in options:
        if option.is_dir() and (option / "knowledge_base.jsonl").is_file():
            return option.resolve()
    return None


def shelf(limit: int = 200, *, db: str | Path | None = None) -> dict[str, Any]:
    """List the indexed workspaces with their size and verification state.

    Parameters
    ----------
    limit : int, optional
        Maximum entries returned; the full count is always reported.
    db : str or pathlib.Path or None, optional
        Global index override.

    Returns
    -------
    dict
        ``{"global": {...}, "shelf": [...], "returned": int}``.

    Raises
    ------
    CorpusError
        If the global index is missing or unreadable.
    """

    module = _load_global_module()
    path = global_db_path(db)
    try:
        with _stdout_to_stderr():
            report = module.status(path)
    except (ValueError, OSError) as exc:
        raise CorpusError(str(exc)) from exc
    rows = sorted(
        report["workspaces"],
        key=lambda row: (-int(row["reader_chunks"]), str(row["name"])),
    )
    entries = [
        {
            "workspace": row["name"],
            "reader_chunks": int(row["reader_chunks"]),
            "page_chunks": int(row["page_chunks"]),
            "archive_chunks": int(row["archive_chunks"]),
            "report_status": row["report_status"],
        }
        for row in rows[: max(limit, 0)]
    ]
    return {
        "global": {
            "database": report["database"],
            "built_at": report.get("built_at"),
            "schema_version": report.get("schema_version"),
            "integrity": report.get("integrity"),
            "chinese_gate": report.get("chinese_gate"),
            "workspace_count": int(report["workspace_count"]),
            "chunks_by_kind": report.get("chunks_by_kind", {}),
        },
        "shelf": entries,
        "returned": len(entries),
    }


# ── the two-stage query plan ──────────────────────────────────────────────────


def _dedupe_key(hit: Mapping[str, Any]) -> str:
    """Return the identity used to merge the discovery and deep-reading stages."""

    return f"{hit.get('workspace')}\x00{hit.get('id')}"


def lexical_terms(query: str) -> list[str]:
    """Split a query into the terms a passage must show to count as evidence.

    Mirrors ``global_knowledge_base._query_terms`` for the shapes that matter
    here: Latin words of three or more characters are used whole, longer CJK
    runs are cut into overlapping three-character n-grams. One- and two-character
    CJK words cannot form a trigram and are therefore *not* used as gates — a
    single common character would let any passage through.

    Parameters
    ----------
    query : str
        The user's question.

    Returns
    -------
    list[str]
        Casefolded gate terms, in first-occurrence order.
    """

    terms: list[str] = []
    for part in re.findall(
        r"[A-Za-z0-9_]+|[\u3400-\u9fff]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+",
        unicodedata.normalize("NFKC", query),
    ):
        if re.match(r"[A-Za-z0-9_]", part):
            if len(part) >= 3:
                terms.append(part)
        elif len(part) >= 3:
            terms.extend(part[index:index + 3] for index in range(len(part) - 2))
    return [term.casefold() for term in dict.fromkeys(terms)][:64]


def has_lexical_evidence(hit: Mapping[str, Any], terms: Sequence[str]) -> bool:
    """Report whether a passage literally shows any query term.

    Embedding retrieval has no relevance floor: asked for a token that exists
    nowhere in the corpus it still returns its nearest neighbours, which read
    exactly like evidence. Requiring one literal term in the passage or its
    chapter title is what separates "the vector space put this close" from "this
    book actually talks about it".

    Parameters
    ----------
    hit : mapping
        One retrieval hit carrying ``content`` and/or ``title``.
    terms : sequence of str
        Gate terms from :func:`lexical_terms`; an empty sequence disables the gate.

    Returns
    -------
    bool
        True when the passage may be reported as evidence.
    """

    if not terms:
        return True
    haystack = f"{hit.get('title') or ''}\n{hit.get('content') or hit.get('excerpt') or ''}"
    folded = normalize_for_match(haystack)
    return any(term in folded for term in terms)


def _dedupe_hits(hits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Drop repeated rows while preserving order.

    The publication pipeline can register the same bytes under two source files;
    a repeated passage occupies budget without adding evidence.

    Parameters
    ----------
    hits : sequence of mapping
        Candidate hits.

    Returns
    -------
    list of dict
        One entry per distinct ``(workspace, id)``.
    """

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        key = _dedupe_key(hit)
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(hit))
    return unique


_NAME_SEPARATORS = re.compile(r"[\s=＝:：,，、()（）\[\]【】<>《》|·・—\-_/\\]+")
_NAME_NOISE = {
    "z", "library", "sk", "1lib", "z-lib", "z-lib.sk", "ocr", "书签",
    "汉译世界学术名著丛书", "pdf", "epub", "docx", "著", "译", "主编",
}

# Two characters is the shortest segment worth matching: a single CJK character
# occurs in almost any query and would route every book into the deep stage.
_MIN_SEGMENT_CHARS = 2

# A matched segment this long or longer is distinctive enough to route on its
# own. Shorter matches (an author's surname, a short title) only route when they
# are the whole query: "鲁迅" is asking for Lu Xun, but "鲁迅 国民性" is asking
# about a concept and must not pull in the complete works.
_DISTINCTIVE_SEGMENT_CHARS = 3


def name_segments(workspace: str) -> list[str]:
    """Extract the distinctive bibliographic fragments of a workspace name.

    Workspace directories are file names, not titles: they carry the author, the
    translator, the imprint, edition markers and the source site. Matching a
    query against the raw name would let ``z-library`` route every book, so the
    name is split on punctuation and the packaging noise is dropped.

    Parameters
    ----------
    workspace : str
        Workspace directory name.

    Returns
    -------
    list[str]
        Distinctive fragments, longest first.
    """

    segments: list[str] = []
    for part in _NAME_SEPARATORS.split(workspace):
        candidate = part.strip().strip("。.!！?？")
        if len(candidate) < _MIN_SEGMENT_CHARS:
            continue
        if candidate.casefold() in _NAME_NOISE:
            continue
        if not re.search(r"[\u3400-\u9fff\u3040-\u30ffA-Za-z]", candidate):
            continue
        segments.append(candidate)
    return sorted(dict.fromkeys(segments), key=len, reverse=True)


def route_by_name(
    query: str,
    shelf: Sequence[Mapping[str, Any]],
    *,
    limit: int = 3,
) -> list[tuple[str, bool]]:
    """Find the workspaces whose own name the query already names.

    Cross-book discovery matches chunk *content*, so a book whose Chinese title
    is a Japanese phrase — ``日本现代文学的起源``, ``反文学論`` — is reachable only
    through its Chinese body text, and a book whose body is entirely Japanese is
    reachable not at all. The workspace name is the one bibliographic signal the
    index already holds, and a query that spells it out is the strongest routing
    evidence available, so it is read directly instead of being left to compete
    as content trigrams.

    Parameters
    ----------
    query : str
        The user's question.
    shelf : sequence of mapping
        Workspace entries carrying at least ``workspace``.
    limit : int, optional
        Maximum routed workspaces.

    Returns
    -------
    list[tuple[str, bool]]
        ``(workspace, matched_title)``, titles first, longest match first.
    """

    folded = normalize_for_search(query)
    exact = len(folded) <= _DISTINCTIVE_SEGMENT_CHARS
    matches: list[tuple[int, int, str]] = []
    for entry in shelf:
        workspace = str(entry.get("workspace") or "")
        if not workspace:
            continue
        hits = [
            len(segment)
            for segment in name_segments(workspace)
            if normalize_for_search(segment) in folded
        ]
        if not hits:
            continue
        best = max(hits)
        if best < _DISTINCTIVE_SEGMENT_CHARS and not exact:
            continue
        # A long match is the book's own title; a short one is its author. Both
        # are real signals, but they are not equal evidence: asking about
        # "反文学論 的女性" should open that book, not every book by its author,
        # so title matches sort ahead of author matches.
        is_title = best >= 5
        matches.append((0 if is_title else 1, -best, workspace))
    matches.sort()
    return [(workspace, rank == 0) for rank, _, workspace in matches[: max(limit, 0)]]


def query_script(query: str) -> str:
    """Classify a query's dominant writing system.

    Parameters
    ----------
    query : str
        The user's question.

    Returns
    -------
    str
        ``kana`` for Japanese, ``han`` for Han-only text, else ``latin``.
    """

    if re.search(r"[\u3040-\u30ff]", query):
        return "kana"
    if re.search(r"[\u3400-\u9fff]", query):
        return "han"
    return "latin"


def merge_hits(
    global_hits: Sequence[Mapping[str, Any]],
    deep_hits: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge both retrieval stages into one ranked, deduplicated hit list.

    A workspace reached by the close-reading stage is promoted above discovery
    hits, because the deep pass ranks whole passages inside one book instead of
    one excerpt per book. When both stages return the same chunk, the deep
    version (full ``content``) replaces the discovery version (a short
    ``excerpt``).

    Parameters
    ----------
    global_hits : sequence of mapping
        Discovery-stage hits in rank order.
    deep_hits : sequence of mapping
        Close-reading-stage hits in rank order.
    limit : int
        Maximum merged hits returned.

    Returns
    -------
    list of dict
        Merged hits, each carrying ``stage`` = ``deep`` or ``discovery``.
    """

    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}

    def _add(hit: Mapping[str, Any]) -> None:
        item = dict(hit)
        key = _dedupe_key(item)
        existing = index.get(key)
        if existing is None:
            index[key] = len(merged)
            merged.append(item)
            return
        if item.get("stage") == "deep" and merged[existing].get("stage") == "discovery":
            merged[existing] = item

    for hit in global_hits:
        _add(hit)
    for hit in deep_hits:
        _add(hit)

    def _rank(hit: Mapping[str, Any]) -> tuple[int, float, int]:
        stage = 0 if hit.get("stage") == "deep" else 1
        score = hit.get("score")
        numeric = float(score) if isinstance(score, (int, float)) else 0.0
        return (stage, numeric, int(hit.get("chapter_order") or 0))

    merged.sort(key=_rank)
    return merged[: max(limit, 0)]


def _apply_budget(
    hits: Sequence[Mapping[str, Any]],
    *,
    max_chars: int,
    hit_chars: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Trim the merged hit list to a character budget.

    Discoveries carry only an excerpt, so a hit is dropped only when nothing
    fits. Deep hits are truncated from the end rather than dropped, keeping the
    citation and its opening evidence together.

    Parameters
    ----------
    hits : sequence of mapping
        Merged hits in rank order.
    max_chars : int
        Total passage character budget.
    hit_chars : int
        Per-hit character ceiling for ``content``.

    Returns
    -------
    tuple
        ``(kept_hits, dropped_row_ids)``.
    """

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    used = 0
    for hit in hits:
        item = dict(hit)
        content = str(item.get("content") or "")
        ceiling = max(600, min(hit_chars, max_chars - used))
        if not content:
            item["content"] = str(item.get("excerpt") or "")
        if len(content) > ceiling:
            item["content"] = content[:ceiling].rstrip() + TRUNCATION_MARK
            item["truncated"] = True
        cost = len(item["content"]) + 200
        if used + cost > max_chars and kept:
            dropped.append(str(item.get("id")))
            continue
        used += cost
        kept.append(item)
    return kept, dropped


def ask(
    query: str,
    *,
    workspace: str | None = None,
    scope: str = "reader",
    limit: int = 6,
    deep: bool = True,
    per_book_cap: int = 1,
    max_chars: int = DEFAULT_MAX_CHARS,
    hit_chars: int = DEFAULT_HIT_CHARS,
    verified_only: bool = False,
    semantic: bool = True,
    apparatus_weight: float | None = None,
    db: str | Path | None = None,
    outputs: str | Path | None = None,
    deep_books: int = 2,
) -> dict[str, Any]:
    """Retrieve passages from the book corpus for one question.

    Stage 1 searches the repository-wide index for candidate books and
    passages. Stage 2 re-reads the best candidate books through their own RAG
    index, which ranks whole passages instead of excerpts and can use the
    embedding index. Formal evidence uses only hybrid deep hits under one budget.

    Parameters
    ----------
    query : str
        The question or keyword set to find evidence for.
    workspace : str or None, optional
        Constrain the search to one workspace name, or to a directory path
        under ``outputs``.
    scope : str, optional
        ``reader`` (published reading layer), ``pages`` (source-page OCR and
        per-page translations), ``archive`` (chapter snapshots and reviewed
        chapters), or ``all``.
    limit : int, optional
        Maximum merged passages returned.
    deep : bool, optional
        Run the close-reading stage.
    per_book_cap : int, optional
        Maximum discovery hits per book; 0 disables the cap.
    max_chars : int, optional
        Total passage budget.
    hit_chars : int, optional
        Per-passage ceiling.
    verified_only : bool, optional
        Keep only workspaces whose release report passed and is not stale.
    semantic : bool, optional
        Require hybrid close reading; false explicitly selects offline lexical diagnostics.
    apparatus_weight : float or None, optional
        Override the apparatus demotion policy.
    db : str or pathlib.Path or None, optional
        Global index override.
    outputs : str or pathlib.Path or None, optional
        Outputs root override.
    deep_books : int, optional
        How many candidate books the close-reading stage re-reads.

    Returns
    -------
    dict
        A JSON-ready report: ``query``, ``hits``, ``context``,
        ``diagnostics``, and ``shelf``.

    Raises
    ------
    CorpusError
        If no corpus can serve the query.
    ValueError
        If arguments are out of range.
    """

    if not query.strip():
        raise ValueError("query 不能为空")
    if scope not in {"reader", "pages", "archive", "all"}:
        raise ValueError(f"未知的检索层：{scope}")
    if semantic and deep and scope != "reader":
        raise ValueError("正式 hybrid 取证仅支持 scope=reader；其他层请显式使用离线诊断 --no-deep")
    if limit < 1:
        raise ValueError("limit 必须为正整数")
    if deep_books < 0:
        raise ValueError("deep_books 不能为负")
    # A cap only makes sense when something bounded the candidate set. A bare
    # number would silently drop the book a query named by title once several
    # author-matched books had filled the list, so the default is "no cap" and
    # only the caller's explicit ``deep_books`` limits the deep stage.
    deep_cap = deep_books if deep_books > 0 else None

    module = _load_global_module()
    database = global_db_path(db)
    shelf_entries: list[dict[str, Any]] = []
    if database.is_file():
        try:
            shelf_entries = shelf(limit=400, db=database)["shelf"]
        except CorpusError:
            shelf_entries = []
    # Bibliographic channel: a query that spells out a book's name routes to that
    # book directly. Content-only discovery cannot reach a book whose Chinese
    # title is a foreign phrase, nor one whose body is not Chinese at all.
    routed = route_by_name(query, shelf_entries) if not workspace else []
    named_by_title = [name for name, is_title in routed if is_title]
    named_by_author = [name for name, is_title in routed if not is_title]
    named_books = [name for name, _ in routed]
    diagnostics: dict[str, Any] = {
        "requested_mode": "hybrid" if semantic and deep else "lexical",
        "effective_mode": None,
        "diagnostic_only": not (semantic and deep),
        "partial_coverage": False,
        "scope": scope,
        "deep_stage": False,
        "deep_books": [],
        "global_hits": 0,
        "semantic_used": False,
        "semantic_error": None,
        "verified_only": verified_only,
        "degraded": None,
        "query_script": query_script(query),
        "routed_by_name": named_books,
        "routed_by_title": named_by_title,
        "routed_by_author": named_by_author,
    }
    global_hits: list[dict[str, Any]] = []
    discovery_error: str | None = None
    if database.is_file():
        try:
            with _stdout_to_stderr():
                rows = module.search(
                    query,
                    db_path=database,
                    scope=scope,
                    workspace=None,
                    limit=max(limit * 3, limit),
                    verified_only=verified_only,
                    per_book_cap=per_book_cap or None,
                )
        except (ValueError, OSError, sqlite3.Error) as exc:
            discovery_error = f"{exc.__class__.__name__}: {exc}"
            rows = []
    else:
        discovery_error = f"全局索引不存在：{database}"
        rows = []

    for row in rows:
        if workspace and row["workspace"] != workspace:
            continue
        global_hits.append(
            {
                "id": row["id"],
                "workspace": row["workspace"],
                "book": row["workspace"],
                "author": None,
                "language": None,
                "title": row["title"],
                "content": row["excerpt"],
                "excerpt": row["excerpt"],
                "is_excerpt": True,
                "score": round(float(row["score"]), 6),
                "channels": "lexical",
                "chapter_order": row["chapter_order"],
                "report_status": row["report_status"],
                "source_path": row["source_path"],
                "stage": "discovery",
            }
        )
    diagnostics["global_hits"] = len(global_hits)
    diagnostics["discovery_error"] = discovery_error

    deep_hits: list[dict[str, Any]] = []
    if deep:
        root = outputs_root(outputs)
        candidates: list[str] = []
        if workspace:
            candidates = [workspace]
        else:
            seen: set[str] = set()
            # Name-routed books lead: the query named them, so their own index is
            # a better ranking authority than the content-trigram candidates.
            # A title-routed book is always opened; an author-routed one is
            # subject to the cap, because an author name can legitimately pull in
            # a whole shelf.
            for name in named_by_title:
                if name not in seen:
                    seen.add(name)
                    candidates.append(name)
            capped = [name for name in named_by_author if name not in seen]
            for name in capped[: max(deep_cap or len(capped), 1)] if deep_cap else capped:
                if name not in seen:
                    seen.add(name)
                    candidates.append(name)
            for hit in global_hits:
                name = str(hit["workspace"])
                if name in seen:
                    continue
                seen.add(name)
                candidates.append(name)
                if deep_cap is not None and len(candidates) >= deep_cap:
                    break
        for name in candidates:
            base = resolve_workspace(name, root)
            if base is None:
                diagnostics.setdefault("skipped", []).append(
                    {"workspace": name, "reason": "workspace_not_found"}
                )
                continue
            try:
                report = _deep_search(
                    base,
                    query,
                    top_k=max(limit, 3),
                    max_chars=max_chars,
                    semantic=semantic,
                    apparatus_weight=apparatus_weight,
                )
            except CorpusError as exc:
                diagnostics.setdefault("skipped", []).append(
                    {"workspace": name, "reason": str(exc)}
                )
                continue
            diagnostics["deep_books"].append(name)
            diagnostics["deep_stage"] = True
            if report["diagnostics"].get("semantic_error"):
                diagnostics["semantic_error"] = report["diagnostics"]["semantic_error"]
            if report["diagnostics"].get("used_mode") in {"hybrid", "semantic"}:
                diagnostics["semantic_used"] = True
            deep_hits.extend(report["hits"])

    # Evidence gate. FTS5 discovery answers "which books actually contain these
    # terms"; the close-reading stage only ranks passages inside a book, so its
    # hits are admissible when they land in a chapter that discovery already
    # matched. When discovery matched nothing the book is not known to cover the
    # topic, and a hit is admitted only if the passage literally shows a query
    # term — embedding retrieval has no relevance floor, so a nearest neighbour
    # would otherwise be reported with the same confidence as real evidence.
    # Without this gate a question the corpus cannot answer comes back with
    # confident-looking citations, the one failure mode a citation-first reader
    # must not have.
    #
    # A name-routed book is the third way in: the query named the book, so the
    # book is on topic even though no single passage repeats the query's words.
    # It still has to show a query term OR be scoped, because naming a book does
    # not make every page of it evidence for the question asked about it.
    terms = lexical_terms(query)
    discovery_books = {str(hit["workspace"]) for hit in global_hits}
    matched_titles = {
        (str(hit["workspace"]), str(hit["title"]))
        for hit in global_hits
    }
    named = set(named_books)
    admitted: list[dict[str, Any]] = []
    rejected: list[str] = []
    rejected_books: list[str] = []
    for hit in _dedupe_hits(deep_hits):
        book = str(hit.get("workspace"))
        in_matched_chapter = (book, str(hit.get("title"))) in matched_titles
        scoped_workspace = bool(workspace)
        in_named_book = book in named
        shows_terms = has_lexical_evidence(hit, terms)
        if (
            in_matched_chapter
            or scoped_workspace
            or (in_named_book and (shows_terms or not terms))
            or (not discovery_books and shows_terms)
        ):
            admitted.append(hit)
        else:
            rejected.append(str(hit.get("id")))
            if book not in rejected_books:
                rejected_books.append(book)
    diagnostics["deep_rejected_ids"] = rejected
    diagnostics["deep_rejected_books"] = rejected_books
    deep_hits = admitted

    formal_hybrid = semantic and deep
    diagnostics["partial_coverage"] = bool(diagnostics.get("skipped"))
    diagnostics["effective_mode"] = (
        "hybrid" if formal_hybrid and diagnostics["deep_stage"]
        else "not_run" if formal_hybrid else "lexical"
    )
    if formal_hybrid and not diagnostics["deep_stage"] and diagnostics.get("skipped"):
        raise CorpusError("hybrid unavailable: " + json.dumps(diagnostics["skipped"], ensure_ascii=False))
    hits = merge_hits([] if formal_hybrid else global_hits, deep_hits, limit=limit)
    hits, dropped = _apply_budget(hits, max_chars=max_chars, hit_chars=hit_chars)
    diagnostics["dropped_ids"] = dropped

    if not hits:
        if workspace and deep_hits == [] and not database.is_file():
            raise CorpusError(discovery_error or "没有可用的知识库")
        if discovery_error and not deep_hits:
            diagnostics["degraded"] = discovery_error
        # A silent empty result is the failure mode that made this channel
        # necessary: the corpus did have the material, the query just could not
        # reach it. Say what to try instead of returning nothing.
        hints: list[str] = []
        if named_books:
            hints.append(
                "查询已按书名路由到 " + "、".join(named_books[:3])
                + "，但这些工作区没有返回含查询词元的段落；改用该书自己的术语（原文用词，而非你的概括）重试。"
            )
        if rejected_books:
            others = [book for book in rejected_books if book not in named_books]
            if others:
                hints.append(
                    "以下工作区有相关段落，但均不含查询词元，已被证据门拦下："
                    + "、".join(others[:3])
                    + "。它们可能是外文正文——用该书原文语言（如日文）重试，或直接用 workspace 限定该书。"
                )
        if not named_books and not global_hits:
            hints.append(
                "全库未命中任何工作区。请确认书名是否被写全（kb_library 可列出全部藏书），"
                "并换用书中术语而非概括词重试。"
            )
        diagnostics["hints"] = hints

    context_blocks: list[str] = []
    for index, hit in enumerate(hits, start=1):
        label = hit.get("chapter_order")
        head = (
            f"[{index}] 书={hit.get('book')} | 工作区={hit.get('workspace')}"
            f" | 章节={hit.get('title')}"
        )
        if hit.get("author"):
            head += f" | 作者={hit['author']}"
        if hit.get("language"):
            head += f" | 语言={hit['language']}"
        head += f" | 通道={hit.get('channels')} | 层={hit.get('stage')}"
        if label is not None:
            head += f" | 章序={label}"
        if hit.get("is_excerpt"):
            head += " | 注意=此条为检索摘要(片段)，非完整段落"
        context_blocks.append(f"{head}\n{hit.get('content')}")

    return {
        "query": query,
        "discovery": global_hits,
        "hits": hits,
        "context": "\n\n".join(context_blocks),
        "diagnostics": diagnostics,
        "shelf": shelf_entries,
    }


# ── quote verification ────────────────────────────────────────────────────────


def _line_span(haystack_lines: Sequence[str], needle_lines: Sequence[str]) -> tuple[int, int] | None:
    """Locate a contiguous multi-line quote and return its ``(start, end)`` lines."""

    if not needle_lines:
        return None
    first = needle_lines[0]
    span = len(needle_lines)
    for index in range(len(haystack_lines) - span + 1):
        if haystack_lines[index] != first:
            continue
        if all(
            haystack_lines[index + offset] == needle_lines[offset]
            for offset in range(1, span)
        ):
            return index, index + span
    return None


def _first_difference(left: str, right: str, radius: int = 24) -> dict[str, Any] | None:
    """Describe the first position where two folded strings diverge.

    Parameters
    ----------
    left : str
        The quotation, as folded.
    right : str
        The candidate passage, as folded.
    radius : int, optional
        Characters of context kept around the divergence.

    Returns
    -------
    dict or None
        ``{"quote_char", "corpus_char", "quote_context", "corpus_context"}``, or
        None when the two strings are equal.
    """

    limit = min(len(left), len(right))
    index = next((i for i in range(limit) if left[i] != right[i]), limit)
    if index >= limit and len(left) == len(right):
        return None
    return {
        "quote_char": left[index] if index < len(left) else None,
        "corpus_char": right[index] if index < len(right) else None,
        "quote_context": left[max(0, index - radius): index + radius],
        "corpus_context": right[max(0, index - radius): index + radius],
    }


def verify_quote(
    quote: str,
    *,
    workspace: str | None = None,
    limit: int = 6,
    db: str | Path | None = None,
) -> dict[str, Any]:
    """Check whether a quotation actually occurs in the indexed corpus.

    Four verdicts are possible:

    ``verbatim``
        The quote occurs exactly as printed, ignoring only whitespace and
        full-width punctuation. Safe to cite word for word.
    ``verbatim_normalized``
        The passage matches, but the rendering differs in case or other folding
        a reader would still see. Cite it — after copying the corpus form, which
        is reported in ``differences``.
    ``mismatch``
        Retrieval found the passage, but the quote does not occur in it — the
        quotation is paraphrased, translated differently, or invented.
    ``not_found``
        Retrieval returned nothing, so the quote is not in the reader-facing
        corpus (it may still exist in the page or archive layers).

    Parameters
    ----------
    quote : str
        The quotation to check, without surrounding quotation marks.
    workspace : str or None, optional
        Restrict the check to one workspace.
    limit : int, optional
        Retrieval depth used to locate the passage.
    db : str or pathlib.Path or None, optional
        Global index override.

    Returns
    -------
    dict
        ``{"quote", "verdict", "workspace_filter", "differences", "locations",
        "diagnostics"}``.

    Raises
    ------
    ValueError
        If the quote is shorter than a verifiable phrase.
    """

    stripped = quote.strip()
    if len(stripped) < 8:
        raise ValueError("引文太短，至少需要 8 个字符才能核验")
    module = _load_global_module()
    database = global_db_path(db)
    if not database.is_file():
        raise CorpusError(f"全局索引不存在：{database}")
    probe = re.sub(r"\s+", " ", stripped)[:120]
    # A short probe returns the pre-truncated excerpt; the full chunk text is
    # read back per candidate so the comparison never runs against a fragment
    # that retrieval cut. Without this, a genuinely verbatim quote that starts
    # past the excerpt window would be reported as a mismatch.
    with _stdout_to_stderr():
        rows = module.search(
            probe,
            db_path=database,
            scope="reader",
            workspace=workspace,
            limit=max(limit, 3),
            per_book_cap=0,
            excerpt=False,
        )
    needle = normalize_for_match(stripped)
    locations: list[dict[str, Any]] = []
    exact_seen = False
    normalized_seen = False
    for row in rows:
        content = str(row.get("content") or "")
        if not content and row.get("excerpt"):
            content = str(row["excerpt"])
        haystack = normalize_for_match(content)
        exact = needle in haystack
        folded = False
        span: tuple[int, int] | None = None
        if not exact:
            # Case differences are reported, not accepted silently: the reader
            # will see them on the page.
            folded = needle.casefold() in haystack.casefold()
        if not exact and not folded:
            span = _line_span(_plain_lines(content), _plain_lines(stripped))
            if span is not None:
                folded = True
        exact_seen = exact_seen or exact
        normalized_seen = normalized_seen or folded
        match_kind = "exact" if exact else ("normalized" if folded else None)
        difference = None
        if folded and not exact:
            position = haystack.casefold().find(needle.casefold())
            difference = _first_difference(
                needle, haystack[position:position + len(needle)]
            ) if position >= 0 else None
        locations.append(
            {
                "workspace": row["workspace"],
                "title": row["title"],
                "chapter_order": row["chapter_order"],
                "report_status": row["report_status"],
                "source_path": row["source_path"],
                "content_sha256": row["content_sha256"],
                "matched": exact or folded,
                # `match_kind` is omitted for a non-match: the tool's output
                # schema types it as a string, so a null here fails validation
                # and the whole call returns no verdict at all.
                **({"match_kind": match_kind} if match_kind else {}),
                **({"difference": difference} if difference else {}),
                "line_span": list(span) if span else None,
                "excerpt": excerpt_around(content, stripped[:60]),
            }
        )
    if exact_seen:
        verdict = "verbatim"
    elif normalized_seen:
        verdict = "verbatim_normalized"
    elif locations:
        verdict = "mismatch"
    else:
        verdict = "not_found"
    differences = [
        item["difference"] for item in locations if item.get("difference")
    ]
    return {
        "quote": stripped,
        "verdict": verdict,
        "workspace_filter": workspace,
        "differences": differences,
        "locations": locations,
        "diagnostics": {
            "probe": probe,
            "candidates": len(rows),
            "normalization": (
                "NFKC + 全角标点折叠 + 空白折叠；不折叠大小写，不做繁简转换"
            ),
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def force_utf8_stdio() -> None:
    """Make stdout/stderr carry UTF-8 regardless of the host console code page.

    On a Windows host whose ANSI code page is GBK, a Python child process
    spawned by the plugin inherits ``cp936`` for stdout. Every JSON document
    this module emits contains book titles and passages, so the first non-GBK
    character (a Japanese middle dot in a workspace name is enough) raises
    ``UnicodeEncodeError`` and the caller receives an empty document instead of
    results. Reconfiguring the existing streams, rather than replacing them,
    keeps any wrapper the caller installed and is a no-op where the streams
    already use UTF-8.
    """

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            # A host that replaced the stream with a non-reconfigurable object
            # keeps its own codec; the caller's own output contract applies.
            continue


def _write(payload: Mapping[str, Any]) -> None:
    """Write one JSON document to stdout, UTF-8 and unescaped."""

    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns
    -------
    argparse.ArgumentParser
        Parser exposing ``status``, ``ask`` and ``verify-quote``.
    """

    parser = argparse.ArgumentParser(
        prog="kb_qa",
        description="知识库哲学/文学问答检索核心（JSON 输出）",
    )
    parser.add_argument("--db", type=Path, default=None, help="全局 SQLite 索引路径")
    parser.add_argument("--outputs", type=Path, default=None, help="工作区根目录")
    parser.add_argument(
        "--stdin-json",
        action="store_true",
        help=(
            "从 stdin 读取 {'command': ..., 'args': {...}} 并执行；argv 传入的参数会被忽略。"
            "宿主插件用它在 Windows 上传中文 argv，避免代码页转换破坏查询词。"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="索引健康与可检索书目清单")
    status.add_argument("--limit", type=int, default=200)
    status.add_argument("--json-shelf", action="store_true", help="保留完整书目字段")

    ask_cmd = commands.add_parser("ask", help="两段式检索：跨书发现 + 单书精读")
    ask_cmd.add_argument("query")
    ask_cmd.add_argument("--workspace", default=None, help="限定工作区名称或目录")
    ask_cmd.add_argument(
        "--scope", choices=("reader", "pages", "archive", "all"), default="reader"
    )
    ask_cmd.add_argument("--limit", type=int, default=6)
    ask_cmd.add_argument("--no-deep", action="store_true", help="仅离线诊断：只做跨书发现，非正式取证")
    ask_cmd.add_argument("--per-book-cap", type=int, default=1)
    ask_cmd.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ask_cmd.add_argument("--hit-chars", type=int, default=DEFAULT_HIT_CHARS)
    ask_cmd.add_argument("--verified-only", action="store_true")
    ask_cmd.add_argument("--mode", choices=["hybrid"], default="hybrid", help="正式取证固定 hybrid")
    ask_cmd.add_argument("--lexical", action="store_true", help="仅离线诊断：禁用向量检索，非正式取证")
    ask_cmd.add_argument("--apparatus-weight", type=float, default=None)
    ask_cmd.add_argument("--deep-books", type=int, default=2)

    verify = commands.add_parser("verify-quote", help="核验引文是否真的存在于语料中")
    verify.add_argument("quote")
    verify.add_argument("--workspace", default=None)
    verify.add_argument("--limit", type=int, default=6)
    return parser


def run_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one request object and return its JSON-ready result.

    Shared by the argv CLI and the ``--stdin-json`` bridge, so both entrypoints
    enforce the same validation and error surface.

    Parameters
    ----------
    payload : mapping
        ``{"command": "ask" | "status" | "verify-quote", "args": {...}}``.
        Recognized keys under ``args`` mirror the CLI flags with underscores
        (``per_book_cap``), plus ``db`` and ``outputs``.

    Returns
    -------
    dict
        The command's JSON-ready result.

    Raises
    ------
    ValueError
        If the command is unknown or an argument is out of range.
    CorpusError
        If the corpus cannot serve the request.
    """

    command = str(payload.get("command") or "")
    args: Mapping[str, Any] = payload.get("args") or {}
    db = args.get("db")
    outputs = args.get("outputs")
    if command == "status":
        return shelf(int(args.get("limit") or 200), db=db)
    if command == "ask":
        return ask(
            str(args.get("query") or ""),
            workspace=args.get("workspace") or None,
            scope=str(args.get("scope") or "reader"),
            limit=int(args.get("limit") or 6),
            deep=bool(args.get("deep", True)),
            per_book_cap=int(args.get("per_book_cap", 1)),
            max_chars=int(args.get("max_chars") or DEFAULT_MAX_CHARS),
            hit_chars=int(args.get("hit_chars") or DEFAULT_HIT_CHARS),
            verified_only=bool(args.get("verified_only", False)),
            semantic=bool(args.get("semantic", True)),
            apparatus_weight=(
                float(args["apparatus_weight"])
                if args.get("apparatus_weight") is not None
                else None
            ),
            db=db,
            outputs=outputs,
            deep_books=int(args.get("deep_books") or 2),
        )
    if command == "verify-quote":
        return verify_quote(
            str(args.get("quote") or ""),
            workspace=args.get("workspace") or None,
            limit=int(args.get("limit") or 6),
            db=db,
        )
    raise ValueError(f"未知命令：{command or '<empty>'}")


def _stdin_request() -> dict[str, Any]:
    """Read and parse the JSON request document from stdin.

    The bytes are decoded as UTF-8 explicitly rather than through
    ``sys.stdin``'s text layer. A Windows host whose ANSI code page is GBK
    otherwise decodes the UTF-8 request with cp936, and a Chinese query reaches
    retrieval as mojibake — a silent empty result instead of an error, which is
    the worst possible failure for a search tool.

    Returns
    -------
    dict
        The parsed request object.

    Raises
    ------
    ValueError
        If stdin is empty or does not hold a JSON object.
    """

    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw = buffer.read().decode("utf-8", errors="replace")
    else:  # A host that replaced stdin with a text object keeps its own codec.
        raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("--stdin-json 需要从 stdin 读取 JSON 请求")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("--stdin-json 请求必须是 JSON 对象")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command and emit its JSON result.

    Parameters
    ----------
    argv : sequence of str or None, optional
        Arguments excluding the program name.

    Returns
    -------
    int
        Process exit status: 0 on success, 1 on a domain error, 2 on a usage error.
    """

    load_project_env()
    force_utf8_stdio()
    # `--stdin-json` carries its own command and must be accepted without a
    # positional subcommand, so it is parsed before the subparser requirement
    # applies. The subparser still owns every human-facing invocation.
    if "--stdin-json" in (argv if argv is not None else sys.argv[1:]):
        try:
            payload = run_request(_stdin_request())
        except (CorpusError, ValueError, OSError, json.JSONDecodeError) as exc:
            _write({"error": {"kind": exc.__class__.__name__, "message": str(exc)}})
            return 1
        _write(payload)
        return 0
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            payload = shelf(args.limit, db=args.db)
        elif args.command == "ask":
            payload = ask(
                args.query,
                workspace=args.workspace,
                scope=args.scope,
                limit=args.limit,
                deep=not args.no_deep,
                per_book_cap=args.per_book_cap,
                max_chars=args.max_chars,
                hit_chars=args.hit_chars,
                verified_only=args.verified_only,
                semantic=not args.lexical,
                apparatus_weight=args.apparatus_weight,
                db=args.db,
                outputs=args.outputs,
                deep_books=args.deep_books,
            )
        else:
            payload = verify_quote(
                args.quote,
                workspace=args.workspace,
                limit=args.limit,
                db=args.db,
            )
    except (CorpusError, ValueError, OSError, json.JSONDecodeError) as exc:
        _write({"error": {"kind": exc.__class__.__name__, "message": str(exc)}})
        return 1
    _write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
