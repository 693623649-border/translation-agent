"""Concurrent, hash-bound DeepSeek runner for semantic EPUB units."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable
import urllib.error
import urllib.request

from pipeline_profiles import ModelProfile, load_pipeline_profiles


RUNNER_VERSION = "semantic-deepseek-batch-v3"
TOKEN_PATTERN = re.compile(
    r"\[\^[^\]\s]+\]"
    r"|\[\[[A-Z]+:[^\]]+\]\]"
    r"|(?<=\]\()[^)\s]+(?=\))"
    r"|</?[A-Za-z][^>]*>"
)
UNIT_START = "⟦UNIT:{unit_id}:START⟧"
UNIT_END = "⟦UNIT:{unit_id}:END⟧"


class SemanticTranslationError(ValueError):
    pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SemanticTranslationError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(item, dict):
            raise SemanticTranslationError(f"unit must be an object: {path}:{line_number}")
        records.append(item)
    return records


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE secrets without executing the file as shell code."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = value


def load_glossary(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip() for k, v in value.items()):
        raise SemanticTranslationError("glossary must be a string-to-string JSON object")
    return {k.strip(): v.strip() for k, v in value.items()}


def protect_tokens(markdown: str) -> tuple[str, tuple[str, ...]]:
    tokens: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"⟦SEMANTIC_TOKEN_{len(tokens) - 1:04d}⟧"

    return TOKEN_PATTERN.sub(replace, markdown), tuple(tokens)


def restore_tokens(value: str, tokens: Iterable[str]) -> str:
    result = value.strip()
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>[\s\S]*?)\n```", result, flags=re.I)
    if fenced:
        result = fenced.group("body").strip()
    token_list = tuple(tokens)
    found = re.findall(r"⟦SEMANTIC_TOKEN_(\d{4})⟧", result)
    expected = [f"{index:04d}" for index in range(len(token_list))]
    if found != expected:
        raise SemanticTranslationError(f"model changed protected token order/count: expected={expected}, actual={found}")
    for index, token in enumerate(token_list):
        result = result.replace(f"⟦SEMANTIC_TOKEN_{index:04d}⟧", token)
    return result.strip()


def _validate_units(units: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    previous: tuple[str, int] | None = None
    for unit in units:
        unit_id = str(unit.get("id") or "")
        source = str(unit.get("source_markdown") or "")
        key = (str(unit.get("chapter_id") or ""), int(unit.get("sequence") or 0))
        if not unit_id or unit_id in seen or hashlib.sha256(source.encode()).hexdigest() != unit.get("source_sha256"):
            raise SemanticTranslationError(f"duplicate id or stale source hash: {unit_id!r}")
        if previous is not None and key[0] == previous[0] and key[1] != previous[1] + 1:
            raise SemanticTranslationError(f"non-contiguous chapter units: {key[0]}")
        seen.add(unit_id); previous = key


def batch_units(units: list[dict[str, Any]], *, max_chars: int = 9000) -> list[list[dict[str, Any]]]:
    if max_chars < 1:
        raise SemanticTranslationError("max_chars must be positive")
    _validate_units(units)
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for unit in units:
        size = len(str(unit["source_markdown"])) + len(str(unit["id"])) * 2 + 64
        if current and (str(unit["chapter_id"]) != str(current[-1]["chapter_id"]) or current_chars + size > max_chars):
            batches.append(current); current = []; current_chars = 0
        current.append(unit); current_chars += size
    if current:
        batches.append(current)
    return batches


def build_prompt(batch: list[dict[str, Any]], *, target_language: str, glossary: dict[str, str]) -> tuple[str, list[tuple[str, tuple[str, ...]]]]:
    sections: list[str] = []
    protected: list[tuple[str, tuple[str, ...]]] = []
    for unit in batch:
        unit_id = str(unit["id"])
        body, tokens = protect_tokens(str(unit["source_markdown"]))
        protected.append((unit_id, tokens))
        sections.append(f"{UNIT_START.format(unit_id=unit_id)}\n{body}\n{UNIT_END.format(unit_id=unit_id)}")
    terms = "\n".join(f"- {a} => {b}" for a, b in glossary.items()) or "- 无"
    return (
        f"将以下英文图书 Markdown 完整、忠实地翻译为{target_language}，采用严谨、流畅的学术出版文体。\n"
        "不得摘要、删节、扩写或评论；正文、引文、标题、表格、图注和脚注都必须逐项翻译。\n"
        "只输出由 UNIT 标记包围的译文，不要代码围栏、说明、译者按或其他附加文字。\n"
        "UNIT 标记、SEMANTIC_TOKEN 占位符、标题层级、强调、列表、表格、链接和脚注结构必须原样且顺序不变。\n"
        "保持每个 UNIT 独立，不得跨 UNIT 合并、拆分或移动内容。修复明显的电子书断词或字母间误空格，但不猜改含义。\n"
        "书目与脚注引文中的作者名、原文书名/篇名、期刊名、出版信息和页码须准确保留；其解释性文字仍须翻译。\n"
        "索引概念词可翻译，人名、原文书名和定位页码须保持可识别且不得遗漏。\n"
        "使用中国大陆通行的简体字、中文标点和既定学术译名；同一术语和人名在全书中保持一致。\n"
        f"术语表：\n{terms}\n\n" + "\n\n".join(sections),
        protected,
    )


def _batch_key(batch: list[dict[str, Any]], *, model: str, target_language: str, glossary: dict[str, str]) -> str:
    payload = {"runner": RUNNER_VERSION, "units": [(u["id"], u["source_sha256"]) for u in batch], "model": model, "target": target_language, "glossary": glossary}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _parse_batch(raw: str, protected: list[tuple[str, tuple[str, ...]]], batch: list[dict[str, Any]], *, target_language: str) -> list[str]:
    cursor = 0
    values: list[str] = []
    for (unit_id, tokens), unit in zip(protected, batch):
        start = UNIT_START.format(unit_id=unit_id); end = UNIT_END.format(unit_id=unit_id)
        start_at = raw.find(start, cursor)
        if start_at < 0 or raw.find(start, start_at + len(start)) >= 0:
            raise SemanticTranslationError(f"missing or duplicate UNIT start marker: {unit_id}")
        end_at = raw.find(end, start_at + len(start))
        if end_at < 0:
            raise SemanticTranslationError(f"missing UNIT end marker: {unit_id}")
        value = restore_tokens(raw[start_at + len(start):end_at], tokens)
        source = str(unit["source_markdown"])
        if not value or len(value) < max(1, int(len(source) * 0.12)) or len(value) > max(300, int(len(source) * 4.5)):
            raise SemanticTranslationError(f"implausible translated length: {unit_id}")
        if target_language == "简体中文" and re.search(r"[A-Za-z]{8,}", source) and not re.search(r"[\u4e00-\u9fff]", value):
            raise SemanticTranslationError(f"translated unit has no Chinese coverage: {unit_id}")
        values.append(value); cursor = end_at + len(end)
    if re.search(r"⟦UNIT:[^:]+:(?:START|END)⟧", raw[cursor:]):
        raise SemanticTranslationError("unexpected trailing UNIT marker")
    return values


def translate_units(units_path: Path, output_path: Path, *, target_language: str = "简体中文", glossary: dict[str, str] | None = None, model: str = "deepseek-v4-flash", cache_dir: Path | None = None, request: Callable[[str], str] | None = None, max_chars: int = 9000, concurrency: int = 16, retries: int = 3, progress: Callable[[int, int, bool], None] | None = None) -> dict[str, Any]:
    glossary = glossary or {}; units = _read_jsonl(units_path); batches = batch_units(units, max_chars=max_chars)
    prepared = []
    for index, batch in enumerate(batches):
        prompt, protected = build_prompt(batch, target_language=target_language, glossary=glossary)
        key = _batch_key(batch, model=model, target_language=target_language, glossary=glossary)
        prepared.append((index, batch, prompt, protected, key))
    if request is None:
        rows = [{"batch": index + 1, "unit_ids": [u["id"] for u in batch], "model": model, "target_language": target_language, "prompt": prompt, "cache_key": key} for index, batch, prompt, _, key in prepared]
        _atomic_text(output_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        return {"status": "passed", "mode": "prepare", "unit_count": len(units), "batch_count": len(batches), "cache_hits": 0, "output": str(output_path.resolve())}

    def execute(item: tuple[int, list[dict[str, Any]], str, list[tuple[str, tuple[str, ...]]], str]) -> tuple[int, list[dict[str, Any]], bool]:
        index, batch, prompt, protected, key = item

        def run_batch(
            current_batch: list[dict[str, Any]],
            current_prompt: str,
            current_protected: list[tuple[str, tuple[str, ...]]],
            current_key: str,
        ) -> tuple[list[dict[str, Any]], bool]:
            cache_path = cache_dir / f"{current_key}.json" if cache_dir else None
            cached = bool(cache_path and cache_path.is_file())
            last_error: Exception | None = None
            for attempt in range(1, retries + 1):
                try:
                    if cached:
                        raw = str(json.loads(cache_path.read_text(encoding="utf-8"))["model_output"])
                    else:
                        raw = request(current_prompt)
                    values = _parse_batch(raw, current_protected, current_batch, target_language=target_language)
                    if cache_path and not cached:
                        _atomic_text(cache_path, json.dumps({"cache_key": current_key, "model_output": raw}, ensure_ascii=False) + "\n")
                    rows = [{"schema_version": u.get("schema_version", 1), "id": u["id"], "chapter_id": u["chapter_id"], "sequence": u["sequence"], "source_sha256": u["source_sha256"], "translated_markdown": value, "translation_model": model, "translation_cache_key": current_key} for u, value in zip(current_batch, values)]
                    return rows, cached
                except Exception as exc:
                    last_error = exc
                    if cached:
                        raise SemanticTranslationError(f"invalid batch cache {current_key}: {exc}") from exc
                    if attempt < retries:
                        time.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))

            if len(current_batch) > 1 and isinstance(last_error, SemanticTranslationError):
                midpoint = len(current_batch) // 2
                split_rows: list[dict[str, Any]] = []
                split_cached = True
                for child in (current_batch[:midpoint], current_batch[midpoint:]):
                    child_prompt, child_protected = build_prompt(child, target_language=target_language, glossary=glossary)
                    child_key = _batch_key(child, model=model, target_language=target_language, glossary=glossary)
                    child_rows, child_cached = run_batch(child, child_prompt, child_protected, child_key)
                    split_rows.extend(child_rows)
                    split_cached = split_cached and child_cached
                return split_rows, split_cached
            raise SemanticTranslationError(f"batch failed after {retries} attempts: {last_error}")

        rows, cached = run_batch(batch, prompt, protected, key)
        return index, rows, cached

    completed: dict[int, list[dict[str, Any]]] = {}; cache_hits = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
        futures = [executor.submit(execute, item) for item in prepared]
        for future in as_completed(futures):
            index, rows, cached = future.result(); completed[index] = rows; cache_hits += int(cached)
            if progress is not None:
                progress(len(completed), len(batches), cached)
    results = [row for index in range(len(batches)) for row in completed[index]]
    _atomic_text(output_path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results))
    return {"status": "passed", "mode": "run", "unit_count": len(results), "batch_count": len(batches), "cache_hits": cache_hits, "output": str(output_path.resolve())}


def _deepseek_request(*, api_key: str, base_url: str, model: str, timeout: int, thinking: str = "disabled", max_tokens: int = 32768) -> Callable[[str], str]:
    def request(prompt: str) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": "你是严谨的英译中学术图书译者，必须完整保留输入的语义和结构契约。",
                },
                {"role": "user", "content": prompt},
            ],
        }
        if thinking != "omit":
            payload["thinking"] = {"type": thinking}
        endpoint = f"{base_url.rstrip('/')}/chat/completions"
        http_request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1200]
            raise SemanticTranslationError(f"DeepSeek HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SemanticTranslationError(f"DeepSeek request failed: {exc}") from exc
        try:
            value = response_payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SemanticTranslationError("DeepSeek response has no message content") from exc
        if not isinstance(value, str) or not value.strip():
            raise SemanticTranslationError("DeepSeek returned empty content")
        return value
    return request


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch translate semantic Markdown with protected structure.")
    parser.add_argument("command", choices=("prepare", "run")); parser.add_argument("units"); parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--target-language", default="简体中文"); parser.add_argument("--glossary")
    parser.add_argument("--config"); parser.add_argument("--translation-profile")
    parser.add_argument("--model", default=None); parser.add_argument("--cache-dir", default=".translation-cache")
    parser.add_argument("--api-key-env", default=None); parser.add_argument("--api-base", default=None)
    parser.add_argument("--env-file", default=str(Path(__file__).with_name(".env")))
    parser.add_argument("--max-chars", type=int, default=9000); parser.add_argument("--concurrency", type=int, default=None); parser.add_argument("--retries", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv); profile: ModelProfile | None = None
    load_env_file(Path(args.env_file).expanduser())
    if args.config:
        profile = load_pipeline_profiles(args.config).for_stage("translation", args.translation_profile)
    model = args.model or (profile.model if profile else "deepseek-v4-flash")
    base_url = args.api_base or (profile.base_url if profile else "https://api.deepseek.com")
    credential_env = args.api_key_env or (profile.credential_env if profile else "DEEPSEEK_API_KEY")
    concurrency = args.concurrency or (profile.concurrency if profile else 16)
    request = None
    if args.command == "run":
        api_key = os.getenv(credential_env, "")
        if not api_key:
            raise SystemExit(f"missing credential environment variable: {credential_env}")
        request = _deepseek_request(api_key=api_key, base_url=base_url, model=model, timeout=profile.timeout if profile else 120, thinking=profile.thinking if profile else "disabled")
    result = translate_units(
        Path(args.units).expanduser(),
        Path(args.output).expanduser(),
        target_language=args.target_language,
        glossary=load_glossary(Path(args.glossary).expanduser() if args.glossary else None),
        model=model,
        cache_dir=Path(args.cache_dir).expanduser(),
        request=request,
        max_chars=args.max_chars,
        concurrency=concurrency,
        retries=args.retries,
        progress=(lambda done, total, cached: print(f"[semantic-translate] {done}/{total} batches ({'cache' if cached else 'model'})", file=sys.stderr, flush=True)) if args.command == "run" else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
