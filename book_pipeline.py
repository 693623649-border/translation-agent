from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import functools
import hashlib
import html
import http.client
import inspect
import json
import os
import re
import select
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
import ssl
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol

import fitz
from PIL import Image

from pipeline_profiles import (
    ModelIdentity,
    ModelProfile,
    PipelineProfiles,
    load_pipeline_profiles,
)
from pipeline_runtime import (
    RetryPolicy,
    SharedAdaptiveRateLimiter,
    StartRateLimiter,
    retry_with_backoff,
)
from publication_verifier import verify_publication
from docx_footnotes import patch_docx_footnotes
from publication_semantics import (
    append_markdown_footnotes,
    markdown_footnote_contract_sha256,
    markdown_footnotes_to_docx_markers,
    parse_markdown_footnotes,
    reconstruct_page_footnotes,
    semantic_audit_summary,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility path.
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX compatibility path.
    msvcrt = None


DEFAULT_CODING_API_BASE = "https://open.bigmodel.cn/api/coding/paas/v4"
DEFAULT_STANDARD_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_OCR_CONCURRENCY = 4
DEFAULT_TRANSLATION_CONCURRENCY = 16
TRANSLATION_PROMPT_VERSION = "book-translation-v3"
PROOFREAD_PROMPT_VERSION = "book-ocr-proofread-ja-v1"
TOC_KINDS = {"part", "chapter", "section", "subsection", "frontmatter", "other"}
NON_CONTENT_MARKERS = {"[无法辨认]", "[空白页]"}


def join_physical_page_texts(pages: Iterable[str]) -> str:
    """Join ordered physical pages without serialising an internal marker."""

    return "\n\n".join(str(page).strip() for page in pages).strip()


def _validated_physical_page_texts(
    text: str,
    pages: Iterable[str],
) -> tuple[str, ...] | None:
    """Return trustworthy structured halves, or ``None`` for legacy/plain text."""

    normalized = tuple(str(page).strip() for page in pages)
    if len(normalized) < 2:
        return None
    if join_physical_page_texts(normalized) != text.strip():
        return None
    return normalized


class PhysicalPageOCRText(str):
    """A string-compatible OCR result carrying right-to-left physical pages.

    OCR backends historically return ``tuple[str, request_id]``.  A ``str``
    subclass keeps that public contract intact while allowing ``ocr_pdf`` to
    persist the otherwise-lost boundary as structured checkpoint metadata.
    """

    physical_page_texts: tuple[str, ...]

    def __new__(cls, pages: Iterable[str]) -> "PhysicalPageOCRText":
        normalized = tuple(str(page).strip() for page in pages)
        instance = super().__new__(cls, join_physical_page_texts(normalized))
        instance.physical_page_texts = normalized
        return instance


@dataclass
class PageRecord:
    pdf_page: int
    text: str
    language: str = "unknown"
    translated_text: str = ""
    translation_source_sha256: str = ""
    translation_provider: str = ""
    translation_model: str = ""
    translation_target_language: str = ""
    translation_prompt_version: str = ""
    translation_fingerprint: str = ""
    notes: str = ""
    ocr_model: str = ""
    # Appended after the legacy fields so positional PageRecord callers keep
    # their historical argument order. The layer remains separate from
    # ``text`` so the OCR checkpoint is always auditable and recoverable.
    proofread_text: str = ""
    proofread_source_sha256: str = ""
    proofread_provider: str = ""
    proofread_model: str = ""
    proofread_language: str = ""
    proofread_prompt_version: str = ""
    proofread_fingerprint: str = ""
    # Ordered physical pages inside one PDF scan. Japanese two-page spreads
    # are stored as (right page, left page), matching ascending printed-page
    # order. Separate arrays at every model layer make the boundary immune to
    # proofreading/translation rewrites while old checkpoints remain valid.
    physical_page_texts: list[str] = field(default_factory=list)
    proofread_physical_page_texts: list[str] = field(default_factory=list)
    translated_physical_page_texts: list[str] = field(default_factory=list)

    @property
    def compile_text(self) -> str:
        return (
            self.translated_text
            if self.translation_is_fresh
            else self.effective_text
        ).strip()

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def proofread_is_fresh(self) -> bool:
        return bool(
            self.proofread_text.strip()
            and self.proofread_source_sha256
            and self.proofread_source_sha256 == self.text_sha256
            and self.proofread_provider
            and self.proofread_model
            and self.proofread_language
        )

    def proofread_is_fresh_for(self, identity: ModelIdentity | None) -> bool:
        if identity is None:
            return self.proofread_is_fresh
        return bool(
            self.proofread_is_fresh
            and self.proofread_provider == identity.provider
            and self.proofread_model == identity.model
            and self.proofread_language == identity.target_language
            and self.proofread_prompt_version == identity.prompt_version
            and self.proofread_fingerprint == identity.fingerprint
        )

    @property
    def effective_text(self) -> str:
        """Current source text: fresh proofreading overlay, otherwise raw OCR."""

        return self.proofread_text if self.proofread_is_fresh else self.text

    @property
    def raw_physical_pages(self) -> tuple[str, ...]:
        return _validated_physical_page_texts(
            self.text,
            self.physical_page_texts,
        ) or (self.text.strip(),)

    @property
    def effective_physical_pages(self) -> tuple[str, ...]:
        if self.proofread_is_fresh:
            return _validated_physical_page_texts(
                self.proofread_text,
                self.proofread_physical_page_texts,
            ) or (self.proofread_text.strip(),)
        return self.raw_physical_pages

    @property
    def effective_text_sha256(self) -> str:
        return hashlib.sha256(self.effective_text.encode("utf-8")).hexdigest()

    @property
    def translation_is_fresh(self) -> bool:
        return bool(
            self.translated_text.strip()
            and self.translation_source_sha256
            and self.translation_source_sha256 == self.effective_text_sha256
            and self.translation_provider
            and self.translation_model
            and self.translation_target_language
        )

    def translation_is_fresh_for(
        self,
        identity: ModelIdentity | None = None,
        *,
        provider: str = "",
        model: str = "",
        target_language: str = "",
        prompt_version: str = TRANSLATION_PROMPT_VERSION,
        profile_fingerprint: str = "",
    ) -> bool:
        if identity is not None:
            provider = identity.provider
            model = identity.model
            target_language = identity.target_language
            prompt_version = identity.prompt_version
            profile_fingerprint = identity.fingerprint
        return bool(
            self.translation_is_fresh
            and self.translation_provider == provider
            and self.translation_model == model
            and self.translation_target_language == target_language
            and self.translation_prompt_version == prompt_version
            and self.translation_fingerprint == profile_fingerprint
        )

    def compile_text_for(self, identity: ModelIdentity | None) -> str:
        if identity is None:
            return self.compile_text
        return (
            self.translated_text
            if self.translation_is_fresh_for(identity)
            else self.effective_text
        ).strip()

    def compile_physical_pages_for(
        self,
        identity: ModelIdentity | None,
    ) -> tuple[str, ...]:
        translation_is_selected = (
            self.translation_is_fresh
            if identity is None
            else self.translation_is_fresh_for(identity)
        )
        if translation_is_selected:
            return _validated_physical_page_texts(
                self.translated_text,
                self.translated_physical_page_texts,
            ) or (self.translated_text.strip(),)
        return self.effective_physical_pages


@dataclass
class TocEntry:
    id: str
    index: str
    title: str
    level: int
    kind: str
    printed_page: int | None
    pdf_page: int | None = None
    end_pdf_page: int | None = None

    @property
    def display_title(self) -> str:
        return " ".join(part for part in (self.index.strip(), self.title.strip()) if part)


class Translator(Protocol):
    """Translation extension point for OCR text produced in non-Chinese languages."""

    def translate(self, text: str, *, source_language: str, target_language: str) -> str: ...


class Proofreader(Protocol):
    """Non-destructive correction extension point for raw OCR text."""

    def proofread(self, text: str, *, language: str) -> str: ...


class TextChatBackend(Protocol):
    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str: ...

    def model_identity(
        self,
        *,
        target_language: str,
        prompt_version: str,
    ) -> ModelIdentity: ...


class OCRBackend(Protocol):
    ocr_model: str

    def ocr_image(self, image_path: Path) -> tuple[str, str]: ...

    def close(self) -> None: ...


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Concurrent OCR/translation workers may update different page records in
    # the same directory. A fixed ``.tmp`` sibling lets one process rename
    # another process's temporary file, causing a FileNotFoundError (or,
    # worse, publishing the wrong page). Keep the temporary name unique while
    # retaining the same-filesystem atomic replace.
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def slugify(value: str, max_len: int = 100) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip(" ._")
    return (value[:max_len] or "untitled").strip(" ._")


def parse_page_spec(value: str) -> list[int]:
    pages: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        match = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if match:
            start, end = int(match.group(1)), int(match.group(2))
            if start < 1 or end < start:
                raise ValueError(f"Invalid page range: {token}")
            pages.update(range(start, end + 1))
        elif token.isdigit() and int(token) >= 1:
            pages.add(int(token))
        else:
            raise ValueError(f"Invalid page value: {token}")
    if not pages:
        raise ValueError("Page list is empty.")
    return sorted(pages)


def parse_model_prefixes(value: str | None) -> tuple[str, ...]:
    """Parse comma-separated cache/model prefixes consistently across stages."""

    return tuple(
        prefix.strip()
        for prefix in (value or "").split(",")
        if prefix.strip()
    )


def detect_language(text: str) -> str:
    han = len(re.findall(r"[\u3400-\u9fff]", text))
    kana = len(re.findall(r"[\u3040-\u30ff]", text))
    hangul = len(re.findall(r"[\uac00-\ud7af]", text))
    cyrillic = len(re.findall(r"[\u0400-\u04ff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    significant = han + kana + hangul + cyrillic + latin
    if significant < 12:
        return "unknown"
    if kana >= 5 and kana / max(1, han + kana) >= 0.08:
        return "ja"
    if hangul >= max(5, significant * 0.2):
        return "ko"
    if cyrillic >= max(5, significant * 0.25):
        return "ru"
    if han >= max(8, latin * 0.35):
        return "zh"
    if latin >= max(8, significant * 0.6):
        return "en"
    return "other"


def page_record_needs_translation(record: PageRecord) -> bool:
    """Return whether a nonblank effective OCR page requires Chinese translation."""

    text = record.effective_text.strip()
    if not text or text in NON_CONTENT_MARKERS:
        return False
    language = record.language
    if language in {"", "unknown"}:
        language = detect_language(text)
    return language not in {"zh", "unknown"}


def split_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in re.split(r"(\n\s*\n)", text):
        if size + len(paragraph) > max_chars and current:
            chunks.append("".join(current).strip())
            current, size = [], 0
        while len(paragraph) > max_chars:
            if current:
                chunks.append("".join(current).strip())
                current, size = [], 0
            chunks.append(paragraph[:max_chars].strip())
            paragraph = paragraph[max_chars:]
        current.append(paragraph)
        size += len(paragraph)
    if current:
        chunks.append("".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            raise ValueError("The model did not return a JSON object.")
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("The model response must be a JSON object.")
    return value


def clean_ocr_text(text: str) -> str:
    """Remove wrappers and postscript commentary occasionally added by vision OCR."""
    cleaned = text.strip()
    lines = cleaned.splitlines()
    fence = re.compile(r"^```(?:markdown|md|text|plaintext)?\s*$", flags=re.I)
    # Remove a fence only when it encloses the complete response.  The old
    # prefix-only match could erase real text after a leading empty code block.
    if len(lines) >= 2 and fence.fullmatch(lines[0].strip()):
        closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "```"), None)
        if closing is not None:
            remainder = "\n".join(lines[closing + 1 :]).strip()
            if not remainder or re.match(
                r"^(?:\*\*)?(?:Content Type|Language/Format|OCR Corrections|Quality Notes)",
                remainder,
                flags=re.I,
            ):
                lines = lines[1:closing]
    while len(lines) >= 2 and fence.fullmatch(lines[0].strip()) and lines[1].strip() == "```":
        lines = lines[2:]
        while lines and not lines[0].strip():
            lines.pop(0)
    # Segmented OCR occasionally returns only the opening or closing wrapper
    # fence. A lone edge fence cannot delimit a real Markdown block, so it is
    # safe to drop without touching balanced fences inside book content.
    if lines and fence.fullmatch(lines[0].strip()) and not any(
        line.strip() == "```" for line in lines[1:]
    ):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```" and not any(
        fence.fullmatch(line.strip()) for line in lines[:-1]
    ):
        lines = lines[:-1]
    # Empty image bands sometimes arrive as a fenced unreadable marker after
    # otherwise valid page text.  Drop only that narrow trailing artifact.
    if lines and lines[-1].strip() == "```":
        for index in range(len(lines) - 2, -1, -1):
            if fence.fullmatch(lines[index].strip()):
                enclosed = "\n".join(lines[index + 1 : -1]).strip()
                if re.fullmatch(r"\[无法辨认\]|[（(]?(?:无|没有)可见(?:文字|内容)[）)]?", enclosed):
                    lines = lines[:index]
                break
    cleaned = "\n".join(lines).strip()
    cleaned = re.sub(r"^(?:plaintext|markdown|text)\s*\n", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"^(?:#{1,6}\s*)?(?:\*\*)?Extracted Text(?::)?(?:\*\*)?\s*",
        "",
        cleaned,
        flags=re.I,
    )
    commentary = re.search(
        r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:\*\*)?"
        r"(?:Content Type|Language/Format|OCR Corrections|Quality Notes)"
        r"(?:\*\*)?(?:\s*:)?",
        cleaned,
        flags=re.I,
    )
    if commentary:
        cleaned = cleaned[: commentary.start()]
    return cleaned


def clean_translation_text(text: str) -> str:
    """Remove model commentary while preserving the translated book text."""
    cleaned = clean_ocr_text(text)
    lines = cleaned.splitlines()

    # Some chat models prepend a prose quality/OCR notice despite being asked
    # to return only the translation.  It is never part of the source book.
    first_content = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_content is not None and re.match(
        r"^(?:修正说明|翻译说明|译文说明|OCR\s*说明|说明)[：:]",
        lines[first_content].strip(),
        flags=re.I,
    ):
        end = first_content + 1
        while end < len(lines) and lines[end].strip():
            end += 1
        del lines[first_content:end]

    # Likewise, discard an appended translator-note section invented by the
    # model.  Inline uncertainty markers remain because they are traceable to
    # a specific damaged source phrase.
    note_start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.search(r"译者注(?:释)?", line, flags=re.I)
            and re.match(r"^\s*(?:#{1,6}\s*)?(?:[*_]+\s*)?译者注", line, flags=re.I)
        ),
        None,
    )
    if note_start is not None:
        while note_start > 0 and (
            not lines[note_start - 1].strip()
            or re.fullmatch(r"\s*(?:\*{3,}|-{3,}|_{3,})\s*", lines[note_start - 1])
        ):
            note_start -= 1
        del lines[note_start:]

    lines = [
        line
        for line in lines
        if not re.search(r"(?:翻译|译文)如下[。.:：]?\s*$", line.strip())
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_target_script_state = threading.local()


def normalize_target_script(text: str, target_language: str) -> str:
    """Deterministically normalize model output to the requested Chinese script."""
    if target_language != "简体中文" or not text:
        return text
    try:
        from opencc import OpenCC  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Simplified-Chinese output requires opencc-python-reimplemented; "
            "install requirements.txt."
        ) from exc
    converter = getattr(_target_script_state, "simplified_converter", None)
    if converter is None:
        converter = OpenCC("t2s")
        _target_script_state.simplified_converter = converter
    # OpenCC may convert some lexical uses of 著 (for example 名著) to 着,
    # while model output can also contain the Taiwanese aspect marker 著.
    # Mark every 著 that participates in a known lexical term both before and
    # after conversion. Computing positions before substitution also handles
    # overlapping terms such as 所著名著 without losing either character.
    protected_terms = (
        "著者", "著作", "著名", "显著", "卓著", "编著", "译著", "原著", "所著", "著有",
        "巨著", "名著", "专著", "论著", "土著", "著书", "著述", "著称", "著录", "著文",
    )
    lexical_sentinel = "\ue000"

    def protect_lexical_zhu(value: str) -> str:
        protected_positions: set[int] = set()
        for term in protected_terms:
            start = 0
            while True:
                position = value.find(term, start)
                if position < 0:
                    break
                protected_positions.update(
                    position + offset
                    for offset, character in enumerate(term)
                    if character == "著"
                )
                start = position + 1
        return "".join(
            lexical_sentinel if index in protected_positions else character
            for index, character in enumerate(value)
        )

    normalized = str(converter.convert(protect_lexical_zhu(text)))
    normalized = protect_lexical_zhu(normalized)
    normalized = normalized.replace("著", "着")
    normalized = normalized.replace(lexical_sentinel, "著")
    normalized = normalized.replace("著作家", "着作家")
    return normalized


class ProviderHTTPError(RuntimeError):
    def __init__(self, service_name: str, status_code: int, detail: str) -> None:
        super().__init__(f"{service_name} HTTP {status_code}: {detail}")
        self.status_code = status_code


