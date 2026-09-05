from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from book_pipeline import split_text, write_knowledge_base
from rag_knowledge_base import (
    RagError,
    RagFormatError,
    RagKnowledgeBase,
    ZhipuEmbeddingProvider,
    build_embedding_index,
    initialize_rag_manifest,
    load_metadata_sidecar,
    manifest_path_for,
    metadata_sidecar_path_for,
    read_rag_manifest,
    vector_index_path_for,
)


DEFAULT_KNOWLEDGE_BASE = "knowledge_base.jsonl"


def _knowledge_base_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_dir():
        return path / DEFAULT_KNOWLEDGE_BASE
    return path


def _json_line(payload: dict[str, Any], stdout: TextIO) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _zhipu_provider(args: argparse.Namespace) -> ZhipuEmbeddingProvider:
    return ZhipuEmbeddingProvider(
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        model=args.model,
        dimensions=args.dimensions,
    )


def _provider_from_manifest(
    knowledge_base_path: Path,
    args: argparse.Namespace,
) -> ZhipuEmbeddingProvider | None:
    try:
        manifest = read_rag_manifest(knowledge_base_path)
    except RagError:
        return None
    embedding = manifest["retrieval"]["embedding"]
    if embedding.get("status") != "ready" or embedding.get("provider") != "zhipu":
        return None
    dimensions = embedding.get("dimensions")
    if not isinstance(dimensions, int):
        return None
    return ZhipuEmbeddingProvider(
        api_key_env=args.api_key_env,
        base_url=args.base_url,
        model=str(embedding.get("model") or args.model or "embedding-3"),
        dimensions=dimensions,
    )


def _status_payload(knowledge_base_path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "knowledge_base": str(knowledge_base_path),
        "exists": knowledge_base_path.is_file(),
    }
    if not knowledge_base_path.is_file():
        payload["current"] = False
        payload["error"] = "knowledge_base_missing"
        return payload
    try:
        knowledge_base = RagKnowledgeBase.open(knowledge_base_path)
        manifest = knowledge_base.manifest
        metadata = load_metadata_sidecar(knowledge_base_path)
    except RagError as exc:
        payload["current"] = False
        payload["error"] = exc.__class__.__name__
        payload["message"] = str(exc)
        return payload
    embedding = manifest["retrieval"]["embedding"]
    vector_path = vector_index_path_for(knowledge_base_path)
    payload.update(
        {
            "current": True,
            "chunk_count": knowledge_base.chunk_count,
            "manifest": str(manifest_path_for(knowledge_base_path)),
            "lexical": manifest["retrieval"]["lexical"]["status"],
            "metadata": {
                "path": str(metadata_sidecar_path_for(knowledge_base_path)),
                "exists": metadata_sidecar_path_for(knowledge_base_path).is_file(),
                "row_count": len(metadata),
                "coverage": round(
                    len(metadata) / max(knowledge_base.chunk_count, 1),
                    6,
                ),
            },
            "embedding": {
                "status": embedding["status"],
                "provider": embedding.get("provider"),
                "model": embedding.get("model"),
                "dimensions": embedding.get("dimensions"),
                "index": str(vector_path),
                "index_exists": vector_path.is_file(),
            },
        }
    )
    return payload


def _command_status(args: argparse.Namespace, stdout: TextIO) -> int:
    payload = _status_payload(_knowledge_base_path(args.path))
    _json_line(payload, stdout)
    return 0 if payload.get("exists") else 1


def _command_register(args: argparse.Namespace, stdout: TextIO) -> int:
    knowledge_base_path = _knowledge_base_path(args.path)
    initialize_rag_manifest(knowledge_base_path)
    payload = _status_payload(knowledge_base_path)
    if not args.lexical_only:
        metadata = build_embedding_index(
            knowledge_base_path,
            _zhipu_provider(args),
            batch_size=args.batch_size,
        )
        payload = _status_payload(knowledge_base_path)
        payload["registered"] = {
            "embedding_index": str(metadata.index_path),
            "provider": metadata.provider_name,
            "model": metadata.model,
            "dimensions": metadata.dimensions,
            "chunk_count": metadata.chunk_count,
        }
    else:
        payload["registered"] = {"embedding_index": None, "mode": "lexical"}
    _json_line(payload, stdout)
    return 0


def _hits_payload(hits: Sequence[Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": hit.id,
            "title": hit.title,
            "chapter_id": hit.chapter_id,
            "chapter_order": hit.chapter_order,
            "score": hit.score,
            "retrieval_mode": hit.retrieval_mode,
            "book": getattr(hit, "book", ""),
            "channels": getattr(hit, "channels", ""),
            "content": hit.content,
        }
        for hit in hits
    ]


