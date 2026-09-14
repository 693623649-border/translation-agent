"""Chinese normalisation for knowledge-base corpora: detect → translate → record.

The published corpus feeds an embedding model and a Chinese bigram BM25 index, so
a chunk that is not Chinese is close to unreachable for a Chinese query.  This
module inserts one gate between chunking and publication:

    chunks (id, title, chapter_id, chapter_order, content)
        │
        ├─ classify ──► zh/unknown .............. pass through untouched
        │               reference material ....... pass through (see below)
        │               non-Chinese body ........ translate
        ▼
    chunks with Chinese bodies + knowledge_base.translation.json

Two deliberate constraints:

* The canonical five-field corpus schema is enforced by the publication gate, so
  provenance cannot ride along on each row.  It is published as a sidecar bound
  to the corpus by sha256 instead.
* Reference material is *exempt*, not translated.  A 术语对照 / 译名对照 /
  参考书目 / 索引 chunk is bilingual by construction; translating it destroys
  the mapping that makes it useful.  Those entries are still recorded so the
  decision is auditable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol

from book_pipeline import detect_language

VERSION = 1
SIDECAR_SUFFIX = ".translation.json"
CHINESE = "zh"

# Bilingual or bibliographic material: translating it would destroy its use.
_REFERENCE_MARKERS = (
    "对照",
    "译名",
    "术语表",
    "参考书目",
    "参考文献",
    "书目",
    "索引",
    "bibliography",
    "references",
    "index",
)
# Markdown/image/path shrapnel that must never be counted as prose.
_NON_PROSE_RE = re.compile(
    r"!\[[^\]]*\]\([^)]*\)"          # images
    r"|\[[^\]]*\]\([^)]*\)"          # links
    r"|</?[A-Za-z][^>]*>"            # html tags
    r"|\b[\w.-]+/[\w./-]+\b"         # paths and urls
)
_MIN_PROSE_MASS = 24
_HAN_RE = re.compile(r"[\u3400-\u9fff]")

_MARKER = "<<<SEG {index:04d}>>>"
_MARKER_SCAN_RE = re.compile(r"<<<SEG (\d{4})>>>")

_SYSTEM_PROMPT = (
    "你是严谨的学术图书译者，把用户给出的文本逐段译为简体中文。"
    "必须原样保留每段的 <<<SEG nnnn>>> 标记与顺序，不得合并、拆分、增删段落。"
    "保留 ⟦SEMANTIC_TOKEN_xxxx⟧ 占位符、专名、数字与文献信息。"
    "只输出段落与标记，不要任何解释或代码围栏。"
)


class KbTranslationError(ValueError):
    """The translation step could not produce a trustworthy 1:1 mapping."""


class Translator(Protocol):
    """Minimal text-in/text-out model boundary (injectable for tests)."""

    provider_name: str
    model: str

    def translate(self, prompt: str) -> str: ...


class DeepSeekTranslator:
    """OpenAI-compatible chat completions client, mirroring the embedding adapter."""

    provider_name = "deepseek"

    def __init__(
        self,
        *,
        api_key_env: str = "DEEPSEEK_API_KEY",
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 300,
    ) -> None:
        self.api_key_env = api_key_env
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def translate(self, prompt: str) -> str:
        api_key = os.getenv(self.api_key_env, "").strip()
        if not api_key:
            raise KbTranslationError(
                f"Missing translation API key; set the {self.api_key_env} environment variable"
            )
        payload = {
            "model": self.model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise KbTranslationError(f"{self.provider_name} HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise KbTranslationError(f"{self.provider_name} request failed: {exc}") from exc
        try:
            value = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise KbTranslationError(f"{self.provider_name} response has no message content") from exc
        if not isinstance(value, str) or not value.strip():
            raise KbTranslationError(f"{self.provider_name} returned empty content")
        return value


def _prose_mass(text: str) -> int:
    """Count non-whitespace characters left after removing non-prose shrapnel.

    Counting letters only would misjudge Japanese text (Han-dominant, sparse
    kana) and short English chunks as "not prose"; whitespace is excluded so
    image/link markup cannot inflate the score.
    """

    return len(re.sub(r"\s+", "", _NON_PROSE_RE.sub(" ", text)))


def classify_row(title: str, content: str) -> dict[str, Any]:
    """Decide whether one chunk must be translated before publication.

    ``detect_language`` reports ``unknown`` below its significance floor, which
    also covers short Chinese tail chunks.  The script check below resolves that
    case, so a brief English fragment is still translated while a brief Chinese
    one is not.
    """

    language = detect_language(content)
    label = str(title or "")
    if language == CHINESE:
        return {"language": language, "needs_translation": False, "reason": "already_chinese"}
    if any(marker in label.casefold() for marker in _REFERENCE_MARKERS):
        return {"language": language, "needs_translation": False, "reason": "reference_material"}
    stripped = _NON_PROSE_RE.sub(" ", content)
    if language == "unknown" and _HAN_RE.search(stripped):
        # Below the detector's significance floor but Han-bearing: Chinese.
        return {"language": CHINESE, "needs_translation": False, "reason": "already_chinese"}
    if _prose_mass(content) < _MIN_PROSE_MASS:
        return {"language": language, "needs_translation": False, "reason": "not_prose"}
    if language == "unknown":
        return {"language": "other", "needs_translation": True, "reason": "non_chinese_body"}
    return {"language": language, "needs_translation": True, "reason": "non_chinese_body"}


def plan_translation(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Classify every chunk so callers can report before spending a model call."""

    entries: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    for row in rows:
        verdict = classify_row(str(row.get("title") or ""), str(row.get("content") or ""))
        record = {
            "id": str(row.get("id") or ""),
            "title": str(row.get("title") or ""),
            "chapter_id": str(row.get("chapter_id") or ""),
            **verdict,
        }
        entries.append(record)
        if not verdict["needs_translation"]:
            skipped[verdict["reason"]] = skipped.get(verdict["reason"], 0) + 1
    pending = [entry for entry in entries if entry["needs_translation"]]
    return {
        "chunk_count": len(entries),
        "translate_count": len(pending),
        "skipped": skipped,
        "languages": _count_by(entries, "language"),
        "entries": entries,
    }