class GlmClient:
    service_name = "GLM"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = DEFAULT_STANDARD_API_BASE,
        ocr_model: str = "glm-ocr",
        text_model: str = "glm-5.2",
        timeout: int = 240,
        retries: int = 6,
        provider_name: str = "glm",
        adapter_name: str = "openai-chat",
        thinking: str = "disabled",
    ) -> None:
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.ocr_model = ocr_model
        self.text_model = text_model
        self.timeout = timeout
        self.retries = retries
        self.provider_name = provider_name
        self.adapter_name = adapter_name
        if thinking not in {"enabled", "disabled", "omit"}:
            raise ValueError("thinking must be enabled, disabled, or omit")
        self.thinking = thinking

    def model_identity(
        self,
        *,
        target_language: str,
        prompt_version: str,
    ) -> ModelIdentity:
        return ModelIdentity(
            provider=self.provider_name,
            adapter=self.adapter_name,
            base_url=self.api_base,
            model=self.text_model,
            target_language=target_language,
            prompt_version=prompt_version,
        )

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        def request_once() -> dict[str, Any]:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
                raise ProviderHTTPError(self.service_name, exc.code, detail) from exc

        try:
            return retry_with_backoff(
                request_once,
                policy=RetryPolicy(
                    attempts=self.retries,
                    base_delay=1.5,
                    max_delay=8.0,
                    rate_limit_base_delay=5.0,
                    rate_limit_max_delay=45.0,
                    rate_limit_jitter=3.0,
                ),
                should_retry=lambda exc: not (
                    isinstance(exc, ProviderHTTPError)
                    and exc.status_code in {400, 401, 403, 404}
                ),
            )
        except (
            ProviderHTTPError,
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            TimeoutError,
            ssl.SSLError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(f"{self.service_name} request failed: {exc}") from exc

    def ocr_image(self, image_path: Path) -> tuple[str, str]:
        mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        response = self._post(
            "layout_parsing",
            {
                "model": self.ocr_model,
                "file": f"data:{mime};base64,{encoded}",
                "return_crop_images": False,
                "need_layout_visualization": False,
            },
        )
        text = response.get("md_results")
        if not isinstance(text, str):
            raise RuntimeError(f"Unexpected GLM-OCR response; request_id={response.get('request_id', 'unknown')}")
        request_id = str(response.get("request_id") or response.get("id") or "")
        return clean_ocr_text(text), request_id

    def chat_json(self, prompt: str, *, system: str, max_tokens: int = 16384) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if self.thinking != "omit":
            payload["thinking"] = {"type": self.thinking}
        response = self._post("chat/completions", payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Unexpected GLM chat response.") from exc
        return parse_json_response(str(content))

    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
        payload: dict[str, Any] = {
            "model": self.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if self.thinking != "omit":
            payload["thinking"] = {"type": self.thinking}
        response = self._post("chat/completions", payload)
        try:
            return str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Unexpected GLM chat response.") from exc

    def close(self) -> None:
        return


class DeepSeekClient(GlmClient):
    """OpenAI-compatible DeepSeek text client, isolated from GLM OCR/TOC credentials."""

    service_name = "DeepSeek"

    def __init__(
        self,
        *,
        api_key: str,
        api_base: str = DEFAULT_DEEPSEEK_API_BASE,
        text_model: str = DEFAULT_DEEPSEEK_MODEL,
        timeout: int = 120,
        retries: int = 6,
        thinking: str = "disabled",
        adapter_name: str = "openai-chat",
    ) -> None:
        super().__init__(
            api_key=api_key,
            api_base=api_base,
            text_model=text_model,
            timeout=timeout,
            retries=retries,
            provider_name="deepseek",
            adapter_name=adapter_name,
            thinking=thinking,
        )

    def _chat(self, prompt: str, *, system: str, max_tokens: int) -> str:
        payload: dict[str, Any] = {
            "model": self.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }
        if self.thinking != "omit":
            payload["thinking"] = {"type": self.thinking}
        response = self._post("chat/completions", payload)
        try:
            return str(response["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Unexpected DeepSeek chat response.") from exc

    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
        return self._chat(prompt, system=system, max_tokens=max_tokens)

    def chat_json(self, prompt: str, *, system: str, max_tokens: int = 16384) -> dict[str, Any]:
        return parse_json_response(self._chat(prompt, system=system, max_tokens=max_tokens))


class McpStdioClient:
    """Minimal newline-delimited JSON-RPC client for the official Coding Plan vision MCP."""

    def __init__(
        self,
        command: list[str],
        *,
        api_key: str,
        vision_model: str = "glm-4.6v",
        request_timeout: int = 120,
        reading_direction: str = "horizontal",
    ) -> None:
        if not command or shutil.which(command[0]) is None:
            raise RuntimeError(
                "Coding Plan vision OCR requires Node.js 18+ and npx. "
                "Install Node.js, then verify: npx -y @z_ai/mcp-server@0.1.4"
            )
        environment = os.environ.copy()
        environment["Z_AI_API_KEY"] = api_key
        environment.setdefault("Z_AI_MODE", "ZHIPU")
        environment["Z_AI_VISION_MODEL"] = vision_model
        self.request_timeout = max(30, int(request_timeout))
        # The upstream MCP package retries a failed tool call internally. Keep
        # its first HTTP deadline just below our JSON-RPC deadline so a dense
        # page can be split locally instead of multiplying two retry loops.
        environment["Z_AI_TIMEOUT"] = str(
            max(30, self.request_timeout - 5) * 1000
        )
        self.stderr_lines: deque[str] = deque(maxlen=40)
        self._api_key = api_key
        self.reading_direction = reading_direction
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
            start_new_session=(os.name == "posix"),
        )
        self.stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name=f"vision-mcp-stderr-{self.process.pid}",
            daemon=True,
        )
        self.stderr_thread.start()
        self.next_id = 1
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "translation-agent", "version": "2.0.0"},
            },
        )
        self._notify("notifications/initialized", {})
        tools_result = self._request("tools/list", {})
        tools = tools_result.get("tools", []) if isinstance(tools_result, dict) else []
        self.tool = next(
            (tool for tool in tools if isinstance(tool, dict) and tool.get("name") == "extract_text_from_screenshot"),
            None,
        )
        if self.tool is None:
            self.close()
            raise RuntimeError("Coding Plan vision MCP does not expose extract_text_from_screenshot.")

    def _drain_stderr(self) -> None:
        if self.process.stderr is None:
            return
        try:
            for raw_line in self.process.stderr:
                line = raw_line.strip().replace(self._api_key, "<redacted>")
                if line:
                    self.stderr_lines.append(line)
        except (OSError, ValueError):
            # close() may close the pipe while the daemon reader is blocked.
            return

    def _diagnostics(self) -> str:
        if not self.stderr_lines:
            return ""
        return " MCP stderr: " + " | ".join(list(self.stderr_lines)[-8:])

    def _write(self, message: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Vision MCP stdin is unavailable.")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if self.process.stdout is None:
            raise RuntimeError("Vision MCP stdout is unavailable.")
        while True:
            ready, _, _ = select.select([self.process.stdout], [], [], self.request_timeout)
            if not ready:
                raise RuntimeError(
                    f"Vision MCP request timed out after {self.request_timeout} seconds."
                    f"{self._diagnostics()}"
                )
            line = self.process.stdout.readline()
            if not line:
                code = self.process.poll()
                raise RuntimeError(
                    f"Vision MCP stopped before responding (exit={code})."
                    f"{self._diagnostics()}"
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(
                    f"Vision MCP error: {message['error']}{self._diagnostics()}"
                )
            result = message.get("result", {})
            return result if isinstance(result, dict) else {"value": result}

    def _tool_arguments(self, image_path: Path) -> dict[str, Any]:
        schema = self.tool.get("inputSchema", {}) if isinstance(self.tool, dict) else {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        arguments: dict[str, Any] = {}
        for name, definition in properties.items():
            lowered = name.lower()
            if any(token in lowered for token in ("image", "path", "file")):
                arguments[name] = str(image_path.resolve())
            elif any(token in lowered for token in ("prompt", "query", "instruction")):
                direction = (
                    "这是日文竖排书页。每个单页必须从最右侧文字列开始，列内自上而下读取，再逐列向左；"
                    "把各列连接成正常日文段落，绝不能按左列到右列倒序输出。"
                    "如果单页分成上下两个或多个独立版块，必须先完整读完上方版块，再依次读下方版块，"
                    "不得在上下版块之间来回跳读。页眉和页码单独成行，不要插入正文句中。"
                    if self.reading_direction == "vertical"
                    else "按从左到右、从上到下的自然阅读顺序输出。"
                )
                arguments[name] = (
                    "逐字提取书页中的全部可见文字，不翻译、不总结。"
                    f"{direction}保持标题、段落、列表、表格和脚注结构，输出 Markdown；"
                    "无法辨认处标记 [无法辨认]。只输出识别文本。"
                )
            elif name in required and isinstance(definition, dict) and "default" in definition:
                arguments[name] = definition["default"]
        missing = [name for name in required if name not in arguments]
        if missing:
            raise RuntimeError(f"Unsupported vision MCP tool schema; required arguments={missing}")
        return arguments

    def extract_text(self, image_path: Path) -> tuple[str, str]:
        result = self._request(
            "tools/call",
            {
                "name": "extract_text_from_screenshot",
                "arguments": self._tool_arguments(image_path),
            },
        )
        if result.get("isError"):
            raise RuntimeError(f"Vision MCP tool failed: {result.get('content')}")
        parts: list[str] = []
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "\n".join(parts).strip()
        if not text:
            raise RuntimeError("Vision MCP returned no OCR text.")
        return clean_ocr_text(text), f"mcp-{uuid.uuid4().hex[:12]}"

    def close(self) -> None:
        if self.process.poll() is None:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows compatibility path.
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows compatibility path.
                    self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()
        if self.stderr_thread.is_alive():
            self.stderr_thread.join(timeout=1)
        # Do not close a TextIOWrapper while the drain thread owns its read
        # lock. A terminated child closes the write end, so the daemon reader
        # will observe EOF without cross-thread close().
        if (
            not self.stderr_thread.is_alive()
            and self.process.stderr is not None
            and not self.process.stderr.closed
        ):
            self.process.stderr.close()


class CodingPlanVisionOCR:
    """One persistent official vision-MCP process per OCR worker thread."""

    ocr_model = "coding-plan/glm-4.6v-vision-mcp"

    def __init__(
        self,
        *,
        api_key: str,
        command: str,
        reading_direction: str = "horizontal",
        vision_model: str = "glm-4.6v",
        request_timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.command = shlex.split(command)
        self.vision_model = vision_model.strip() or "glm-4.6v"
        self.request_timeout = max(30, int(request_timeout))
        self.prompt_version = f"{reading_direction}-v2"
        self.ocr_model = (
            f"coding-plan/{self.vision_model}-vision-mcp/{self.prompt_version}"
        )
        if reading_direction not in {"horizontal", "vertical"}:
            raise ValueError(f"Unsupported OCR reading direction: {reading_direction}")
        self.reading_direction = reading_direction
        self.local = threading.local()
        self.clients: list[McpStdioClient] = []
        self.clients_lock = threading.Lock()
        self.closed = threading.Event()
        request_delay = max(
            0.0,
            float(os.getenv("CODING_PLAN_VISION_REQUEST_DELAY", "0")),
        )
        credential_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        self.request_limiter = SharedAdaptiveRateLimiter(
            request_delay,
            identity=f"coding-plan-{credential_fingerprint}",
            min_interval=float(
                os.getenv(
                    "CODING_PLAN_VISION_MIN_REQUEST_DELAY",
                    "5" if request_delay > 0 else "0",
                )
            ),
            max_interval=float(
                os.getenv("CODING_PLAN_VISION_MAX_REQUEST_DELAY", "60")
            ),
            success_window=max(
                1,
                int(os.getenv("CODING_PLAN_VISION_SPEEDUP_WINDOW", "8")),
            ),
        )

    def _wait_for_request_slot(self) -> None:
        limiter = getattr(self, "request_limiter", None)
        if limiter is not None:
            limiter.wait()

    def _report_request_success(self) -> None:
        limiter = getattr(self, "request_limiter", None)
        if limiter is None:
            return
        interval, changed = limiter.report_success()
        if changed:
            print(f"[ocr-rate] status=speed-up interval={interval:.1f}s", flush=True)

    def _report_request_rate_limit(self) -> None:
        limiter = getattr(self, "request_limiter", None)
        if limiter is None:
            return
        interval, changed = limiter.report_rate_limit()
        if changed:
            print(f"[ocr-rate] status=throttled interval={interval:.1f}s", flush=True)

    def _is_vertical_two_page_spread(self, image_path: Path) -> bool:
        if getattr(self, "reading_direction", "horizontal") != "vertical":
            return False
        enabled = os.getenv("CODING_PLAN_SPLIT_SPREADS", "1").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return False
        with Image.open(image_path) as image:
            return image.width >= image.height * 1.25

    def _client(self) -> McpStdioClient:
        closed = getattr(self, "closed", None)
        if closed is not None and closed.is_set():
            raise RuntimeError("Vision MCP OCR backend is closed.")
        client = getattr(self.local, "client", None)
        if client is None:
            client = McpStdioClient(
                self.command,
                api_key=self.api_key,
                vision_model=self.vision_model,
                request_timeout=self.request_timeout,
                reading_direction=self.reading_direction,
            )
            self.local.client = client
            with self.clients_lock:
                self.clients.append(client)
        return client

    @staticmethod
    def _is_content_filter_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(token in message for token in ("contentfilter", '"code":"1301"', "potentially unsafe"))

    @staticmethod
    def _is_timeout_error(error: Exception) -> bool:
        message = str(error).lower()
        return "timed out" in message or "timeout" in message

    @staticmethod
    def _is_short_ocr_error(error: Exception) -> bool:
        return "below configured minimum" in str(error).lower()

    @staticmethod
    def _is_rate_limit_error(error: Exception) -> bool:
        message = str(error).lower()
        return "429" in message or "rate limit" in message or "rate-limit" in message

    @staticmethod
    def _blank_row_cuts(image: Image.Image, segments: int) -> list[int]:
        """Choose near-even horizontal cuts on the whitest available rows."""
        gray = image.convert("L")
        width, height = gray.size
        search_radius = max(12, height // (segments * 8))
        cuts: list[int] = []
        for index in range(1, segments):
            target = height * index // segments
            low = max((cuts[-1] + 20) if cuts else 20, target - search_radius)
            high = min(height - 20, target + search_radius)
            if high <= low:
                cuts.append(target)
                continue
            # Printed text leaves fully or nearly white scan rows between lines.
            # Darkness (rather than a single darkest pixel) tolerates speckles.
            best = min(
                range(low, high + 1),
                key=lambda row: sum(
                    255 - pixel
                    for pixel in gray.crop((0, row, width, row + 1)).get_flattened_data()
                ),
            )
            cuts.append(best)
        return cuts

    @staticmethod
    def _blank_column_cuts(image: Image.Image, segments: int) -> list[int]:
        """Choose near-even vertical cuts between right-to-left text columns."""
        gray = image.convert("L")
        width, height = gray.size
        search_radius = max(12, width // (segments * 8))
        cuts: list[int] = []
        for index in range(1, segments):
            target = width * index // segments
            low = max((cuts[-1] + 20) if cuts else 20, target - search_radius)
            high = min(width - 20, target + search_radius)
            if high <= low:
                cuts.append(target)
                continue
            best = min(
                range(low, high + 1),
                key=lambda column: sum(
                    255 - pixel
                    for pixel in gray.crop((column, 0, column + 1, height)).get_flattened_data()
                ),
            )
            cuts.append(best)
        return cuts

    def _ocr_segmented(
        self,
        image_path: Path,
        client: McpStdioClient,
        *,
        segments: int,
        physical_spread: bool = False,
    ) -> tuple[str, str]:
        """OCR a filtered page as ordered horizontal bands using the same MCP."""
        part_paths: list[tuple[Path, int | None]] = []
        texts: list[str] = []
        physical_page_texts: list[list[str]] = [[], []]
        try:
            with Image.open(image_path) as source:
                image = source.convert("RGB")
                width, height = image.size
                if getattr(self, "reading_direction", "horizontal") == "vertical":
                    boundaries = [0, *self._blank_column_cuts(image, segments), width]
                    physical_page_regions = [
                        (left, 0, right, height)
                        for left, right in reversed(list(zip(boundaries, boundaries[1:])))
                    ]
                    page_rows = max(
                        1,
                        int(os.getenv("CODING_PLAN_VERTICAL_PAGE_ROWS", "1")),
                    )
                    page_columns = max(
                        1,
                        int(os.getenv("CODING_PLAN_VERTICAL_PAGE_COLUMNS", "1")),
                    )
                    # With the ordinary two-way spread split, each outer
                    # region is one physical page (right page first).  Dense
                    # vertical layouts can opt into a finer grid without
                    # changing the ordering of the content-filter fallbacks,
                    # which may deliberately request 4/8/16 outer bands.
                    if segments == 2 and (page_rows > 1 or page_columns > 1):
                        region_specs: list[
                            tuple[tuple[int, int, int, int], int | None]
                        ] = []
                        for physical_page_index, (
                            page_left,
                            page_top,
                            page_right,
                            page_bottom,
                        ) in enumerate(physical_page_regions):
                            physical_page = image.crop(
                                (page_left, page_top, page_right, page_bottom)
                            )
                            page_width, page_height = physical_page.size
                            row_boundaries = [
                                0,
                                *self._blank_row_cuts(physical_page, page_rows),
                                page_height,
                            ]
                            for row_top, row_bottom in zip(
                                row_boundaries, row_boundaries[1:]
                            ):
                                row_image = physical_page.crop(
                                    (0, row_top, page_width, row_bottom)
                                )
                                column_boundaries = [
                                    0,
                                    *self._blank_column_cuts(row_image, page_columns),
                                    page_width,
                                ]
                                for column_left, column_right in reversed(
                                    list(
                                        zip(
                                            column_boundaries,
                                            column_boundaries[1:],
                                        )
                                    )
                                ):
                                    region_specs.append(
                                        (
                                            (
                                                page_left + column_left,
                                                page_top + row_top,
                                                page_left + column_right,
                                                page_top + row_bottom,
                                            ),
                                            (
                                                physical_page_index
                                                if physical_spread
                                                else None
                                            ),
                                        )
                                    )
                    else:
                        region_specs = []
                        for region in physical_page_regions:
                            left, _top, right, _bottom = region
                            physical_page_index = (
                                0 if (left + right) / 2 >= width / 2 else 1
                            )
                            region_specs.append(
                                (
                                    region,
                                    physical_page_index if physical_spread else None,
                                )
                            )
                else:
                    boundaries = [0, *self._blank_row_cuts(image, segments), height]
                    region_specs = [
                        ((0, top, width, bottom), None)
                        for top, bottom in zip(boundaries, boundaries[1:])
                    ]
                for index, (region, physical_page_index) in enumerate(
                    region_specs,
                    start=1,
                ):
                    part_path = image_path.with_name(
                        f"{image_path.stem}_segment_{index}_{uuid.uuid4().hex[:8]}.jpg"
                    )
                    image.crop(region).save(part_path, "JPEG", quality=95, optimize=True)
                    part_paths.append((part_path, physical_page_index))
            for part_index, (part_path, physical_page_index) in enumerate(
                part_paths,
                start=1,
            ):
                segment_started_at = time.monotonic()
                print(
                    f"[ocr-segment-start] page_image={image_path.stem} "
                    f"segment={part_index}/{len(part_paths)}",
                    flush=True,
                )
                # Preserve previously completed segments when only the current
                # request is rate-limited. Retrying at page level would repeat
                # every successful segment from the beginning.
                text, _ = retry_with_backoff(
                    lambda: self._ocr_filtered_band(part_path, depth=0),
                    policy=RetryPolicy(
                        attempts=max(
                            1,
                            int(os.getenv("CODING_PLAN_SEGMENT_ATTEMPTS", "4")),
                        ),
                        base_delay=2.0,
                        max_delay=12.0,
                        rate_limit_base_delay=float(
                            os.getenv("CODING_PLAN_SEGMENT_RATE_LIMIT_DELAY", "15")
                        ),
                        rate_limit_max_delay=float(
                            os.getenv("CODING_PLAN_SEGMENT_RATE_LIMIT_MAX_DELAY", "60")
                        ),
                        rate_limit_jitter=float(
                            os.getenv("CODING_PLAN_SEGMENT_RATE_LIMIT_JITTER", "3")
                        ),
                    ),
                    should_retry=self._is_rate_limit_error,
                    is_rate_limited=lambda _exc: True,
                )
                print(
                    f"[ocr-segment-response] page_image={image_path.stem} "
                    f"segment={part_index}/{len(part_paths)} "
                    f"elapsed={time.monotonic() - segment_started_at:.1f}s "
                    f"chars={len(text)}",
                    flush=True,
                )
                text = text.strip()
                if text and not self._is_empty_band_text(text):
                    texts.append(text)
                    if physical_page_index is not None:
                        physical_page_texts[physical_page_index].append(text)
        finally:
            for part_path, _physical_page_index in part_paths:
                part_path.unlink(missing_ok=True)
        if not texts:
            raise RuntimeError("Segmented vision MCP OCR returned an empty band.")
        if physical_spread:
            pages = [
                self._merge_band_texts(page_texts) if page_texts else ""
                for page_texts in physical_page_texts
            ]
            return (
                PhysicalPageOCRText(pages),
                f"mcp-segmented-{uuid.uuid4().hex[:12]}",
            )
        return self._merge_band_texts(texts), f"mcp-segmented-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _is_empty_band_text(text: str) -> bool:
        return bool(
            re.fullmatch(
                r"(?:```\s*)?[（(\[]?(?:\[无法辨认\]|(?:无|没有)可见(?:文字|内容)|空白页|blank)"
                r"(?:[，,：:].*)?[）)\]]?[。.]?(?:\s*```)?",
                text.strip(),
                flags=re.I,
            )
        )

    @staticmethod
    def _merge_band_texts(texts: list[str]) -> str:
        merged = texts[0].rstrip()
        for following in texts[1:]:
            following = following.lstrip()
            if (
                merged
                and following
                and merged[-1] not in "。！？!?；;：:…—.”’\"'）)]】》〉」』"
                and not re.search(r"\d{1,4}\s*$", merged.splitlines()[-1])
                and re.match(r"[\w\u3400-\u9fff]", following)
            ):
                separator = (
                    " "
                    if merged[-1].isascii()
                    and merged[-1].isalnum()
                    and following[0].isascii()
                    and following[0].isalnum()
                    else ""
                )
                merged += separator + following
            else:
                merged += "\n\n" + following
        return merged

    def _ocr_filtered_band(self, image_path: Path, *, depth: int) -> tuple[str, str]:
        """Recursively split a band that is filtered or too dense to finish."""
        client = self._client()
        try:
            self._wait_for_request_slot()
            result = client.extract_text(image_path)
            minimum_chars = max(
                0,
                int(os.getenv("CODING_PLAN_MIN_OCR_CHARS", "0")),
            )
            compact_length = len(re.sub(r"\s+", "", result[0]))
            # This opt-in guard is evaluated only for the complete physical
            # page/band. Recursive children may legitimately contain a short
            # heading or sparse lower block, so they must not inherit it.
            if depth == 0 and minimum_chars and compact_length < minimum_chars:
                raise RuntimeError(
                    "Vision MCP OCR text is below configured minimum: "
                    f"chars={compact_length} minimum={minimum_chars}."
                )
            self._report_request_success()
            return result
        except Exception as exc:
            if self._is_rate_limit_error(exc):
                self._report_request_rate_limit()
            is_filter = self._is_content_filter_error(exc)
            is_timeout = self._is_timeout_error(exc)
            is_short = self._is_short_ocr_error(exc)
            max_depth = 6 if is_filter else 3
            if not (is_filter or is_timeout or is_short) or depth >= max_depth:
                if is_filter or is_timeout or is_short:
                    client.close()
                    self.local.client = None
                raise
            fallback_reason = (
                "content-filter" if is_filter else "short-output" if is_short else "timeout"
            )
            print(
                f"[ocr-fallback] page_image={image_path.stem} "
                f"reason={fallback_reason} "
                f"depth={depth} next_depth={depth + 1}",
                flush=True,
            )
            # A filtered MCP process can stop answering. Recreate it before
            # sending the smaller child bands.
            client.close()
            self.local.client = None
            child_paths: list[Path] = []
            texts: list[str] = []
            try:
                with Image.open(image_path) as source:
                    image = source.convert("RGB")
                    width, height = image.size
                    is_vertical = getattr(self, "reading_direction", "horizontal") == "vertical"
                    is_vertical_grid_cell = (
                        is_vertical
                        and "_segment_" in image_path.stem
                        and int(os.getenv("CODING_PLAN_VERTICAL_PAGE_ROWS", "1")) > 1
                    )
                    # A normal portrait physical page in this magazine is laid
                    # out as an upper block followed by a lower block.  When a
                    # whole page reaches the recursive fallback, splitting it
                    # into right/left halves interleaves those blocks.  Narrow
                    # column bands created by _ocr_segmented still need the
                    # ordinary vertical right-to-left split.  Re-evaluating the
                    # geometry at every recursion also lets a horizontal child
                    # band fall back to its constituent vertical columns.
                    is_portrait_page = (
                        is_vertical
                        and not is_vertical_grid_cell
                        and width < height
                        and width * 2 >= height
                    )
                    split_horizontally = not is_vertical or is_portrait_page
                    split_dimension = height if split_horizontally else width
                    if split_dimension < 40:
                        raise RuntimeError(
                            "Vision MCP fallback persisted at the minimum safe band size."
                        ) from exc
                    if split_horizontally:
                        cut = self._blank_row_cuts(image, 2)[0]
                        regions = ((0, 0, width, cut), (0, cut, width, height))
                    else:
                        cut = self._blank_column_cuts(image, 2)[0]
                        regions = ((cut, 0, width, height), (0, 0, cut, height))
                    if cut <= 0 or cut >= split_dimension:
                        raise RuntimeError(
                            "Vision MCP fallback could not be split into nonempty bands."
                        ) from exc
                    for index, region in enumerate(regions, start=1):
                        child = image_path.with_name(
                            f"{image_path.stem}_filtered_{depth}_{index}_{uuid.uuid4().hex[:8]}.jpg"
                        )
                        image.crop(region).save(
                            child, "JPEG", quality=95, optimize=True
                        )
                        child_paths.append(child)
                for child in child_paths:
                    text, _ = self._ocr_filtered_band(child, depth=depth + 1)
                    if text.strip() and not self._is_empty_band_text(text):
                        texts.append(text.strip())
            finally:
                for child in child_paths:
                    child.unlink(missing_ok=True)
            if not texts:
                raise RuntimeError("Filtered-band vision OCR returned no text.") from exc
            return self._merge_band_texts(texts), f"mcp-filtered-{uuid.uuid4().hex[:12]}"

    def ocr_image(self, image_path: Path) -> tuple[str, str]:
        attempts = max(1, int(os.getenv("CODING_PLAN_OCR_ATTEMPTS", "2")))

        def recognize_once() -> tuple[str, str]:
            client = self._client()
            used_segmented_path = False
            try:
                if self._is_vertical_two_page_spread(image_path):
                    used_segmented_path = True
                    spread_segments = max(
                        2,
                        int(os.getenv("CODING_PLAN_SPREAD_SEGMENTS", "2")),
                    )
                    return self._ocr_segmented(
                        image_path,
                        client,
                        segments=spread_segments,
                        physical_spread=True,
                    )
                self._wait_for_request_slot()
                result = client.extract_text(image_path)
                self._report_request_success()
                return result
            except Exception as exc:  # noqa: BLE001 - MCP/network failures are retried per page.
                # Segmented requests report each 429 at the exact failing
                # segment. Avoid penalising the shared interval twice when the
                # final segment retry bubbles up to this page-level boundary.
                if self._is_rate_limit_error(exc) and not used_segmented_path:
                    self._report_request_rate_limit()
                retry_error: Exception = exc
                is_filter = self._is_content_filter_error(exc)
                is_timeout = self._is_timeout_error(exc)
                if is_filter or is_timeout:
                    # A whole scholarly page can trip input filtering because
                    # of an isolated historical phrase.  Retrying the identical
                    # image cannot help, so use the same OCR service on ordered
                    # page bands.  Eight bands are a final fallback when four
                    # still contain the triggering context.
                    # Recreate the stdio client first: some MCP server builds
                    # stop answering subsequent tool calls after error 1301.
                    # Recursive band handling may already have replaced the
                    # client captured above. Close the currently active MCP
                    # process, not a stale wrapper, before starting a retry.
                    local_state = getattr(self, "local", None)
                    active_client = (
                        getattr(local_state, "client", None)
                        if local_state is not None
                        else None
                    ) or client
                    close_client = getattr(active_client, "close", None)
                    if callable(close_client):
                        close_client()
                    if hasattr(self, "local"):
                        self.local.client = None
                    client = self._client()
                    segment_counts = (4, 8, 16, 32) if is_filter else (2,)
                    for segments in segment_counts:
                        try:
                            return self._ocr_segmented(image_path, client, segments=segments)
                        except Exception as segmented_error:  # noqa: BLE001
                            retry_error = segmented_error
                            if not self._is_content_filter_error(segmented_error):
                                break
                process = getattr(client, "process", None)
                if (
                    (process is not None and process.poll() is not None)
                    or self._is_timeout_error(exc)
                ):
                    client.close()
                    self.local.client = None
                raise retry_error

        try:
            return retry_with_backoff(
                recognize_once,
                policy=RetryPolicy(
                    attempts=attempts,
                    base_delay=2.0,
                    max_delay=12.0,
                    rate_limit_base_delay=5.0,
                    rate_limit_max_delay=45.0,
                    rate_limit_jitter=3.0,
                ),
                should_retry=lambda exc: not (
                    (getattr(self, "closed", None) is not None and self.closed.is_set())
                    or self._is_content_filter_error(exc)
                    or self._is_timeout_error(exc)
                    or self._is_rate_limit_error(exc)
                ),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Vision MCP OCR failed after {attempts} attempts: {exc}"
            ) from exc

    def close(self) -> None:
        closed = getattr(self, "closed", None)
        if closed is not None:
            closed.set()
        with self.clients_lock:
            clients = list(self.clients)
            self.clients.clear()
        for client in clients:
            client.close()


class TesseractOCR:
    """Local OCR fallback, including Japanese vertical-writing language packs."""

    def __init__(self, *, language: str, psm: int) -> None:
        if shutil.which("tesseract") is None:
            raise RuntimeError("Tesseract OCR is not installed.")
        self.language = language
        self.psm = psm
        self.ocr_model = f"tesseract/{language}/psm-{psm}"

    def ocr_image(self, image_path: Path) -> tuple[str, str]:
        command = [
            "tesseract",
            str(image_path),
            "stdout",
            "-l",
            self.language,
            "--psm",
            str(self.psm),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        if completed.returncode != 0:
            raise RuntimeError(f"Tesseract failed: {completed.stderr.strip()[:1200]}")
        text = clean_ocr_text(completed.stdout)
        if not text:
            raise RuntimeError("Tesseract returned no OCR text.")
        # Vertical Japanese models often insert a space between every glyph.
        japanese = r"\u3040-\u30ff\u3400-\u9fff"
        while re.search(fr"(?<=[{japanese}]) (?=[{japanese}])", text):
            text = re.sub(fr"(?<=[{japanese}]) (?=[{japanese}])", "", text)
        return text, "local-tesseract"

    def close(self) -> None:
        return


_NUMBERED_TRANSLATION_UNIT = re.compile(
    r"(?m)^(?:[ \t]*\d{1,3}|[ \t]?(?:\[\d{1,4}\]|\(\d{1,4}\)|\d{1,3}[)）]))"
    r"[ \t]+(?=\S)"
)
_LEADING_NUMBERED_LINE = re.compile(
    r"(?m)^(?:[ \t]*(\d{1,3})|[ \t]?(?:\[(\d{1,4})\]|\((\d{1,4})\)|(\d{1,3})[)）]))"
    r"[ \t]+(?=\S)"
)


def _leading_numbered_line_labels(text: str) -> list[str]:
    """Return normalized labels for footnotes/lists that start a source line."""

    labels = [
        next(value for value in match.groups() if value is not None)
        for match in _LEADING_NUMBERED_LINE.finditer(text)
    ]
    # Four-digit years in bibliographic prose are not footnote identifiers.
    return [label for label in labels if not 1800 <= int(label) <= 2099]


_LINE_PUNCTUATION = frozenset(
    "。．、，,.．！？!?…：:；;·\"'「」『』()（）〈〉《》〔〕"
)
_SENTENCE_TERMINAL = frozenset("。．.!?！？…")


def _kana_ratio(text: str) -> float:
    """Fraction of non-whitespace characters that are Japanese kana."""

    if not text:
        return 0.0
    meaningful = [ch for ch in text if not ch.isspace()]
    if not meaningful:
        return 0.0
    return len(KANA_CHARS.findall(text)) / len(meaningful)


KANA_CHARS = re.compile(r"[぀-ヿ]")


def _enforceable_numbered_labels(text: str) -> list[str]:
    """Labels the footnote-preservation gate must require in the translation.

    Scanned-book pages frequently carry OCR artifacts that the model is
    right to drop and that must not be mistaken for omitted footnotes:

    * ``|``-separated or ``〇``-marked lines are two-column chronology or
      table rows, not footnote definitions;
    * ``0`` can never be a footnote number (it is a misread circle marker);
    * a bare label whose remainder carries no punctuation is a chapter
      number, running head or layout fragment, not a definition;
    * a label that starts a line continuing an unfinished sentence is a
      footnote reference merged by column-aware OCR onto prose, not the
      beginning of a footnote definition.
    """

    if "|" in text or "〇" in text:
        return []
    labels: list[str] = []
    for match in _LEADING_NUMBERED_LINE.finditer(text):
        label = next(value for value in match.groups() if value is not None)
        if label == "0":
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.start())
        if line_end == -1:
            line_end = len(text)
        remainder = text[match.end():line_end]
        if not any(ch in _LINE_PUNCTUATION for ch in remainder):
            continue
        if line_start > 0:
            previous_end = line_start - 1
            previous_start = text.rfind("\n", 0, previous_end) + 1
            previous_line = text[previous_start:previous_end].strip()
            if previous_line and not previous_line.endswith(
                tuple(_SENTENCE_TERMINAL)
            ):
                continue
        if not 1800 <= int(label) <= 2099:
            labels.append(label)
    return labels


def _split_translation_body(text: str, max_chars: int) -> list[str]:
    """Split body prose so numbered footnotes/list items are separate units.

    A bare one-to-three digit label followed by whitespace is the convention
    used by the imported thesis.  Dotted numbers such as ``1853.`` and
    ``240.`` are prose/list data, not footnote boundaries.  A bracketed or
    parenthesized marker is accepted only with at most one leading space, so
    an indented wrapped bibliography line such as ``     2)`` is not mistaken
    for a new item. Continuation lines stay with their preceding unit until
    the next label; only then is an overlong unit passed through the ordinary
    size splitter.
    """

    starts = [match.start() for match in _NUMBERED_TRANSLATION_UNIT.finditer(text)]
    if not starts:
        return split_text(text, max_chars)

    boundaries = ([0] if starts[0] else []) + starts + [len(text)]
    chunks: list[str] = []
    for start, end in zip(boundaries, boundaries[1:]):
        unit = text[start:end].strip()
        if unit:
            chunks.extend(split_text(unit, max_chars))
    return chunks


class ChatTranslator:
    def __init__(self, client: TextChatBackend, *, max_chars: int = 12000) -> None:
        self.client = client
        self.max_chars = max_chars

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        # Reviewed/imported Markdown headings are structural metadata, not
        # translatable prose. Keep them entirely outside the model request so
        # a model cannot silently change an approved Chinese title or term.
        plan: list[tuple[str, str]] = []
        body_lines: list[str] = []

        def flush_body() -> None:
            body = "".join(body_lines).strip()
            body_lines.clear()
            if body:
                plan.extend(
                    ("body", chunk)
                    for chunk in _split_translation_body(body, self.max_chars)
                )

        for line in text.splitlines(keepends=True):
            without_ending = line.rstrip("\r\n")
            if re.fullmatch(r"#{1,6}[ \t]+\S.*", without_ending):
                flush_body()
                plan.append(("heading", without_ending))
            else:
                body_lines.append(line)
        flush_body()

        body_total = sum(kind == "body" for kind, _value in plan)
        body_index = 0
        enforceable_page_labels = _enforceable_numbered_labels(text)
        translated: list[str] = []
        for kind, value in plan:
            if kind == "heading":
                translated.append(value)
                continue
            body_index += 1
            chunk = value
            source_leading_numbers = _leading_numbered_line_labels(chunk)
            numbered_requirement = (
                "\n本分块检测到的行首编号（必须逐项原样保留）："
                + "、".join(source_leading_numbers)
                + "。"
                if source_leading_numbers
                else ""
            )
            output_budget = min(32768, max(4096, len(chunk) * 4))
            prompt = f"""
请把下面的 OCR 原文完整翻译成{target_language}。
源语言标签：{source_language}
分块：{body_index}/{body_total}

要求：
1. 不总结、不删减、不扩写。
2. 保留列表、表格、脚注和段落结构；Markdown 标题已由程序保护，不会出现在本分块中。
3. 源文中每一个行首脚注编号及其对应定义全文都必须逐条保留并完整翻译；严禁合并、跳号、截断、只保留编号或省略出处。{numbered_requirement}
4. 人名、书名、术语前后一致；无法确认的内容保留原文并标注 [存疑]。
5. 输入来自 OCR。先依据源语言的语法和上下文修正明显的字符、断行和空格错误；无法可靠还原时标注 [原文存疑]，不要编造。
6. 正文中出现的日语、英语及其他外语段落或引文也必须译成目标语言，不得整段保留未译；仅专名、必要术语和文献标识可按惯例保留原文。
7. 如果目标是简体中文，必须使用中国大陆通行简体字与标点，不得输出繁体字。
8. 只输出译文，不附加说明或质量报告。
9. 这是翻译任务，不是校勘任务：必须把整页内容译成{target_language}，不得只修正错字后原样输出日文原文；除人名、作品名、文献标识的必要注音外，不得保留日文假名。

原文：
{chunk}
""".strip()
            output = clean_translation_text(
                self.client.chat_text(
                    prompt,
                    system="你是严谨的书籍翻译器，优先保证完整性、准确性和结构可追溯。",
                    max_tokens=output_budget,
                )
            )
            translated.append(normalize_target_script(output, target_language))
        output_labels: Counter[str] = Counter()
        for part in translated:
            output_labels.update(_leading_numbered_line_labels(part))
        missing_numbers = list(
            (Counter(enforceable_page_labels) - output_labels).elements()
        )
        if missing_numbers:
            raise RuntimeError(
                "Translation omitted numbered footnote/list definitions with "
                f"leading labels: {missing_numbers}."
            )
        joined = "\n\n".join(part.strip() for part in translated if part.strip()).strip()
        if target_language == "简体中文":
            kana_ratio = _kana_ratio(joined)
            if kana_ratio > 0.20:
                raise RuntimeError(
                    "Translation retained Japanese text "
                    f"(kana_ratio={kana_ratio:.2f})."
                )
        return joined


class ChatOCRProofreader:
    """Correct Japanese OCR without translating or replacing the raw checkpoint."""

    def __init__(self, client: TextChatBackend, *, max_chars: int = 12000) -> None:
        self.client = client
        self.max_chars = max_chars

    def proofread(self, text: str, *, language: str) -> str:
        corrected: list[str] = []
        chunks = split_text(text, self.max_chars)
        for index, chunk in enumerate(chunks, start=1):
            output_budget = min(32768, max(4096, len(chunk) * 4))
            prompt = f"""
请校勘下面的日文 OCR 原文。语言标签：{language}
分块：{index}/{len(chunks)}

要求：
1. 只修正能够从日语语法和上下文可靠判断的 OCR 错字、漏字、重复行、错误空格、断行和明显的阅读顺序错误。
2. 严禁翻译成中文或任何其他语言；输出必须仍是原文日语。
3. 不总结、不删减、不扩写，不改写作者表达，不凭常识补造原文没有的内容。
4. 保留 Markdown 标题、列表、表格、脚注、引文和段落结构。
5. 无法可靠还原的文字保留原 OCR，并紧邻标注 [原文存疑]。
6. 只输出校勘后的原文，不附加说明、修改清单或质量报告。

OCR 原文：
{chunk}
""".strip()
            corrected.append(
                clean_ocr_text(
                    self.client.chat_text(
                        prompt,
                        system=(
                            "你是严谨的日文书籍 OCR 校勘员。你只校正日文原文，"
                            "绝不翻译、概述或创作。"
                        ),
                        max_tokens=output_budget,
                    )
                )
            )
        return "\n\n".join(corrected).strip()


# Compatibility alias for existing integrations that imported the old name.
GlmTranslator = ChatTranslator


def page_record_path(output_dir: Path, pdf_page: int) -> Path:
    return output_dir / "pages" / f"page_{pdf_page:04d}.json"


class StalePageSourceError(RuntimeError):
    """A model result was produced from OCR text that is no longer current."""


class StalePageTranslationError(RuntimeError):
    """A cached rewrite targeted a translation that is no longer current."""


_FALLBACK_LOCKS: dict[str, threading.Lock] = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _exclusive_page_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - exercised on Windows.
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    key = str(path.resolve())
    with _FALLBACK_LOCKS_GUARD:
        lock = _FALLBACK_LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


@contextlib.contextmanager
def _exclusive_stage_lock(output_dir: Path, stage: str) -> Iterator[None]:
    """Reject duplicate model-stage processes for one output directory."""

    lock_path = output_dir / ".stage_locks" / f"{stage}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.seek(0)
                holder = handle.read().strip() or "unknown"
                raise RuntimeError(
                    f"Another {stage} process already owns {lock_path} "
                    f"(holder={holder}). Wait for it or stop it before resuming."
                ) from exc
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} started={dt.datetime.now(dt.timezone.utc).isoformat()}\n")
            handle.flush()
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    # Windows and platforms without a cross-process flock still benefit from
    # the existing portable exclusive lock implementation.
    with _exclusive_page_lock(lock_path):  # pragma: no cover - POSIX uses flock above.
        yield


def _stage_process_locked(stage: str):
    def decorate(operation):
        signature = inspect.signature(operation)

        @functools.wraps(operation)
        def wrapped(*args: Any, **kwargs: Any):
            bound = signature.bind(*args, **kwargs)
            output_dir = bound.arguments["output_dir"]
            with _exclusive_stage_lock(Path(output_dir), stage):
                return operation(*args, **kwargs)

        return wrapped

    return decorate


def _page_record_from_mapping(data: dict[str, Any]) -> PageRecord:
    return PageRecord(
        **{
            key: data[key]
            for key in PageRecord.__dataclass_fields__
            if key in data
        }
    )


class PageStore:
    """Atomic page checkpoints with cross-process merge/CAS semantics."""

    def __init__(self, output_dir: Path | str) -> None:
        self.output_dir = Path(output_dir)

    def path(self, pdf_page: int) -> Path:
        return page_record_path(self.output_dir, pdf_page)

    def _lock_path(self, pdf_page: int) -> Path:
        return self.output_dir / ".page_locks" / f"page_{pdf_page:04d}.lock"

    def load(self, pdf_page: int) -> PageRecord:
        path = self.path(pdf_page)
        if not path.exists():
            raise FileNotFoundError(path)
        return _page_record_from_mapping(json.loads(path.read_text(encoding="utf-8")))

    def load_all(self) -> list[PageRecord]:
        records: list[PageRecord] = []
        for path in sorted((self.output_dir / "pages").glob("page_*.json")):
            records.append(
                _page_record_from_mapping(json.loads(path.read_text(encoding="utf-8")))
            )
        return sorted(records, key=lambda item: item.pdf_page)

    def _write_unlocked(self, record: PageRecord, *, write_markdown: bool) -> None:
        write_json(self.path(record.pdf_page), asdict(record))
        if write_markdown:
            markdown_path = (
                self.output_dir / "pages" / f"page_{record.pdf_page:04d}.md"
            )
            temporary = markdown_path.with_name(
                f".{markdown_path.name}.{os.getpid()}.{threading.get_ident()}."
                f"{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.parent.mkdir(parents=True, exist_ok=True)
                temporary.write_text(record.text.rstrip() + "\n", encoding="utf-8")
                temporary.replace(markdown_path)
            finally:
                temporary.unlink(missing_ok=True)

    def save(self, record: PageRecord) -> None:
        with _exclusive_page_lock(self._lock_path(record.pdf_page)):
            self._write_unlocked(record, write_markdown=True)

    def commit_translation(
        self,
        pdf_page: int,
        *,
        expected_text_sha256: str,
        translated_text: str,
        identity: ModelIdentity,
        translated_physical_page_texts: Iterable[str] | None = None,
    ) -> PageRecord:
        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.effective_text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} effective OCR text changed while translation was running; "
                    "the stale translation was discarded."
                )
            if (
                len(latest.effective_physical_pages) > 1
                and translated_physical_page_texts is None
            ):
                raise ValueError(
                    "A structured spread translation must preserve its physical pages."
                )
            latest.translated_text = translated_text
            latest.translation_source_sha256 = expected_text_sha256
            latest.translation_provider = identity.provider
            latest.translation_model = identity.model
            latest.translation_target_language = identity.target_language
            latest.translation_prompt_version = identity.prompt_version
            latest.translation_fingerprint = identity.fingerprint
            physical_pages = list(translated_physical_page_texts or [])
            if physical_pages and (
                join_physical_page_texts(physical_pages) != translated_text.strip()
            ):
                raise ValueError(
                    "Translated physical-page text does not reconstruct the page text."
                )
            latest.translated_physical_page_texts = physical_pages
            self._write_unlocked(latest, write_markdown=False)
            return latest

    def update_translation_text(
        self,
        pdf_page: int,
        *,
        expected_text_sha256: str,
        expected_translation_sha256: str,
        expected_translation_fingerprint: str,
        translated_text: str,
        translated_physical_page_texts: Iterable[str] | None = None,
    ) -> PageRecord:
        """Normalize a cached translation without replacing newer OCR/model output."""
        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.effective_text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} effective OCR text changed while cached "
                    "translation was normalized."
                )
            latest_translation_sha256 = hashlib.sha256(
                latest.translated_text.encode("utf-8")
            ).hexdigest()
            if (
                latest_translation_sha256 != expected_translation_sha256
                or latest.translation_fingerprint
                != expected_translation_fingerprint
            ):
                raise StalePageTranslationError(
                    f"Page {pdf_page} translation changed while its cached text "
                    "was normalized."
                )
            if (
                _validated_physical_page_texts(
                    latest.translated_text,
                    latest.translated_physical_page_texts,
                )
                is not None
                and translated_physical_page_texts is None
            ):
                raise ValueError(
                    "A structured spread translation update must preserve its physical pages."
                )
            physical_pages = list(translated_physical_page_texts or [])
            if physical_pages and (
                join_physical_page_texts(physical_pages) != translated_text.strip()
            ):
                raise ValueError(
                    "Translated physical-page text does not reconstruct the page text."
                )
            latest.translated_text = translated_text
            latest.translated_physical_page_texts = physical_pages
            self._write_unlocked(latest, write_markdown=False)
            return latest

    def commit_proofread(
        self,
        pdf_page: int,
        *,
        expected_text_sha256: str,
        proofread_text: str,
        identity: ModelIdentity,
        proofread_physical_page_texts: Iterable[str] | None = None,
    ) -> PageRecord:
        """Publish an OCR correction overlay only if its raw OCR source is current."""

        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} OCR changed while proofreading was running; "
                    "the stale proofreading result was discarded."
                )
            if (
                len(latest.raw_physical_pages) > 1
                and proofread_physical_page_texts is None
            ):
                raise ValueError(
                    "A structured spread proofreading result must preserve its physical pages."
                )
            latest.proofread_text = proofread_text
            latest.proofread_source_sha256 = expected_text_sha256
            latest.proofread_provider = identity.provider
            latest.proofread_model = identity.model
            latest.proofread_language = identity.target_language
            latest.proofread_prompt_version = identity.prompt_version
            latest.proofread_fingerprint = identity.fingerprint
            physical_pages = list(proofread_physical_page_texts or [])
            if physical_pages and (
                join_physical_page_texts(physical_pages) != proofread_text.strip()
            ):
                raise ValueError(
                    "Proofread physical-page text does not reconstruct the page text."
                )
            latest.proofread_physical_page_texts = physical_pages
            # The page Markdown intentionally remains the immutable raw OCR
            # view. Consumers select effective_text from the JSON checkpoint.
            self._write_unlocked(latest, write_markdown=False)
            return latest

    def commit_ocr_cleanup(
        self,
        pdf_page: int,
        *,
        expected_text_sha256: str,
        cleaned_text: str,
        language: str,
    ) -> PageRecord:
        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} OCR changed while deterministic cleanup was running."
                )
            original_physical_pages = _validated_physical_page_texts(
                latest.text,
                latest.physical_page_texts,
            )
            latest.text = cleaned_text
            latest.language = language
            if original_physical_pages is not None:
                cleaned_physical_pages = [
                    clean_ocr_text(page_text)
                    for page_text in original_physical_pages
                ]
                if join_physical_page_texts(cleaned_physical_pages) == cleaned_text:
                    latest.physical_page_texts = cleaned_physical_pages
                else:
                    latest.physical_page_texts = []
            else:
                latest.physical_page_texts = []
            self._write_unlocked(latest, write_markdown=True)
            return latest


