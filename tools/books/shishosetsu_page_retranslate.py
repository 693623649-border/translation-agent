"""Page-level anti-elision retranslation for 私小説論.

The bulk page translation ran without the anti-elision directive, so ~791 of
the 824 ellipsis pairs in the Chinese text were invented by the model to paper
over OCR damage, plus 85 [原文存疑] markers.  This pass retranslates every
flagged page with:

  * the previous page's tail as read-only context, so a page that starts
    mid-sentence is continued instead of elided;
  * the strict anti-elision directive;
  * a per-page write, so an interrupted run keeps finished work;
  * concurrency 24 (the deepseek_flash profile allows 32).

Reasoning is ON (project policy), so throughput is ~60 s/page even in
parallel; progress prints every 10 pages.
"""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kb_translation import DeepSeekTranslator

PAGE_DIRECTIVE = (
    "【本书为 OCR 扫描件，对译文另有硬性要求】\n"
    "1. 源文本存在 OCR 缺字、形近误字与句子交错。请依据上下文推断原意并完整译出，不得跳过、概括或缩写。\n"
    "2. （最重要）严禁用省略号（……）概括、省略或跳过任何内容。原文本没有省略号的地方绝不允许出现省略号；原文自带的省略号原样保留。\n"
    "3. 某处确实完全无法辨认时，用（原文缺损）四字标注，不得用省略号或留空，也不得写“原文存疑”。\n"
    "4. 逐句对译：原文每一句都必须在译文中有一句对应。\n"
    "5. 译文中不得残留日文假名；引文、书名、术语译成中文，必要时括号附原文。\n"
    "6. 人名、书名、论文名称使用通行中文译名。\n"
    "7. 若页面以半句开头（上一页的延续），直接顺势续译，不要补全省略号或重起句子。"
)

ROOT = Path(__file__).resolve().parents[2] / "outputs" / "私小説論"
PAGES_DIR = ROOT / "pages"


def _reference_fingerprint() -> str:
    """Profile fingerprint of the current identity, copied from a page the
    pipeline itself signed — every fresh page of the book carries the same
    value, and the compile-time cache contract pins it per page."""
    for path in sorted(PAGES_DIR.glob("page_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            record.get("translation_model") == "deepseek-flash"
            and record.get("translation_prompt_version") == "book-translation-v5"
            and record.get("translation_fingerprint")
        ):
            return str(record["translation_fingerprint"])
    raise RuntimeError("no fresh deepseek-flash/v5 page found to copy the fingerprint from")


REFERENCE_FINGERPRINT = _reference_fingerprint()

LOCK = threading.Lock()


def load_records():
    records = {}
    for path in sorted(PAGES_DIR.glob("page_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        records[int(record["pdf_page"])] = record
    return records


def page_needs_fix(record) -> bool:
    source = (record.get("text") or "").strip()
    translated = (record.get("translated_text") or "").strip()
    if len(source) < 30:
        return False
    # Stale identity counts: a page signed by a retired model/prompt is
    # compiled from the raw Japanese source, whatever its ellipsis count.
    if (
        record.get("translation_model") != "deepseek-flash"
        or record.get("translation_prompt_version") != "book-translation-v5"
        or record.get("translation_fingerprint") != REFERENCE_FINGERPRINT
    ):
        return True
    excess = translated.count("……") - source.count("……")
    return excess > 0 or "[原文存疑]" in translated or "原文即此" in translated


def main() -> int:
    records = load_records()
    numbers = sorted(n for n, r in records.items() if page_needs_fix(r))
    print(f"pages to retranslate: {len(numbers)}", flush=True)

    translator = DeepSeekTranslator()
    started = time.time()
    done = 0
    written = 0
    rejected: list[tuple[int, str]] = []
    errors: list[tuple[int, str]] = []

    def fix(number: int) -> None:
        nonlocal done, written
        record = records[number]
        source = record["text"].strip()
        context = ""
        previous = records.get(number - 1)
        if previous:
            tail = (previous.get("text") or "").strip()[-160:]
            if tail:
                context = (
                    "（前页末尾的日文原文，仅供理解接续，不要翻译或输出它）\n"
                    f"…{tail}\n\n"
                )
        prompt = (
            f"{PAGE_DIRECTIVE}\n\n{context}"
            f"把下面的日文页面完整翻译为简体中文：\n\n{source}"
        )
        try:
            out = translator.translate(prompt).strip()
        except Exception as exc:  # noqa: BLE001 - batch must survive one page
            with LOCK:
                errors.append((number, f"{type(exc).__name__}: {exc}"[:120]))
                done += 1
            return
        excess = out.count("……") - source.count("……")
        if excess > 0 or "[原文存疑]" in out:
            with LOCK:
                rejected.append((number, f"excess={excess}"))
                done += 1
            return
        record["translated_text"] = out
        record["translation_model"] = translator.model
        record["translation_provider"] = "deepseek"
        record["translation_prompt_version"] = "book-translation-v5"
        record["translation_fingerprint"] = REFERENCE_FINGERPRINT
        (PAGES_DIR / f"page_{number:04d}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        with LOCK:
            written += 1
            done += 1
            if done % 10 == 0 or done == len(numbers):
                rate = done / max(time.time() - started, 1)
                print(
                    f"  {done}/{len(numbers)} done {time.time() - started:.0f}s "
                    f"({rate:.2f} pages/s), written={written}",
                    flush=True,
                )

    with ThreadPoolExecutor(max_workers=24) as pool:
        list(pool.map(fix, numbers))

    print(
        f"finished: written={written} rejected={len(rejected)} errors={len(errors)} "
        f"in {time.time() - started:.0f}s",
        flush=True,
    )
    for number, reason in rejected[:12]:
        print("  REJECT", number, reason, flush=True)
    for number, reason in errors[:12]:
        print("  ERROR", number, reason, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
