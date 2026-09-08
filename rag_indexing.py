"""Build disposable retrieval corpora without changing published source books."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rag_knowledge_base import (
    _atomic_write_text, initialize_rag_manifest, load_knowledge_rows,
    load_metadata_sidecar, metadata_sidecar_path_for,
)

_KIND = "translation-agent.derived-retrieval-corpus"
_MANIFEST = "retrieval_sources.json"


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _spans(text: str, limit: int, overlap: int):
    """Yield exact source offsets, preferring paragraph then sentence ends."""
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            lower = start + max(overlap + 1, limit // 2)
            for pattern in (r"\n\s*\n", r"[。！？.!?](?:\s|$)?", r"\n"):
                matches = [m.end() for m in re.finditer(pattern, text[start:end])
                           if start + m.end() >= lower]
                if matches:
                    end = start + matches[-1]
                    break
        if text[start:end].strip():
            yield start, end
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def validate_retrieval_sources(output_dir: Path | str) -> dict[str, Any]:
    """Report missing/changed source and output bytes without repairing anything."""
    root = Path(output_dir).resolve()
    path = root / _MANIFEST
    if not path.is_file():
        return {"derived": False, "stale": True, "reasons": ["missing source manifest"]}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("kind") != _KIND:
            raise ValueError("unrecognized source manifest")
        reasons = []
        if manifest.get("publication_status") != "complete":
            reasons.append("incomplete publication")
        for source in manifest["sources"]:
            for key, hash_key in (("path", "sha256"), ("metadata_path", "metadata_sha256")):
                if _hash(Path(source[key])) != source[hash_key]:
                    reasons.append(f"changed or missing source: {source[key]}")
        for name, expected in manifest["output_hashes"].items():
            if _hash(root / name) != expected:
                reasons.append(f"changed or missing output: {name}")
        return {"derived": True, "stale": bool(reasons), "reasons": reasons,
                "sources": manifest["sources"]}
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"derived": False, "stale": True, "reasons": [str(exc)]}


def build_retrieval_corpus(
    sources: Sequence[Path | str], output_dir: Path | str, *,
    chunk_chars: int = 1200, overlap_chars: int = 150,
) -> dict[str, Any]:
    """Aggregate explicit books and publish bounded child chunks plus provenance.

    Existing nonempty directories require our source manifest. The final source
    manifest acts as a commit marker: interrupted multi-file publication is
    detected by output checksums. Identical rebuilds leave embedding files intact.
    """
    if (isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int)
            or isinstance(overlap_chars, bool) or not isinstance(overlap_chars, int)
            or chunk_chars < 1 or not 0 <= overlap_chars < chunk_chars):
        raise ValueError("require chunk_chars > 0 and 0 <= overlap_chars < chunk_chars")
    if isinstance(sources, (str, bytes, Path)) or not sources:
        raise ValueError("sources must be a nonempty sequence of explicit books")
    paths = []
    for source in sources:
        path = Path(source).resolve()
        if path.is_dir():
            path = path / "knowledge_base.jsonl"
        if path not in paths:
            paths.append(path)
    root = Path(output_dir).resolve()
    corpus = root / "knowledge_base.jsonl"
    metadata = metadata_sidecar_path_for(corpus)
    marker = root / _MANIFEST
    if any(root == path.parent or corpus == path
           or (corpus.exists() and path.exists() and corpus.samefile(path))
           for path in paths):
        raise ValueError("derived output must not overwrite a source directory")
    old = None
    if root.exists() and any(root.iterdir()):
        try:
            old = json.loads(marker.read_text(encoding="utf-8"))
            if old.get("kind") != _KIND:
                raise ValueError("unrecognized derived directory")
        except (OSError, ValueError, AttributeError) as exc:
            raise ValueError("refusing to overwrite a directory not marked as derived") from exc
    records, metas, source_info = [], [], []
    seen: dict[str, int] = {}
    input_rows = candidate_count = 0
    for path in paths:
        rows = load_knowledge_rows(path)
        sidecar = load_metadata_sidecar(path)
        meta_path = metadata_sidecar_path_for(path)
        source_info.append({"path": str(path), "sha256": _hash(path),
                            "metadata_path": str(meta_path), "metadata_sha256": _hash(meta_path)})
        input_rows += len(rows)
        for row in rows:
            original = sidecar.get(row["id"], {})
            book_id = original.get("book_id") or hashlib.sha1(str(path).encode()).hexdigest()
            book_title = original.get("book_title", "")
            for index, (start, end) in enumerate(_spans(row["content"], chunk_chars, overlap_chars)):
                candidate_count += 1
                content = row["content"][start:end]
                provenance = {"parent_id": row["id"], "source_path": str(path),
                              "source_book_id": book_id, "source_start": start,
                              "source_end": end, "chunk_index": index,
                              "book_id": book_id, "book_title": book_title,
                              "author": original.get("author", ""),
                              "language": original.get("language", ""),
                              "source_metadata": original}
                content_hash = hashlib.sha256(content.encode()).hexdigest()
                if content_hash in seen:
                    metas[seen[content_hash]]["provenance"].append(provenance)
                    continue
                row_id = hashlib.sha1(_json([str(path), row["id"], start, end, content]).encode()).hexdigest()
                title = row["title"]
                if book_title and book_title not in title:
                    title = f"{book_title} / {title}"
                records.append({**row, "id": row_id, "title": title, "content": content})
                metas.append({**original, **provenance, "id": row_id,
                              "parent_title": row["title"], "provenance": [provenance]})
                seen[content_hash] = len(records) - 1
    body = "".join(_json(row) + "\n" for row in records)
    meta_body = "".join(_json({**row, "provenance": _json(row["provenance"]),
                                      "source_metadata": _json(row["source_metadata"])}) + "\n" for row in metas)
    counts = {"sources": len(paths), "input_rows": input_rows, "candidate_chunks": candidate_count,
              "output_chunks": len(records), "deduplicated_chunks": candidate_count - len(records)}
    manifest = {"kind": _KIND, "version": 1, "publication_status": "complete", "sources": source_info,
                "parameters": {"chunk_chars": chunk_chars, "overlap_chars": overlap_chars},
                "counts": counts, "output_hashes": {
                    corpus.name: hashlib.sha256(body.encode()).hexdigest(),
                    metadata.name: hashlib.sha256(meta_body.encode()).hexdigest()}}
    unchanged = old == manifest and not validate_retrieval_sources(root)["stale"]
    if not unchanged:
        root.mkdir(parents=True, exist_ok=True)
        # An initial marker allows safe recovery if the first publication stops
        # between files; checksums keep incomplete output explicitly stale.
        _atomic_write_text(marker, _json({**manifest, "publication_status": "building"}) + "\n")
        _atomic_write_text(corpus, body)
        _atomic_write_text(metadata, meta_body)
        initialize_rag_manifest(corpus)
        _atomic_write_text(marker, _json(manifest) + "\n")
    return {"knowledge_base_path": str(corpus), "metadata_path": str(metadata),
            "source_manifest_path": str(marker), "counts": counts,
            "dedup": counts["deduplicated_chunks"], "sources": source_info,
            "unchanged": unchanged}
