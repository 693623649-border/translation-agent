"""Retranslate selected paragraphs of a delivered _中文 DOCX, in place.

A first-pass translation of an OCR-damaged scan can elide the fragments it
cannot parse, leaving bare ``……`` where the source has none — the reader sees
half-sentences and silently lost plot.  This tool pairs the Japanese source
DOCX with its ``_中文`` translation (same paragraph count, same order — the
translation was written paragraph-by-paragraph in place), re-sends the flagged
paragraphs with an anti-elision directive, and writes the results back into the
translation DOCX without touching styles or the source file.

Flagging criteria (each alone qualifies):
  * excess ellipses: the translation holds two or more more ``……`` than the
    source paragraph — ellipses the source never had;
  * shrinkage: the translation is under 45% the source length while the source
    is at least 60 characters (prose vanished without any marker).

The rewrite is validated per paragraph: the new translation may keep the
source's own ellipses but may not add any.  Offenders are retried once
individually with a stricter directive; survivors keep their old text and are
listed in the report so a human can look at them.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from docx import Document

from docx_translation import _paragraph_text, _write_paragraph
from kb_translation import DeepSeekTranslator, KbTranslationError, translate_texts

DIRECTIVE = (
    "【本书为 OCR 扫描件，对译文另有硬性要求】\n"
    "1. 源文本存在 OCR 缺字与形近误字（如力↔カ、漏假名或汉字、句子残缺）。"
    "请依据上下文推断原意并完整译出，不得因文字残缺而跳过、概括或缩写。\n"
    "2. 严禁用省略号（……）概括、省略或跳过任何原文内容。原文没有省略号的地方，译文绝不允许出现省略号。\n"
    "3. 某处确实完全无法辨认时，用（原文此处缺损）标注，不得用省略号或留空代替。\n"
    "4. 原文中本来就有的省略号必须原样保留其位置与数量。\n"
    "5. 逐句对译：原文的每一句话都必须在译文中有一句对应，不得合并、删减或杜撰情节。\n"
    "6. 译文中不得残留日文假名（ひらがな/カタカナ）；日语引文、惯用语、书名一律译成中文，需要时在括号里附上原文。"
)
STRICT_DIRECTIVE = DIRECTIVE.replace(
    "2. 严禁用省略号（……）概括、省略或跳过任何原文内容。",
    "2. （最重要）译文中绝对不允许出现任何省略号。宁可译出最可能的意思，也绝不允许以任何理由使用省略号。",
)


def paragraph_ellipsis(text: str) -> int:
    return text.count("……")


def flag_paragraphs(
    jp_texts: list[str], zh_texts: list[str]
) -> tuple[list[int], list[int]]:
    """Return (must_fix, borderline) paragraph indices."""

    must: list[int] = []
    borderline: list[int] = []
    for index, (jp, zh) in enumerate(zip(jp_texts, zh_texts, strict=True)):
        if not zh.strip() or not jp.strip():
            continue
        excess = paragraph_ellipsis(zh) - paragraph_ellipsis(jp)
        if excess >= 2:
            must.append(index)
        elif excess == 1:
            borderline.append(index)
        elif len(zh) / max(len(jp), 1) < 0.45 and len(jp) >= 60:
            must.append(index)
    return must, borderline


def validate(text: str, jp_text: str) -> int:
    """Return the ellipsis excess of a candidate translation (0 is a pass)."""

    return max(0, paragraph_ellipsis(text) - paragraph_ellipsis(jp_text))


def retranslate(
    jp_texts: list[str],
    indices: list[int],
    directive: str,
    *,
    batch_chars: int,
    concurrency: int,
) -> list[str]:
    texts = [jp_texts[i] for i in indices]
    return translate_texts(
        texts,
        DeepSeekTranslator(),
        batch_chars=batch_chars,
        concurrency=concurrency,
        extra_instructions=directive,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Japanese source DOCX")
    parser.add_argument("--target", required=True, help="_中文 translation DOCX to repair in place")
    parser.add_argument("--indices", default="", help="comma-separated paragraph indices; default auto-detect")
    parser.add_argument("--batch-chars", type=int, default=8000)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--report", required=True, help="path of the JSON report to write")
    args = parser.parse_args()

    source_path = Path(args.source)
    target_path = Path(args.target)
    if not source_path.exists() or not target_path.exists():
        print("source or target DOCX missing", file=sys.stderr)
        return 2

    jp_doc = Document(str(source_path))
    zh_doc = Document(str(target_path))
    jp_texts = [_paragraph_text(p) for p in jp_doc.paragraphs]
    zh_texts = [_paragraph_text(p) for p in zh_doc.paragraphs]
    if len(jp_texts) != len(zh_texts):
        print(
            f"paragraph count mismatch: source={len(jp_texts)} target={len(zh_texts)}",
            file=sys.stderr,
        )
        return 2

    if args.indices:
        indices = [int(item) for item in args.indices.split(",") if item.strip()]
    else:
        must, _borderline = flag_paragraphs(jp_texts, zh_texts)
        indices = must
    print(f"paragraphs to retranslate: {len(indices)} -> {indices}")

    report: dict[str, Any] = {
        "source": str(source_path),
        "target": str(target_path),
        "paragraphs": len(jp_texts),
        "requested": indices,
        "rounds": [],
        "survivors": [],
        "changed": [],
    }
    pending = list(indices)
    for round_no in range(1, 4):
        if not pending:
            break
        directive = DIRECTIVE if round_no == 1 else STRICT_DIRECTIVE
        started = time.time()
        try:
            outputs = retranslate(
                jp_texts, pending, directive,
                batch_chars=args.batch_chars, concurrency=args.concurrency,
            )
        except KbTranslationError as exc:
            print(f"round {round_no} translation contract failed: {exc}", file=sys.stderr)
            return 1
        still_pending: list[int] = []
        round_report: dict[str, Any] = {"round": round_no, "sent": len(pending), "seconds": round(time.time() - started, 1), "rejected": []}
        for index, candidate in zip(pending, outputs, strict=True):
            excess = validate(candidate, jp_texts[index])
            if excess:
                round_report["rejected"].append({"index": index, "excess_ellipsis": excess})
                still_pending.append(index)
                continue
            if candidate.strip() and candidate.strip() != zh_texts[index].strip():
                _write_paragraph(zh_doc.paragraphs[index], candidate)
                zh_texts[index] = candidate
                if index not in report["changed"]:
                    report["changed"].append(index)
        report["rounds"].append(round_report)
        pending = still_pending
    report["survivors"] = pending

    if report["changed"]:
        backup = target_path.with_name(target_path.stem + "_backup.docx")
        if not backup.exists():
            shutil.copy2(target_path, backup)
            report["backup"] = str(backup)
        zh_doc.save(str(target_path))
        report["saved"] = True
    else:
        report["saved"] = False

    report["borderline_not_touched"] = flag_paragraphs(jp_texts, zh_texts)[1]
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"done: changed={len(report['changed'])} survivors={report['survivors']} "
        f"report={args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
