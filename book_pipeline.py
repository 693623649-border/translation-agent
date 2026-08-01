from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import html
import http.client
import json
import os
import re
import select
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import tomllib
import unicodedata
import urllib.error
import urllib.request
import uuid
import zipfile
import ssl
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
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
from pipeline_runtime import RetryPolicy, StartRateLimiter, retry_with_backoff

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
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_OCR_CONCURRENCY = 4
DEFAULT_TRANSLATION_CONCURRENCY = 16
TRANSLATION_PROMPT_VERSION = "book-translation-v2"
TOC_KINDS = {"part", "chapter", "section", "subsection", "frontmatter", "other"}
NON_CONTENT_MARKERS = {"[无法辨认]", "[空白页]"}


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

    @property
    def compile_text(self) -> str:
        return (self.translated_text if self.translation_is_fresh else self.text).strip()

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def translation_is_fresh(self) -> bool:
        return bool(
            self.translated_text.strip()
            and self.translation_source_sha256
            and self.translation_source_sha256 == self.text_sha256
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
            self.translated_text if self.translation_is_fresh_for(identity) else self.text
        ).strip()


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
    normalized = str(converter.convert(text))
    # OpenCC preserves 著 because it is valid in lexical words such as
    # 著者/名著. DeepSeek can still emit the Taiwanese aspect marker 著 in
    # otherwise simplified prose. Protect genuine lexical uses first.
    protected_terms = (
        "著者", "著作", "著名", "显著", "卓著", "编著", "译著", "原著",
        "巨著", "名著", "专著", "论著", "土著", "著书", "著述", "著称", "著录", "著文",
    )
    placeholders: dict[str, str] = {}
    for index, term in enumerate(protected_terms):
        placeholder = f"\ue000{index}\ue001"
        if term in normalized:
            normalized = normalized.replace(term, placeholder)
            placeholders[placeholder] = term
    normalized = normalized.replace("著", "着")
    for placeholder, term in placeholders.items():
        normalized = normalized.replace(placeholder, term)
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

    def __init__(self, command: list[str], *, api_key: str) -> None:
        if not command or shutil.which(command[0]) is None:
            raise RuntimeError(
                "Coding Plan vision OCR requires Node.js 18+ and npx. "
                "Install Node.js, then verify: npx -y @z_ai/mcp-server@latest"
            )
        environment = os.environ.copy()
        environment["Z_AI_API_KEY"] = api_key
        environment.setdefault("Z_AI_MODE", "ZHIPU")
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        self.next_id = 1
        self.request_timeout = max(30, int(os.getenv("CODING_PLAN_VISION_TIMEOUT", "300")))
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
                )
            line = self.process.stdout.readline()
            if not line:
                code = self.process.poll()
                raise RuntimeError(f"Vision MCP stopped before responding (exit={code}).")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Vision MCP error: {message['error']}")
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
                arguments[name] = (
                    "逐字提取书页中的全部可见文字，不翻译、不总结。保持标题、段落、列表、表格和脚注结构，"
                    "按自然阅读顺序输出 Markdown；无法辨认处标记 [无法辨认]。只输出识别文本。"
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
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.stdout is not None and not self.process.stdout.closed:
            self.process.stdout.close()


class CodingPlanVisionOCR:
    """One persistent official vision-MCP process per OCR worker thread."""

    ocr_model = "coding-plan/glm-4.6v-vision-mcp"

    def __init__(
        self,
        *,
        api_key: str,
        command: str,
        reading_direction: str = "horizontal",
    ) -> None:
        self.api_key = api_key
        self.command = shlex.split(command)
        if reading_direction not in {"horizontal", "vertical"}:
            raise ValueError(f"Unsupported OCR reading direction: {reading_direction}")
        self.reading_direction = reading_direction
        self.local = threading.local()
        self.clients: list[McpStdioClient] = []
        self.clients_lock = threading.Lock()

    def _client(self) -> McpStdioClient:
        client = getattr(self.local, "client", None)
        if client is None:
            client = McpStdioClient(self.command, api_key=self.api_key)
            self.local.client = client
            with self.clients_lock:
                self.clients.append(client)
        return client

    @staticmethod
    def _is_content_filter_error(error: Exception) -> bool:
        message = str(error).lower()
        return any(token in message for token in ("contentfilter", '"code":"1301"', "potentially unsafe"))

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
    ) -> tuple[str, str]:
        """OCR a filtered page as ordered horizontal bands using the same MCP."""
        part_paths: list[Path] = []
        texts: list[str] = []
        try:
            with Image.open(image_path) as source:
                image = source.convert("RGB")
                width, height = image.size
                if getattr(self, "reading_direction", "horizontal") == "vertical":
                    boundaries = [0, *self._blank_column_cuts(image, segments), width]
                    regions = [
                        (left, 0, right, height)
                        for left, right in reversed(list(zip(boundaries, boundaries[1:])))
                    ]
                else:
                    boundaries = [0, *self._blank_row_cuts(image, segments), height]
                    regions = [
                        (0, top, width, bottom)
                        for top, bottom in zip(boundaries, boundaries[1:])
                    ]
                for index, region in enumerate(regions, start=1):
                    part_path = image_path.with_name(
                        f"{image_path.stem}_segment_{index}_{uuid.uuid4().hex[:8]}.jpg"
                    )
                    image.crop(region).save(part_path, "JPEG", quality=95, optimize=True)
                    part_paths.append(part_path)
            for part_path in part_paths:
                text, _ = self._ocr_filtered_band(part_path, depth=0)
                text = text.strip()
                if text and not self._is_empty_band_text(text):
                    texts.append(text)
        finally:
            for part_path in part_paths:
                part_path.unlink(missing_ok=True)
        if not texts or any(not text for text in texts):
            raise RuntimeError("Segmented vision MCP OCR returned an empty band.")
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
        """Recursively split only the band that still triggers error 1301."""
        client = self._client()
        try:
            return client.extract_text(image_path)
        except Exception as exc:
            if not self._is_content_filter_error(exc) or depth >= 6:
                raise
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
                    split_dimension = width if is_vertical else height
                    if split_dimension < 40:
                        raise RuntimeError(
                            "Vision MCP content filter persisted at the minimum safe band height."
                        ) from exc
                    if is_vertical:
                        cut = self._blank_column_cuts(image, 2)[0]
                        regions = ((cut, 0, width, height), (0, 0, cut, height))
                    else:
                        cut = self._blank_row_cuts(image, 2)[0]
                        regions = ((0, 0, width, cut), (0, cut, width, height))
                    if cut <= 0 or cut >= split_dimension:
                        raise RuntimeError(
                            "Vision MCP content filter could not be split into nonempty bands."
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
        attempts = max(1, int(os.getenv("CODING_PLAN_OCR_ATTEMPTS", "4")))

        def recognize_once() -> tuple[str, str]:
            client = self._client()
            try:
                return client.extract_text(image_path)
            except Exception as exc:  # noqa: BLE001 - MCP/network failures are retried per page.
                retry_error: Exception = exc
                if self._is_content_filter_error(exc):
                    # A whole scholarly page can trip input filtering because
                    # of an isolated historical phrase.  Retrying the identical
                    # image cannot help, so use the same OCR service on ordered
                    # page bands.  Eight bands are a final fallback when four
                    # still contain the triggering context.
                    # Recreate the stdio client first: some MCP server builds
                    # stop answering subsequent tool calls after error 1301.
                    close_client = getattr(client, "close", None)
                    if callable(close_client):
                        close_client()
                    if hasattr(self, "local"):
                        self.local.client = None
                    client = self._client()
                    for segments in (4, 8, 16, 32):
                        try:
                            return self._ocr_segmented(image_path, client, segments=segments)
                        except Exception as segmented_error:  # noqa: BLE001
                            retry_error = segmented_error
                            if not self._is_content_filter_error(segmented_error):
                                break
                if client.process.poll() is not None or "timed out" in str(exc).lower():
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
            )
        except Exception as exc:
            raise RuntimeError(
                f"Vision MCP OCR failed after {attempts} attempts: {exc}"
            ) from exc

    def close(self) -> None:
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


