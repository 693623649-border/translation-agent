#!/usr/bin/env python3
"""目标书：柄谷行人ディスカッション（竖排日文影印本）。

源级清理规则：
  逐页剥离版心外的印刷页码（folio）与章节跑眉（runhead）。
  仅匹配完整跑眉行（`^TITLE$` 或带页码组合）与纯数字页码行，保护目录页与章首标题页。
  每端最多剥离一个跑眉行和一个页码行，防止跨行正文被误伤。

审计刷新方式：
  本脚本改动 PageRecord.text 后，各页 effective_text_sha256 更新，已翻译页需重新翻译。
  最终由 graph_pipeline --phase compile 重建章节并刷新
  audit/semantic-reconstruction.json 的 markdown_sha256 与 footnote 契约。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 允许以 `python tools/books/...` 直接运行
try:
    from translation_agent.pipeline.book_pipeline import PageStore, detect_language
except ModuleNotFoundError:  # pragma: no cover
    _ROOT = Path(__file__).resolve().parents[2]
    if (_ROOT / "translation_agent").is_dir():
        sys.path.insert(0, str(_ROOT))
    from translation_agent.pipeline.book_pipeline import PageStore, detect_language

TITLES = [
    "「戦後文学」は鎖国の中でつくられた",
    "〈分裂病〉をめぐって",
    "〈近代の超克〉と西田哲学",
    "芸術の理念と〈日本〉",
    "中上健次をめぐって",
    "「〈戦前〉の思考」",
    "日本文化とジェンダー",
    "ハイパーメディア社会における自己・視線・権力",
    "冷戦終結後の政治と文学",
    "理念、社会、共同体",
]


def _is_frame_title_line(line: str) -> bool:
    """严格判断是否为独立的章题跑眉行，拒绝正文中的句内引用。

    必须满足：整行即为章题，或整行仅包含章题加印刷页码/空白；不允许含有
    正文常见助词/动词（如“という”“について”“を論じる”）。
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 40:
        return False
    # 纯章题匹配
    for t in TITLES:
        if stripped == t:
            return True
        # 章题 + 页码 或 页码 + 章题（版心常见形态）
        m1 = re.fullmatch(rf"(\d{{1,4}})\s+{re.escape(t)}", stripped)
        m2 = re.fullmatch(rf"{re.escape(t)}\s+(\d{{1,4}})", stripped)
        if m1 or m2:
            return True
    return False


def _is_pure_folio_line(line: str, page_number: int) -> bool:
    """严格判断是否为纯数字页码行，拒绝正文以数字开头的句子。"""
    stripped = line.strip()
    if not re.fullmatch(r"\d{1,4}", stripped):
        return False
    folio = int(stripped)
    return abs(folio - page_number) <= 4


def strip_page_frame(
    text: str,
    page_number: int,
    *,
    protected_pages: set[int] | None = None,
) -> tuple[str, list[str]]:
    """返回 (清理后文本, [被删除的行])。

    仅剥离完全独立的跑眉与页码行，每端最多剥离 2 行（一个章题和一个页码）。
    """
    protected = protected_pages or {2, 3, 4, 5}
    if page_number in protected:
        return text, []

    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return text, []

    removed: list[str] = []

    # 1. 页尾检查：至多两行，处理章题与纯页码（顺序任意）
    for _ in range(2):
        if not lines:
            break
        last = lines[-1].strip()
        if _is_frame_title_line(last) or _is_pure_folio_line(last, page_number):
            removed.append(lines.pop().strip())
            while lines and not lines[-1].strip():
                lines.pop()
            continue
        break

    # 2. 页首检查：至多两行
    for _ in range(2):
        if not lines or len(lines) <= 1:
            break
        first = lines[0].strip()
        if _is_frame_title_line(first) or _is_pure_folio_line(first, page_number):
            removed.append(lines.pop(0).strip())
            while lines and not lines[0].strip():
                lines.pop(0)
            continue
        break

    return "\n".join(lines).strip(), removed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    store = PageStore(output_dir)

    protected: set[int] = {2, 3, 4, 5}
    toc_path = output_dir / "toc.json"
    if toc_path.is_file():
        import json

        toc = json.loads(toc_path.read_text(encoding="utf-8"))
        protected.update(toc.get("toc_pdf_pages", []))
        for entry in toc.get("entries", []):
            if entry.get("pdf_page"):
                protected.add(entry["pdf_page"])

    changed = 0
    for record in store.load_all():
        cleaned, removed = strip_page_frame(
            record.text,
            record.pdf_page,
            protected_pages=protected,
        )
        if not removed or cleaned == record.text:
            continue
        changed += 1
        print(f"page={record.pdf_page} removed={removed!r}")
        if args.dry_run:
            continue
        record.text = cleaned
        record.language = detect_language(cleaned)
        record.notes = (
            record.notes + f"; page_frame_removed={removed!r}"
        ).strip("; ")
        store.save(record)
    print(f"changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
