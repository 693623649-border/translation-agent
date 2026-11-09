#!/usr/bin/env python3
"""用 Magpie cheap-first 视觉路由把影印竖排 PDF 逐页转成 OCR 检查点。

数据流：
PDF 页 --(PyMuPDF 200 DPI 渲染)--> JPEG 字节
       --(base64 + 竖排日文转写提示)--> Magpie /v1/chat/completions
       --(清洗模型附加说明、检测空页/截断)--> PageRecord
       --(PageStore 原子写入)--> outputs/<book>/pages/page_XXXX.json + .md

脚本只负责 OCR 检查点；目录、校勘、翻译、章节和知识库仍由现有 Graph/CLI
读取这些检查点后继续，避免把 OCR 与发布逻辑耦合在一起。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pymupdf
from PIL import Image

# 允许以 `python tools/magpie_ocr.py` 直接运行：优先使用已安装的包，
# 否则把 checkout 根目录加入 sys.path，避免调用方必须手工设置 PYTHONPATH。
try:
    from translation_agent.pipeline.book_pipeline import (
        PageRecord,
        clean_ocr_text,
        detect_language,
        is_visually_blank_page,
        save_page_record,
    )
except ModuleNotFoundError:  # pragma: no cover - 仅在未安装项目时触发
    _ROOT = Path(__file__).resolve().parents[1]
    if (_ROOT / "translation_agent").is_dir():
        sys.path.insert(0, str(_ROOT))
    from translation_agent.pipeline.book_pipeline import (
        PageRecord,
        clean_ocr_text,
        detect_language,
        is_visually_blank_page,
        save_page_record,
    )


OCR_MODEL = "magpie/group/cheap-first/vision-v1"
PROMPT_VERSION = "vertical-ja-v1"
DEFAULT_PROMPT = """This is a scanned vertical Japanese book page. Transcribe all visible Japanese text in correct reading order (right-to-left columns, top-to-bottom; for multi-tier pages read each tier right-to-left). Preserve punctuation and line breaks. Do not translate, summarize, explain, or add commentary. Do not use Markdown fences. Output only the transcription. If the page has no readable text, output exactly: [BLANK]"""
RETRY_PROMPT = """This is a scanned vertical Japanese book page. Some text may be faint, small, or partly obscured by the scan. Transcribe every readable Japanese character in correct reading order (right-to-left columns, top-to-bottom; for multi-tier pages read each tier right-to-left). Preserve punctuation and line breaks. Do not translate, summarize, explain, or add commentary. Do not use Markdown fences. Output only the transcription. If the page is truly blank, output exactly: [BLANK]"""


def load_magpie_endpoint(config_path: Path) -> tuple[str, str]:
    """从 Magpie 本机配置读取 PC 网关地址和凭据，凭据不写日志。"""

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    provider = next(item for item in payload["providers"] if item.get("id") == "pc")
    key = str(provider.get("key") or "").strip()
    base = str(provider.get("chat") or "").strip().rstrip("/")
    if not key or not base:
        raise RuntimeError("Magpie PC provider is missing key or chat base URL")
    return base, key


def parse_pages(value: str, page_count: int) -> list[int]:
    """解析 1-10,50,100 形式的页号，保持升序并去重。"""

    pages: set[int] = set()
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = (int(part) for part in chunk.split("-", 1))
            pages.update(range(start, end + 1))
        else:
            pages.add(int(chunk))
    invalid = sorted(page for page in pages if page < 1 or page > page_count)
    if invalid:
        raise ValueError(f"pages outside 1..{page_count}: {invalid[:10]}")
    return sorted(pages)


def render_page(document: pymupdf.Document, page_number: int, dpi: int) -> bytes:
    """把单页渲染成 JPEG；200 DPI 在清晰度与视觉请求体积之间较稳。"""

    page = document.load_page(page_number - 1)
    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return pixmap.tobytes("jpeg", jpg_quality=92)


def response_content(response: dict) -> tuple[str, str]:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content), str(choice.get("finish_reason") or "")


def transcribe_once(
    *,
    base_url: str,
    api_key: str,
    image_bytes: bytes,
    prompt: str,
    max_tokens: int,
    timeout: int,
) -> tuple[str, str, str]:
    """一次视觉转写请求；返回 (文本, finish_reason, 实际模型)。"""

    payload = {
        "model": "group/cheap-first",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(image_bytes).decode("ascii")
                        },
                    },
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    request = urllib.request.Request(
        base_url + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    content, finish_reason = response_content(data)
    return content, finish_reason, str(data.get("model") or "group/cheap-first")


def clean_model_text(text: str) -> str:
    """只去掉模型包装/说明；正文逐字保留，交给管线后续校勘。"""

    cleaned = clean_ocr_text(text)
    cleaned = re.sub(r"^```(?:text|markdown|md|plaintext)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


# 模型拒答/元回答的特征。这类文本不是页面转写，必须重试而不是写入检查点。
REFUSAL_RE = re.compile(
    r"(unable to (transcribe|read|see|process|access)"
    r"|cannot (transcribe|read|see|process)"
    r"|can't (transcribe|read|see|process)"
    r"|image (itself )?(isn't|is not) visible"
    r"|not visible to me"
    r"|no readable (characters|text)"
    r"|as an AI|I'm sorry|I am sorry"
    r"|无法(转写|识别|读取)|抱歉)",
    re.IGNORECASE,
)
CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def reject_non_transcription(text: str) -> None:
    """拒绝模型拒答、版面描述等非转写输出；抛出可重试错误。

    视觉模型偶发返回“我无法转写这一页”“图像不可见”之类的元回答。若把它
    当作 OCR 正文写入检查点，该页正文会被静默替换，且下游完整性检查仍会通过。
    """

    if REFUSAL_RE.search(text):
        raise RuntimeError(
            "vision model returned a refusal/meta answer instead of a transcription: "
            f"{text[:160]!r}"
        )
    if not CJK_RE.search(text) and len(re.findall(r"[A-Za-z]", text)) >= 20:
        # 日文书页的正文必然含假名或汉字；纯拉丁文本基本只可能是模型自述。
        raise RuntimeError(
            "vision model returned Latin-only text for a Japanese page: "
            f"{text[:160]!r}"
        )


def transcribe_page(
    *,
    pdf_path: Path,
    page_number: int,
    output_dir: Path,
    base_url: str,
    api_key: str,
    dpi: int,
    timeout: int,
    max_tokens: int,
    audit_pages: set[int],
    force: bool,
) -> tuple[int, str, str]:
    """转写单页并保存 PageRecord；函数内的重试只针对可恢复的模型响应。"""

    page_path = output_dir / "pages" / f"page_{page_number:04d}.json"
    if page_path.exists() and not force:
        existing = json.loads(page_path.read_text(encoding="utf-8"))
        if str(existing.get("ocr_model") or "").startswith("magpie/group/cheap-first"):
            return page_number, "cached", str(existing.get("text") or "")

    document = pymupdf.open(pdf_path)
    try:
        image_bytes = render_page(document, page_number, dpi)
    finally:
        document.close()

    if page_number in audit_pages:
        (output_dir / "ocr_audit_pages" / f"page_{page_number:04d}.jpg").write_bytes(image_bytes)

    last_error: Exception | None = None
    for attempt in range(1, 5):
        prompt = DEFAULT_PROMPT if attempt == 1 else RETRY_PROMPT
        try:
            raw, finish_reason, resolved_model = transcribe_once(
                base_url=base_url,
                api_key=api_key,
                image_bytes=image_bytes,
                prompt=prompt,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            text = clean_model_text(raw)
            if finish_reason == "length":
                # 输出被截断时只提高一次上限，避免把半页正文写入检查点。
                if max_tokens < 12000:
                    max_tokens = 12000
                    raise RuntimeError("vision response hit max_tokens; retrying with a larger cap")
                raise RuntimeError("vision response still truncated at 12000 tokens")
            if not text:
                raise RuntimeError("empty vision response")
            if text.strip() == "[BLANK]":
                # 模型说空白时用像素墨迹复核；非空白页要求它再读一次。
                temp_image = output_dir / "ocr_audit_pages" / f".blank_check_{page_number:04d}.jpg"
                temp_image.write_bytes(image_bytes)
                try:
                    visually_blank, ink_ratio = is_visually_blank_page(temp_image)
                finally:
                    temp_image.unlink(missing_ok=True)
                if not visually_blank:
                    raise RuntimeError(f"model reported blank but page has ink (ink_ratio={ink_ratio:.6f})")
                text = "[空白页]"
            else:
                # 非空白文本必须通过拒答/元回答校验，防止把“无法转写”写入检查点。
                reject_non_transcription(text)
            language = detect_language(text)
            record = PageRecord(
                pdf_page=page_number,
                text=text,
                language=language,
                notes=(
                    f"source=magpie-cheap-first-vision; prompt={PROMPT_VERSION}; "
                    f"attempt={attempt}; resolved_model={resolved_model}; dpi={dpi}"
                ),
                ocr_model=OCR_MODEL,
            )
            save_page_record(output_dir, record)
            return page_number, "ok", text
        except Exception as exc:  # noqa: BLE001 - 保留最后一次错误并统一重试
            last_error = exc
            if attempt < 4:
                delay = min(45.0, 3.0 * (2 ** (attempt - 1))) + random.uniform(0, 1.5)
                print(
                    f"[ocr-retry] page={page_number} attempt={attempt} "
                    f"error={type(exc).__name__}: {str(exc)[:180]} sleep={delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
    raise RuntimeError(f"page {page_number} failed after retries: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, required=True)
    parser.add_argument("--pages", default="all", help="all or 1-10,50,100")
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument(
        "--magpie-config",
        type=Path,
        default=Path.home() / ".config" / "magpie" / "providers.json",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--audit-pages",
        default="1,2,3,4,5",
        help="逗号分隔的审计保存页码；仅对小于等于实际总页数的页保存原图",
    )
    args = parser.parse_args()

    pdf_path = args.pdf.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "ocr_audit_pages").mkdir(exist_ok=True)

    with pymupdf.open(pdf_path) as document:
        page_count = document.page_count
    pages = list(range(1, page_count + 1)) if args.pages == "all" else parse_pages(args.pages, page_count)
    raw_audit = [int(p) for p in args.audit_pages.split(",") if p.strip().isdigit()]
    audit_pages = {p for p in raw_audit if 1 <= p <= page_count}
    base_url, api_key = load_magpie_endpoint(args.magpie_config)

    started = time.time()
    print(
        f"[magpie-ocr] pages={len(pages)} total={page_count} dpi={args.dpi} "
        f"concurrency={args.concurrency} model=group/cheap-first",
        flush=True,
    )
    completed = 0
    failures: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as executor:
        futures = {
            executor.submit(
                transcribe_page,
                pdf_path=pdf_path,
                page_number=page,
                output_dir=output_dir,
                base_url=base_url,
                api_key=api_key,
                dpi=args.dpi,
                timeout=args.timeout,
                max_tokens=args.max_tokens,
                audit_pages=audit_pages,
                force=args.force,
            ): page
            for page in pages
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_number, status, text = future.result()
                completed += 1
                print(
                    f"[magpie-ocr] page={page_number} status={status} "
                    f"chars={len(text)} completed={completed}/{len(pages)} "
                    f"elapsed={time.time() - started:.1f}s",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 - 汇总所有页失败后统一返回
                failures.append((page, str(exc)))
                print(f"[magpie-ocr-error] page={page}: {exc}", file=sys.stderr, flush=True)

    manifest = {
        "schema_version": 1,
        "source_pdf": str(pdf_path),
        "source_pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "page_count": page_count,
        "processed_pages": pages,
        "ocr_model": OCR_MODEL,
        "prompt_version": PROMPT_VERSION,
        "dpi": args.dpi,
        "concurrency": args.concurrency,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "completed": completed,
        "failures": failures,
    }
    (output_dir / "ocr_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[magpie-ocr] done completed={completed}/{len(pages)} failures={len(failures)}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
