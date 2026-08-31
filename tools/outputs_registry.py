"""outputs/ 产出版本注册表生成器。

扫描 outputs/ 下每本书的工作目录，把交付物、知识库状态、发布门结论与
流水线来源身份汇总为 ``MANIFEST.json`` + ``MANIFEST.md``，作为产出版本的
单一事实清单。只读工具；每次交付或重跑流水线后重新生成即可。

用法::

    python tools/outputs_registry.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = REPO_ROOT / "outputs"
KB_FILES = ("knowledge_base.jsonl", "knowledge_base.rag.json", "knowledge_base.vectors.jsonl")


def _load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _kb_status(book: Path) -> dict:
    kb = book / "knowledge_base.jsonl"
    payload: dict = {"jsonl": kb.is_file()}
    if kb.is_file():
        try:
            payload["chunks"] = sum(1 for line in kb.open(encoding="utf-8") if line.strip())
        except OSError:
            payload["chunks"] = 0
    vectors = book / "knowledge_base.vectors.jsonl"
    payload["vectors"] = vectors.is_file()
    rag = book / "knowledge_base.rag.json"
    if rag.is_file():
        manifest = _load_json(rag)
        if isinstance(manifest, dict):
            embedding = manifest.get("retrieval", {}).get("embedding", {})
            payload["embed_status"] = embedding.get("status")
            payload["embed_model"] = embedding.get("model")
    else:
        payload["embed_status"] = None
    return payload


def _report_status(book: Path) -> dict:
    payload: dict = {}
    for name in ("word-release-report.json", "release-report.json"):
        report = book / "audit" / name
        if not report.is_file():
            continue
        data = _load_json(report)
        if not isinstance(data, dict):
            continue
        summary = data.get("summary") or {}
        failed_checks = [
            check.get("id") or check.get("name")
            for check in (data.get("checks") or [])
            if check.get("status") == "failed"
        ]
        payload = {
            "report": name,
            "ok": data.get("ok"),
            "status": data.get("status"),
            "release_ready": data.get("release_ready"),
            "publication_profile": data.get("publication_profile"),
            "schema_version": data.get("schema_version"),
            "checks_passed": summary.get("passed"),
            "checks_failed": summary.get("failed"),
            "checks_skipped": summary.get("skipped"),
            "failed_check_ids": failed_checks,
            "chapters": summary.get("chapter_count") or summary.get("selected_chapter_count"),
            "docx_text_matches": summary.get("docx_text_matches"),
            "docx_footnotes": summary.get("docx_footnotes"),
        }
        break
    return payload


def _source_identity(book: Path) -> dict:
    source = _load_json(book / ".pipeline_graph" / "source.json")
    if not isinstance(source, dict):
        return {}
    return {
        "source_mode": source.get("source_mode"),
        "adapter": source.get("adapter"),
        "source_page_count": source.get("page_count"),
        "source_sha256": str(source.get("sha256") or "")[:16],
    }


def _book_entry(book: Path) -> dict:
    deliverables = sorted(
        item.name
        for item in book.iterdir()
        if item.is_file()
        and item.suffix.lower() in {".docx", ".epub", ".pdf", ".jsonl", ".json"}
        and not item.name.startswith(".")
        and item.name not in {"chapters.json"}
    )
    manifest = _load_json(book / "chapters.json")
    chapter_count = len(manifest) if isinstance(manifest, list) else None
    ocr_pages = None
    pages_dir = book / "pages"
    if pages_dir.is_dir():
        ocr_pages = len(list(pages_dir.glob("page_*.json")))
    return {
        "name": book.name,
        "updated": datetime.fromtimestamp(
            book.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="minutes"),
        "chapter_count": chapter_count,
        "ocr_page_checkpoints": ocr_pages,
        "deliverables": deliverables,
        "knowledge_base": _kb_status(book),
        "publication": _report_status(book),
        "source": _source_identity(book),
    }


def _markdown(entries: list[dict]) -> str:
    lines = [
        "# outputs/ 产出版本注册表",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）",
        "重新生成：`python tools/outputs_registry.py`。",
        "",
        "| 书 | 章 | OCR页 | 交付物 | 知识库 | 发布门 | 来源 |",
        "|---|---|---|---|---|---|---|",
    ]
    for entry in entries:
        kb = entry["knowledge_base"]
        kb_text = []
        if kb.get("jsonl"):
            kb_text.append(f"{kb.get('chunks')}块")
        kb_text.append(f"向量={'有' if kb.get('vectors') else '无'}")
        embed = kb.get("embed_status")
        if embed:
            kb_text.append(f"语义={embed}")
        pub = entry.get("publication") or {}
        epub_na = {"manifest.valid", "checkpoints.complete"}
        if pub:
            failed_ids = set(pub.get("failed_check_ids") or [])
            leftover = failed_ids - epub_na
            if pub.get("status") == "failed" and not leftover:
                pub_text = "通过（EPUB 预期 N/A×%d）" % len(failed_ids)
            elif pub.get("status") == "passed":
                pub_text = "passed"
            else:
                pub_text = (
                    f"{pub.get('status', '-')}"
                    f"({pub.get('checks_failed', '?')}失败: "
                    f"{', '.join(sorted(leftover))[:28]})"
                )
        else:
            pub_text = "未验证"
        src = entry.get("source") or {}
        src_text = src.get("source_mode") or "EPUB/外部"
        chapter_count = entry.get("chapter_count")
        ocr_pages = entry.get("ocr_page_checkpoints")
        lines.append(
            f"| {entry['name'][:38]} | {chapter_count if chapter_count is not None else '-'} "
            f"| {ocr_pages if ocr_pages is not None else '-'} "
            f"| {', '.join(entry['deliverables'])[:42] or '-'} "
            f"| {', '.join(kb_text)} "
            f"| {pub_text} "
            f"| {src_text} |"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs-dir", type=Path, default=OUTPUTS)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)

    outputs = args.outputs_dir.expanduser().resolve()
    if not outputs.is_dir():
        print(f"error: outputs dir not found: {outputs}", file=sys.stderr)
        return 2

    entries = [
        _book_entry(path)
        for path in sorted(outputs.iterdir())
        if path.is_dir() and not path.name.startswith(".")
    ]
    json_path = outputs / "MANIFEST.json"
    json_path.write_text(
        json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "books": entries},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not args.json_only:
        (outputs / "MANIFEST.md").write_text(
            _markdown(entries), encoding="utf-8"
        )
    print(f"[registry] books={len(entries)} json={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