def _count_by(entries: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _protect(text: str) -> tuple[str, tuple[str, ...]]:
    from semantic_translation_runner import protect_tokens

    return protect_tokens(text)


def _restore(text: str, tokens: Sequence[str]) -> str:
    from semantic_translation_runner import restore_tokens

    return restore_tokens(text, tokens)


def _build_prompt(segments: Sequence[str]) -> str:
    blocks = [f"{_MARKER.format(index=index)}\n{segment}" for index, segment in enumerate(segments)]
    return (
        f"把下面 {len(segments)} 段文本逐段译为简体中文。"
        "每段以 <<<SEG nnnn>>> 开头，输出时必须保留同样的标记与顺序。\n\n" + "\n\n".join(blocks)
    )


def _parse_response(text: str, expected: int) -> list[str]:
    matches = list(_MARKER_SCAN_RE.finditer(text))
    found = [int(match.group(1)) for match in matches]
    if found != list(range(expected)):
        raise KbTranslationError(
            f"translation response markers mismatch: expected 0..{expected - 1}, got {found}"
        )
    segments: list[str] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        segments.append(text[match.end():end].strip())
    if any(not segment for segment in segments):
        raise KbTranslationError("translation response contained an empty segment")
    return segments


def translate_texts(
    texts: Sequence[str],
    translator: Translator,
    *,
    batch_chars: int = 8000,
    concurrency: int = 8,
) -> list[str]:
    """Translate texts 1:1, batching on character budget.

    Batches are independent, so they run concurrently; results are re-ordered by
    batch index before being returned.  Fails closed: a batch whose markers do
    not come back intact raises instead of guessing, because a silently
    misaligned corpus is worse than a failed run.
    """

    if not isinstance(batch_chars, int) or isinstance(batch_chars, bool) or batch_chars <= 0:
        raise ValueError("batch_chars must be a positive integer")
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
        raise ValueError("concurrency must be a positive integer")
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("translation inputs must be non-empty strings")
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for text in texts:
        if current and size + len(text) > batch_chars:
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append(current)

    def run_batch(batch: list[str]) -> list[str]:
        protected = [_protect(segment) for segment in batch]
        response = translator.translate(_build_prompt([item[0] for item in protected]))
        return [
            _restore(segment, item[1])
            for segment, item in zip(_parse_response(response, len(batch)), protected, strict=True)
        ]

    def run(batch: list[str]) -> list[str]:
        try:
            return run_batch(batch)
        except KbTranslationError:
            if len(batch) == 1:
                raise
            # One flaky response must not fail a whole book: retry the batch as
            # single-segment calls, where the marker contract is trivially met.
            return [segment for single in batch for segment in run_batch([single])]

    if len(batches) == 1 or concurrency == 1:
        results = [segment for batch in batches for segment in run(batch)]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(concurrency, len(batches))) as pool:
            batches_of_segments = list(pool.map(run, batches))
        results = [segment for batch in batches_of_segments for segment in batch]
    if len(results) != len(texts):
        raise KbTranslationError(f"translated {len(results)} segments for {len(texts)} chunks")
    return results


