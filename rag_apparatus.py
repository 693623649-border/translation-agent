"""Versioned document-role annotations; published corpus and vectors stay intact."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

VERSION = 1
_STRUCTURAL = {
    "目录": "toc", "目次": "toc", "contents": "toc", "tableofcontents": "toc",
    "索引": "index", "人名索引": "index", "主题索引": "index", "重要术语索引": "index",
    "术语索引": "index", "index": "index", "版权页": "copyright", "版权信息": "copyright",
    "版权": "copyright", "copyright": "copyright", "主要参考书目": "bibliography",
    "参考文献": "bibliography", "参考书目": "bibliography", "bibliography": "bibliography",
    "references": "bibliography", "书目": "bibliography", "初刊·底本一览": "bibliography", "封底": "back_cover",
}
_EXPLANATORY = {"出版说明": "publication_note", "关于作者": "author_note",
                "关于译者": "translator_note", "译者名词简释": "glossary"}
_VARIANTS = str.maketrans("錄頁權資獻書譯關於釋詞術語參說題鍵覽", "录页权资献书译关于释词术语参说题键览")


def _titles(path, rows):
    from rag_knowledge_base import load_metadata_sidecar
    metadata = load_metadata_sidecar(path)
    titles = {row["id"]: metadata.get(row["id"], {}).get("parent_title") or row["title"] for row in rows}
    digest = hashlib.sha256(json.dumps(titles, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return titles, digest


def classify_apparatus(title: str, content: str) -> dict[str, Any]:
    label = unicodedata.normalize("NFKC", title).casefold().translate(_VARIANTS).strip()
    label = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", label)
    label = re.sub(r"^(?:第?[零一二三四五六七八九十百\d]+[章节部、.．:：)）\s]+)\s*", "", label)
    label = re.sub(r"\s*(?:\((?:续|\d+)\)|[（(]?(?:片段|分块|chunk)\s*\d+[）)]?)$", "", label).strip()
    label = re.sub(r"\s+", "", label).strip(" :：.。")
    if label in _STRUCTURAL:
        kind, reason, weight = _STRUCTURAL[label], "exact_section_label", .25
    elif label in _EXPLANATORY:
        kind, reason, weight = _EXPLANATORY[label], "explanatory_section_label", .7
    else:
        # Detect an actual leader-and-page list, not an ordinary discussion of a TOC.
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        entries = sum(bool(re.search(r"[.·…‧．]{3,}\s*\d{1,5}\s*$", line)) for line in lines)
        if len(lines) >= 5 and entries / len(lines) >= .7:
            kind, reason, weight = "toc", "leader_page_lines", .25
        else:
            return {"is_apparatus": False, "apparatus_kind": "body", "reason": "no_apparatus_signal", "default_weight": 1.0}
    return {"is_apparatus": True, "apparatus_kind": kind, "reason": reason, "default_weight": weight}


def annotate_apparatus(path: Path | str) -> dict[str, Any]:
    from rag_knowledge_base import load_knowledge_rows, _atomic_write_text
    source = Path(path)
    rows = load_knowledge_rows(source)
    titles, titles_hash = _titles(source, rows)
    payload = {"schema_version": VERSION, "documents_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "titles_sha256": titles_hash,
               "annotations": {row["id"]: classify_apparatus(titles[row["id"]], row["content"]) for row in rows}}
    target = source.with_suffix(".apparatus.json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    unchanged = target.is_file() and target.read_text(encoding="utf-8") == encoded
    if not unchanged:
        _atomic_write_text(target, encoded)
    tagged = sum(item["is_apparatus"] for item in payload["annotations"].values())
    return {"path": str(target), "chunk_count": len(rows), "tagged_count": tagged, "unchanged": unchanged}


def load_apparatus(path: Path | str, rows) -> dict[str, dict[str, Any]]:
    source = Path(path)
    target = source.with_suffix(".apparatus.json")
    if not target.is_file():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        if payload["schema_version"] != VERSION or payload["documents_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
            raise ValueError("Apparatus annotations do not match corpus/version; run annotate-apparatus")
        if payload.get("titles_sha256") != _titles(source, rows)[1]:
            raise ValueError("Apparatus source titles changed; run annotate-apparatus")
        annotations = payload["annotations"]
        if set(annotations) != {row["id"] for row in rows}:
            raise ValueError("Apparatus annotation identifiers do not match corpus")
        for item in annotations.values():
            if not isinstance(item["is_apparatus"], bool) or not isinstance(item["apparatus_kind"], str) or not isinstance(item["reason"], str):
                raise ValueError("Malformed apparatus annotation")
            weight = item["default_weight"]
            if isinstance(weight, bool) or not isinstance(weight, (float, int)) or not 0 <= weight <= 1:
                raise ValueError("Invalid apparatus weight")
            if not item["is_apparatus"] and weight != 1:
                raise ValueError("Ordinary content must keep weight 1")
        return annotations
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed apparatus sidecar") from exc