def _command_retrieve(args: argparse.Namespace, stdout: TextIO) -> int:
    knowledge_base_path = _knowledge_base_path(args.path)
    try:
        knowledge_base = RagKnowledgeBase.open(knowledge_base_path)
    except RagFormatError:
        initialize_rag_manifest(knowledge_base_path)
        knowledge_base = RagKnowledgeBase.open(knowledge_base_path)
    # Explicit --mode wins; the legacy --semantic flag keeps its meaning;
    # otherwise default to hybrid ranking per the retrieval plan.
    mode = getattr(args, "mode", None) or ("semantic" if args.semantic else "hybrid")
    provider = (
        _provider_from_manifest(knowledge_base_path, args)
        if mode in ("semantic", "hybrid")
        else None
    )
    per_book_cap = max(0, int(getattr(args, "per_book_cap", 3) or 0)) or None
    inferred_routes = {"book_ids": frozenset(), "authors": frozenset()}
    auto_route = bool(getattr(args, "auto_route", True))
    if auto_route and not args.book and not args.author:
        inferred_routes = knowledge_base.infer_query_routes(args.query)
    filters = {
        "book_ids": (
            set(args.book)
            if getattr(args, "book", None)
            else set(inferred_routes["book_ids"]) or None
        ),
        "authors": set(getattr(args, "author", None) or []) or None,
        "languages": set(getattr(args, "language", None) or []) or None,
    }
    semantic_error: str | None = None
    try:
        context = knowledge_base.retrieve_context(
            args.query,
            top_k=args.top_k,
            max_chars=args.max_chars,
            chapter_ids=set(args.chapter_id) if args.chapter_id else None,
            embedding_provider=provider,
            mode=mode,
            per_book_cap=per_book_cap,
            candidate_depth=int(getattr(args, "candidate_depth", 30) or 30),
            **filters,
        )
    except RagError as exc:
        if provider is None:
            raise
        semantic_error = str(exc)
        context = knowledge_base.retrieve_context(
            args.query,
            top_k=args.top_k,
            max_chars=args.max_chars,
            chapter_ids=set(args.chapter_id) if args.chapter_id else None,
            embedding_provider=None,
            mode="lexical",
            per_book_cap=per_book_cap,
            **filters,
        )
    _json_line(
        {
            "query": args.query,
            "retrieval_mode": (
                context.hits[0].retrieval_mode if context.hits else mode
            ),
            "requested_mode": mode,
            "books": sorted(set(args.book)) if getattr(args, "book", None) else [],
            "routing": {
                "automatic": auto_route,
                "inferred_books": sorted(inferred_routes["book_ids"]),
                "matched_authors": sorted(inferred_routes["authors"]),
            },
            "per_book_cap": per_book_cap,
            "semantic_requested": mode in ("semantic", "hybrid"),
            "semantic_used": bool(context.hits and context.hits[0].retrieval_mode in ("semantic", "hybrid")),
            "semantic_error": semantic_error,
            "hits": _hits_payload(context.hits),
            "context": context.text,
        },
        stdout,
    )
    return 0


def _normalize_chapter_id(title: str, sequence: int) -> str:
    normalized = re.sub(r"\W+", "-", title.casefold()).strip("-")
    digest = hashlib.sha1(f"{sequence}:{title}".encode("utf-8")).hexdigest()[:10]
    return f"docx-{sequence:04d}-{normalized[:32] or digest}"


def _docx_rows(source: Path) -> list[dict[str, Any]]:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover - dependency boundary.
        raise RuntimeError("derive-docx requires python-docx") from exc

    document = Document(source)
    chapters: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        style = getattr(getattr(paragraph, "style", None), "name", "")
        if style == "Heading 1" and text:
            if current is not None:
                chapters.append(current)
            current = {"title": text, "paragraphs": []}
            continue
        if current is not None and text:
            current["paragraphs"].append(text)
    if current is not None:
        chapters.append(current)
    if not chapters:
        raise RuntimeError("DOCX has no Heading 1 chapters; cannot derive canonical KB rows")

    rows: list[dict[str, Any]] = []
    for sequence, chapter in enumerate(chapters, start=1):
        content = "\n\n".join(chapter["paragraphs"]).strip()
        if not content:
            continue
        chapter_id = _normalize_chapter_id(str(chapter["title"]), sequence)
        for chunk_index, chunk in enumerate(split_text(content, 4000), start=1):
            row_id = hashlib.sha1(
                f"{source.name}:{chapter_id}:{chunk_index}:{chunk}".encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "id": row_id,
                    "title": str(chapter["title"]),
                    "chapter_id": chapter_id,
                    "chapter_order": sequence,
                    "content": chunk,
                }
            )
    if not rows:
        raise RuntimeError("DOCX Heading 1 chapters contain no non-empty body text")
    return rows