def ensure_chinese_rows(
    rows: Sequence[dict[str, Any]],
    translator: Translator,
    *,
    batch_chars: int = 8000,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return rows with Chinese bodies plus a provenance report.

    Chunk ids are position-derived and translation does not move chunks, so ids
    and coordinates are preserved exactly; only ``content`` changes.
    """

    plan = plan_translation(rows)
    pending = [
        index
        for index, entry in enumerate(plan["entries"])
        if entry["needs_translation"]
    ]
    if not pending:
        return [dict(row) for row in rows], _finalise_report(plan, translator, translated=[])
    translated = translate_texts(
        [str(rows[index]["content"]) for index in pending],
        translator,
        batch_chars=batch_chars,
    )
    # Pair positionally: a hand-edited corpus may repeat an id, and matching by
    # id membership would then translate the wrong rows.
    translated_by_index = dict(zip(pending, translated, strict=True))
    output: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        updated = dict(row)
        if index in translated_by_index:
            original = str(row.get("content") or "")
            updated["content"] = translated_by_index[index]
            records.append(
                {
                    "id": str(row.get("id") or ""),
                    "title": str(row.get("title") or ""),
                    "chapter_id": str(row.get("chapter_id") or ""),
                    "source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                    "source_characters": len(original),
                }
            )
        output.append(updated)
    return output, _finalise_report(plan, translator, translated=records)


def _finalise_report(
    plan: dict[str, Any],
    translator: Translator,
    *,
    translated: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    report = dict(plan)
    report.update(
        {
            "provider": getattr(translator, "provider_name", ""),
            "model": getattr(translator, "model", ""),
            "translated_count": len(translated),
            "translated": list(translated),
        }
    )
    return report


def sidecar_path_for(corpus_path: Path | str) -> Path:
    return Path(corpus_path).with_suffix(SIDECAR_SUFFIX)


def source_backup_path_for(corpus_path: Path | str) -> Path:
    """Original text of every translated chunk, so the step stays reversible."""

    return Path(corpus_path).with_suffix(".translation-source.jsonl")


def write_source_backup(corpus_path: Path | str, originals: Sequence[dict[str, Any]]) -> Path:
    from rag_knowledge_base import _atomic_write_text

    target = source_backup_path_for(corpus_path)
    if not originals:
        return target
    body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in originals)
    _atomic_write_text(target, body)
    return target


def restore_translated_chunks(corpus_path: Path | str) -> int:
    """Put the pre-translation text back for chunks recorded in the backup."""

    from book_pipeline import write_knowledge_base
    from rag_knowledge_base import load_knowledge_rows

    corpus = Path(corpus_path)
    backup = source_backup_path_for(corpus)
    if not backup.is_file():
        raise KbTranslationError(f"No translation backup to restore: {backup}")
    originals = {
        str(row["id"]): str(row["content"])
        for row in (json.loads(line) for line in backup.read_text(encoding="utf-8").splitlines() if line.strip())
    }
    rows = load_knowledge_rows(corpus)
    restored = 0
    for row in rows:
        row_id = str(row["id"])
        if row_id in originals and str(row["content"]) != originals[row_id]:
            row["content"] = originals[row_id]
            restored += 1
    if restored:
        write_knowledge_base(corpus, rows)
    return restored


def write_translation_sidecar(corpus_path: Path | str, report: dict[str, Any]) -> dict[str, Any]:
    """Publish provenance bound to the post-translation corpus bytes."""

    from rag_knowledge_base import _atomic_write_text

    corpus = Path(corpus_path)
    payload = {
        "schema_version": VERSION,
        "kind": "translation-agent.kb-translation",
        "documents_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
        "provider": report.get("provider", ""),
        "model": report.get("model", ""),
        "chunk_count": report.get("chunk_count", 0),
        "translated_count": report.get("translated_count", 0),
        "skipped": report.get("skipped", {}),
        "languages": report.get("languages", {}),
        "translated": report.get("translated", []),
    }
    target = sidecar_path_for(corpus)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    unchanged = target.is_file() and target.read_text(encoding="utf-8") == encoded
    if not unchanged:
        _atomic_write_text(target, encoded)
    return {"path": str(target), "unchanged": unchanged, **{k: payload[k] for k in ("translated_count", "chunk_count")}}


def load_translation_sidecar(corpus_path: Path | str) -> dict[str, Any]:
    """Read provenance, rejecting a sidecar that no longer matches the corpus."""

    corpus = Path(corpus_path)
    target = sidecar_path_for(corpus)
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KbTranslationError(f"Cannot read translation sidecar {target}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != VERSION:
        raise KbTranslationError(f"Unsupported translation sidecar schema: {target}")
    if payload.get("documents_sha256") != hashlib.sha256(corpus.read_bytes()).hexdigest():
        raise KbTranslationError(
            f"Translation sidecar does not describe the current corpus: {target}"
        )
    return payload


def normalise_corpus_file(
    corpus_path: Path | str,
    translator: Translator | None = None,
    *,
    batch_chars: int = 8000,
    dry_run: bool = False,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """In-place Chinese normalisation of one ``knowledge_base.jsonl``.

    Writes atomically and re-initialises the RAG manifest, which by design marks
    the embedding index as awaiting a rebuild — translated text invalidates the
    stored vectors.
    """

    from book_pipeline import write_knowledge_base
    from rag_knowledge_base import load_knowledge_rows

    corpus = Path(corpus_path)
    rows = load_knowledge_rows(corpus)
    plan = plan_translation(rows)
    if dry_run or plan["translate_count"] == 0:
        return {**plan, "status": "dry_run" if dry_run else "nothing_to_do", "written": False}
    if translator is None:
        raise KbTranslationError("a translator is required when chunks need translation")
    if progress is not None:
        progress({"phase": "translate", "pending": plan["translate_count"]})
    pending = [
        index
        for index, entry in enumerate(plan["entries"])
        if entry["needs_translation"]
    ]
    originals = [
        {
            "id": str(rows[index].get("id") or ""),
            "title": str(rows[index].get("title") or ""),
            "chapter_id": str(rows[index].get("chapter_id") or ""),
            "content": str(rows[index].get("content") or ""),
        }
        for index in pending
    ]
    translated_rows, report = ensure_chinese_rows(rows, translator, batch_chars=batch_chars)
    backup = write_source_backup(corpus, originals)
    write_knowledge_base(corpus, translated_rows)
    sidecar = write_translation_sidecar(corpus, report)
    return {
        "status": "passed",
        "written": True,
        "translate_count": report["translated_count"],
        "skipped": report["skipped"],
        "languages": report["languages"],
        "provider": report["provider"],
        "model": report["model"],
        "sidecar": sidecar["path"],
        "source_backup": str(backup),
    }