def load_page_records(output_dir: Path) -> list[PageRecord]:
    return PageStore(output_dir).load_all()


def save_page_record(output_dir: Path, record: PageRecord) -> None:
    PageStore(output_dir).save(record)


def normalize_cached_page_records(
    output_dir: Path,
    records: list[PageRecord],
) -> list[PageRecord]:
    """Apply the current deterministic OCR cleaner to legacy checkpoints."""
    changed = 0
    store = PageStore(output_dir)
    for record in records:
        if (
            record.ocr_model == "manual/visually-confirmed-blank"
            and not record.text.strip()
        ):
            cleaned = "[空白页]"
        else:
            cleaned = clean_ocr_text(record.text)
        if cleaned == record.text:
            continue
        try:
            committed = store.commit_ocr_cleanup(
                record.pdf_page,
                expected_text_sha256=record.text_sha256,
                cleaned_text=cleaned,
                language=detect_language(cleaned),
            )
        except (FileNotFoundError, StalePageSourceError):
            continue
        for field_name in PageRecord.__dataclass_fields__:
            setattr(record, field_name, getattr(committed, field_name))
        changed += 1
    if changed:
        print(f"[ocr] normalized_cached_pages={changed}")
    return records


def import_existing_ocr(source_dir: Path, output_dir: Path) -> int:
    candidates: list[dict[str, Any]] = []
    extracted = source_dir / "extracted_pages.json"
    if extracted.exists():
        value = json.loads(extracted.read_text(encoding="utf-8"))
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    checkpoint_dir = source_dir / "_checkpoints"
    if not candidates and checkpoint_dir.exists():
        for path in sorted(checkpoint_dir.glob("page_*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
    page_dir = source_dir / "pages"
    if not candidates and page_dir.exists():
        for path in sorted(page_dir.glob("page_*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
    count = 0
    for item in candidates:
        page_number = item.get("pdf_page", item.get("page_number"))
        if not isinstance(page_number, int):
            continue
        text = str(item.get("text") or item.get("ocr_text") or item.get("embedded_text") or "").strip()
        if not text:
            continue
        translated = str(item.get("translated_text") or item.get("translation") or "").strip()
        record = PageRecord(
            pdf_page=page_number,
            text=text,
            language=str(item.get("language") or detect_language(text)),
            translated_text=translated,
            translation_source_sha256=str(item.get("translation_source_sha256") or ""),
            translation_provider=str(item.get("translation_provider") or ""),
            translation_model=str(item.get("translation_model") or ""),
            translation_target_language=str(item.get("translation_target_language") or ""),
            translation_prompt_version=str(item.get("translation_prompt_version") or ""),
            translation_fingerprint=str(item.get("translation_fingerprint") or ""),
            proofread_text=str(item.get("proofread_text") or ""),
            proofread_source_sha256=str(item.get("proofread_source_sha256") or ""),
            proofread_provider=str(item.get("proofread_provider") or ""),
            proofread_model=str(item.get("proofread_model") or ""),
            proofread_language=str(item.get("proofread_language") or ""),
            proofread_prompt_version=str(item.get("proofread_prompt_version") or ""),
            proofread_fingerprint=str(item.get("proofread_fingerprint") or ""),
            physical_page_texts=[
                str(value)
                for value in item.get("physical_page_texts", [])
            ]
            if isinstance(item.get("physical_page_texts"), list)
            else [],
            proofread_physical_page_texts=[
                str(value)
                for value in item.get("proofread_physical_page_texts", [])
            ]
            if isinstance(item.get("proofread_physical_page_texts"), list)
            else [],
            translated_physical_page_texts=[
                str(value)
                for value in item.get("translated_physical_page_texts", [])
            ]
            if isinstance(item.get("translated_physical_page_texts"), list)
            else [],
            notes=str(item.get("notes") or ""),
            ocr_model=str(item.get("ocr_model") or "imported"),
        )
        save_page_record(output_dir, record)
        count += 1
    return count


def render_pdf_page(pdf_path: Path, pdf_page: int, image_path: Path, *, dpi: int, max_side: int, quality: int) -> None:
    with fitz.open(pdf_path) as document:
        page = document.load_page(pdf_page - 1)
        desired_scale = dpi / 72.0
        max_dimension = max(float(page.rect.width), float(page.rect.height))
        scale = min(desired_scale, max_side / max_dimension)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, colorspace=fitz.csRGB)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    image.save(image_path, "JPEG", quality=quality, optimize=True)


@_stage_process_locked("ocr")
def ocr_pdf(
    pdf_path: Path,
    output_dir: Path,
    client: OCRBackend,
    *,
    start_page: int,
    end_page: int,
    concurrency: int,
    dpi: int,
    max_image_side: int,
    jpeg_quality: int,
    keep_page_images: bool,
    force: bool,
    cache_model_prefix: str | None = None,
    cache_model_exact: str | None = None,
    request_delay: float = 0.0,
) -> list[PageRecord]:
    existing = {record.pdf_page: record for record in load_page_records(output_dir)}
    cache_model_prefixes = parse_model_prefixes(cache_model_prefix)
    pages = list(range(start_page, end_page + 1))
    pending = [
        page
        for page in pages
        if force
        or page not in existing
        or not existing[page].text.strip()
        or (
            cache_model_exact
            and existing[page].ocr_model != cache_model_exact
        )
        or (
            cache_model_prefixes
            and not existing[page].ocr_model.startswith(cache_model_prefixes)
        )
    ]
    print(f"[ocr] total={len(pages)} cached={len(pages) - len(pending)} pending={len(pending)}")
    # Separate concurrent CLI processes (whole-book OCR plus a targeted
    # fallback, or several books sharing one output root) must never delete
    # one another's in-flight rendered pages.
    image_dir = (
        output_dir / "_page_images"
        if keep_page_images
        else output_dir / f"_page_images_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    )
    rate_limiter = StartRateLimiter(max(0.0, request_delay))

    def process(pdf_page: int) -> PageRecord:
        started_at = time.monotonic()
        print(f"[ocr-start] page={pdf_page}", flush=True)
        image_path = image_dir / f"page_{pdf_page:04d}.jpg"
        render_pdf_page(
            pdf_path,
            pdf_page,
            image_path,
            dpi=dpi,
            max_side=max_image_side,
            quality=jpeg_quality,
        )
        try:
            rate_limiter.wait()
            text, request_id = client.ocr_image(image_path)
            physical_page_texts = list(
                getattr(text, "physical_page_texts", ())
            )
            text = str(text).strip()
            if physical_page_texts and (
                join_physical_page_texts(physical_page_texts) != text
            ):
                raise RuntimeError(
                    f"OCR physical-page structure is inconsistent on page {pdf_page}."
                )
            elapsed = time.monotonic() - started_at
            print(
                f"[ocr-response] page={pdf_page} elapsed={elapsed:.1f}s chars={len(text)}",
                flush=True,
            )
            record = PageRecord(
                pdf_page=pdf_page,
                text=text,
                language=detect_language(text),
                notes=(
                    f"request_id={request_id}; elapsed_seconds={elapsed:.1f}"
                    if request_id
                    else f"elapsed_seconds={elapsed:.1f}"
                ),
                ocr_model=client.ocr_model,
                physical_page_texts=physical_page_texts,
            )
            save_page_record(output_dir, record)
            return record
        finally:
            if not keep_page_images:
                image_path.unlink(missing_ok=True)

    if pending:
        failures: list[tuple[int, str]] = []
        executor = ThreadPoolExecutor(max_workers=max(1, concurrency))
        interrupted = False
        futures: dict[Any, int] = {}
        try:
            futures = {executor.submit(process, page): page for page in pending}
            completed = 0
            for future in as_completed(futures):
                page = futures[future]
                try:
                    existing[page] = future.result()
                except Exception as exc:
                    failures.append((page, str(exc)))
                    print(f"[ocr-error] page={page}: {exc}", file=sys.stderr)
                    continue
                completed += 1
                print(f"[ocr] page={page} completed={completed}/{len(pending)}")
        except KeyboardInterrupt:
            interrupted = True
            # A normal ThreadPoolExecutor context waits for every queued job,
            # which made a user-requested pause start pages that had not begun.
            # Cancel queued work immediately; only already-running model calls
            # are allowed to unwind.
            for future in futures:
                future.cancel()
            close_backend = getattr(client, "close", None)
            if callable(close_backend):
                close_backend()
            # Closing the backend terminates in-flight MCP process groups, so
            # running workers unwind promptly. Wait for them here to guarantee
            # that a paused CLI cannot survive as an orphan and duplicate the
            # next resume run.
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            if not interrupted:
                executor.shutdown(wait=True)
        if not keep_page_images and image_dir.exists() and not any(image_dir.iterdir()):
            image_dir.rmdir()
        if failures:
            failed_pages = [page for page, _ in failures]
            raise RuntimeError(
                f"OCR completed with {len(failures)} failed page(s): {failed_pages}. "
                "Rerun the same command to resume only missing pages."
            )
    if not keep_page_images and image_dir.exists() and not any(image_dir.iterdir()):
        image_dir.rmdir()
    return [existing[page] for page in pages if page in existing]


@_stage_process_locked("translate")
def translate_non_chinese_pages(
    records: list[PageRecord],
    output_dir: Path,
    translator: Translator,
    *,
    target_language: str,
    force: bool,
    concurrency: int = 3,
    request_delay: float = 0.0,
    source_language: str | None = None,
    translation_provider: str = "unspecified",
    translation_model: str = "unspecified",
    translation_identity: ModelIdentity | None = None,
) -> None:
    identity = translation_identity or ModelIdentity(
        provider=translation_provider,
        adapter="legacy",
        base_url="",
        model=translation_model,
        target_language=target_language,
        prompt_version=TRANSLATION_PROMPT_VERSION,
    )
    store = PageStore(output_dir)
    normalized_cached = 0
    if target_language == "简体中文":
        for record in records:
            if not record.translated_text:
                continue
            cached_physical_pages = _validated_physical_page_texts(
                record.translated_text,
                record.translated_physical_page_texts,
            )
            if cached_physical_pages is not None:
                normalized_physical_pages = [
                    normalize_target_script(
                        clean_translation_text(page_text),
                        target_language,
                    )
                    for page_text in cached_physical_pages
                ]
                normalized = join_physical_page_texts(normalized_physical_pages)
            else:
                normalized_physical_pages = []
                normalized = normalize_target_script(
                    clean_translation_text(record.translated_text),
                    target_language,
                )
            if normalized != record.translated_text:
                try:
                    committed = store.update_translation_text(
                        record.pdf_page,
                        expected_text_sha256=record.effective_text_sha256,
                        expected_translation_sha256=hashlib.sha256(
                            record.translated_text.encode("utf-8")
                        ).hexdigest(),
                        expected_translation_fingerprint=record.translation_fingerprint,
                        translated_text=normalized,
                        translated_physical_page_texts=normalized_physical_pages,
                    )
                except (
                    FileNotFoundError,
                    StalePageSourceError,
                    StalePageTranslationError,
                ):
                    continue
                record.translated_text = committed.translated_text
                normalized_cached += 1
    if normalized_cached:
        print(f"[translate] normalized_cached_to_simplified={normalized_cached}")
    candidates = [
        record
        for record in records
        if record.effective_text.strip()
        and record.effective_text.strip() not in NON_CONTENT_MARKERS
        and (source_language is not None or record.language not in {"zh", "unknown"})
        and (
            force
            or not record.translation_is_fresh_for(identity)
        )
    ]
    print(
        f"[translate] total={len(candidates)} concurrency={max(1, concurrency)} "
        f"request_delay={max(0.0, request_delay):g}s"
    )
    rate_limiter = StartRateLimiter(max(0.0, request_delay))

    def process(record: PageRecord) -> int:
        if not store.path(record.pdf_page).exists():
            store.save(record)
        print(f"[translate] page={record.pdf_page} language={record.language} started")
        source_sha256 = record.effective_text_sha256
        source_physical_pages = record.effective_physical_pages
        translated_physical_pages: list[str] = []
        for physical_page_index, source_text in enumerate(
            source_physical_pages,
            start=1,
        ):
            if not source_text.strip():
                translated_physical_pages.append("")
                continue
            rate_limiter.wait()
            if len(source_physical_pages) > 1:
                print(
                    f"[translate] page={record.pdf_page} "
                    f"physical_page={physical_page_index}/{len(source_physical_pages)} started"
                )
            translated_physical_pages.append(
                translator.translate(
                    source_text,
                    source_language=source_language or record.language,
                    target_language=target_language,
                )
            )
        translated_text = join_physical_page_texts(translated_physical_pages)
        committed = store.commit_translation(
            record.pdf_page,
            expected_text_sha256=source_sha256,
            translated_text=translated_text,
            identity=identity,
            translated_physical_page_texts=(
                translated_physical_pages
                if len(source_physical_pages) > 1
                else None
            ),
        )
        for field_name in PageRecord.__dataclass_fields__:
            setattr(record, field_name, getattr(committed, field_name))
        return record.pdf_page

    failures: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {executor.submit(process, record): record.pdf_page for record in candidates}
        completed = 0
        for future in as_completed(futures):
            page = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - API failures are resumable per page.
                failures.append((page, str(exc)))
                print(f"[translate-error] page={page}: {exc}", file=sys.stderr)
                continue
            completed += 1
            print(f"[translate] page={page} completed={completed}/{len(candidates)}")
    if failures:
        failed_pages = [page for page, _ in failures]
        raise RuntimeError(
            f"Translation completed with {len(failures)} failed page(s): {failed_pages}. "
            "Rerun the same command to resume only missing translations."
        )


@_stage_process_locked("proofread")
def proofread_ocr_pages(
    records: list[PageRecord],
    output_dir: Path,
    proofreader: Proofreader,
    *,
    language: str,
    identity: ModelIdentity,
    force: bool,
    concurrency: int = 3,
    request_delay: float = 0.0,
) -> None:
    """Create model-attributed OCR correction overlays with page-level CAS."""

    store = PageStore(output_dir)
    candidates = [
        record
        for record in records
        if record.text.strip()
        and record.text.strip() not in NON_CONTENT_MARKERS
        and (
            record.language in {language, "unknown", "other"}
            or (
                language.lower() == "ja"
                and re.search(r"[\u3040-\u30ff]", record.text) is not None
            )
        )
        and (force or not record.proofread_is_fresh_for(identity))
    ]
    print(
        f"[proofread] total={len(candidates)} concurrency={max(1, concurrency)} "
        f"request_delay={max(0.0, request_delay):g}s language={language}"
    )
    rate_limiter = StartRateLimiter(max(0.0, request_delay))

    def process(record: PageRecord) -> int:
        if not store.path(record.pdf_page).exists():
            store.save(record)
        print(f"[proofread] page={record.pdf_page} started")
        source_sha256 = record.text_sha256
        source_physical_pages = record.raw_physical_pages
        proofread_physical_pages: list[str] = []
        for physical_page_index, source_text in enumerate(
            source_physical_pages,
            start=1,
        ):
            if not source_text.strip():
                proofread_physical_pages.append("")
                continue
            rate_limiter.wait()
            if len(source_physical_pages) > 1:
                print(
                    f"[proofread] page={record.pdf_page} "
                    f"physical_page={physical_page_index}/{len(source_physical_pages)} started"
                )
            proofread_physical_pages.append(
                proofreader.proofread(source_text, language=language)
            )
        proofread_text = join_physical_page_texts(proofread_physical_pages)
        if not proofread_text.strip():
            raise RuntimeError("Proofreading model returned no text.")
        committed = store.commit_proofread(
            record.pdf_page,
            expected_text_sha256=source_sha256,
            proofread_text=proofread_text,
            identity=identity,
            proofread_physical_page_texts=(
                proofread_physical_pages
                if len(source_physical_pages) > 1
                else None
            ),
        )
        for field_name in PageRecord.__dataclass_fields__:
            setattr(record, field_name, getattr(committed, field_name))
        return record.pdf_page

    failures: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = {executor.submit(process, record): record.pdf_page for record in candidates}
        completed = 0
        for future in as_completed(futures):
            page = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - per-page work is resumable.
                failures.append((page, str(exc)))
                print(f"[proofread-error] page={page}: {exc}", file=sys.stderr)
                continue
            completed += 1
            print(f"[proofread] page={page} completed={completed}/{len(candidates)}")
    if failures:
        failed_pages = [page for page, _ in failures]
        raise RuntimeError(
            f"Proofreading completed with {len(failures)} failed page(s): "
            f"{failed_pages}. Rerun the same command to resume only missing overlays."
        )


def build_toc_prompt(records: list[PageRecord], *, fixed_toc_pages: list[int] | None) -> str:
    page_text = "\n\n".join(
        f"===== PDF_PAGE {record.pdf_page} =====\n{record.compile_text}" for record in records
    )
    fixed_instruction = (
        f"用户已确认目录位于 PDF 页 {fixed_toc_pages}，只解析这些页。"
        if fixed_toc_pages
        else "请先判断这些前置页中哪些连续页面属于目录页，再解析目录。"
    )
    return f"""
你将收到一本影印书前部若干页的逐页 OCR 文本。
{fixed_instruction}

请输出严格 JSON，格式如下：
{{
  "toc_pdf_pages": [6, 7, 8],
  "entries": [
    {{
      "index": "第一章",
      "title": "标题",
      "level": 1,
      "kind": "chapter",
      "printed_page": 1
    }}
  ]
}}

规则：
1. toc_pdf_pages 使用 PDF 文件从 1 开始的页码，不是书内印刷页码。
2. printed_page 是目录印出的书内页码，转成阿拉伯整数；未印页码时为 null。
3. level 从 1 开始，保持部、章、节、小节的层级。
4. kind 只能是 part、chapter、section、subsection、frontmatter、other。
5. index 只放“第一章”“1.2”等序号；title 不要重复序号。
6. 不要臆造 OCR 中不存在的条目，不要计算 PDF 页码偏移。

OCR 文本：
{page_text}
""".strip()


def normalize_toc_payload(payload: dict[str, Any], *, fallback_pages: list[int] | None = None) -> dict[str, Any]:
    raw_pages = payload.get("toc_pdf_pages", fallback_pages or [])
    toc_pages = sorted({int(page) for page in raw_pages if str(page).isdigit() and int(page) >= 1})
    entries: list[TocEntry] = []
    for position, raw in enumerate(payload.get("entries", []), start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        printed = raw.get("printed_page")
        if isinstance(printed, str):
            match = re.search(r"\d+", printed)
            printed = int(match.group()) if match else None
        if not isinstance(printed, int) or printed < 1:
            printed = None
        kind = str(raw.get("kind") or "other").lower()
        if kind not in TOC_KINDS:
            kind = "other"
        try:
            level = max(1, int(raw.get("level", 1)))
        except (TypeError, ValueError):
            level = 1
        entries.append(
            TocEntry(
                id=str(raw.get("id") or f"toc-{position:04d}"),
                index=str(raw.get("index") or "").strip(),
                title=title,
                level=level,
                kind=kind,
                printed_page=printed,
                pdf_page=int(raw["pdf_page"]) if isinstance(raw.get("pdf_page"), int) else None,
                end_pdf_page=int(raw["end_pdf_page"]) if isinstance(raw.get("end_pdf_page"), int) else None,
            )
        )
    if not entries:
        raise ValueError("No valid TOC entries were found.")
    entry_ids = [entry.id for entry in entries]
    duplicate_ids = sorted(
        entry_id for entry_id, count in Counter(entry_ids).items() if count > 1
    )
    if duplicate_ids:
        raise ValueError(f"TOC entry IDs must be unique; duplicates: {duplicate_ids}")
    mapped_pages = [entry.pdf_page for entry in entries if entry.pdf_page is not None]
    if mapped_pages != sorted(mapped_pages):
        raise ValueError("TOC PDF page mappings must be monotonic.")
    raw_divisor = payload.get("printed_pages_per_pdf_page")
    try:
        printed_pages_per_pdf_page = (
            int(raw_divisor) if raw_divisor is not None else None
        )
    except (TypeError, ValueError):
        printed_pages_per_pdf_page = None
    if (
        printed_pages_per_pdf_page is not None
        and printed_pages_per_pdf_page < 1
    ):
        raise ValueError("printed_pages_per_pdf_page must be a positive integer.")
    return {
        "schema_version": 1,
        "toc_pdf_pages": toc_pages,
        "page_offset": payload.get("page_offset"),
        "printed_pages_per_pdf_page": printed_pages_per_pdf_page,
        "offset_evidence": payload.get("offset_evidence", []),
        "entries": [asdict(entry) for entry in entries],
    }


def extract_toc(
    records: list[PageRecord],
    client: GlmClient,
    *,
    front_matter_pages: int,
    toc_pages: list[int] | None,
) -> dict[str, Any]:
    if toc_pages:
        selected = [record for record in records if record.pdf_page in set(toc_pages)]
        missing = sorted(set(toc_pages) - {record.pdf_page for record in selected})
        if missing:
            raise ValueError(f"Missing OCR text for TOC PDF pages: {missing}")
    else:
        selected = [record for record in records if record.pdf_page <= front_matter_pages]
    if not selected:
        raise ValueError("No front-matter OCR pages are available for TOC detection.")
    prompt = build_toc_prompt(selected, fixed_toc_pages=toc_pages)
    payload = client.chat_json(
        prompt,
        system="你是严谨的书籍目录结构化助手，只依据给定 OCR 文本返回 JSON。",
        max_tokens=32768,
    )
    return normalize_toc_payload(payload, fallback_pages=toc_pages)


def normalize_match_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    # Common Japanese TOC labels are often written in kana while the opening
    # page uses kanji.  Normalising these pairs lets an unnumbered preface be
    # mapped from its heading without inventing a printed page number.
    for kana, kanji in (
        ("まえがき", "前書き"),
        ("あとがき", "後書き"),
        ("はじめに", "初めに"),
        ("おわりに", "終わりに"),
    ):
        value = value.replace(kana, kanji)
    # Critical-edition margin numbers can precede a chapter heading on its
    # opening page (for example ``37 第一章 ...``).  They are pagination aids,
    # not part of the title used for duplicate-heading detection.
    value = re.sub(
        r"^[#\s]*\d{1,4}\s+(?=第?[一二三四五六七八九十百零〇0-9ivxlcdm]+[章节部篇卷])",
        "",
        value,
    )
    value = re.sub(r"^[#\s]*(?:第?[一二三四五六七八九十百零〇0-9ivxlcdm]+[章节部篇卷.、：:\-]*)", "", value)
    return re.sub(r"[^\w\u3400-\u9fff\u3040-\u30ff]+", "", value)


def title_page_score(title: str, page_text: str) -> float:
    target = normalize_match_text(title)
    if len(target) < 2:
        return 0.0
    head = page_text[:2400]
    normalized_head = normalize_match_text(head)
    normalized_lines = [
        normalize_match_text(line) for line in head.splitlines() if line.strip()
    ][:30]
    # Short labels such as “结语” must occur as a heading line; accepting an
    # arbitrary body mention would map them to an earlier, unrelated page.
    if len(target) <= 8 and target in normalized_lines:
        return 1.0
    if len(target) > 8 and target in normalized_head:
        return 1.0
    best = 0.0
    for candidate in normalized_lines:
        if not candidate:
            continue
        if target in candidate or candidate in target:
            ratio = min(len(target), len(candidate)) / max(len(target), len(candidate))
            best = max(best, 0.85 + 0.15 * ratio)
        else:
            best = max(best, SequenceMatcher(None, target, candidate).ratio())
    return best


def find_title_evidence(entries: list[TocEntry], records: list[PageRecord], toc_end: int) -> list[dict[str, Any]]:
    searchable = [record for record in records if record.pdf_page > toc_end and record.compile_text]
    evidence: list[dict[str, Any]] = []
    for entry in entries:
        if entry.kind not in {
            "chapter",
            "section",
            "subsection",
            "frontmatter",
            "other",
        }:
            continue
        best_page: int | None = None
        best_score = 0.0
        for record in searchable:
            score = title_page_score(entry.display_title, record.compile_text)
            if score > best_score:
                best_page, best_score = record.pdf_page, score
            if score >= 1.0:
                break
        if best_page is not None and best_score >= 0.82:
            evidence.append(
                {
                    "entry_id": entry.id,
                    "title": entry.display_title,
                    "printed_page": entry.printed_page,
                    "pdf_page": best_page,
                    "offset": (
                        best_page - entry.printed_page
                        if entry.printed_page is not None
                        else None
                    ),
                    "score": round(best_score, 3),
                }
            )
    return evidence


def infer_page_offset(entries: list[TocEntry], records: list[PageRecord], toc_end: int) -> tuple[int, list[dict[str, Any]]]:
    offset, _divisor, evidence = infer_page_mapping(
        entries,
        records,
        toc_end,
        divisor_candidates=(1,),
    )
    return offset, evidence


def _dominant_page_offset(offsets: list[int]) -> int:
    if not offsets:
        raise ValueError("Cannot infer page offset without title evidence.")
    counts = Counter(offsets)
    best_count = max(counts.values())
    if best_count >= 2:
        candidates = [offset for offset, count in counts.items() if count == best_count]
        return min(candidates, key=lambda item: (abs(item), item))
    return int(round(statistics.median(offsets)))


def _mapping_evidence(
    evidence: list[dict[str, Any]],
    divisor: int,
) -> tuple[int, list[dict[str, Any]], tuple[int, int, int]]:
    annotated: list[dict[str, Any]] = []
    offsets: list[int] = []
    for item in evidence:
        if item.get("printed_page") is None:
            annotated.append(
                {
                    **item,
                    "offset": None,
                    "printed_pages_per_pdf_page": divisor,
                }
            )
            continue
        printed_page = int(item["printed_page"])
        pdf_page = int(item["pdf_page"])
        offset = pdf_page - printed_page // divisor
        offsets.append(offset)
        annotated.append(
            {
                **item,
                "offset": offset,
                "printed_pages_per_pdf_page": divisor,
            }
        )
    offset = _dominant_page_offset(offsets)
    exact_support = sum(value == offset for value in offsets)
    dispersion = sum(abs(value - offset) for value in offsets)
    # Prefer the simplest one-page mapping when evidence is otherwise tied.
    rank = (exact_support, -dispersion, -divisor)
    return offset, annotated, rank


def infer_page_mapping(
    entries: list[TocEntry],
    records: list[PageRecord],
    toc_end: int,
    *,
    divisor_candidates: tuple[int, ...] = (1, 2),
) -> tuple[int, int, list[dict[str, Any]]]:
    evidence = find_title_evidence(entries, records, toc_end)
    if not any(item.get("printed_page") is not None for item in evidence):
        raise ValueError("Cannot infer page offset from OCR text. Pass --page-offset after checking one chapter page.")
    valid_candidates = tuple(
        sorted({int(value) for value in divisor_candidates if int(value) >= 1})
    )
    if not valid_candidates:
        raise ValueError("At least one positive printed-page divisor is required.")
    ranked = [
        (*_mapping_evidence(evidence, divisor), divisor)
        for divisor in valid_candidates
    ]
    offset, annotated, _rank, divisor = max(ranked, key=lambda item: item[2])
    return offset, divisor, annotated


def apply_page_mapping(
    toc_payload: dict[str, Any],
    records: list[PageRecord],
    *,
    page_offset: int | None,
    source_page_count: int | None = None,
    printed_pages_per_pdf_page: int | None = None,
) -> dict[str, Any]:
    normalized = normalize_toc_payload(toc_payload)
    entries = [TocEntry(**item) for item in normalized["entries"]]
    toc_end = max(normalized.get("toc_pdf_pages") or [0])
    evidence = find_title_evidence(entries, records, toc_end)
    manual_override = page_offset is not None
    stored_divisor = normalized.get("printed_pages_per_pdf_page")
    divisor = printed_pages_per_pdf_page or (
        int(stored_divisor) if isinstance(stored_divisor, int) else None
    )
    if divisor is not None and divisor < 1:
        raise ValueError("printed_pages_per_pdf_page must be a positive integer.")
    if page_offset is None:
        existing_offset = normalized.get("page_offset")
        if isinstance(existing_offset, int):
            page_offset = existing_offset
            divisor = divisor or 1
        else:
            page_offset, divisor, evidence = infer_page_mapping(
                entries,
                records,
                toc_end,
                divisor_candidates=(divisor,) if divisor is not None else (1, 2),
            )
    else:
        divisor = divisor or 1
        if any(item.get("printed_page") is not None for item in evidence):
            _unused, evidence, _rank = _mapping_evidence(evidence, divisor)
    assert divisor is not None
    direct_pages = {
        item["entry_id"]: int(item["pdf_page"])
        for item in evidence
        if item.get("score", 0) >= 0.95
    }
    max_page = source_page_count or max((record.pdf_page for record in records), default=0)
    for entry in entries:
        if entry.id in direct_pages and (not manual_override or entry.printed_page is None):
            entry.pdf_page = direct_pages[entry.id]
        elif entry.printed_page is not None:
            mapped = entry.printed_page // divisor + page_offset
            entry.pdf_page = mapped if 1 <= mapped <= max_page else None

    # A leading preface commonly has no printed page in the TOC.  When its
    # heading was too damaged to match, place it in the only available front-
    # matter slot(s) between the TOC and the first numbered entry.  This keeps
    # the fallback deterministic and never shifts numbered chapter mappings.
    first_numbered_index = next(
        (
            index
            for index, entry in enumerate(entries)
            if entry.printed_page is not None and entry.pdf_page is not None
        ),
        None,
    )
    if first_numbered_index is not None:
        first_numbered_page = int(entries[first_numbered_index].pdf_page or 0)
        fallback_page = toc_end + 1
        for entry in entries[:first_numbered_index]:
            if (
                entry.pdf_page is None
                and entry.printed_page is None
                and entry.kind in {"frontmatter", "other"}
                and fallback_page < first_numbered_page
            ):
                entry.pdf_page = fallback_page
                fallback_page += 1
    normalized["page_offset"] = page_offset
    normalized["printed_pages_per_pdf_page"] = divisor
    normalized["offset_evidence"] = evidence
    normalized["entries"] = [asdict(entry) for entry in entries]
    return normalized


def select_entries(entries: list[TocEntry], granularity: str) -> list[TocEntry]:
    with_pages = [entry for entry in entries if entry.pdf_page is not None]
    if granularity == "all":
        return with_pages
    if granularity == "chapter":
        # A book-level compilation must not silently discard unnumbered top-level
        # material such as prefaces, the author's note, or a conclusion.  Keep
        # those alongside numbered chapters, while still excluding container
        # parts and nested section entries.
        selected = [
            entry
            for entry in with_pages
            if entry.kind in {"frontmatter", "chapter"}
            or (entry.kind == "other" and entry.level == 1)
        ]
        if selected:
            return selected
        non_parts = [entry for entry in with_pages if entry.kind not in {"part", "frontmatter"}]
        if not non_parts:
            return with_pages
        level = min(entry.level for entry in non_parts)
        return [entry for entry in non_parts if entry.level == level]
    wanted_kind = "subsection" if granularity == "subsection" else "section"
    selected = [entry for entry in with_pages if entry.kind == wanted_kind]
    if selected:
        return selected
    if granularity == "section":
        selected = [entry for entry in with_pages if entry.kind == "subsection"]
        if selected:
            return selected
    leaves: list[TocEntry] = []
    for index, entry in enumerate(with_pages):
        next_entry = with_pages[index + 1] if index + 1 < len(with_pages) else None
        if next_entry is None or next_entry.level <= entry.level:
            leaves.append(entry)
    return leaves or with_pages


def resolve_compile_granularity(
    output_dir: Path,
    toc_payload: dict[str, Any],
    requested: str | None,
) -> str:
    """Preserve an existing chapter selection unless explicitly overridden."""

    if requested:
        return requested
    manifest_path = output_dir / "chapters.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest = None
        if isinstance(manifest, list) and manifest:
            tagged = {
                str(item.get("granularity") or "")
                for item in manifest
                if isinstance(item, dict)
            }
            if len(tagged) == 1:
                saved = next(iter(tagged))
                if saved in {"chapter", "section", "subsection", "all"}:
                    return saved
            existing_ids = [
                str(item.get("id") or "")
                for item in manifest
                if isinstance(item, dict)
            ]
            try:
                entries = [
                    TocEntry(**item)
                    for item in toc_payload.get("entries", [])
                    if isinstance(item, dict)
                ]
            except TypeError:
                entries = []
            for candidate in ("chapter", "section", "subsection", "all"):
                if [entry.id for entry in select_entries(entries, candidate)] == existing_ids:
                    return candidate
    return "chapter"


def _title_heading_span(text: str, title: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    normalized_title = normalize_match_text(title)
    loose_title = re.sub(r"(?:的|之|の)", "", normalized_title)

    def matches(candidate: str) -> bool:
        if not candidate:
            return False
        if candidate == normalized_title:
            return True
        loose_candidate = re.sub(r"(?:的|之|の)", "", candidate)
        if loose_candidate and loose_candidate == loose_title:
            return True
        # OCR/translation can move a leading descriptor such as “批判” to the
        # end of a title. An identical multiset of title characters is a safe
        # match within the opening heading search window.
        if len(normalized_title) >= 5 and Counter(candidate) == Counter(normalized_title):
            return True
        if len(normalized_title) >= 4 and (
            normalized_title in candidate or candidate in normalized_title
        ):
            coverage = min(len(normalized_title), len(candidate)) / max(
                len(normalized_title), len(candidate)
            )
            if coverage >= 0.8:
                return True
        return (
            len(normalized_title) >= 5
            and SequenceMatcher(None, loose_title, loose_candidate).ratio() >= 0.88
        )

    # Search each possible heading start instead of assuming that the first
    # OCR line is the chapter title. A running book header can precede it.
    # A shared two-page scan can contain the previous chapter's entire final
    # printed page before the next heading, so inspect more than ten lines.
    for start in range(min(40, len(lines))):
        if not lines[start].strip():
            continue
        combined = ""
        for index in range(start, min(start + 4, len(lines), 40)):
            if not lines[index].strip():
                continue
            combined += normalize_match_text(lines[index])
            if matches(combined):
                return start, index
    return None


def remove_duplicate_title(text: str, title: str) -> str:
    lines = text.splitlines()
    span = _title_heading_span(text, title)
    if span is not None:
        _first_title_line, last_title_line = span
        del lines[: last_title_line + 1]
        while lines and not lines[0].strip():
            del lines[0]
    return "\n".join(lines).strip()


def trim_before_next_title(text: str, title: str) -> str:
    """Keep only the previous printed page from a shared chapter boundary."""

    lines = text.splitlines()
    span = _title_heading_span(text, title)
    if span is None:
        return text.strip()
    first_title_line, _last_title_line = span
    cut = first_title_line
    # Drop the next printed page number and blank separators immediately
    # preceding the detected chapter heading. Some scans omit that number but
    # retain a short running header; remove at most two compact publication
    # headers. Once an
    # explicit page number was seen, never consume a short line behind it,
    # because that line belongs to the previous printed page.
    removed_page_number = bool(
        re.fullmatch(r"(?:```)?\s*\d{1,4}", lines[first_title_line].strip())
    )
    removed_running_headers = 0
    while cut > 0:
        previous = lines[cut - 1].strip()
        if not previous:
            cut -= 1
            continue
        if re.fullmatch(r"(?:```)?\s*\d{1,4}", previous):
            removed_page_number = True
            cut -= 1
            continue
        normalized = normalize_match_text(previous)
        review_header = bool(
            re.search(
                r"(?:cross\s*review|関連近作レビュー|相关近作(?:评论|短评|评述)|"
                r"近作(?:评论|短评|评述))",
                previous,
                flags=re.I,
            )
        )
        if (
            not removed_page_number
            and removed_running_headers < 2
            and normalized
            and (len(normalized) <= 10 or review_header)
        ):
            removed_running_headers += 1
            cut -= 1
            continue
        break
    return "\n".join(lines[:cut]).strip()


def annotate_printed_page_markers(text: str, printed_pages: Iterable[int]) -> str:
    """Turn known book-page furniture into audit-only page markers.

    OCR frequently retains a small ornament next to a printed page number and
    may attach both to the first/last body fragment (``●30出血`` or
    ``© 330肃。``).  The caller supplies the exact page numbers possible on
    this physical PDF page, so these decorated edge forms can be removed
    without treating years, citations, index coordinates, or numbered lists as
    publication metadata.
    """

    expected = {int(page) for page in printed_pages if int(page) >= 0}
    if not expected:
        return text
    output: list[str] = []
    ornaments = r"●©◎○◉◯⊙•·◆◇"

    def marker(printed_page: int) -> str:
        return (
            f'<span epub:type="pagebreak" id="printed-page-{printed_page}" '
            f'title="{printed_page}"></span>'
        )

    for line in text.splitlines():
        match = re.fullmatch(r"[-—–\s]*(\d{1,3})[-—–\s]*", line.strip())
        if match and int(match.group(1)) in expected:
            output.append(marker(int(match.group(1))))
            continue
        decorated = re.fullmatch(
            rf"[{ornaments}\s]*(\d{{1,3}})[{ornaments}\s]*",
            line.strip(),
        )
        if decorated and int(decorated.group(1)) in expected:
            output.append(marker(int(decorated.group(1))))
            continue
        prefixed = re.match(
            rf"^\s*[{ornaments}]\s*(\d{{1,3}})\s*(.+)$",
            line,
        )
        if prefixed and int(prefixed.group(1)) in expected:
            output.extend(
                [marker(int(prefixed.group(1))), prefixed.group(2).lstrip()]
            )
            continue
        suffixed = re.match(
            rf"^\s*(\d{{1,3}})\s*[{ornaments}]\s*(.+)$",
            line,
        )
        if suffixed and int(suffixed.group(1)) in expected:
            output.extend(
                [marker(int(suffixed.group(1))), suffixed.group(2).lstrip()]
            )
            continue
        output.append(line)
    return "\n".join(output)


def _load_reviewed_chapter_override(
    output_dir: Path,
    entry: TocEntry,
) -> str | None:
    """Load and validate an optional human-reviewed chapter replacement."""

    override_path = output_dir / "reviewed_chapters" / f"{entry.id}.md"
    if not override_path.is_file():
        return None
    markdown = override_path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    if not markdown:
        raise ValueError(f"Reviewed chapter override is empty: {override_path}")
    first_line = markdown.splitlines()[0].strip()
    heading = re.fullmatch(r"#\s+(.+?)\s*", first_line)
    if heading is None or heading.group(1).strip() != entry.display_title:
        raise ValueError(
            "Reviewed chapter override H1 must match the TOC title "
            f"{entry.display_title!r}: {override_path}"
        )
    if not "\n".join(markdown.splitlines()[1:]).strip():
        raise ValueError(f"Reviewed chapter override body is empty: {override_path}")
    return markdown.rstrip() + "\n"


def _reviewed_chapter_body(markdown: str) -> str:
    """Return reviewed chapter content without its validated document H1."""

    lines = markdown.splitlines()
    return "\n".join(lines[1:]).strip()


def build_knowledge_rows_from_manifest(
    pdf_path: Path,
    chapter_dir: Path,
    manifest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build stable reader-facing knowledge chunks from chapter Markdown.

    Keeping this derivation separate from chapter assembly lets the graph
    expose the knowledge-base publisher as an independently replaceable node.
    The legacy compiler calls the same helper, so both execution engines keep
    byte-for-byte compatible row IDs and content.
    """

    rows: list[dict[str, Any]] = []
    for item in manifest:
        markdown = (chapter_dir / str(item["filename"])).read_text(
            encoding="utf-8"
        )
        reader_content = _reviewed_chapter_body(markdown)
        row_source = "reviewed" if item.get("reviewed_override") else "compiled"
        for chunk_index, chunk in enumerate(
            split_text(reader_content, 4000),
            start=1,
        ):
            row_id = hashlib.sha1(
                (
                    f"{pdf_path.name}:{item['id']}:{row_source}:"
                    f"{chunk_index}"
                ).encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "id": row_id,
                    "title": str(item["display_title"]),
                    "chapter_id": str(item["id"]),
                    "chapter_order": int(item["sequence"]),
                    "content": chunk,
                }
            )
    return rows


def compile_chapters(
    pdf_path: Path,
    output_dir: Path,
    records: list[PageRecord],
    toc_payload: dict[str, Any],
    *,
    granularity: str,
    publication_title: str | None = None,
    require_translation: bool = False,
    expected_translation_identity: ModelIdentity | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries = [TocEntry(**item) for item in toc_payload["entries"]]
    selected = select_entries(entries, granularity)
    if not selected:
        raise ValueError(f"No TOC entries are available for granularity={granularity}.")
    entry_positions = {entry.id: index for index, entry in enumerate(entries)}
    selected.sort(key=lambda entry: entry_positions.get(entry.id, 10**9))
    last_pdf_page = max((record.pdf_page for record in records), default=0)
    record_map = {record.pdf_page: record for record in records}
    printed_pages_per_pdf_page = int(
        toc_payload.get("printed_pages_per_pdf_page") or 1
    )
    page_offset = int(toc_payload.get("page_offset") or 0)
    chapter_ranges: dict[
        str,
        tuple[int, int, int | None, bool, TocEntry | None],
    ] = {}
    reviewed_overrides: dict[str, str] = {}

    # Validate every selected range before touching a previous successful
    # chapter build. A late missing page or stale translation must not leave a
    # half-replaced chapters directory.
    for sequence, entry in enumerate(selected, start=1):
        reviewed_override = _load_reviewed_chapter_override(output_dir, entry)
        if reviewed_override is not None:
            reviewed_override = strip_reviewed_publication_metadata(
                reviewed_override
            )
            if not _reviewed_chapter_body(reviewed_override).strip():
                override_path = (
                    output_dir / "reviewed_chapters" / f"{entry.id}.md"
                )
                raise ValueError(
                    "Reviewed chapter override body is empty after publication "
                    f"metadata removal: {override_path}"
                )
            reviewed_overrides[entry.id] = reviewed_override
        start = int(entry.pdf_page or 0)
        next_start: int | None = None
        next_level: int | None = None
        next_entry: TocEntry | None = None
        if granularity == "all":
            if sequence < len(selected) and selected[sequence].pdf_page:
                next_entry = selected[sequence]
                next_start = int(next_entry.pdf_page)
                next_level = next_entry.level
        else:
            position = entry_positions.get(entry.id, -1)
            for candidate in entries[position + 1 :]:
                if candidate.pdf_page is None or candidate.level > entry.level:
                    continue
                if int(candidate.pdf_page) >= start:
                    next_entry = candidate
                    next_start = int(candidate.pdf_page)
                    next_level = candidate.level
                    break
        overlaps_next = bool(
            next_entry is not None
            and next_level == entry.level
            and (
                granularity in {"section", "subsection"}
                or (
                    printed_pages_per_pdf_page > 1
                    and next_entry.printed_page is not None
                    and int(next_entry.printed_page)
                    % printed_pages_per_pdf_page
                    != 0
                )
            )
        )
        if next_start is None:
            end = last_pdf_page
        elif overlaps_next:
            end = max(start, next_start)
        else:
            end = max(start, next_start - 1)
        entry.end_pdf_page = end
        missing = [page for page in range(start, end + 1) if page not in record_map]
        if missing:
            preview = missing[:12]
            suffix = "..." if len(missing) > len(preview) else ""
            raise ValueError(f"Missing OCR pages for {entry.display_title}: {preview}{suffix}")
        if require_translation and reviewed_override is None:
            stale_or_missing_translation = [
                page
                for page in range(start, end + 1)
                if record_map[page].effective_text.strip()
                and record_map[page].effective_text.strip() not in NON_CONTENT_MARKERS
                and page_record_needs_translation(record_map[page])
                and not (
                    record_map[page].translation_is_fresh_for(
                        expected_translation_identity
                    )
                    if expected_translation_identity is not None
                    else record_map[page].translation_is_fresh
                )
            ]
            if stale_or_missing_translation:
                preview = stale_or_missing_translation[:12]
                suffix = "..." if len(stale_or_missing_translation) > len(preview) else ""
                raise ValueError(
                    f"Missing or stale translation for {entry.display_title}: "
                    f"{preview}{suffix}. Run the translation phase again."
                )
        chapter_ranges[entry.id] = (
            start,
            end,
            next_start,
            overlaps_next,
            next_entry,
        )

    chapter_dir = output_dir / "chapters"
    if chapter_dir.exists():
        for old_path in chapter_dir.glob("*.md"):
            old_path.unlink()
    chapter_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    semantic_chapters: list[dict[str, Any]] = []

    for sequence, entry in enumerate(selected, start=1):
        start, end, next_start, overlaps_next, next_entry = chapter_ranges[entry.id]
        filename = f"{sequence:03d}_{slugify(entry.display_title)}.md"
        reviewed_override = reviewed_overrides.get(entry.id)
        chapter_semantic_pages: list[dict[str, Any]] = []
        chapter_footnotes: list[Any] = []
        chapter_semantic_issues: list[dict[str, Any]] = []
        if reviewed_override is not None:
            markdown = reviewed_override
            reviewed_inventory = parse_markdown_footnotes(markdown)
            for code, values in (
                ("semantic_markdown_duplicate_definitions", reviewed_inventory.duplicate_definitions),
                ("semantic_markdown_missing_definitions", reviewed_inventory.missing_definitions),
                ("semantic_markdown_unused_definitions", reviewed_inventory.unused_definitions),
                ("semantic_markdown_duplicate_references", reviewed_inventory.duplicate_references),
            ):
                if values:
                    chapter_semantic_issues.append(
                        {
                            "code": code,
                            "message": "人工审定章的 Markdown 脚注未形成一对一闭环。",
                            "source_page": f"reviewed:{entry.id}",
                            "note_label": None,
                            "blocking": True,
                            "evidence": {"values": list(values)},
                        }
                    )
        else:
            parts = [
                f"# {entry.display_title}",
                "",
                f"<!-- source-pdf: {pdf_path.name} -->",
                f"<!-- pdf-pages: {start}-{end} -->",
                "",
            ]
            for page in range(start, end + 1):
                record = record_map[page]
                physical_pages = record.compile_physical_pages_for(
                    expected_translation_identity
                )
                has_exact_physical_pages = (
                    printed_pages_per_pdf_page > 1
                    and len(physical_pages) == printed_pages_per_pdf_page
                )
                physical_start = 0
                physical_end = len(physical_pages)
                if (
                    has_exact_physical_pages
                    and page == start
                    and entry.printed_page is not None
                ):
                    physical_start = (
                        int(entry.printed_page) % printed_pages_per_pdf_page
                    )
                if (
                    has_exact_physical_pages
                    and next_entry is not None
                    and next_start == page
                    and next_entry.printed_page is not None
                ):
                    physical_end = (
                        int(next_entry.printed_page)
                        % printed_pages_per_pdf_page
                    )
                selected_physical_pages = list(
                    physical_pages[physical_start:physical_end]
                )
                if (
                    not has_exact_physical_pages
                    and page == end
                    and overlaps_next
                    and next_start == page
                    and next_entry is not None
                ):
                    selected_physical_pages = [
                        trim_before_next_title(
                            join_physical_page_texts(selected_physical_pages),
                            next_entry.title,
                        )
                    ]
                semantic_page_bodies: list[str] = []
                for physical_index, physical_text in enumerate(
                    selected_physical_pages,
                    start=physical_start + 1,
                ):
                    source_page = (
                        f"pdf-{page:04d}-physical-{physical_index:02d}"
                    )
                    semantic_page = reconstruct_page_footnotes(
                        physical_text,
                        source_page=source_page,
                    )
                    semantic_page_bodies.append(semantic_page.body)
                    chapter_footnotes.extend(semantic_page.footnotes)
                    page_audit = {
                        "source_page": source_page,
                        **semantic_page.to_audit_dict(),
                    }
                    chapter_semantic_pages.append(page_audit)
                    chapter_semantic_issues.extend(page_audit["issues"])
                content = join_physical_page_texts(semantic_page_bodies)
                if page == start:
                    content = remove_duplicate_title(content, entry.title)
                first_printed_page = (page - page_offset) * printed_pages_per_pdf_page
                expected_printed_pages = range(
                    first_printed_page + physical_start,
                    first_printed_page + (
                        physical_end
                        if has_exact_physical_pages
                        else printed_pages_per_pdf_page
                    ),
                )
                marked_content = annotate_printed_page_markers(
                    content,
                    expected_printed_pages,
                )
                parts.extend(
                    [
                        f'<span epub:type="pagebreak" id="pdf-page-{page}" title="{page}"></span>',
                        f"<!-- PDF_PAGE: {page} -->",
                        "",
                        marked_content,
                        "",
                    ]
                )
            # Chapter Markdown is a reader-facing output just like EPUB,
            # Word, and the knowledge base. Keep PDF/page coordinates only in
            # checkpoints and chapters.json; never expose them in book text.
            semantic_markdown = append_markdown_footnotes(
                "\n".join(parts).rstrip() + "\n",
                chapter_footnotes,
            )
            markdown = strip_publication_metadata(
                semantic_markdown,
                publication_title=publication_title,
                chapter_title=entry.display_title,
            )
        (chapter_dir / filename).write_text(markdown, encoding="utf-8")
        manifest.append(
            {
                **asdict(entry),
                "sequence": sequence,
                "display_title": entry.display_title,
                "filename": filename,
                "pdf_page": start,
                "end_pdf_page": end,
                "boundary_mode": "closed-overlap" if overlaps_next else "non-overlap",
                "reviewed_override": reviewed_override is not None,
                "granularity": granularity,
                "semantic_footnote_count": (
                    len(chapter_footnotes)
                    if reviewed_override is None
                    else len(parse_markdown_footnotes(markdown).definitions)
                ),
                "semantic_issue_count": len(chapter_semantic_issues),
            }
        )
        semantic_chapters.append(
            {
                "chapter_id": entry.id,
                "filename": filename,
                "reviewed_override": reviewed_override is not None,
                "footnote_count": (
                    len(chapter_footnotes)
                    if reviewed_override is None
                    else len(parse_markdown_footnotes(markdown).definitions)
                ),
                "pages": chapter_semantic_pages,
                "issues": chapter_semantic_issues,
                "release_blocked": any(
                    bool(issue.get("blocking", True))
                    for issue in chapter_semantic_issues
                ),
                "markdown_sha256": hashlib.sha256(
                    markdown.encode("utf-8")
                ).hexdigest(),
                "footnote_contract_sha256": markdown_footnote_contract_sha256(
                    markdown
                ),
            }
        )
    write_json(output_dir / "chapters.json", manifest)
    semantic_summary = semantic_audit_summary(semantic_chapters)
    write_json(
        output_dir / "audit" / "semantic-reconstruction.json",
        {
            "schema_version": 1,
            "status": "blocked" if semantic_summary["release_blocked"] else "passed",
            "summary": semantic_summary,
            "chapters": semantic_chapters,
        },
    )
    # Derive RAG chunks from the final reader-facing Markdown, not from
    # individual source pages. This makes every chapter exactly
    # reconstructable and prevents page-boundary cleanup from diverging
    # between Markdown and the knowledge base.
    knowledge_rows = build_knowledge_rows_from_manifest(
        pdf_path,
        chapter_dir,
        manifest,
    )
    return manifest, knowledge_rows


def write_knowledge_base(output_path: Path, rows: list[dict[str, Any]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def strip_publication_metadata(
    markdown_text: str,
    *,
    publication_title: str | None = None,
    chapter_title: str | None = None,
) -> str:
    """Remove audit-only source/page markers from reader-facing documents."""
    output: list[str] = []
    normalized_chapter_title = normalize_match_text(chapter_title or "")
    is_contents_chapter = normalized_chapter_title in {
        "目录",
        "目次",
        "contents",
        "tableofcontents",
    }
    normalized_titles: list[str] = []
    for title in (publication_title, chapter_title):
        if not title:
            continue
        variants = [title, *re.split(r"[：:·|｜]", title)]
        for variant in variants:
            normalized = normalize_match_text(variant)
            if len(normalized) >= 2 and normalized not in normalized_titles:
                normalized_titles.append(normalized)

    def is_running_title(line: str) -> bool:
        stripped = line.strip()
        if not normalized_titles or not stripped:
            return False
        if stripped.startswith("#"):
            # Keep the generated chapter H1 at the start, but still recognize
            # OCR that spuriously formats a later running header as Markdown.
            if not output:
                return False
            stripped = re.sub(r"^#{1,6}\s*", "", stripped)
        without_page = re.sub(r"^\s*\d{1,4}\s*", "", stripped)
        without_page = re.sub(r"\s*\d{1,4}\s*$", "", without_page)
        candidate = normalize_match_text(without_page)
        if not candidate:
            return False
        for normalized_title in normalized_titles:
            if candidate == normalized_title or (
                len(candidate) >= 2
                and candidate in normalized_title
                and len(without_page) <= len(normalized_title) + 4
            ):
                return True
            # OCR often changes one or two title glyphs (for example 普遍→普通)
            # and appends the author after a divider.  Keep the threshold strict
            # enough that ordinary body sentences cannot be mistaken for headers.
            title_prefix = re.split(r"[|｜]", without_page, maxsplit=1)[0]
            if "〉" in title_prefix:
                title_prefix = title_prefix.split("〉", maxsplit=1)[0] + "〉"
            comparable = normalize_match_text(title_prefix)
            if (
                len(comparable) <= len(normalized_title) + 12
                and SequenceMatcher(None, normalized_title, comparable).ratio() >= 0.68
            ):
                return True
        return False

    def discard_trailing_printed_page() -> None:
        # Column-aware OCR can split a printed page number across two lines
        # (for example ``4`` / ``0`` for page 40), so every consecutive
        # trailing standalone digit line is a page footer, not body text.
        while output and not output[-1].strip():
            output.pop()
        while output and re.fullmatch(
            r"[-—–\s]*(\d{1,3})[-—–\s]*",
            output[-1].strip(),
        ):
            output.pop()
        while output and not output[-1].strip():
            output.pop()

    def discard_vertical_running_titles(lines: list[str]) -> list[str]:
        """Remove a running title OCR emitted as one glyph per line.

        Sideways margin titles in otherwise horizontal books are sometimes
        returned as ``中`` / ``产`` / ... rather than one title line.  Match
        only a contiguous run of single CJK/kana glyphs against a configured
        publication/chapter title so ordinary prose containing the same words
        remains untouched.
        """
        cleaned: list[str] = []
        index = 0
        while index < len(lines):
            if not re.fullmatch(r"[\u3400-\u9fff\u3040-\u30ff]", lines[index].strip()):
                cleaned.append(lines[index])
                index += 1
                continue
            end = index
            glyphs: list[str] = []
            while end < len(lines) and re.fullmatch(
                r"[\u3400-\u9fff\u3040-\u30ff]",
                lines[end].strip(),
            ):
                glyphs.append(lines[end].strip())
                end += 1
            candidate = normalize_match_text("".join(glyphs))
            matched = any(
                candidate == normalized_title
                or (
                    len(normalized_title) >= 4
                    and normalized_title in candidate
                    and len(candidate) <= len(normalized_title) + 2
                )
                for normalized_title in normalized_titles
            )
            if not matched:
                cleaned.extend(lines[index:end])
            index = end
        return cleaned

    def should_join_page_boundary(previous: str, following: str) -> bool:
        """Join a sentence or word split only because the scanned page changed."""
        previous = previous.rstrip()
        following = following.lstrip()
        if not previous or not following:
            return False
        if previous.lstrip().startswith(("#", "- ", "* ", "> ", "|")):
            return False
        if following.startswith(("#", "- ", "* ", "> ", "|")):
            return False
        if re.fullmatch(r"\d{4}", previous.strip()):
            return False
        if re.fullmatch(r"\d{4}", following.strip()):
            return False
        if re.match(r"^(?:第[一二三四五六七八九十百零〇0-9]+[章节部篇卷]|\d+[.、．]\s*)", following):
            return False
        # A signed translator/editor note is a complete footer even when it
        # lacks sentence punctuation.  Joining it to the next page's body
        # creates corrupt text such as ``——译者处于……``.
        if re.search(r"(?:译者|编者|作者|校者|原注|校注)\s*$", previous):
            return False
        if previous[-1] in "。！？!?；;：:…—.”’\"'）)]】》〉」』":
            return False
        return bool(
            re.match(r"[\w\u3400-\u9fff]", following)
            and re.search(r"[\w\u3400-\u9fff]$", previous)
        )

    pending_page_boundary = False
    skipped_leading_page_number = False
    # Remove a vertically emitted running title before page-boundary joining.
    # Otherwise its first glyph can be mistaken for the continuation of the
    # previous page (for example ``凯`` + ``中``), leaving a corrupt fragment
    # even if the remaining title glyphs are removed later.
    source_lines = discard_vertical_running_titles(markdown_text.splitlines())
    for line in source_lines:
        stripped = line.strip()
        if stripped.startswith('<span epub:type="pagebreak"'):
            is_known_printed_marker = 'id="printed-page-' in stripped
            if not is_known_printed_marker:
                discard_trailing_printed_page()
            pending_page_boundary = True
            # The known printed-page number was replaced by this marker, so a
            # following standalone number can be real content and must remain.
            skipped_leading_page_number = is_known_printed_marker
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue
        # A body copy of the scanned contents page can contain page numbers
        # throughout the page rather than only at the physical-page boundary.
        # Keep standalone numbers in normal chapters as real content, but omit
        # them from reader-facing contents chapters where they are navigation
        # coordinates tied to the source edition.
        if is_contents_chapter and re.fullmatch(
            r"[-—–\s]*\d{1,4}[-—–\s]*",
            stripped,
        ):
            continue
        if is_running_title(line):
            continue
        if output and re.match(r"^#\s+", line):
            # The generated chapter title is the only reader-facing H1.
            # OCR/model-created headings inside its body remain navigable but
            # are demoted so Word/EPUB structure stays a proper hierarchy.
            line = "#" + line
            stripped = line.strip()
        if output and re.fullmatch(r"=+\s*", line):
            line = re.sub(r"=+", "---", line, count=1)
            stripped = line.strip()
        if output and re.search(r"<\s*/?\s*h1\b", line, flags=re.I):
            line = re.sub(r"<(\s*/?\s*)h1\b", r"<\1h2", line, flags=re.I)
            stripped = line.strip()
        if pending_page_boundary:
            if not stripped:
                continue
            # A standalone number immediately following the page marker is
            # the printed page header. Do not discard the same-looking line
            # elsewhere: it may be a real numbered item or data value.
            numeric_header = re.fullmatch(r"[-—–\s]*(\d{1,3})[-—–\s]*", stripped)
            if (
                numeric_header
                and not skipped_leading_page_number
            ):
                skipped_leading_page_number = True
                continue
            while output and not output[-1].strip():
                output.pop()
            if output and should_join_page_boundary(output[-1], line):
                output[-1] = output[-1].rstrip() + line.lstrip()
            else:
                if output:
                    output.append("")
                output.append(line.rstrip())
            pending_page_boundary = False
            continue
        output.append(line.rstrip())
    discard_trailing_printed_page()
    output = discard_vertical_running_titles(output)

    compact: list[str] = []
    for line in output:
        if not line.strip() and compact and not compact[-1].strip():
            continue
        compact.append(line)
    return "\n".join(compact).strip() + "\n"


def strip_reviewed_publication_metadata(markdown_text: str) -> str:
    """Remove only explicit source/page markers from a reviewed override.

    Human-reviewed Markdown has already had running headers and source page
    numbers resolved by an editor.  It must not pass through the fuzzy
    running-title heuristics used for raw OCR, because a short title fragment
    (for example a name separated by ``·``) can also occur throughout the
    legitimate body text.
    """

    def pagebreak_numbers(line: str) -> set[int] | None:
        anchor = re.fullmatch(
            r"<span\b(?P<attrs>[^>]*)>\s*</span>",
            line,
            flags=re.I,
        ) or re.fullmatch(
            r"<span\b(?P<attrs>[^>]*)/\s*>",
            line,
            flags=re.I,
        )
        if anchor is None:
            return None
        attributes = anchor.group("attrs")
        if not re.search(
            r"\bepub:type\s*=\s*(['\"])pagebreak\1",
            attributes,
            flags=re.I,
        ):
            return None
        numbers = {
            int(value)
            for value in re.findall(
                r"\b(?:pdf|printed)[-_]page[-_](\d{1,6})\b",
                attributes,
                flags=re.I,
            )
        }
        title = re.search(
            r"\btitle\s*=\s*(['\"])(\d{1,6})\1",
            attributes,
            flags=re.I,
        )
        if title is not None:
            numbers.add(int(title.group(2)))
        return numbers

    output: list[str] = []
    removed_marker = False
    pending_page_numbers: set[int] | None = None
    for line in markdown_text.splitlines():
        stripped = line.strip()
        anchor_numbers = pagebreak_numbers(stripped)
        if anchor_numbers is not None:
            removed_marker = True
            pending_page_numbers = anchor_numbers
            continue
        metadata = re.fullmatch(
            r"<!--\s*(?P<key>source[-_ ]pdf|pdf[-_ ]pages|pdf[-_ ]page)"
            r"\s*:\s*(?P<value>.*?)\s*-->",
            stripped,
            flags=re.I,
        )
        if metadata is not None:
            removed_marker = True
            key = re.sub(r"[- ]", "_", metadata.group("key").lower())
            if key == "pdf_page":
                page_number = re.fullmatch(
                    r"[-—–\s]*(\d{1,6})[-—–\s]*",
                    metadata.group("value"),
                )
                if page_number is not None:
                    if pending_page_numbers is None:
                        pending_page_numbers = set()
                    pending_page_numbers.add(int(page_number.group(1)))
            continue
        if pending_page_numbers is not None:
            if not stripped:
                output.append(line)
                continue
            adjacent_number = re.fullmatch(
                r"[-—–\s]*(\d{1,6})[-—–\s]*",
                stripped,
            )
            if (
                adjacent_number is not None
                and int(adjacent_number.group(1)) in pending_page_numbers
            ):
                removed_marker = True
                pending_page_numbers = None
                continue
            pending_page_numbers = None
        output.append(line)

    if not removed_marker:
        return markdown_text
    cleaned = "\n".join(output)
    if markdown_text.endswith(("\n", "\r")):
        cleaned += "\n"
    return cleaned


def markdown_to_html(markdown_text: str) -> str:
    try:
        import markdown  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("EPUB compilation requires Markdown>=3.6; install requirements.txt.") from exc
    return markdown.markdown(
        markdown_text,
        extensions=["extra", "sane_lists", "footnotes"],
        output_format="xhtml",
    )


def _join_wrapped_lines(lines: list[str]) -> str:
    joined = ""
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if (
            joined
            and joined[-1].isascii()
            and joined[-1].isalnum()
            and value[0].isascii()
            and value[0].isalnum()
        ):
            joined += " "
        joined += value
    return joined


def _markdown_blocks(markdown_text: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in markdown_text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(_join_wrapped_lines(current))
            current = []
    if current:
        blocks.append(_join_wrapped_lines(current))
    return blocks


def markdown_inline_to_plain_text(value: str) -> str:
    """Convert the small inline-Markdown/HTML subset used by page translations."""
    cleaned = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", value)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"[`*_]{1,3}", "", cleaned).strip()


def _html_local_name(element: ET.Element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _html_element_text(
    element: ET.Element,
    *,
    skip_tags: frozenset[str] = frozenset(),
) -> str:
    """Extract visible XHTML text while retaining authored hard line breaks."""

    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        if node.text:
            parts.append(node.text)
        for child in node:
            tag = _html_local_name(child)
            if tag == "br":
                parts.append("\n")
            elif tag not in skip_tags:
                visit(child)
            if child.tail:
                parts.append(child.tail)

    visit(element)
    return "".join(parts).strip()


def _docx_style_name(
    document: Any,
    preferred: str,
    fallback: str | None = None,
) -> str | None:
    try:
        document.styles[preferred]
    except KeyError:
        return fallback
    return preferred


def _append_markdown_to_docx(
    document: Any,
    markdown_text: str,
    *,
    body_style: str | None = None,
) -> None:
    """Render reader-facing Markdown without flattening its structure."""

    fragment = markdown_to_html(markdown_text)
    root = ET.fromstring(f"<document>{fragment}</document>")

    def append_inline(
        paragraph: Any,
        element: ET.Element,
        *,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        skip_tags: frozenset[str] = frozenset(),
    ) -> None:
        """Append the supported inline XHTML subset without losing run styles."""

        def add_run(text: str | None, *, run_bold: bool, run_italic: bool, run_underline: bool) -> None:
            if not text:
                return
            run = paragraph.add_run(text)
            if run_bold:
                run.bold = True
            if run_italic:
                run.italic = True
            if run_underline:
                run.underline = True

        add_run(
            element.text,
            run_bold=bold,
            run_italic=italic,
            run_underline=underline,
        )
        for child in element:
            tag = _html_local_name(child)
            if tag in skip_tags:
                pass
            elif tag == "br":
                add_run(
                    "\n",
                    run_bold=bold,
                    run_italic=italic,
                    run_underline=underline,
                )
            else:
                append_inline(
                    paragraph,
                    child,
                    bold=bold or tag in {"b", "strong", "th"},
                    italic=italic or tag in {"em", "i"},
                    underline=underline or tag in {"u", "ins"},
                    skip_tags=skip_tags,
                )
            add_run(
                child.tail,
                run_bold=bold,
                run_italic=italic,
                run_underline=underline,
            )

    def add_paragraph(
        element: ET.Element,
        *,
        style: str | None = None,
        bold: bool = False,
        skip_tags: frozenset[str] = frozenset(),
    ) -> Any | None:
        if not _html_element_text(element, skip_tags=skip_tags):
            return None
        paragraph = document.add_paragraph(style=style)
        append_inline(
            paragraph,
            element,
            bold=bold,
            skip_tags=skip_tags,
        )
        return paragraph

    def render_list(element: ET.Element, level: int = 0) -> None:
        ordered = _html_local_name(element) == "ol"
        base_style = "List Number" if ordered else "List Bullet"
        preferred_style = (
            base_style
            if level == 0
            else f"{base_style} {min(level + 1, 3)}"
        )
        style = _docx_style_name(
            document,
            preferred_style,
            _docx_style_name(document, base_style),
        )
        for item in element:
            if _html_local_name(item) != "li":
                continue
            add_paragraph(
                item,
                style=style,
                skip_tags=frozenset({"ol", "ul"}),
            )
            for nested in item:
                if _html_local_name(nested) in {"ol", "ul"}:
                    render_list(nested, level + 1)

    def render_table(element: ET.Element) -> None:
        rows = [node for node in element.iter() if _html_local_name(node) == "tr"]
        cells_by_row = [
            [
                cell
                for cell in row
                if _html_local_name(cell) in {"td", "th"}
            ]
            for row in rows
        ]
        column_count = max((len(cells) for cells in cells_by_row), default=0)
        if not rows or not column_count:
            return
        table = document.add_table(rows=len(rows), cols=column_count)
        table_style = _docx_style_name(document, "Table Grid")
        if table_style is not None:
            table.style = table_style
        for row_index, cells in enumerate(cells_by_row):
            for column_index, source_cell in enumerate(cells):
                target_cell = table.cell(row_index, column_index)
                target_cell.text = ""
                paragraph = target_cell.paragraphs[0]
                append_inline(
                    paragraph,
                    source_cell,
                    bold=_html_local_name(source_cell) == "th",
                )

    def render(element: ET.Element, *, quote: bool = False) -> None:
        tag = _html_local_name(element)
        if re.fullmatch(r"h[1-6]", tag):
            level = min(3, int(tag[1]))
            heading = document.add_heading("", level=level)
            append_inline(heading, element)
            return
        if tag == "p":
            style = (
                _docx_style_name(document, "Quote")
                if quote
                else body_style
            )
            add_paragraph(element, style=style)
            return
        if tag == "blockquote":
            for child in element:
                render(child, quote=True)
            return
        if tag in {"ol", "ul"}:
            render_list(element)
            return
        if tag == "table":
            render_table(element)
            return
        if tag == "pre":
            add_paragraph(element, style=_docx_style_name(document, "No Spacing"))
            return
        if tag == "hr":
            return
        if tag in {"div", "section", "article", "document"}:
            for child in element:
                render(child, quote=quote)
            return
        add_paragraph(
            element,
            style=_docx_style_name(document, "Quote") if quote else None,
        )

    render(root)


def _docx_manifest_body_style(item: dict[str, Any]) -> str | None:
    """Return a semantic body style for back-matter prose when applicable."""

    identity = " ".join(
        str(item.get(name) or "")
        for name in ("id", "kind", "index", "title", "display_title")
    ).casefold()
    if re.search(r"(?:bibliograph|references?|参考书目|参考文献|书目)", identity):
        return "Bibliography Entry"
    if re.search(r"(?:^|\s)index(?:\s|$)|索引", identity):
        return "Index Entry"
    return None


def _set_docx_style_font(
    style: Any,
    *,
    east_asia: str,
    latin: str,
    size: float,
) -> None:
    """Set all Word font slots instead of depending on theme fallbacks."""

    from docx.oxml.ns import qn  # type: ignore[import-not-found]
    from docx.shared import Pt  # type: ignore[import-not-found]

    style.font.name = latin
    style.font.size = Pt(size)
    rpr = style._element.get_or_add_rPr()
    fonts = rpr.get_or_add_rFonts()
    fonts.set(qn("w:ascii"), latin)
    fonts.set(qn("w:hAnsi"), latin)
    fonts.set(qn("w:eastAsia"), east_asia)
    fonts.set(qn("w:cs"), latin)


def _configure_book_docx_styles(document: Any) -> str:
    """Install the deterministic, monochrome style sheet used by book DOCX."""

    from docx.enum.style import WD_STYLE_TYPE  # type: ignore[import-not-found]
    from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore[import-not-found]
    from docx.shared import Pt, RGBColor  # type: ignore[import-not-found]

    normal = document.styles["Normal"]
    _set_docx_style_font(
        normal,
        east_asia="Songti SC",
        latin="Times New Roman",
        size=11,
    )
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.first_line_indent = Pt(22)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.widow_control = True

    title_style_name = "Codex Book Title"
    try:
        title = document.styles[title_style_name]
    except KeyError:
        title = document.styles.add_style(title_style_name, WD_STYLE_TYPE.PARAGRAPH)
    _set_docx_style_font(
        title,
        east_asia="Hiragino Sans GB",
        latin="Arial",
        size=24,
    )
    title.font.bold = True
    title.font.color.rgb = RGBColor(0, 0, 0)
    title.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.first_line_indent = Pt(0)
    title.paragraph_format.space_before = Pt(120)
    title.paragraph_format.space_after = Pt(24)

    for name, size in (
        ("Heading 1", 18),
        ("Heading 2", 14),
        ("Heading 3", 12),
    ):
        style = document.styles[name]
        _set_docx_style_font(
            style,
            east_asia="Hiragino Sans GB",
            latin="Arial",
            size=size,
        )
        style.font.bold = True
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.paragraph_format.first_line_indent = Pt(0)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.widow_control = True
        style.paragraph_format.page_break_before = name == "Heading 1"
        style.paragraph_format.line_spacing = 1.15
        style.paragraph_format.space_before = Pt(0 if name == "Heading 1" else 14)
        style.paragraph_format.space_after = Pt(8)

    quote = document.styles["Quote"]
    _set_docx_style_font(
        quote,
        east_asia="Songti SC",
        latin="Times New Roman",
        size=10.5,
    )
    quote.paragraph_format.left_indent = Pt(22)
    quote.paragraph_format.right_indent = Pt(22)
    quote.paragraph_format.first_line_indent = Pt(0)
    quote.paragraph_format.line_spacing = 1.35
    quote.paragraph_format.space_before = Pt(4)
    quote.paragraph_format.space_after = Pt(4)

    semantic_styles = (
        ("Bibliography Entry", 10, 20, -20, 1.2, 2),
        ("Index Entry", 9.5, 0, 0, 1.15, 1),
        ("Table Text", 9.5, 0, 0, 1.1, 0),
    )
    for name, size, left, first, spacing, after in semantic_styles:
        try:
            style = document.styles[name]
        except KeyError:
            style = document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        _set_docx_style_font(
            style,
            east_asia="Songti SC",
            latin="Times New Roman",
            size=size,
        )
        style.paragraph_format.left_indent = Pt(left)
        style.paragraph_format.first_line_indent = Pt(first)
        style.paragraph_format.line_spacing = spacing
        style.paragraph_format.space_before = Pt(0)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.widow_control = True

    try:
        footnote_text = document.styles["Footnote Text"]
    except KeyError:
        footnote_text = document.styles.add_style(
            "Footnote Text", WD_STYLE_TYPE.PARAGRAPH
        )
    _set_docx_style_font(
        footnote_text,
        east_asia="Songti SC",
        latin="Times New Roman",
        size=9,
    )
    footnote_text.paragraph_format.first_line_indent = Pt(0)
    footnote_text.paragraph_format.line_spacing = 1.0
    footnote_text.paragraph_format.space_before = Pt(0)
    footnote_text.paragraph_format.space_after = Pt(0)
    footnote_text.paragraph_format.widow_control = False
    return title_style_name


def _configure_docx_footnote_numbering(section: Any) -> None:
    from docx.oxml import OxmlElement  # type: ignore[import-not-found]
    from docx.oxml.ns import qn  # type: ignore[import-not-found]

    section_properties = section._sectPr
    footnote_properties = section_properties.find(qn("w:footnotePr"))
    if footnote_properties is None:
        footnote_properties = OxmlElement("w:footnotePr")
        section_properties.insert(0, footnote_properties)
    for child in list(footnote_properties):
        if child.tag in {qn("w:numFmt"), qn("w:numRestart")}:
            footnote_properties.remove(child)
    number_format = OxmlElement("w:numFmt")
    number_format.set(qn("w:val"), "decimal")
    footnote_properties.append(number_format)
    restart = OxmlElement("w:numRestart")
    restart.set(qn("w:val"), "eachPage")
    footnote_properties.append(restart)


def _add_docx_page_number_footer(document: Any) -> None:
    """Add only a centered PAGE field; the title page intentionally stays blank."""

    from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore[import-not-found]
    from docx.oxml import OxmlElement  # type: ignore[import-not-found]
    from docx.oxml.ns import qn  # type: ignore[import-not-found]
    from docx.shared import Pt, RGBColor  # type: ignore[import-not-found]

    section = document.sections[0]
    section.different_first_page_header_footer = True
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.first_line_indent = Pt(0)
    run = paragraph.add_run()
    run.font.name = "Times New Roman"
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(96, 96, 96)
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    cached_run = OxmlElement("w:r")
    cached_text = OxmlElement("w:t")
    cached_text.text = "1"
    cached_run.append(cached_text)
    field.append(cached_run)
    paragraph._p.append(field)


def _style_docx_tables(document: Any) -> None:
    """Apply fixed, internally consistent DXA geometry to every book table."""

    import math

    from docx.enum.table import (  # type: ignore[import-not-found]
        WD_CELL_VERTICAL_ALIGNMENT,
        WD_TABLE_ALIGNMENT,
    )
    from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore[import-not-found]
    from docx.oxml import OxmlElement  # type: ignore[import-not-found]
    from docx.oxml.ns import qn  # type: ignore[import-not-found]
    from docx.shared import Twips  # type: ignore[import-not-found]

    section = document.sections[0]
    total_width = int(
        section.page_width.twips
        - section.left_margin.twips
        - section.right_margin.twips
    )

    def ensure(parent: Any, tag: str) -> Any:
        node = parent.find(qn(tag))
        if node is None:
            node = OxmlElement(tag)
            parent.append(node)
        return node

    for table in document.tables:
        if not table.columns:
            continue
        max_lengths = [
            max(
                (max(1, len(row.cells[index].text.strip())) for row in table.rows),
                default=1,
            )
            for index in range(len(table.columns))
        ]
        weights = [max(1.2, math.sqrt(value)) for value in max_lengths]
        total_weight = sum(weights)
        widths = [int(round(total_width * weight / total_weight)) for weight in weights]
        widths[-1] += total_width - sum(widths)

        table.style = "Table Grid"
        table.autofit = False
        table.alignment = WD_TABLE_ALIGNMENT.LEFT
        table_properties = table._tbl.tblPr
        for tag, width in (("w:tblW", total_width), ("w:tblInd", 120)):
            node = ensure(table_properties, tag)
            node.set(qn("w:type"), "dxa")
            node.set(qn("w:w"), str(width))
        layout = ensure(table_properties, "w:tblLayout")
        layout.set(qn("w:type"), "fixed")

        grid = table._tbl.tblGrid
        for child in list(grid):
            grid.remove(child)
        for width in widths:
            grid_column = OxmlElement("w:gridCol")
            grid_column.set(qn("w:w"), str(width))
            grid.append(grid_column)

        if table.rows:
            row_properties = table.rows[0]._tr.get_or_add_trPr()
            header = row_properties.find(qn("w:tblHeader"))
            if header is None:
                header = OxmlElement("w:tblHeader")
                row_properties.append(header)
            header.set(qn("w:val"), "true")

        for index, width in enumerate(widths):
            table.columns[index].width = Twips(width)
        for row_index, row in enumerate(table.rows):
            row.height = None
            for column_index, cell in enumerate(row.cells):
                width = widths[column_index]
                cell.width = Twips(width)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                cell_properties = cell._tc.get_or_add_tcPr()
                cell_width = ensure(cell_properties, "w:tcW")
                cell_width.set(qn("w:type"), "dxa")
                cell_width.set(qn("w:w"), str(width))
                margins = ensure(cell_properties, "w:tcMar")
                for side, margin_width in (
                    ("top", 80),
                    ("bottom", 80),
                    ("start", 120),
                    ("end", 120),
                ):
                    margin = ensure(margins, f"w:{side}")
                    margin.set(qn("w:type"), "dxa")
                    margin.set(qn("w:w"), str(margin_width))
                for paragraph in cell.paragraphs:
                    paragraph.style = document.styles["Table Text"]
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    for run in paragraph.runs:
                        run.bold = row_index == 0


def build_docx(
    output_path: Path,
    chapter_dir: Path,
    manifest: list[dict[str, Any]],
    *,
    book_title: str,
    author: str | None = None,
) -> None:
    try:
        from docx import Document  # type: ignore[import-not-found]
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore[import-not-found]
        from docx.enum.text import WD_BREAK  # type: ignore[import-not-found]
        from docx.shared import Cm, Pt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Word compilation requires python-docx>=1.1; install requirements.txt.") from exc

    document = Document()
    section = document.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.35)
    section.bottom_margin = Cm(2.25)
    section.left_margin = Cm(2.55)
    section.right_margin = Cm(2.55)
    section.header_distance = Cm(1.2)
    section.footer_distance = Cm(1.25)
    title_style_name = _configure_book_docx_styles(document)
    _configure_docx_footnote_numbering(section)
    _add_docx_page_number_footer(document)

    title = document.add_paragraph(style=title_style_name)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.add_run(book_title)
    if author:
        author_paragraph = document.add_paragraph(style="Normal")
        author_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        author_paragraph.paragraph_format.first_line_indent = Pt(0)
        author_paragraph.paragraph_format.space_before = Pt(0)
        author_paragraph.paragraph_format.space_after = Pt(0)
        author_paragraph.add_run(author)
    title_break = document.add_paragraph()
    title_break.paragraph_format.first_line_indent = Pt(0)
    title_break.add_run().add_break(WD_BREAK.PAGE)
    ordered_notes: list[tuple[str, str]] = []
    for sequence, item in enumerate(manifest, start=1):
        source = (chapter_dir / item["filename"]).read_text(encoding="utf-8")
        publication = (
            strip_reviewed_publication_metadata(source)
            if item.get("reviewed_override")
            else strip_publication_metadata(
                source,
                publication_title=book_title,
                chapter_title=str(item.get("display_title") or ""),
            )
        )
        rendered_markdown, chapter_notes = markdown_footnotes_to_docx_markers(
            publication,
            namespace=str(item.get("id") or f"chapter-{sequence}"),
        )
        ordered_notes.extend(
            (stable_id, markdown_inline_to_plain_text(note_text))
            for stable_id, note_text in chapter_notes
        )
        _append_markdown_to_docx(
            document,
            rendered_markdown,
            body_style=_docx_manifest_body_style(item),
        )
    _style_docx_tables(document)
    document.core_properties.title = book_title
    if author:
        document.core_properties.author = author
    document.core_properties.subject = "由章节 Markdown 合并生成的文字版 Word 文档"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".docx",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        document.save(temporary_path)
        if ordered_notes:
            patch_docx_footnotes(
                temporary_path,
                output_path,
                ordered_notes,
            )
        else:
            os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_epub(
    output_path: Path,
    chapter_dir: Path,
    manifest: list[dict[str, Any]],
    *,
    book_title: str,
    language: str,
) -> None:
    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, book_title)}"
    modified = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    nav_items: list[str] = []
    spine_items: list[str] = []
    manifest_items: list[str] = []
    chapter_files: list[tuple[str, str]] = []
    for item in manifest:
        md_path = chapter_dir / item["filename"]
        xhtml_name = md_path.with_suffix(".xhtml").name
        source_markdown = md_path.read_text(encoding="utf-8")
        body = markdown_to_html(
            strip_reviewed_publication_metadata(source_markdown)
            if item.get("reviewed_override")
            else strip_publication_metadata(
                source_markdown,
                publication_title=book_title,
                chapter_title=str(item.get("display_title") or ""),
            )
        )
        title = html.escape(str(item["display_title"]))
        document = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}">
<head><title>{title}</title><link rel="stylesheet" type="text/css" href="style.css" /></head>
<body>{body}</body>
</html>'''
        chapter_files.append((xhtml_name, document))
        item_id = f"chapter-{int(item['sequence']):04d}"
        manifest_items.append(f'<item id="{item_id}" href="{html.escape(xhtml_name)}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')
        nav_items.append(f'<li><a href="{html.escape(xhtml_name)}">{title}</a></li>')
    nav = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}">
<head><title>目录</title></head><body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>{''.join(nav_items)}</ol></nav></body>
</html>'''
    package = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="book-id">{book_id}</dc:identifier><dc:title>{html.escape(book_title)}</dc:title>
<dc:language>{html.escape(language)}</dc:language><meta property="dcterms:modified">{modified}</meta>
</metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="style" href="style.css" media-type="text/css"/>{''.join(manifest_items)}</manifest>
<spine>{''.join(spine_items)}</spine></package>'''
    container = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    css = "body{font-family:serif;line-height:1.7;margin:5%;}h1,h2,h3{page-break-after:avoid;}table{border-collapse:collapse;}td,th{border:1px solid #888;padding:.3em;}"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/package.opf", package)
        archive.writestr("OEBPS/nav.xhtml", nav)
        archive.writestr("OEBPS/style.css", css)
        for filename, content in chapter_files:
            archive.writestr(f"OEBPS/{filename}", content)


def build_bookmarked_pdf(source_pdf: Path, output_pdf: Path, toc_payload: dict[str, Any]) -> None:
    entries = []
    for item in toc_payload["entries"]:
        if not item.get("pdf_page"):
            continue
        entry = TocEntry(**item)
        # Structural cover sentinels are useful for chapter range calculation,
        # but they are not reader-facing navigation destinations.
        if entry.kind == "part" and normalize_match_text(entry.title) in {
            "封面",
            "封底",
            "frontcover",
            "backcover",
        }:
            continue
        entries.append(entry)
    if not entries:
        raise ValueError("No mapped TOC entries are available for PDF bookmarks.")
    min_level = min(entry.level for entry in entries)
    bookmarks: list[list[Any]] = []
    previous_level = 0
    for entry in entries:
        level = max(1, entry.level - min_level + 1)
        level = min(level, previous_level + 1)
        bookmarks.append([level, entry.display_title, int(entry.pdf_page)])
        previous_level = level
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(source_pdf) as document:
        document.set_toc(bookmarks)
        document.save(output_pdf, garbage=3, deflate=True)


def load_toc(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"TOC JSON not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("TOC JSON root must be an object.")
    return normalize_toc_payload(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a scanned PDF into page OCR, structured TOC, chapter Markdown, EPUB, Word, bookmarks, and RAG JSONL.",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help=(
            "Scanned PDF input. Optional for translate, epub, docx, status, "
            "incremental verify, or verify with --no-bookmarked-pdf."
        ),
    )
    parser.add_argument("-o", "--output-dir", default="outputs/book", help="Stable work/output directory; reruns resume automatically.")
    parser.add_argument(
        "--phase",
        choices=["all", "ocr", "proofread", "translate", "toc", "compile", "epub", "docx", "verify", "status"],
        default="all",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="TOML model-profile configuration; credentials are referenced by environment variable name.",
    )
    parser.add_argument("--ocr-profile", default=None)
    parser.add_argument("--toc-profile", default=None)
    parser.add_argument(
        "--proofread-profile",
        default=None,
        help="Text-model Profile for the optional OCR proofreading stage.",
    )
    parser.add_argument("--translation-profile", default=None)
    parser.add_argument("--api-mode", choices=["coding-plan", "standard"], default=os.getenv("GLM_API_MODE", "coding-plan"))
    parser.add_argument(
        "--api-key",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--api-key-env",
        default=None,
        help="Environment variable containing the GLM credential.",
    )
    parser.add_argument(
        "--api-base",
        default=None,
        help="Text model base URL; selected from --api-mode when omitted.",
    )
    parser.add_argument("--ocr-model", default=os.getenv("GLM_OCR_MODEL", "glm-ocr"))
    parser.add_argument("--text-model", default=os.getenv("GLM_TEXT_MODEL", "glm-5.2"))
    parser.add_argument(
        "--api-timeout",
        type=int,
        default=int(os.getenv("GLM_API_TIMEOUT", "120")),
        help="Timeout in seconds for each standard GLM HTTP request (default: GLM_API_TIMEOUT or 120).",
    )
    parser.add_argument(
        "--ocr-backend",
        choices=["coding-plan-mcp", "glm-ocr", "tesseract"],
        default=os.getenv("OCR_BACKEND", "coding-plan-mcp"),
        help="Coding Plan vision MCP (recommended) or separately billed standard GLM-OCR API.",
    )
    parser.add_argument(
        "--ocr-reading-direction",
        choices=["horizontal", "vertical"],
        default=None,
        help=(
            "Page reading direction used by the content-filter band fallback. "
            "Use vertical for traditional Japanese right-to-left columns. "
            "Defaults to the OCR profile setting, then horizontal."
        ),
    )
    parser.add_argument("--tesseract-language", default="jpn_vert+eng")
    parser.add_argument("--tesseract-psm", type=int, default=3)
    parser.add_argument("--ocr-api-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--ocr-api-key-env",
        default=None,
        help="Environment variable containing the separately billed OCR credential.",
    )
    parser.add_argument("--ocr-api-base", default=os.getenv("GLM_OCR_API_BASE", DEFAULT_STANDARD_API_BASE))
    parser.add_argument(
        "--ocr-command",
        default=os.getenv("CODING_PLAN_VISION_MCP_COMMAND", "npx -y @z_ai/mcp-server@0.1.4"),
        help="Official Coding Plan vision MCP stdio command.",
    )
    parser.add_argument("--start-page", type=int, default=1)
    parser.add_argument("--end-page", type=int, default=None)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Legacy worker count for both OCR and translation; stage-specific options take precedence.",
    )
    parser.add_argument(
        "--ocr-concurrency",
        type=int,
        default=None,
        help=f"Parallel OCR workers (default: OCR_CONCURRENCY or {DEFAULT_OCR_CONCURRENCY}).",
    )
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--max-image-side", type=int, default=3000)
    parser.add_argument("--jpeg-quality", type=int, default=90)
    parser.add_argument("--keep-page-images", action="store_true")
    parser.add_argument(
        "--ocr-delay",
        type=float,
        default=0.0,
        help="Minimum seconds between OCR request starts across workers.",
    )
    parser.add_argument(
        "--ocr-cache-model-prefix",
        default=None,
        help=(
            "Reuse a cached OCR page only when its ocr_model starts with one of these "
            "comma-separated prefixes; for example coding-plan/,manual/visually-confirmed-blank."
        ),
    )
    parser.add_argument(
        "--ocr-cache-model",
        default=None,
        help=(
            "Reuse cached OCR only when ocr_model exactly equals this identity. "
            "The Graph adapter sets this automatically; prefix matching remains "
            "available for compatibility."
        ),
    )
    parser.add_argument("--force", action="store_true", help="Re-run cached OCR/translation for the requested pages.")
    parser.add_argument("--skip-ocr", action="store_true", help="Require already imported/cached page OCR.")
    parser.add_argument("--import-ocr-dir", default=None, help="Import legacy extracted_pages.json or _checkpoints first.")
    parser.add_argument("--front-matter-pages", type=int, default=40)
    parser.add_argument("--toc-pages", default=None, help="Confirmed PDF TOC pages, e.g. 6-10,12.")
    parser.add_argument("--toc-json", default=None, help="Use a manually prepared TOC JSON instead of calling the LLM.")
    parser.add_argument(
        "--page-offset",
        type=int,
        default=None,
        help=(
            "PDF page minus floor(printed page / printed-pages-per-PDF-page); "
            "auto-detected when omitted."
        ),
    )
    parser.add_argument(
        "--printed-pages-per-pdf-page",
        type=int,
        default=None,
        help=(
            "Printed pages contained in one PDF page; auto-detects 1 or 2 from "
            "chapter-title evidence when omitted."
        ),
    )
    parser.add_argument(
        "--granularity",
        choices=["chapter", "section", "subsection", "all"],
        default=None,
        help=(
            "Chapter merge level. When omitted, preserve the existing output's "
            "selection; a first compile defaults to chapter."
        ),
    )
    parser.add_argument(
        "--proofread-language",
        default="ja",
        help="Language tag to proofread without translation (default: ja).",
    )
    parser.add_argument(
        "--proofread-concurrency",
        type=int,
        default=None,
        help="Parallel OCR-proofreading workers; defaults to the selected Profile.",
    )
    parser.add_argument(
        "--proofread-delay",
        type=float,
        default=0.0,
        help="Minimum seconds between proofreading request starts across workers.",
    )
    parser.add_argument("--proofread-max-chars", type=int, default=12000)
    parser.add_argument("--translate-non-chinese", action="store_true")
    parser.add_argument(
        "--translation-provider",
        choices=["deepseek", "glm"],
        default=os.getenv("TRANSLATION_PROVIDER", "deepseek"),
        help="Independent text translation provider; OCR and automatic TOC remain on the selected GLM path.",
    )
    parser.add_argument(
        "--translation-api-key",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--translation-api-key-env",
        default=None,
        help="Environment variable containing the translation credential.",
    )
    parser.add_argument("--translation-api-base", default=None)
    parser.add_argument("--translation-model", default=None)
    parser.add_argument(
        "--translation-api-timeout",
        type=int,
        default=None,
        help="Translation request timeout (default: DEEPSEEK_API_TIMEOUT or 120 seconds).",
    )
    parser.add_argument(
        "--translation-concurrency",
        type=int,
        default=None,
        help=(
            "Parallel page-translation workers "
            f"(default: TRANSLATION_CONCURRENCY or {DEFAULT_TRANSLATION_CONCURRENCY})."
        ),
    )
    parser.add_argument("--translation-max-chars", type=int, default=12000)
    parser.add_argument(
        "--translation-source-language",
        default="auto",
        help=(
            "Source-language override such as ja/en. With auto (default), "
            "only pages detected as non-Chinese are translated."
        ),
    )
    parser.add_argument(
        "--translation-delay",
        type=float,
        default=0.0,
        help="Minimum seconds between translation request starts; leave at 0 for maximum parallel throughput.",
    )
    parser.add_argument("--target-language", default="简体中文")
    parser.add_argument("--title", default=None, help="EPUB/Word title; defaults to the PDF filename.")
    parser.add_argument(
        "--author",
        default=None,
        help="Optional author shown on the Word title page and stored in DOCX properties.",
    )
    parser.add_argument("--no-epub", action="store_true")
    parser.add_argument("--no-docx", action="store_true")
    parser.add_argument("--no-kb", action="store_true")
    parser.add_argument("--no-bookmarked-pdf", action="store_true")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the automatic publication quality gate after compile/all.",
    )
    parser.add_argument(
        "--no-docx-render",
        action="store_true",
        help=(
            "Skip LibreOffice rendering of Word only for an unvalidated "
            "intermediate build; a full report will not be release-ready."
        ),
    )
    parser.add_argument(
        "--verification-profile",
        choices=("full", "word"),
        default="full",
        help=(
            "Verification contract: full requires every selected publication "
            "container; word requires DOCX structure and LibreOffice rendering "
            "while allowing EPUB, knowledge base, and reference PDF to remain "
            "outside the release scope."
        ),
    )
    parser.add_argument(
        "--report",
        default=None,
        help=(
            "Verification report path (default: OUTPUT/audit/release-report.json, "
            "word-release-report.json for the word profile, or "
            "chapter-report.json with --chapter-id)."
        ),
    )
    parser.add_argument(
        "--chapter-id",
        action="append",
        default=[],
        help=(
            "Limit --phase verify to one manifest chapter id or sequence; repeat the "
            "option for multiple repaired chapters."
        ),
    )
    parser.add_argument(
        "--require-all-reviewed",
        action="store_true",
        help="Fail verification unless every compiled chapter has a reviewed override.",
    )
    parser.add_argument(
        "--require-complete-ocr",
        action="store_true",
        help="Before compiling, require cached OCR records for every source-PDF page.",
    )
    parser.add_argument(
        "--require-translation",
        action="store_true",
        help="Before compiling, require a current translation for every nonblank selected page.",
    )
    parser.add_argument(
        "--required-ocr-model-prefix",
        default=None,
        help=(
            "Before compiling, require each cached page's ocr_model to start with one of "
            "these comma-separated values."
        ),
    )
    return parser


def _credential_from_env(name: str | None) -> str:
    return str(os.getenv(name, "") if name else "").strip()


def resolve_api_key(
    args: argparse.Namespace,
    profile: ModelProfile | None = None,
) -> str:
    explicit_env = _credential_from_env(getattr(args, "api_key_env", None))
    profile_key = (
        profile.resolve_credential(required=False).get_secret_value()
        if profile is not None
        else ""
    )
    if profile is not None:
        return str(args.api_key or explicit_env or profile_key).strip()
    if args.api_mode == "coding-plan":
        return str(
            args.api_key
            or explicit_env
            or profile_key
            or os.getenv("GLM_CODING_API_KEY")
            or os.getenv("Z_AI_API_KEY")
            or ""
        ).strip()
    return str(
        args.api_key
        or explicit_env
        or profile_key
        or os.getenv("GLM_API_KEY")
        or os.getenv("ZAI_API_KEY")
        or ""
    ).strip()


def resolve_translation_api_key(
    args: argparse.Namespace,
    profile: ModelProfile | None = None,
) -> str:
    """Resolve a translation credential without leaking or reusing an unrelated provider key."""
    explicit_env = _credential_from_env(
        getattr(args, "translation_api_key_env", None)
    )
    profile_key = (
        profile.resolve_credential(required=False).get_secret_value()
        if profile is not None
        else ""
    )
    if profile is not None:
        return str(
            args.translation_api_key or explicit_env or profile_key
        ).strip()
    provider = (
        profile.provider.strip().lower()
        if profile is not None
        else args.translation_provider
    )
    if provider == "deepseek":
        return str(
            args.translation_api_key
            or explicit_env
            or profile_key
            or os.getenv("DEEPSEEK_API_KEY")
            or ""
        ).strip()
    return str(
        args.translation_api_key
        or explicit_env
        or profile_key
        or resolve_api_key(args)
    ).strip()


def resolve_ocr_api_key(
    args: argparse.Namespace,
    profile: ModelProfile | None = None,
) -> str:
    """Resolve the separately billed OCR credential without profile fallthrough."""

    explicit_env = _credential_from_env(getattr(args, "ocr_api_key_env", None))
    profile_key = (
        profile.resolve_credential(required=False).get_secret_value()
        if profile is not None
        else ""
    )
    if profile is not None:
        return str(args.ocr_api_key or explicit_env or profile_key).strip()
    return str(
        args.ocr_api_key
        or explicit_env
        or os.getenv("GLM_OCR_API_KEY")
        or (resolve_api_key(args) if args.api_mode == "standard" else "")
    ).strip()


def resolve_worker_counts(args: argparse.Namespace) -> tuple[int, int]:
    """Return OCR/translation workers with stage-specific, legacy, then environment precedence."""
    legacy = args.concurrency
    ocr_default = int(os.getenv("OCR_CONCURRENCY", str(DEFAULT_OCR_CONCURRENCY)))
    translation_default = int(
        os.getenv("TRANSLATION_CONCURRENCY", str(DEFAULT_TRANSLATION_CONCURRENCY))
    )
    ocr_workers = args.ocr_concurrency if args.ocr_concurrency is not None else legacy
    translation_workers = (
        args.translation_concurrency if args.translation_concurrency is not None else legacy
    )
    return (
        int(ocr_workers if ocr_workers is not None else ocr_default),
        int(translation_workers if translation_workers is not None else translation_default),
    )


def build_translation_client(
    args: argparse.Namespace,
    *,
    glm_api_base: str,
    profile: ModelProfile | None = None,
) -> TextChatBackend | None:
    key = resolve_translation_api_key(args, profile)
    if not key:
        return None
    timeout = int(
        args.translation_api_timeout
        if args.translation_api_timeout is not None
        else (
            (str(profile.timeout) if profile is not None else "")
            or os.getenv("DEEPSEEK_API_TIMEOUT")
            or os.getenv("TRANSLATION_API_TIMEOUT")
            or "120"
        )
    )
    provider = (
        profile.provider.strip().lower()
        if profile is not None
        else args.translation_provider
    )
    if provider == "deepseek":
        return DeepSeekClient(
            api_key=key,
            api_base=(
                args.translation_api_base
                or (profile.base_url if profile is not None else "")
                or os.getenv("DEEPSEEK_API_BASE")
                or os.getenv("DEEPSEEK_BASE_URL")
                or DEFAULT_DEEPSEEK_API_BASE
            ),
            text_model=(
                args.translation_model
                or (profile.model if profile is not None else "")
                or os.getenv("DEEPSEEK_MODEL")
                or DEFAULT_DEEPSEEK_MODEL
            ),
            timeout=timeout,
            thinking=(profile.thinking if profile is not None else "disabled"),
            adapter_name=(profile.adapter if profile is not None else "openai-chat"),
        )
    client = GlmClient(
        api_key=key,
        api_base=(
            args.translation_api_base
            or (profile.base_url if profile is not None else "")
            or glm_api_base
        ),
        text_model=(
            args.translation_model
            or (profile.model if profile is not None else "")
            or args.text_model
        ),
        timeout=timeout,
        provider_name=provider or "glm",
        adapter_name=(profile.adapter if profile is not None else "openai-chat"),
        thinking=(profile.thinking if profile is not None else "disabled"),
    )
    return client


def build_proofread_client(
    args: argparse.Namespace,
    *,
    glm_api_base: str,
    profile: ModelProfile | None = None,
) -> TextChatBackend | None:
    """Build the proofreading text backend using the selected model Profile."""

    return build_translation_client(args, glm_api_base=glm_api_base, profile=profile)


def resolve_translation_identity(
    args: argparse.Namespace,
    *,
    glm_api_base: str,
    profile: ModelProfile | None = None,
) -> ModelIdentity:
    provider = (
        profile.provider.strip().lower()
        if profile is not None
        else args.translation_provider
    )
    adapter = profile.adapter if profile is not None else "openai-chat"
    if provider == "deepseek":
        base_url = (
            args.translation_api_base
            or (profile.base_url if profile is not None else "")
            or os.getenv("DEEPSEEK_API_BASE")
            or os.getenv("DEEPSEEK_BASE_URL")
            or DEFAULT_DEEPSEEK_API_BASE
        )
        model = (
            args.translation_model
            or (profile.model if profile is not None else "")
            or os.getenv("DEEPSEEK_MODEL")
            or DEFAULT_DEEPSEEK_MODEL
        )
    else:
        base_url = (
            args.translation_api_base
            or (profile.base_url if profile is not None else "")
            or glm_api_base
        )
        model = (
            args.translation_model
            or (profile.model if profile is not None else "")
            or args.text_model
        )
    return ModelIdentity(
        provider=provider,
        adapter=adapter,
        base_url=base_url,
        model=model,
        target_language=args.target_language,
        prompt_version=TRANSLATION_PROMPT_VERSION,
    )


def resolve_toc_api_base(
    args: argparse.Namespace,
    *,
    toc_profile: ModelProfile | None = None,
) -> str:
    if args.api_mode == "coding-plan":
        default_glm_base = (
            args.api_base
            or os.getenv("GLM_CODING_API_BASE")
            or DEFAULT_CODING_API_BASE
        )
    else:
        default_glm_base = (
            args.api_base or os.getenv("GLM_API_BASE") or DEFAULT_STANDARD_API_BASE
        )
    return (
        args.api_base
        or (toc_profile.base_url if toc_profile is not None else "")
        or default_glm_base
    )


def resolve_expected_translation_identity(
    args: argparse.Namespace,
    *,
    toc_profile: ModelProfile | None = None,
    translation_profile: ModelProfile | None = None,
) -> ModelIdentity:
    """Resolve the cache identity used by CLI status, compilation, and Python callers."""

    return resolve_translation_identity(
        args,
        glm_api_base=resolve_toc_api_base(args, toc_profile=toc_profile),
        profile=translation_profile,
    )


def resolve_proofread_identity(
    args: argparse.Namespace,
    *,
    glm_api_base: str,
    profile: ModelProfile | None = None,
) -> ModelIdentity:
    """Resolve proofreading cache identity without requiring a credential."""

    fallback = resolve_translation_identity(
        args,
        glm_api_base=glm_api_base,
        profile=profile,
    )
    return ModelIdentity(
        provider=fallback.provider,
        adapter=fallback.adapter,
        base_url=fallback.base_url,
        model=fallback.model,
        target_language=args.proofread_language,
        prompt_version=PROOFREAD_PROMPT_VERSION,
    )


def resolve_ocr_reading_direction(
    args: argparse.Namespace,
    ocr_profile: ModelProfile | None = None,
) -> str:
    """Resolve OCR layout consistently for execution, cache identity, and status."""

    return (
        args.ocr_reading_direction
        or (ocr_profile.reading_direction if ocr_profile is not None else "")
        or "horizontal"
    )


def resolve_expected_ocr_model_prefix(
    args: argparse.Namespace,
    ocr_profile: ModelProfile | None = None,
) -> str | None:
    """Resolve the selected OCR cache identity without duplicating status logic."""

    if args.required_ocr_model_prefix:
        return args.required_ocr_model_prefix
    if args.ocr_cache_model_prefix:
        return str(args.ocr_cache_model_prefix)
    if ocr_profile is not None and ocr_profile.adapter == "coding-plan-mcp":
        direction = resolve_ocr_reading_direction(args, ocr_profile)
        return f"coding-plan/{ocr_profile.model}-vision-mcp/{direction}-v2"
    return None


def resolve_expected_ocr_model_exact(
    args: argparse.Namespace,
    ocr_profile: ModelProfile | None = None,
) -> str | None:
    """Resolve the exact OCR checkpoint identity selected for execution.

    Explicit prefix matching remains a compatibility escape hatch.  In that
    mode status deliberately falls back to the prefix resolver above.
    """

    if args.ocr_cache_model:
        return str(args.ocr_cache_model)
    if args.ocr_cache_model_prefix:
        return None
    backend = ocr_profile.adapter if ocr_profile is not None else args.ocr_backend
    if backend == "coding-plan-mcp":
        direction = resolve_ocr_reading_direction(args, ocr_profile)
        model = (
            ocr_profile.model
            if ocr_profile is not None
            else os.getenv("Z_AI_VISION_MODEL", "glm-4.6v")
        )
        return f"coding-plan/{model}-vision-mcp/{direction}-v2"
    if backend == "glm-ocr":
        return ocr_profile.model if ocr_profile is not None else args.ocr_model
    if backend == "tesseract":
        return f"tesseract/{args.tesseract_language}/psm-{args.tesseract_psm}"
    return None


def output_status(
    output_dir: Path,
    *,
    expected_translation_identity: ModelIdentity | None = None,
    expected_proofread_identity: ModelIdentity | None = None,
    expected_ocr_model_prefix: str | None = None,
    expected_ocr_model_exact: str | None = None,
) -> dict[str, Any]:
    records = load_page_records(output_dir)
    toc_path = output_dir / "toc.json"
    chapters_path = output_dir / "chapters.json"
    verification_paths = (
        output_dir / "audit" / "release-report.json",
        output_dir / "audit" / "word-release-report.json",
    )
    verification_path: Path | None = None
    verification: dict[str, Any] | None = None
    verification_stale = False
    verification_candidates: list[tuple[int, Path, dict[str, Any]]] = []
    for candidate_path in verification_paths:
        if not candidate_path.exists():
            continue
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("mode") == "full":
                verification_candidates.append(
                    (candidate_path.stat().st_mtime_ns, candidate_path, payload)
                )
        except (OSError, ValueError):
            continue
    if verification_candidates:
        report_mtime, verification_path, verification = max(
            verification_candidates,
            key=lambda item: item[0],
        )
        watched_paths = [
            output_dir / "toc.json",
            output_dir / "chapters.json",
            output_dir / "knowledge_base.jsonl",
            *output_dir.glob("*.epub"),
            *output_dir.glob("*.docx"),
            *output_dir.glob("*_带目录.pdf"),
            *(output_dir / "chapters").glob("*.md"),
            *(output_dir / "reviewed_chapters").glob("*.md"),
            *(output_dir / "pages").glob("page_*.json"),
        ]
        verification_stale = any(
            path.is_file() and path.stat().st_mtime_ns > report_mtime
            for path in watched_paths
        )
    full_verification = verification
    artifacts = sorted(
        path.name
        for path in output_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".epub", ".docx", ".pdf", ".jsonl"}
    ) if output_dir.exists() else []
    expected_ocr_model_prefixes = parse_model_prefixes(
        expected_ocr_model_prefix
    )
    return {
        "output_dir": str(output_dir),
        "pages": len(records),
        "ocr_pages_profile_fresh": (
            sum(
                1
                for record in records
                if (
                    record.ocr_model == expected_ocr_model_exact
                    if expected_ocr_model_exact is not None
                    else record.ocr_model.startswith(expected_ocr_model_prefixes)
                )
            )
            if expected_ocr_model_exact is not None or expected_ocr_model_prefixes
            else None
        ),
        "ocr_models": dict(sorted(Counter(record.ocr_model for record in records).items())),
        "proofread_pages_source_fresh": sum(
            1 for record in records if record.proofread_is_fresh
        ),
        "proofread_pages_profile_fresh": (
            sum(
                1
                for record in records
                if record.proofread_is_fresh_for(expected_proofread_identity)
            )
            if expected_proofread_identity is not None
            else None
        ),
        "proofread_models": dict(
            sorted(
                Counter(
                    record.proofread_model
                    for record in records
                    if record.proofread_model
                ).items()
            )
        ),
        "effective_text_pages": sum(
            1 for record in records if record.proofread_is_fresh
        ),
        "translations_source_fresh": sum(
            1 for record in records if record.translation_is_fresh
        ),
        "translations_profile_fresh": (
            sum(
                1
                for record in records
                if record.translation_is_fresh_for(expected_translation_identity)
            )
            if expected_translation_identity is not None
            else None
        ),
        "toc_ready": toc_path.exists(),
        "chapters_ready": chapters_path.exists(),
        "verification_ready": full_verification is not None,
        "verification_status": (
            full_verification.get("status")
            if full_verification is not None
            else None
        ),
        "verification_profile": (
            full_verification.get("publication_profile", "full")
            if full_verification is not None
            else None
        ),
        "verification_release_ready": (
            bool(full_verification.get("release_ready"))
            if full_verification is not None
            else False
        ),
        "verification_stale": (
            verification_stale if full_verification is not None else False
        ),
        "verification_summary": (
            full_verification.get("summary")
            if full_verification is not None
            else None
        ),
        "verification_report": (
            str(verification_path)
            if full_verification is not None and verification_path is not None
            else None
        ),
        "artifacts": artifacts,
    }


def _shared_output_directory_locked(operation: Any) -> Any:
    """Serialize legacy and Graph entry points on the same output directory."""

    @functools.wraps(operation)
    def wrapped(argv: list[str] | None = None) -> int:
        effective_argv = list(sys.argv[1:] if argv is None else argv)
        load_env_file(Path(__file__).with_name(".env"))
        lock_args = build_parser().parse_args(effective_argv)
        output_dir = Path(lock_args.output_dir).expanduser().resolve()
        # Preserve the established read-only failure semantics for a missing
        # status directory instead of creating it merely to acquire a lock.
        if lock_args.phase == "status" and not output_dir.exists():
            return operation(effective_argv)
        from pipeline_graph.core import OutputDirectoryLock

        with OutputDirectoryLock(
            output_dir / ".pipeline_graph" / "output.lock"
        ):
            return operation(effective_argv)

    return wrapped


def _main_unlocked(argv: list[str] | None = None) -> int:
    """Run the pipeline without taking the shared output lock.

    This is an internal integration seam for ``pipeline_graph``.  Public
    callers must use :func:`main`, which owns the output-directory lock.
    """

    load_env_file(Path(__file__).with_name(".env"))
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.ocr_cache_model and args.ocr_cache_model_prefix:
        parser.error(
            "--ocr-cache-model and --ocr-cache-model-prefix are mutually exclusive."
        )
    if args.chapter_id and args.phase != "verify":
        parser.error("--chapter-id is only valid with --phase verify.")
    output_dir = Path(args.output_dir).expanduser().resolve()
    if args.phase == "status" and not output_dir.exists():
        parser.error(f"Output directory does not exist: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    profile_config: PipelineProfiles | None = None
    if args.config:
        try:
            profile_config = load_pipeline_profiles(args.config)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            parser.error(f"Invalid profile configuration: {exc}")
    elif any(
        (
            args.ocr_profile,
            args.toc_profile,
            args.proofread_profile,
            args.translation_profile,
        )
    ):
        parser.error(
            "--ocr-profile/--toc-profile/--proofread-profile/"
            "--translation-profile require --config."
        )
    try:
        ocr_profile = (
            profile_config.for_stage("ocr", args.ocr_profile)
            if profile_config is not None
            else None
        )
        toc_profile = (
            profile_config.for_stage("toc", args.toc_profile)
            if profile_config is not None
            else None
        )
        proofread_profile = (
            profile_config.for_stage("proofread", args.proofread_profile)
            if profile_config is not None
            else None
        )
        translation_profile = (
            profile_config.for_stage("translation", args.translation_profile)
            if profile_config is not None
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.ocr_reading_direction = resolve_ocr_reading_direction(args, ocr_profile)
    if (
        toc_profile is not None
        and toc_profile.adapter not in {"openai-chat", "glm-chat"}
    ):
        parser.error(
            f"TOC profile {toc_profile.name!r} requires an OpenAI-compatible chat adapter."
        )
    if (
        proofread_profile is not None
        and proofread_profile.adapter not in {"openai-chat", "glm-chat"}
    ):
        parser.error(
            f"Proofread profile {proofread_profile.name!r} requires an "
            "OpenAI-compatible chat adapter."
        )
    if (
        translation_profile is not None
        and translation_profile.adapter not in {"openai-chat", "glm-chat"}
    ):
        parser.error(
            f"Translation profile {translation_profile.name!r} requires an OpenAI-compatible chat adapter."
        )

    if any((args.api_key, args.ocr_api_key, args.translation_api_key)):
        print(
            "[warning] Raw API-key CLI flags are deprecated because argv may be visible. "
            "Use a profile credential_env or a *-api-key-env option.",
            file=sys.stderr,
        )

    pdf_required = args.phase in {"all", "ocr", "toc", "compile"} or (
        args.phase == "verify"
        and not args.chapter_id
        and not args.no_bookmarked_pdf
    )
    pdf_path: Path | None = None
    pdf_page_count = 0
    if args.input:
        pdf_path = Path(args.input).expanduser().resolve()
        if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
            parser.error(f"Input must be an existing PDF: {pdf_path}")
        with fitz.open(pdf_path) as document:
            pdf_page_count = document.page_count
    elif pdf_required:
        parser.error(f"--phase {args.phase} requires an input PDF.")

    if args.import_ocr_dir:
        imported = import_existing_ocr(
            Path(args.import_ocr_dir).expanduser().resolve(),
            output_dir,
        )
        print(f"[import] imported_pages={imported}")
    records = load_page_records(output_dir)

    toc_api_base = resolve_toc_api_base(args, toc_profile=toc_profile)
    expected_translation_identity = resolve_translation_identity(
        args,
        glm_api_base=toc_api_base,
        profile=translation_profile,
    )
    expected_proofread_identity = resolve_proofread_identity(
        args,
        glm_api_base=toc_api_base,
        profile=proofread_profile,
    )

    if args.phase == "status":
        expected_ocr_prefix = resolve_expected_ocr_model_prefix(args, ocr_profile)
        expected_ocr_exact = resolve_expected_ocr_model_exact(args, ocr_profile)
        print(
            json.dumps(
                output_status(
                    output_dir,
                    expected_translation_identity=expected_translation_identity,
                    expected_proofread_identity=expected_proofread_identity,
                    expected_ocr_model_prefix=expected_ocr_prefix,
                    expected_ocr_model_exact=expected_ocr_exact,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    book_title = args.title or (
        pdf_path.stem if pdf_path is not None else output_dir.name
    )
    default_report_name = (
        "chapter-report.json"
        if args.chapter_id
        else (
            "word-release-report.json"
            if args.verification_profile == "word"
            else "release-report.json"
        )
    )
    verification_report_path = (
        Path(args.report).expanduser().resolve()
        if args.report
        else output_dir / "audit" / default_report_name
    )
    if args.phase == "verify":
        report = verify_publication(
            output_dir,
            source_pdf=pdf_path,
            book_title=args.title,
            expected_language=(
                "zh-CN"
                if args.target_language == "简体中文"
                else args.target_language
            ),
            expected_translation_fingerprint=(
                expected_translation_identity.fingerprint
                if args.require_translation
                else None
            ),
            require_translation=args.require_translation,
            require_epub=not args.no_epub,
            require_docx=not args.no_docx,
            require_docx_render=(
                not args.no_docx and not args.no_docx_render
            ),
            require_knowledge_base=not args.no_kb,
            require_bookmarked_pdf=not args.no_bookmarked_pdf,
            require_all_reviewed=args.require_all_reviewed,
            publication_profile=args.verification_profile,
            chapter_ids=args.chapter_id or None,
            report_path=verification_report_path,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if bool(report.get("ok")) else 1
    if args.phase in {"epub", "docx"}:
        manifest_path = output_dir / "chapters.json"
        if not manifest_path.exists():
            parser.error(f"Chapter manifest does not exist: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if args.phase == "epub":
            build_epub(
                output_dir / f"{slugify(book_title)}.epub",
                output_dir / "chapters",
                manifest,
                book_title=book_title,
                language=(
                    "zh-CN"
                    if args.target_language == "简体中文"
                    else args.target_language
                ),
            )
        else:
            build_docx(
                output_dir / f"{slugify(book_title)}.docx",
                output_dir / "chapters",
                manifest,
                book_title=book_title,
                author=args.author,
            )
        return 0

    available_page_count = pdf_page_count or max(
        (record.pdf_page for record in records),
        default=0,
    )
    if not available_page_count:
        parser.error(
            "No source PDF page count or page checkpoints are available."
        )
    end_page = args.end_page or available_page_count
    if (
        args.start_page < 1
        or end_page < args.start_page
        or end_page > available_page_count
    ):
        parser.error(f"Page range must be within 1-{available_page_count}.")

    try:
        ocr_workers, translation_workers = resolve_worker_counts(args)
    except ValueError as exc:
        parser.error(f"Invalid worker environment value: {exc}")
    if (
        ocr_profile is not None
        and args.ocr_concurrency is None
        and args.concurrency is None
    ):
        ocr_workers = ocr_profile.concurrency
    if (
        translation_profile is not None
        and args.translation_concurrency is None
        and args.concurrency is None
    ):
        translation_workers = translation_profile.concurrency
    proofread_workers = (
        args.proofread_concurrency
        if args.proofread_concurrency is not None
        else (
            args.concurrency
            if args.concurrency is not None
            else (
                proofread_profile.concurrency
                if proofread_profile is not None
                else translation_workers
            )
        )
    )
    if ocr_workers < 1 or translation_workers < 1 or proofread_workers < 1:
        parser.error("OCR, proofreading, and translation worker counts must be positive.")
    if args.translation_max_chars < 1 or args.proofread_max_chars < 1:
        parser.error("Translation and proofreading max-chars values must be positive.")
    if args.proofread_delay < 0:
        parser.error("--proofread-delay cannot be negative.")
    if args.api_timeout < 1 or (
        args.translation_api_timeout is not None and args.translation_api_timeout < 1
    ):
        parser.error("API timeouts must be positive.")
    toc_key = resolve_api_key(args, toc_profile)
    ocr_key = resolve_api_key(args, ocr_profile)
    toc_text_model = (
        toc_profile.model if toc_profile is not None else args.text_model
    )
    toc_timeout = toc_profile.timeout if toc_profile is not None else args.api_timeout
    glm_client = GlmClient(
        api_key=toc_key,
        api_base=toc_api_base,
        ocr_model=args.ocr_model,
        text_model=toc_text_model,
        timeout=toc_timeout,
        provider_name=(toc_profile.provider if toc_profile is not None else "glm"),
        adapter_name=(toc_profile.adapter if toc_profile is not None else "openai-chat"),
        thinking=(toc_profile.thinking if toc_profile is not None else "disabled"),
    ) if toc_key else None
    ocr_backend: OCRBackend | None = None

    try:
        if args.phase in {"all", "ocr"}:
            assert pdf_path is not None
            if args.skip_ocr:
                records = load_page_records(output_dir)
                requested = set(range(args.start_page, end_page + 1))
                missing = sorted(requested - {record.pdf_page for record in records})
                if missing:
                    raise ValueError(f"--skip-ocr was used but cached OCR pages are missing: {missing[:20]}")
            else:
                ocr_backend_name = (
                    ocr_profile.adapter if ocr_profile is not None else args.ocr_backend
                )
                if ocr_backend_name == "coding-plan-mcp":
                    if not ocr_key:
                        raise ValueError("Coding Plan vision OCR requires GLM_CODING_API_KEY or Z_AI_API_KEY.")
                    if ocr_profile is not None and ocr_profile.command:
                        ocr_command = (
                            ocr_profile.command[0]
                            if len(ocr_profile.command) == 1
                            else shlex.join(ocr_profile.command)
                        )
                    else:
                        ocr_command = args.ocr_command
                    ocr_backend = CodingPlanVisionOCR(
                        api_key=ocr_key,
                        command=ocr_command,
                        reading_direction=args.ocr_reading_direction,
                        vision_model=(
                            ocr_profile.model
                            if ocr_profile is not None
                            else os.getenv("Z_AI_VISION_MODEL", "glm-4.6v")
                        ),
                        request_timeout=(
                            ocr_profile.timeout
                            if ocr_profile is not None
                            else int(os.getenv("CODING_PLAN_VISION_TIMEOUT", "120"))
                        ),
                    )
                elif ocr_backend_name == "glm-ocr":
                    standard_ocr_key = resolve_ocr_api_key(args, ocr_profile)
                    if not standard_ocr_key:
                        raise ValueError(
                            "Standard GLM-OCR is not covered by Coding Plan. Set GLM_OCR_API_KEY separately, "
                            "or use --ocr-backend coding-plan-mcp."
                        )
                    ocr_backend = GlmClient(
                        api_key=standard_ocr_key,
                        api_base=(
                            ocr_profile.base_url
                            if ocr_profile is not None and ocr_profile.base_url
                            else args.ocr_api_base
                        ),
                        ocr_model=(
                            ocr_profile.model
                            if ocr_profile is not None
                            else args.ocr_model
                        ),
                        text_model=toc_text_model,
                        timeout=(
                            ocr_profile.timeout
                            if ocr_profile is not None
                            else args.api_timeout
                        ),
                    )
                elif ocr_backend_name == "tesseract":
                    ocr_backend = TesseractOCR(
                        language=args.tesseract_language,
                        psm=args.tesseract_psm,
                    )
                else:
                    raise ValueError(
                        f"Unsupported OCR profile adapter: {ocr_backend_name}"
                    )
                records = ocr_pdf(
                    pdf_path,
                    output_dir,
                    ocr_backend,
                    start_page=args.start_page,
                    end_page=end_page,
                    concurrency=ocr_workers,
                    dpi=args.dpi,
                    max_image_side=args.max_image_side,
                    jpeg_quality=args.jpeg_quality,
                    keep_page_images=args.keep_page_images,
                    force=args.force,
                    cache_model_prefix=args.ocr_cache_model_prefix,
                    cache_model_exact=args.ocr_cache_model,
                    request_delay=args.ocr_delay,
                )
            # Release the model subprocess as soon as its stage finishes so
            # translation/compilation and the publication gate never run
            # while an idle OCR MCP service is still alive.
            if ocr_backend is not None:
                ocr_backend.close()
                ocr_backend = None
            records = normalize_cached_page_records(output_dir, records)
            records = load_page_records(output_dir)
            if args.phase == "ocr":
                print(f"[done] page OCR: {output_dir / 'pages'}")
                return 0
        records = load_page_records(output_dir)
        normalization_records = records
        if args.phase in {"proofread", "translate"}:
            # A targeted text-model run must not rewrite unrelated pages.
            # Besides avoiding needless I/O, this prevents a translation
            # process from publishing a stale copy of a page that a concurrent
            # OCR worker has just refreshed.
            normalization_records = [
                record
                for record in records
                if args.start_page <= record.pdf_page <= end_page
            ]
        normalize_cached_page_records(output_dir, normalization_records)
        records = load_page_records(output_dir)
        if not records:
            raise ValueError("No page OCR records found. Run --phase ocr or import existing OCR first.")

        if args.phase == "proofread":
            proofread_client = build_proofread_client(
                args,
                glm_api_base=toc_api_base,
                profile=proofread_profile,
            )
            if proofread_client is None:
                raise ValueError(
                    f"{expected_proofread_identity.provider} proofreading requires "
                    "the credential environment variable selected by its profile."
                )
            proofread_identity = proofread_client.model_identity(
                target_language=args.proofread_language,
                prompt_version=PROOFREAD_PROMPT_VERSION,
            )
            proofread_records = [
                record
                for record in records
                if args.start_page <= record.pdf_page <= end_page
            ]
            proofread_ocr_pages(
                proofread_records,
                output_dir,
                ChatOCRProofreader(
                    proofread_client,
                    max_chars=args.proofread_max_chars,
                ),
                language=args.proofread_language,
                identity=proofread_identity,
                force=args.force,
                concurrency=proofread_workers,
                request_delay=args.proofread_delay,
            )
            print(f"[done] OCR proofreading overlays: {output_dir / 'pages'}")
            return 0

        if args.translate_non_chinese and args.phase in {"all", "translate", "compile"}:
            translation_client = build_translation_client(
                args,
                glm_api_base=toc_api_base,
                profile=translation_profile,
            )
            if translation_client is None:
                raise ValueError(
                    f"{expected_translation_identity.provider} translation requires "
                    "the credential environment variable selected by its profile."
                )
            translation_identity = translation_client.model_identity(
                target_language=args.target_language,
                prompt_version=TRANSLATION_PROMPT_VERSION,
            )
            translation_records = [
                record
                for record in records
                if args.start_page <= record.pdf_page <= end_page
            ]
            translate_non_chinese_pages(
                translation_records,
                output_dir,
                ChatTranslator(translation_client, max_chars=args.translation_max_chars),
                target_language=args.target_language,
                force=args.force,
                concurrency=translation_workers,
                request_delay=args.translation_delay,
                source_language=(
                    None
                    if args.translation_source_language.strip().lower() == "auto"
                    else args.translation_source_language.strip().lower()
                ),
                translation_provider=args.translation_provider,
                translation_model=translation_identity.model,
                translation_identity=translation_identity,
            )
            records = load_page_records(output_dir)
        if args.phase == "translate":
            if not args.translate_non_chinese:
                raise ValueError("--phase translate requires --translate-non-chinese.")
            print(f"[done] page translation: {output_dir / 'pages'}")
            return 0

        toc_path = output_dir / "toc.json"
        if args.phase in {"all", "toc"}:
            if args.toc_json:
                manual = json.loads(Path(args.toc_json).expanduser().read_text(encoding="utf-8"))
                if not isinstance(manual, dict):
                    raise ValueError("Manual TOC JSON root must be an object.")
                toc_payload = normalize_toc_payload(manual)
            else:
                if glm_client is None:
                    raise ValueError("Automatic TOC parsing requires the selected API key, or pass --toc-json.")
                toc_pages = parse_page_spec(args.toc_pages) if args.toc_pages else None
                toc_payload = extract_toc(
                    records,
                    glm_client,
                    front_matter_pages=args.front_matter_pages,
                    toc_pages=toc_pages,
                )
            toc_payload = apply_page_mapping(
                toc_payload,
                records,
                page_offset=args.page_offset,
                source_page_count=pdf_page_count,
                printed_pages_per_pdf_page=args.printed_pages_per_pdf_page,
            )
            write_json(toc_path, toc_payload)
            print(
                f"[toc] entries={len(toc_payload['entries'])} "
                f"offset={toc_payload['page_offset']} "
                f"printed_pages_per_pdf_page={toc_payload['printed_pages_per_pdf_page']} "
                f"path={toc_path}"
            )
            if args.phase == "toc":
                return 0

        if args.phase in {"all", "compile"}:
            assert pdf_path is not None
            if args.require_complete_ocr:
                cached_pages = {record.pdf_page for record in records}
                missing_pages = [
                    page for page in range(1, pdf_page_count + 1) if page not in cached_pages
                ]
                extra_pages = sorted(
                    page for page in cached_pages if page < 1 or page > pdf_page_count
                )
                if missing_pages or extra_pages:
                    preview = missing_pages[:20]
                    suffix = "..." if len(missing_pages) > len(preview) else ""
                    raise ValueError(
                        "Complete OCR is required but cached page numbers do not exactly "
                        f"match the source PDF: missing={preview}{suffix}, "
                        f"extra={extra_pages[:20]}"
                    )
            required_ocr_model_prefix = args.required_ocr_model_prefix
            if not required_ocr_model_prefix and args.require_complete_ocr:
                required_ocr_model_prefix = resolve_expected_ocr_model_prefix(
                    args,
                    ocr_profile,
                )
            if required_ocr_model_prefix:
                required_prefixes = parse_model_prefixes(
                    required_ocr_model_prefix
                )
                wrong_models = [
                    (record.pdf_page, record.ocr_model)
                    for record in records
                    if not required_prefixes
                    or not record.ocr_model.startswith(required_prefixes)
                ]
                if wrong_models:
                    preview = wrong_models[:12]
                    suffix = "..." if len(wrong_models) > len(preview) else ""
                    raise ValueError(
                        f"OCR model prefix {required_ocr_model_prefix!r} is required, "
                        f"but cached pages do not match: {preview}{suffix}"
                    )
            toc_payload = load_toc(toc_path)
            if (
                not isinstance(toc_payload.get("page_offset"), int)
                or args.page_offset is not None
                or args.printed_pages_per_pdf_page is not None
            ):
                toc_payload = apply_page_mapping(
                    toc_payload,
                    records,
                    page_offset=args.page_offset,
                    source_page_count=pdf_page_count,
                    printed_pages_per_pdf_page=args.printed_pages_per_pdf_page,
                )
                write_json(toc_path, toc_payload)
            compile_granularity = resolve_compile_granularity(
                output_dir,
                toc_payload,
                args.granularity,
            )
            print(f"[compile] granularity={compile_granularity}")
            manifest, knowledge_rows = compile_chapters(
                pdf_path,
                output_dir,
                records,
                toc_payload,
                granularity=compile_granularity,
                publication_title=book_title,
                require_translation=args.require_translation,
                expected_translation_identity=expected_translation_identity,
            )
            if not args.no_kb:
                write_knowledge_base(output_dir / "knowledge_base.jsonl", knowledge_rows)
            if not args.no_epub:
                build_epub(
                    output_dir / f"{slugify(book_title)}.epub",
                    output_dir / "chapters",
                    manifest,
                    book_title=book_title,
                    language="zh-CN" if args.target_language == "简体中文" else args.target_language,
                )
            if not args.no_docx:
                build_docx(
                    output_dir / f"{slugify(book_title)}.docx",
                    output_dir / "chapters",
                    manifest,
                    book_title=book_title,
                    author=args.author,
                )
            if not args.no_bookmarked_pdf:
                build_bookmarked_pdf(
                    pdf_path,
                    output_dir / f"{slugify(book_title)}_带目录.pdf",
                    toc_payload,
                )
            if not args.no_verify:
                report = verify_publication(
                    output_dir,
                    source_pdf=pdf_path,
                    book_title=book_title,
                    expected_language=(
                        "zh-CN"
                        if args.target_language == "简体中文"
                        else args.target_language
                    ),
                    expected_translation_fingerprint=(
                        expected_translation_identity.fingerprint
                        if args.require_translation
                        else None
                    ),
                    require_translation=args.require_translation,
                    require_epub=not args.no_epub,
                    require_docx=not args.no_docx,
                    require_docx_render=(
                        not args.no_docx and not args.no_docx_render
                    ),
                    require_knowledge_base=not args.no_kb,
                    require_bookmarked_pdf=not args.no_bookmarked_pdf,
                    require_all_reviewed=args.require_all_reviewed,
                    publication_profile=args.verification_profile,
                    report_path=verification_report_path,
                )
                summary = report.get("summary", {})
                print(
                    "[verify] "
                    f"status={report.get('status')} "
                    f"passed={summary.get('passed', 0)} "
                    f"failed={summary.get('failed', 0)} "
                    f"report={verification_report_path}"
                )
                if not bool(report.get("ok")):
                    raise ValueError(
                        "Publication quality gate failed; inspect "
                        f"{verification_report_path}."
                    )
            print(f"[done] chapters={len(manifest)} output={output_dir}")
        return 0
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if ocr_backend is not None:
            ocr_backend.close()


@_shared_output_directory_locked
def main(argv: list[str] | None = None) -> int:
    """Run the legacy-compatible CLI under the shared output lock."""

    return _main_unlocked(argv)


if __name__ == "__main__":
    raise SystemExit(main())