def _command_derive_docx(args: argparse.Namespace, stdout: TextIO) -> int:
    source = Path(args.docx).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".docx":
        raise RuntimeError(f"derive-docx expects a .docx file: {source}")
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else source.parent / source.stem
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    knowledge_base_path = output_dir / DEFAULT_KNOWLEDGE_BASE
    rows = _docx_rows(source)
    write_knowledge_base(knowledge_base_path, rows)
    payload = _status_payload(knowledge_base_path)
    payload["derived_from"] = str(source)
    _json_line(payload, stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="translation-agent-kb",
        description="Register, inspect, retrieve, and derive RAG knowledge bases.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    provider = argparse.ArgumentParser(add_help=False)
    provider.add_argument("--api-key-env", default="ZHIPU_API_KEY")
    provider.add_argument("--base-url", default=None)
    provider.add_argument("--model", default=None)
    provider.add_argument("--dimensions", type=int, default=None)

    register = subparsers.add_parser("register", parents=[provider])
    register.add_argument("path", help="knowledge_base.jsonl path or artifact directory")
    register.add_argument("--batch-size", type=int, default=64)
    register.add_argument("--lexical-only", action="store_true")
    register.set_defaults(func=_command_register)

    retrieve = subparsers.add_parser("retrieve", parents=[provider])
    retrieve.add_argument("path", help="knowledge_base.jsonl path or artifact directory")
    retrieve.add_argument("query")
    retrieve.add_argument("--semantic", action="store_true")
    retrieve.add_argument("--top-k", type=int, default=5)
    retrieve.add_argument("--max-chars", type=int, default=12_000)
    retrieve.add_argument("--chapter-id", action="append", default=[])
    retrieve.add_argument(
        "--mode",
        choices=("hybrid", "lexical", "semantic"),
        default=None,
        help=(
            "Retrieval ranking: hybrid (BM25+vector RRF fusion, default), "
            "lexical (BM25 only), or semantic (cosine only)."
        ),
    )
    retrieve.add_argument(
        "--book",
        action="append",
        default=[],
        help="Restrict retrieval to a book title/id (repeatable).",
    )
    retrieve.add_argument(
        "--author",
        action="append",
        default=[],
        help="Restrict retrieval to an author via the metadata sidecar (repeatable).",
    )
    retrieve.add_argument(
        "--language",
        action="append",
        default=[],
        help="Restrict retrieval to a language via the metadata sidecar (repeatable).",
    )
    retrieve.add_argument(
        "--per-book-cap",
        type=int,
        default=3,
        help=(
            "Maximum results per book so large books cannot flood the answer; "
            "0 disables the cap (default 3)."
        ),
    )
    retrieve.add_argument(
        "--candidate-depth",
        type=int,
        default=30,
        help="Per-channel candidate pool before RRF fusion.",
    )
    auto_route = retrieve.add_mutually_exclusive_group()
    auto_route.add_argument(
        "--auto-route",
        dest="auto_route",
        action="store_true",
        help="Route explicit book/author mentions before ranking (default).",
    )
    auto_route.add_argument(
        "--no-auto-route",
        dest="auto_route",
        action="store_false",
        help="Disable automatic query routing.",
    )
    retrieve.set_defaults(auto_route=True)
    retrieve.set_defaults(func=_command_retrieve)

    status = subparsers.add_parser("status")
    status.add_argument("path", help="knowledge_base.jsonl path or artifact directory")
    status.set_defaults(func=_command_status)

    derive_docx = subparsers.add_parser("derive-docx")
    derive_docx.add_argument("docx")
    derive_docx.add_argument("--output-dir", default=None)
    derive_docx.set_defaults(func=_command_derive_docx)
    return parser


def main(argv: Sequence[str] | None = None, stdout: TextIO | None = None) -> int:
    stdout = stdout or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args, stdout))
    except (OSError, RagError, RuntimeError, ValueError) as exc:
        _json_line(
            {
                "ok": False,
                "error": exc.__class__.__name__,
                "message": str(exc),
            },
            stdout,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