class ChatTranslator:
    def __init__(self, client: TextChatBackend, *, max_chars: int = 12000) -> None:
        self.client = client
        self.max_chars = max_chars

    def translate(self, text: str, *, source_language: str, target_language: str) -> str:
        translated: list[str] = []
        chunks = split_text(text, self.max_chars)
        for index, chunk in enumerate(chunks, start=1):
            output_budget = min(32768, max(4096, len(chunk) * 4))
            prompt = f"""
请把下面的 OCR 原文完整翻译成{target_language}。
源语言标签：{source_language}
分块：{index}/{len(chunks)}

要求：
1. 不总结、不删减、不扩写。
2. 保留 Markdown 标题、列表、表格、脚注和段落结构。
3. 人名、书名、术语前后一致；无法确认的内容保留原文并标注 [存疑]。
4. 输入来自 OCR。先根据日语语法和上下文修正明显的字符、断行和空格错误；无法可靠还原时标注 [原文存疑]，不要编造。
5. 正文中出现的日语、英语及其他外语段落或引文也必须译成目标语言，不得整段保留未译；仅专名、必要术语和文献标识可按惯例保留原文。
6. 如果目标是简体中文，必须使用中国大陆通行简体字与标点，不得输出繁体字。
7. 只输出译文，不附加说明或质量报告。

原文：
{chunk}
""".strip()
            translated.append(
                clean_translation_text(self.client.chat_text(
                    prompt,
                    system="你是严谨的书籍翻译器，优先保证完整性、准确性和结构可追溯。",
                    max_tokens=output_budget,
                ))
            )
        return normalize_target_script("\n\n".join(translated).strip(), target_language)


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
    ) -> PageRecord:
        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} OCR changed while translation was running; "
                    "the stale translation was discarded."
                )
            latest.translated_text = translated_text
            latest.translation_source_sha256 = expected_text_sha256
            latest.translation_provider = identity.provider
            latest.translation_model = identity.model
            latest.translation_target_language = identity.target_language
            latest.translation_prompt_version = identity.prompt_version
            latest.translation_fingerprint = identity.fingerprint
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
    ) -> PageRecord:
        """Normalize a cached translation without replacing newer OCR/model output."""
        with _exclusive_page_lock(self._lock_path(pdf_page)):
            latest = self.load(pdf_page)
            if latest.text_sha256 != expected_text_sha256:
                raise StalePageSourceError(
                    f"Page {pdf_page} OCR changed while cached translation was normalized."
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
            latest.translated_text = translated_text
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
            latest.text = cleaned_text
            latest.language = language
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
    request_delay: float = 0.0,
) -> list[PageRecord]:
    existing = {record.pdf_page: record for record in load_page_records(output_dir)}
    cache_model_prefixes = tuple(
        prefix.strip()
        for prefix in (cache_model_prefix or "").split(",")
        if prefix.strip()
    )
    pages = list(range(start_page, end_page + 1))
    pending = [
        page
        for page in pages
        if force
        or page not in existing
        or not existing[page].text.strip()
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
            record = PageRecord(
                pdf_page=pdf_page,
                text=text,
                language=detect_language(text),
                notes=f"request_id={request_id}" if request_id else "",
                ocr_model=client.ocr_model,
            )
            save_page_record(output_dir, record)
            return record
        finally:
            if not keep_page_images:
                image_path.unlink(missing_ok=True)

    if pending:
        failures: list[tuple[int, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
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
            normalized = normalize_target_script(
                clean_translation_text(record.translated_text),
                target_language,
            )
            if normalized != record.translated_text:
                try:
                    committed = store.update_translation_text(
                        record.pdf_page,
                        expected_text_sha256=record.text_sha256,
                        expected_translation_sha256=hashlib.sha256(
                            record.translated_text.encode("utf-8")
                        ).hexdigest(),
                        expected_translation_fingerprint=record.translation_fingerprint,
                        translated_text=normalized,
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
        if record.text.strip()
        and record.text.strip() not in NON_CONTENT_MARKERS
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
        rate_limiter.wait()
        print(f"[translate] page={record.pdf_page} language={record.language} started")
        source_sha256 = record.text_sha256
        translated_text = translator.translate(
            record.text,
            source_language=source_language or record.language,
            target_language=target_language,
        )
        committed = store.commit_translation(
            record.pdf_page,
            expected_text_sha256=source_sha256,
            translated_text=translated_text,
            identity=identity,
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
    return {
        "schema_version": 1,
        "toc_pdf_pages": toc_pages,
        "page_offset": payload.get("page_offset"),
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
        if entry.printed_page is None or entry.kind not in {
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
                    "offset": best_page - entry.printed_page,
                    "score": round(best_score, 3),
                }
            )
    return evidence


def infer_page_offset(entries: list[TocEntry], records: list[PageRecord], toc_end: int) -> tuple[int, list[dict[str, Any]]]:
    evidence = find_title_evidence(entries, records, toc_end)
    if not evidence:
        raise ValueError("Cannot infer page offset from OCR text. Pass --page-offset after checking one chapter page.")
    offsets = [int(item["offset"]) for item in evidence]
    counts = Counter(offsets)
    best_count = max(counts.values())
    if best_count >= 2:
        candidates = [offset for offset, count in counts.items() if count == best_count]
        offset = min(candidates, key=lambda item: (abs(item), item))
    else:
        offset = int(round(statistics.median(offsets)))
    matching = [item for item in evidence if abs(int(item["offset"]) - offset) <= 1]
    if len(evidence) >= 3 and not matching:
        raise ValueError("TOC/page-title matches disagree; pass --page-offset manually.")
    return offset, evidence


def apply_page_mapping(
    toc_payload: dict[str, Any],
    records: list[PageRecord],
    *,
    page_offset: int | None,
    source_page_count: int | None = None,
) -> dict[str, Any]:
    normalized = normalize_toc_payload(toc_payload)
    entries = [TocEntry(**item) for item in normalized["entries"]]
    toc_end = max(normalized.get("toc_pdf_pages") or [0])
    evidence = find_title_evidence(entries, records, toc_end)
    manual_override = page_offset is not None
    if page_offset is None:
        existing_offset = normalized.get("page_offset")
        if isinstance(existing_offset, int):
            page_offset = existing_offset
        else:
            page_offset, evidence = infer_page_offset(entries, records, toc_end)
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
            mapped = entry.printed_page + page_offset
            entry.pdf_page = mapped if 1 <= mapped <= max_page else None
    normalized["page_offset"] = page_offset
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


def remove_duplicate_title(text: str, title: str) -> str:
    lines = text.splitlines()
    normalized_title = normalize_match_text(title)
    loose_title = re.sub(r"(?:的|之|の)", "", normalized_title)
    last_title_line: int | None = None

    def matches(candidate: str) -> bool:
        if not candidate:
            return False
        if candidate == normalized_title:
            return True
        loose_candidate = re.sub(r"(?:的|之|の)", "", candidate)
        if loose_candidate and loose_candidate == loose_title:
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
    for start in range(min(10, len(lines))):
        if not lines[start].strip():
            continue
        combined = ""
        for index in range(start, min(start + 4, len(lines), 10)):
            if not lines[index].strip():
                continue
            combined += normalize_match_text(lines[index])
            if matches(combined):
                last_title_line = index
                break
        if last_title_line is not None:
            break
    if last_title_line is not None:
        del lines[: last_title_line + 1]
        while lines and not lines[0].strip():
            del lines[0]
    return "\n".join(lines).strip()


def compile_chapters(
    pdf_path: Path,
    output_dir: Path,
    records: list[PageRecord],
    toc_payload: dict[str, Any],
    *,
    granularity: str,
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
    overlap_boundary = granularity in {"section", "subsection"}
    chapter_ranges: dict[str, tuple[int, int, int | None, bool]] = {}

    # Validate every selected range before touching a previous successful
    # chapter build. A late missing page or stale translation must not leave a
    # half-replaced chapters directory.
    for sequence, entry in enumerate(selected, start=1):
        start = int(entry.pdf_page or 0)
        next_start: int | None = None
        next_level: int | None = None
        if granularity == "all":
            if sequence < len(selected) and selected[sequence].pdf_page:
                next_start = int(selected[sequence].pdf_page)
                next_level = selected[sequence].level
        else:
            position = entry_positions.get(entry.id, -1)
            for candidate in entries[position + 1 :]:
                if candidate.pdf_page is None or candidate.level > entry.level:
                    continue
                if int(candidate.pdf_page) >= start:
                    next_start = int(candidate.pdf_page)
                    next_level = candidate.level
                    break
        overlaps_next = overlap_boundary and next_level == entry.level
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
        if require_translation:
            stale_or_missing_translation = [
                page
                for page in range(start, end + 1)
                if record_map[page].text.strip()
                and record_map[page].text.strip() not in NON_CONTENT_MARKERS
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
        chapter_ranges[entry.id] = (start, end, next_start, overlaps_next)

    chapter_dir = output_dir / "chapters"
    if chapter_dir.exists():
        for old_path in chapter_dir.glob("*.md"):
            old_path.unlink()
    chapter_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    knowledge_rows: list[dict[str, Any]] = []

    for sequence, entry in enumerate(selected, start=1):
        start, end, next_start, overlaps_next = chapter_ranges[entry.id]
        filename = f"{sequence:03d}_{slugify(entry.display_title)}.md"
        parts = [
            f"# {entry.display_title}",
            "",
            f"<!-- source-pdf: {pdf_path.name} -->",
            f"<!-- pdf-pages: {start}-{end} -->",
            "",
        ]
        for page in range(start, end + 1):
            record = record_map[page]
            content = record.compile_text_for(expected_translation_identity)
            if page == start:
                content = remove_duplicate_title(content, entry.title)
            parts.extend(
                [
                    f'<span epub:type="pagebreak" id="pdf-page-{page}" title="{page}"></span>',
                    f"<!-- PDF_PAGE: {page} -->",
                    "",
                    content,
                    "",
                ]
            )
            for chunk_index, chunk in enumerate(split_text(content, 4000), start=1):
                row_id = hashlib.sha1(
                    f"{pdf_path.name}:{entry.id}:{page}:{chunk_index}".encode("utf-8")
                ).hexdigest()
                knowledge_rows.append(
                    {
                        "id": row_id,
                        "title": entry.display_title,
                        "chapter_id": entry.id,
                        "chapter_order": sequence,
                        "content": chunk,
                        "source_pdf": pdf_path.name,
                        "pdf_page_start": page,
                        "pdf_page_end": page,
                        "printed_page": (
                            None
                            if entry.printed_page is None
                            else int(entry.printed_page) + page - start
                        ),
                        "boundary_overlap": overlaps_next and next_start == page,
                    }
                )
        markdown = "\n".join(parts).rstrip() + "\n"
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
            }
        )
    write_json(output_dir / "chapters.json", manifest)
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
            if normalized_title in candidate or (
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
        while output and not output[-1].strip():
            output.pop()
        if output:
            candidate = output[-1].strip()
            digits = re.fullmatch(r"[-—–\s]*(\d{1,3})[-—–\s]*", candidate)
            if digits:
                output.pop()
        while output and not output[-1].strip():
            output.pop()

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
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('<span epub:type="pagebreak"'):
            discard_trailing_printed_page()
            pending_page_boundary = True
            skipped_leading_page_number = False
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
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

    compact: list[str] = []
    for line in output:
        if not line.strip() and compact and not compact[-1].strip():
            continue
        compact.append(line)
    return "\n".join(compact).strip() + "\n"


def markdown_to_html(markdown_text: str) -> str:
    try:
        import markdown  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("EPUB compilation requires Markdown>=3.6; install requirements.txt.") from exc
    return markdown.markdown(markdown_text, extensions=["extra", "sane_lists"], output_format="xhtml")


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


def build_docx(
    output_path: Path,
    chapter_dir: Path,
    manifest: list[dict[str, Any]],
    *,
    book_title: str,
) -> None:
    try:
        from docx import Document  # type: ignore[import-not-found]
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore[import-not-found]
        from docx.oxml.ns import qn  # type: ignore[import-not-found]
        from docx.shared import Cm, Pt  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("Word compilation requires python-docx>=1.1; install requirements.txt.") from exc

    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.4)
    section.left_margin = Cm(2.7)
    section.right_margin = Cm(2.7)

    normal = document.styles["Normal"]
    normal.font.name = "宋体"
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.first_line_indent = Pt(22)
    for name, size in (("Title", 24), ("Heading 1", 18), ("Heading 2", 15), ("Heading 3", 13)):
        style = document.styles[name]
        style.font.name = "黑体"
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        style.font.size = Pt(size)
        style.font.bold = True
        style.paragraph_format.first_line_indent = Pt(0)
        style.paragraph_format.space_before = Pt(14)
        style.paragraph_format.space_after = Pt(8)

    title = document.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.add_run(book_title)
    for item in manifest:
        source = (chapter_dir / item["filename"]).read_text(encoding="utf-8")
        publication = strip_publication_metadata(
            source,
            publication_title=book_title,
            chapter_title=str(item.get("display_title") or ""),
        )
        for block in _markdown_blocks(publication):
            heading = re.fullmatch(r"(#{1,4})\s+(.+)", block)
            if heading:
                document.add_heading(heading.group(2).strip(), level=min(3, len(heading.group(1))))
                continue
            cleaned = markdown_inline_to_plain_text(block)
            if re.fullmatch(r"[一二三四五六七八九十]+", cleaned):
                document.add_heading(cleaned, level=2)
            elif re.match(r"^\d+[.、．]\s*\S", cleaned) and len(cleaned) <= 80:
                document.add_heading(cleaned, level=2)
            elif cleaned:
                document.add_paragraph(cleaned)
    document.core_properties.title = book_title
    document.core_properties.subject = "由章节 Markdown 合并生成的文字版 Word 文档"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)


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
            strip_publication_metadata(
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
        help="Scanned PDF input. Optional for translate, epub, docx, and status.",
    )
    parser.add_argument("-o", "--output-dir", default="outputs/book", help="Stable work/output directory; reruns resume automatically.")
    parser.add_argument(
        "--phase",
        choices=["all", "ocr", "translate", "toc", "compile", "epub", "docx", "status"],
        default="all",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="TOML model-profile configuration; credentials are referenced by environment variable name.",
    )
    parser.add_argument("--ocr-profile", default=None)
    parser.add_argument("--toc-profile", default=None)
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
        default="horizontal",
        help=(
            "Page reading direction used by the content-filter band fallback. "
            "Use vertical for traditional Japanese right-to-left columns."
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
        default=os.getenv("CODING_PLAN_VISION_MCP_COMMAND", "npx -y @z_ai/mcp-server@latest"),
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
    parser.add_argument("--force", action="store_true", help="Re-run cached OCR/translation for the requested pages.")
    parser.add_argument("--skip-ocr", action="store_true", help="Require already imported/cached page OCR.")
    parser.add_argument("--import-ocr-dir", default=None, help="Import legacy extracted_pages.json or _checkpoints first.")
    parser.add_argument("--front-matter-pages", type=int, default=40)
    parser.add_argument("--toc-pages", default=None, help="Confirmed PDF TOC pages, e.g. 6-10,12.")
    parser.add_argument("--toc-json", default=None, help="Use a manually prepared TOC JSON instead of calling the LLM.")
    parser.add_argument("--page-offset", type=int, default=None, help="PDF page minus printed page; auto-detected when omitted.")
    parser.add_argument("--granularity", choices=["chapter", "section", "subsection", "all"], default="chapter")
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
    parser.add_argument("--no-epub", action="store_true")
    parser.add_argument("--no-docx", action="store_true")
    parser.add_argument("--no-kb", action="store_true")
    parser.add_argument("--no-bookmarked-pdf", action="store_true")
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


def output_status(
    output_dir: Path,
    *,
    expected_translation_identity: ModelIdentity | None = None,
) -> dict[str, Any]:
    records = load_page_records(output_dir)
    toc_path = output_dir / "toc.json"
    chapters_path = output_dir / "chapters.json"
    artifacts = sorted(
        path.name
        for path in output_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in {".epub", ".docx", ".pdf", ".jsonl"}
    ) if output_dir.exists() else []
    return {
        "output_dir": str(output_dir),
        "pages": len(records),
        "ocr_models": dict(sorted(Counter(record.ocr_model for record in records).items())),
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
        "artifacts": artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    load_env_file(Path(__file__).with_name(".env"))
    parser = build_parser()
    args = parser.parse_args(argv)
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
    elif args.ocr_profile or args.toc_profile or args.translation_profile:
        parser.error("--ocr-profile/--toc-profile/--translation-profile require --config.")
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
        translation_profile = (
            profile_config.for_stage("translation", args.translation_profile)
            if profile_config is not None
            else None
        )
    except ValueError as exc:
        parser.error(str(exc))
    if (
        toc_profile is not None
        and toc_profile.adapter not in {"openai-chat", "glm-chat"}
    ):
        parser.error(
            f"TOC profile {toc_profile.name!r} requires an OpenAI-compatible chat adapter."
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

    pdf_required = args.phase in {"all", "ocr", "toc", "compile"}
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

    if args.phase == "status":
        print(
            json.dumps(
                output_status(
                    output_dir,
                    expected_translation_identity=expected_translation_identity,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    book_title = args.title or (
        pdf_path.stem if pdf_path is not None else output_dir.name
    )
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
    if ocr_workers < 1 or translation_workers < 1:
        parser.error("OCR and translation worker counts must both be positive.")
    if args.translation_max_chars < 1:
        parser.error("--translation-max-chars must be positive.")
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
                    request_delay=args.ocr_delay,
                )
            records = normalize_cached_page_records(output_dir, records)
            records = load_page_records(output_dir)
            if args.phase == "ocr":
                print(f"[done] page OCR: {output_dir / 'pages'}")
                return 0
        records = load_page_records(output_dir)
        normalization_records = records
        if args.phase == "translate":
            # A targeted translation run must not rewrite unrelated pages.
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
            )
            write_json(toc_path, toc_payload)
            print(f"[toc] entries={len(toc_payload['entries'])} offset={toc_payload['page_offset']} path={toc_path}")
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
            if args.required_ocr_model_prefix:
                required_prefixes = tuple(
                    prefix.strip()
                    for prefix in args.required_ocr_model_prefix.split(",")
                    if prefix.strip()
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
                        f"OCR model prefix {args.required_ocr_model_prefix!r} is required, "
                        f"but cached pages do not match: {preview}{suffix}"
                    )
            toc_payload = load_toc(toc_path)
            if not isinstance(toc_payload.get("page_offset"), int) or args.page_offset is not None:
                toc_payload = apply_page_mapping(
                    toc_payload,
                    records,
                    page_offset=args.page_offset,
                    source_page_count=pdf_page_count,
                )
                write_json(toc_path, toc_payload)
            manifest, knowledge_rows = compile_chapters(
                pdf_path,
                output_dir,
                records,
                toc_payload,
                granularity=args.granularity,
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
                )
            if not args.no_bookmarked_pdf:
                build_bookmarked_pdf(
                    pdf_path,
                    output_dir / f"{slugify(book_title)}_带目录.pdf",
                    toc_payload,
                )
            print(f"[done] chapters={len(manifest)} output={output_dir}")
        return 0
    except Exception as exc:
        print(f"[error] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if ocr_backend is not None:
            ocr_backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
