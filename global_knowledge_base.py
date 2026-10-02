"""Repository-wide, local-only search index for published output workspaces.

The per-book knowledge_base.jsonl files remain owned by their publishers. This
module reads them, fills uncovered chapters from Markdown, and keeps page text
in a separate retrieval tier. A complete SQLite database is built beside the
old one and atomically replaces it only after every workspace has been read.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from opencc import OpenCC
except ImportError:  # The standalone script can still run without project extras.
    OpenCC = None

try:
    from rag_apparatus import VERSION as _APPARATUS_SCHEMA_VERSION
except ImportError:  # Standalone deployments pin the published schema manually.
    _APPARATUS_SCHEMA_VERSION = 1


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUTS = PROJECT_ROOT / "outputs"
DEFAULT_DB = PROJECT_ROOT / "global_knowledge_base.sqlite3"
READER_KINDS = ("knowledge_base", "chapter_fallback")
PAGE_KINDS = ("source_page", "page_translation", "raw_ocr")
ARCHIVE_KINDS = ("chapter_snapshot", "reviewed_chapter")
ASSET_SUFFIXES = {".docx", ".epub", ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
REQUIRED_KB_FIELDS = {"id", "title", "chapter_id", "chapter_order", "content"}
# v4 aligns FTS rowids with chunks and indexes one/two-character CJK substrings.
SCHEMA_VERSION = 4
SHORT_INDEX_STORAGE = "contentless-v1"
_T2S = OpenCC("t2s") if OpenCC is not None else None
_NORMALIZATION = "nfkc+t2s" if _T2S is not None else "nfkc"


def _normalize_search_text(value: str, mode: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if mode == "nfkc+t2s":
        if _T2S is None:
            raise ValueError(
                "This index uses OpenCC; install the project's opencc-python-reimplemented dependency"
            )
        return _T2S.convert(normalized)
    if mode == "nfkc":
        return normalized
    raise ValueError(f"Unsupported search normalization: {mode}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _within(path: Path, parent: Path) -> bool:
    return path.resolve().is_relative_to(parent.resolve())


def _split_text(text: str, limit: int = 4000) -> list[str]:
    """Split chapter/page text without losing any non-whitespace characters."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    current = ""
    for part in re.split(r"(\n\s*\n)", text):
        if current and len(current) + len(part) > limit:
            pieces.append(current.strip())
            current = ""
        while len(part) > limit:
            pieces.append(part[:limit].strip())
            part = part[limit:]
        current += part
    if current.strip():
        pieces.append(current.strip())
    return [piece for piece in pieces if piece]


def _load_kb(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = _read_bytes(path)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not REQUIRED_KB_FIELDS <= set(row):
            raise ValueError(f"Invalid knowledge-base fields at {path}:{number}")
        if (not isinstance(row["id"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", row["id"])
                or row["id"] in seen
                or not isinstance(row["chapter_id"], str)
                or not isinstance(row["title"], str)
                or not isinstance(row["content"], str)
                or not row["content"].strip()
                or type(row["chapter_order"]) is not int):
            raise ValueError(f"Invalid knowledge-base row at {path}:{number}")
        seen.add(row["id"])
        rows.append(row)
    if not rows:
        raise ValueError(f"Empty knowledge base: {path}")
    return rows, _sha256(raw)


def _load_manifest(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Parse and hash the chapter manifest from one stable read.

    Parsing and hashing the same bytes removes the torn-read window where a
    concurrent publisher rewrites chapters.json between the two reads.
    """

    if not path.is_file():
        return [], ""
    raw = _read_bytes(path)
    payload = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError(f"Invalid chapter manifest: {path}")
    return [item for item in payload if isinstance(item, dict)], _sha256(raw)


def _stat(path: Path) -> tuple[Path, int, int]:
    info = path.stat()
    return path, info.st_mtime_ns, info.st_size


def _assert_sources_stable(stats: list[tuple[Path, int, int]]) -> None:
    """Fail the sync when a source changed between reading and replacing.

    The atomic database replacement only guarantees the output side; the
    input files are read one by one across the whole sync. Re-checking the
    recorded (mtime, size) snapshots before os.replace turns a torn mix of
    two publish generations into an explicit error instead of a silently
    inconsistent index.
    """

    moved: list[Path] = []
    for path, mtime_ns, size in stats:
        try:
            info = path.stat()
        except OSError:
            moved.append(path)
            continue
        if info.st_mtime_ns != mtime_ns or info.st_size != size:
            moved.append(path)
    if moved:
        listing = ", ".join(str(path) for path in sorted(moved)[:5])
        raise ValueError(f"Sources changed during sync; rerun: {listing}")


def _report_status(workspace: Path, reader_paths: Iterable[Path]) -> str:
    path = workspace / "audit" / "release-report.json"
    if not path.is_file():
        return "missing"
    try:
        report = _read_json(path)
        if not isinstance(report, dict) or report.get("status") != "passed":
            return "failed"
        checked_at = path.stat().st_mtime_ns
        watched = [
            *reader_paths,
            workspace / "toc.json",
            workspace / "audit" / "semantic-review.json",
            workspace / "audit" / "review-decisions.jsonl",
            *workspace.glob("*.epub"),
            *workspace.glob("*.docx"),
            *workspace.glob("*_带目录.pdf"),
        ]
        if any(item.is_file() and item.stat().st_mtime_ns > checked_at
               for item in watched):
            return "stale"
        return "passed"
    except (OSError, ValueError, TypeError):
        return "invalid"


def _create_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE workspaces(
            name TEXT PRIMARY KEY, chapter_count INTEGER NOT NULL,
            reader_chunks INTEGER NOT NULL, page_chunks INTEGER NOT NULL,
            archive_chunks INTEGER NOT NULL,
            asset_count INTEGER NOT NULL, report_status TEXT NOT NULL
        );
        CREATE TABLE source_files(
            path TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            kind TEXT NOT NULL, sha256 TEXT NOT NULL,
            FOREIGN KEY(workspace) REFERENCES workspaces(name)
        );
        CREATE TABLE chunks(
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL, kind TEXT NOT NULL,
            chapter_id TEXT NOT NULL, chapter_order INTEGER NOT NULL,
            title TEXT NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL,
            source_path TEXT NOT NULL, source_row_id TEXT,
            source_metadata TEXT NOT NULL,
            apparatus_weight REAL NOT NULL DEFAULT 1.0,
            FOREIGN KEY(workspace) REFERENCES workspaces(name)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            id UNINDEXED, title, content, tokenize='trigram'
        );
        CREATE VIRTUAL TABLE chunks_short_fts USING fts5(
            tokens, tokenize='ascii', detail='none', content=''
        );
        CREATE TABLE assets(
            path TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            kind TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            FOREIGN KEY(workspace) REFERENCES workspaces(name)
        );
        CREATE INDEX chunks_workspace_kind ON chunks(workspace, kind);
        CREATE INDEX chunks_content_hash ON chunks(content_sha256);
        CREATE INDEX source_files_workspace ON source_files(workspace);
        CREATE INDEX assets_workspace ON assets(workspace);
    """)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _require_current_schema(db: sqlite3.Connection, path: Path) -> int:
    """Reject an index built by an older schema instead of failing on a column.

    v1 stored no apparatus weights, so a leftover index would only reveal itself
    as ``no such column: c.apparatus_weight`` from deep inside the search SQL.
    """

    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"Global knowledge base {path} uses schema version {version}, "
            f"this build expects {SCHEMA_VERSION}; rerun sync_outputs/sync")
    return version


def _short_search_tokens(value: str) -> set[str]:
    """Encode exact CJK characters and adjacent pairs as ordinary FTS tokens.

    ASCII encodings avoid tokenizer-dependent CJK word boundaries. Pairs never
    cross punctuation or whitespace, preserving the existing substring rule.
    """
    tokens: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]+", value):
        tokens.update(f"u{ord(character):04x}" for character in run)
        tokens.update(f"b{ord(left):04x}{ord(right):04x}"
                      for left, right in zip(run, run[1:]))
    return tokens


def _short_index_text(title: str, content: str) -> str:
    return " ".join(sorted(_short_search_tokens(title) | _short_search_tokens(content)))


def _insert_chunk(
    db: sqlite3.Connection, *, workspace: str, kind: str,
    chapter_id: str, chapter_order: int, title: str, content: str,
    source_path: str, source_row_id: str, source_metadata: dict[str, Any],
    apparatus_weight: float = 1.0,
) -> None:
    chunk_id = hashlib.sha1(
        json.dumps([workspace, kind, source_path, source_row_id], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    inserted = db.execute(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chunk_id, workspace, kind, chapter_id, chapter_order, title, content,
         _sha256(content.encode("utf-8")), source_path, source_row_id,
         json.dumps(source_metadata, ensure_ascii=False, sort_keys=True),
         float(apparatus_weight)),
    )
    search_title = _normalize_search_text(f"{workspace} {title}", _NORMALIZATION)
    search_content = _normalize_search_text(content, _NORMALIZATION)
    db.execute("INSERT INTO chunks_fts(rowid, id, title, content) VALUES (?, ?, ?, ?)",
               (inserted.lastrowid, chunk_id, search_title, search_content))
    db.execute("INSERT INTO chunks_short_fts(rowid, tokens) VALUES (?, ?)",
               (inserted.lastrowid, _short_index_text(search_title, search_content)))


def _apparatus_weights(
    sidecar: Path, sidecar_raw: bytes,
    rows: list[dict[str, Any]], kb_digest: str,
) -> dict[str, float]:
    """Chunk-id -> weight from the per-book apparatus sidecar, when present.

    A missing sidecar is valid and every chunk keeps weight 1.0.  A sidecar
    that exists but is unreadable, stale, or does not cover exactly the
    published rows aborts the sync: silently ignoring it would turn curated
    demotions into a no-op that stays invisible until search quality
    degrades.  This mirrors rag_apparatus.load_apparatus; its titles_sha256
    check is redundant here because a title edit also changes the corpus
    digest checked below.
    """

    try:
        payload = json.loads(sidecar_raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError(f"Unreadable apparatus sidecar {sidecar}: {exc}")
    if not isinstance(payload, dict):
        raise ValueError(f"Apparatus sidecar is not a JSON object: {sidecar}")
    if payload.get("schema_version") != _APPARATUS_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported apparatus sidecar schema {sidecar}: "
            f"expected {_APPARATUS_SCHEMA_VERSION}")
    if payload.get("documents_sha256") != kb_digest:
        raise ValueError(
            f"Apparatus sidecar does not match knowledge-base bytes: {sidecar}; "
            "rerun translation-agent-kb annotate-apparatus")
    annotations = payload.get("annotations")
    if (not isinstance(annotations, dict)
            or set(annotations) != {row["id"] for row in rows}):
        raise ValueError(
            f"Apparatus annotations do not cover the knowledge-base rows: {sidecar}")
    weights: dict[str, float] = {}
    for row_id, verdict in annotations.items():
        if (not isinstance(verdict, dict)
                or not isinstance(verdict.get("is_apparatus"), bool)):
            raise ValueError(f"Malformed apparatus annotation for {row_id}: {sidecar}")
        weight = verdict.get("default_weight")
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not 0 <= weight <= 1):
            raise ValueError(f"Invalid apparatus weight for {row_id}: {sidecar}")
        if not verdict["is_apparatus"] and weight != 1:
            raise ValueError(f"Ordinary content must keep weight 1: {row_id}")
        if verdict["is_apparatus"]:
            weights[row_id] = float(weight)
    return weights


def _tally_foreign(
    inventory: dict[str, dict[str, Any]], name: str,
    classify_row, title: str, content: str,
) -> None:
    """Record one reader chunk against the Chinese-language gate."""

    entry = inventory[name]
    entry["chunks"] += 1
    if classify_row(str(title), str(content))["needs_translation"]:
        entry["foreign"] += 1
        if len(entry["samples"]) < 3:
            entry["samples"].append(str(title)[:40])


def _ingest_workspace(
    db: sqlite3.Connection, root: Path, workspace: Path,
    foreign_inventory: dict[str, dict[str, Any]] | None = None,
    source_inventory: dict[str, list[Any]] | None = None,
) -> tuple[dict[str, int], list[tuple[Path, int, int]]]:
    name = workspace.name
    inventory_paths = ({root / relative: entry for relative, entry in source_inventory.items()}
                       if source_inventory is not None else None)
    classify_row = None
    if foreign_inventory is not None:
        try:
            from kb_translation import classify_row as _classify_row
        except ImportError as exc:  # pragma: no cover - repository boundary.
            raise ValueError(
                "The Chinese-language quality gate requires kb_translation "
                "from this repository; rerun from the repo or pass "
                "require_chinese=False"
            ) from exc
        classify_row = _classify_row
        foreign_inventory[name] = {"workspace": str(workspace), "chunks": 0,
                                   "foreign": 0, "samples": []}
    counts: Counter[str] = Counter()
    sources: list[tuple[str, str, str, str]] = []
    reader_paths: list[Path] = []
    read_stats: list[tuple[Path, int, int]] = []
    manifest, manifest_digest = _load_manifest(workspace / "chapters.json")
    db.execute("INSERT INTO workspaces VALUES (?, ?, 0, 0, 0, 0, ?)",
               (name, len(manifest), "missing"))
    manifest_path = workspace / "chapters.json"
    if manifest_path.is_file():
        sources.append((manifest_path.relative_to(root).as_posix(), name,
                        "chapter_manifest", manifest_digest))
        reader_paths.append(manifest_path)
        read_stats.append(_stat(manifest_path))
    kb_path = workspace / "knowledge_base.jsonl"
    covered: set[str] = set()
    apparatus: dict[str, float] = {}
    if kb_path.is_file():
        rows, digest = _load_kb(kb_path)
        relative = kb_path.relative_to(root).as_posix()
        sources.append((relative, name, "knowledge_base", digest))
        reader_paths.append(kb_path)
        read_stats.append(_stat(kb_path))
        sidecar = workspace / "knowledge_base.apparatus.json"
        if sidecar.is_file():
            sidecar_raw = _read_bytes(sidecar)
            apparatus = _apparatus_weights(sidecar, sidecar_raw, rows, digest)
            sources.append((sidecar.relative_to(root).as_posix(), name,
                            "apparatus", _sha256(sidecar_raw)))
            read_stats.append(_stat(sidecar))
        for row in rows:
            if classify_row is not None:
                _tally_foreign(foreign_inventory, name, classify_row,
                               row["title"], row["content"])
            covered.add(row["chapter_id"])
            _insert_chunk(
                db, workspace=name, kind="knowledge_base",
                chapter_id=row["chapter_id"], chapter_order=row["chapter_order"],
                title=row["title"], content=row["content"],
                source_path=relative, source_row_id=row["id"],
                source_metadata={key: value for key, value in row.items()
                                 if key not in REQUIRED_KB_FIELDS},
                apparatus_weight=apparatus.get(row["id"], 1.0),
            )
            counts["reader_chunks"] += 1
        # Older DOCX-derived books use independent ``docx-*`` chapter IDs.
        # Keep the published KB as the reader tier and retain the Markdown
        # chapters as archive material when the two ID systems do not overlap.
    manifest_uses_kb_ids = bool(covered & {str(item.get("id") or "") for item in manifest})

    chapter_dir = workspace / "chapters"
    listed_chapters = {str(item.get("filename")) for item in manifest}
    unlisted_chapters = {path.name for path in chapter_dir.glob("*.md")} - listed_chapters
    if unlisted_chapters:
        raise ValueError(f"Unlisted chapter files in {workspace}: {sorted(unlisted_chapters)}")
    listed_reviewed = {f"{item.get('id')}.md" for item in manifest}
    unlisted_reviewed = {
        path.name for path in (workspace / "reviewed_chapters").glob("*.md")
    } - listed_reviewed
    if unlisted_reviewed:
        raise ValueError(f"Unlisted reviewed chapters in {workspace}: {sorted(unlisted_reviewed)}")
    for item in manifest:
        chapter_id = str(item.get("id") or "")
        kind = ("chapter_snapshot" if chapter_id in covered or
                (kb_path.is_file() and not manifest_uses_kb_ids)
                else "chapter_fallback")
        filename = item.get("filename")
        if not chapter_id or not isinstance(filename, str):
            raise ValueError(f"Invalid chapter entry in {workspace / 'chapters.json'}")
        chapter_path = chapter_dir / filename
        if not chapter_path.is_file() or not _within(chapter_path, chapter_dir):
            raise ValueError(f"Missing or unsafe chapter file: {chapter_path}")
        raw = _read_bytes(chapter_path)
        relative = chapter_path.relative_to(root).as_posix()
        sources.append((relative, name, kind, _sha256(raw)))
        reader_paths.append(chapter_path)
        read_stats.append(_stat(chapter_path))
        markdown = raw.decode("utf-8-sig")
        body = re.sub(r"^#\s+[^\n]*\n?", "", markdown, count=1).strip()
        title = str(item.get("display_title") or item.get("title") or chapter_id)
        order = int(item.get("sequence") or 0)
        for index, chunk in enumerate(_split_text(body), 1):
            if classify_row is not None and kind == "chapter_fallback":
                _tally_foreign(foreign_inventory, name, classify_row, title, chunk)
            _insert_chunk(
                db, workspace=name, kind=kind,
                chapter_id=chapter_id, chapter_order=order, title=title,
                content=chunk, source_path=relative,
                source_row_id=f"{chapter_id}:{index}",
                source_metadata={"source_format": item.get("source_format", "")},
            )
            counts["archive_chunks" if kind == "chapter_snapshot" else "reader_chunks"] += 1
        reviewed_path = workspace / "reviewed_chapters" / f"{chapter_id}.md"
        if reviewed_path.is_file():
            if not _within(reviewed_path, workspace / "reviewed_chapters"):
                raise ValueError(f"Unsafe reviewed chapter file: {reviewed_path}")
            reviewed_raw = _read_bytes(reviewed_path)
            reviewed_relative = reviewed_path.relative_to(root).as_posix()
            sources.append((reviewed_relative, name, "reviewed_chapter",
                            _sha256(reviewed_raw)))
            reader_paths.append(reviewed_path)
            read_stats.append(_stat(reviewed_path))
            if reviewed_raw != raw:
                reviewed_body = re.sub(
                    r"^#\s+[^\n]*\n?", "", reviewed_raw.decode("utf-8-sig"), count=1
                ).strip()
                for index, chunk in enumerate(_split_text(reviewed_body), 1):
                    _insert_chunk(
                        db, workspace=name, kind="reviewed_chapter",
                        chapter_id=chapter_id, chapter_order=order, title=title,
                        content=chunk, source_path=reviewed_relative,
                        source_row_id=f"{chapter_id}:{index}", source_metadata={},
                    )
                    counts["archive_chunks"] += 1

    pages_dir = workspace / "pages"
    if pages_dir.is_dir():
        for page_path in sorted(pages_dir.glob("page_*.json")):
            if not page_path.is_file() or not _within(page_path, pages_dir):
                continue
            raw = _read_bytes(page_path)
            page = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(page, dict):
                raise ValueError(f"Invalid page record: {page_path}")
            relative = page_path.relative_to(root).as_posix()
            sources.append((relative, name, "page_record", _sha256(raw)))
            reader_paths.append(page_path)
            read_stats.append(_stat(page_path))
            page_number = page.get("pdf_page")
            if type(page_number) is not int:
                page_number = int(re.search(r"\d+", page_path.stem).group())
            preferred = page.get("proofread_text") or page.get("text") or ""
            variants = [("source_page", preferred)]
            if page.get("proofread_text") and page.get("text") != preferred:
                variants.append(("raw_ocr", page.get("text") or ""))
            variants.append(("page_translation", page.get("translated_text") or ""))
            for kind, value in variants:
                if not isinstance(value, str):
                    continue
                for index, chunk in enumerate(_split_text(value), 1):
                    _insert_chunk(
                        db, workspace=name, kind=kind,
                        chapter_id=f"page-{page_number:04d}",
                        chapter_order=page_number,
                        title=f"{name} / 第 {page_number} 页", content=chunk,
                        source_path=relative,
                        source_row_id=f"page-{page_number}:{kind}:{index}",
                        source_metadata={
                            "language": page.get("language", ""),
                            "ocr_model": page.get("ocr_model", ""),
                            "translation_model": page.get("translation_model", ""),
                        },
                    )
                    counts["page_chunks"] += 1

    assets: list[tuple[str, str, str, int, str]] = []
    for path in workspace.rglob("*"):
        if (path.is_file() and not path.is_symlink()
                and path.suffix.lower() in ASSET_SUFFIXES):
            relative = path.relative_to(root).as_posix()
            digest = (inventory_paths[path][0] if inventory_paths is not None
                      else _sha256_file(path))
            assets.append((path.relative_to(root).as_posix(), name,
                           path.suffix.lower().lstrip("."), path.stat().st_size,
                           digest))
            read_stats.append(_stat(path))
    # report_status is a snapshot of the release report and the audit files it
    # compares against, so those bytes belong in source_files: any later edit
    # must flip verify_sources to not-current instead of keeping the stale
    # passed/failed label searchable.
    audit = workspace / "audit"
    for dependency, kind in (
            (audit / "release-report.json", "release_report"),
            (workspace / "toc.json", "report_dependency"),
            (audit / "semantic-review.json", "report_dependency"),
            (audit / "review-decisions.jsonl", "report_dependency")):
        if dependency.is_file():
            sources.append((dependency.relative_to(root).as_posix(), name,
                            kind, _sha256(_read_bytes(dependency))))
            read_stats.append(_stat(dependency))
    db.execute("UPDATE workspaces SET reader_chunks=?, page_chunks=?, archive_chunks=?, asset_count=?, "
               "report_status=? WHERE name=?",
               (counts["reader_chunks"], counts["page_chunks"], counts["archive_chunks"], len(assets),
                _report_status(workspace, reader_paths), name))
    db.executemany("INSERT INTO source_files VALUES (?, ?, ?, ?)", sources)
    db.executemany("INSERT INTO assets VALUES (?, ?, ?, ?, ?)", assets)
    counts["source_files"] = len(sources)
    counts["assets"] = len(assets)
    return dict(counts), read_stats


def _workspace_inventory(root: Path, workspace: Path) -> tuple[dict[str, list[Any]], list[tuple[Path, int, int]]]:
    """Hash every ingestion/report input, including unlisted chapter files.

    mtimes also matter: report_status uses publication ordering. Hashes detect
    edits even when a publisher preserves size and modification time.
    """
    fixed = {"chapters.json", "knowledge_base.jsonl", "knowledge_base.apparatus.json",
              "toc.json", "audit/release-report.json", "audit/semantic-review.json",
              "audit/review-decisions.jsonl"}
    manifest, _digest = _load_manifest(workspace / "chapters.json")
    chapter_dir = workspace / "chapters"
    listed_paths: set[Path] = set()
    for item in manifest:
        filename = item.get("filename")
        if not isinstance(filename, str):
            raise ValueError(f"Invalid chapter entry in {workspace / 'chapters.json'}")
        chapter_path = chapter_dir / filename
        if not _within(chapter_path, chapter_dir):
            raise ValueError(f"Missing or unsafe chapter file: {chapter_path}")
        listed_paths.add(chapter_path)
    # Retain the path spelling used by ingestion. On Windows a directory entry
    # may be CHAPTERS.JSON while workspace / 'chapters.json' is the same file;
    # Path equality provides platform-correct identity without merging distinct
    # case-sensitive files on Linux.
    files: dict[Path, Path] = {}
    for relative in sorted(fixed):
        dependency = workspace / relative
        if dependency.is_file():
            files[dependency] = dependency
    for dependency in sorted(listed_paths):
        if dependency.is_file():
            files.setdefault(dependency, dependency)
    for directory, pattern in (("chapters", "*.md"), ("reviewed_chapters", "*.md"),
                               ("pages", "page_*.json")):
        for dependency in (workspace / directory).glob(pattern):
            if dependency.is_file():
                files.setdefault(dependency, dependency)
    stats = [_stat(workspace)]
    for path in workspace.rglob("*"):
        if path.is_dir():
            stats.append(_stat(path))
            continue
        relative = path.relative_to(workspace).as_posix()
        if (path.is_file() and (
                relative in fixed
                or path in listed_paths
                or (relative.casefold().startswith(("chapters/", "reviewed_chapters/"))
                    and path.suffix.casefold() == ".md")
                or (relative.startswith("pages/") and path.match("page_*.json"))
                or (not path.is_symlink() and path.suffix.lower() in ASSET_SUFFIXES))):
            files.setdefault(path, path)
    inventory = {}
    for path in sorted(files.values()):
        snapshot = _stat(path)
        digest = _sha256_file(path)
        _assert_sources_stable([snapshot])
        stats.append(snapshot)
        inventory[path.relative_to(root).as_posix()] = [digest, snapshot[1], snapshot[2]]
    _assert_sources_stable(stats)
    return inventory, stats


def _delete_workspace(db: sqlite3.Connection, name: str) -> None:
    # Contentless FTS stores postings without a second copy of all encoded
    # tokens. Its documented delete command needs the original token stream.
    for rowid, title, content in db.execute(
            "SELECT rowid, title, content FROM chunks WHERE workspace=?", (name,)):
        tokens = _short_index_text(
            _normalize_search_text(f"{name} {title}", _NORMALIZATION),
            _normalize_search_text(content, _NORMALIZATION))
        db.execute("INSERT INTO chunks_short_fts(chunks_short_fts, rowid, tokens) "
                   "VALUES ('delete', ?, ?)", (rowid, tokens))
    db.execute("DELETE FROM chunks_fts WHERE rowid IN "
               "(SELECT rowid FROM chunks WHERE workspace=?)", (name,))
    for table in ("chunks", "source_files", "assets"):
        db.execute(f"DELETE FROM {table} WHERE workspace=?", (name,))
    db.execute("DELETE FROM workspace_inventory WHERE workspace=?", (name,))
    db.execute("DELETE FROM workspaces WHERE name=?", (name,))


def _sync_counts(db: sqlite3.Connection) -> dict[str, int]:
    counts = dict(zip(("reader_chunks", "page_chunks", "archive_chunks", "assets"),
                     db.execute("SELECT coalesce(sum(reader_chunks),0), coalesce(sum(page_chunks),0), "
                                "coalesce(sum(archive_chunks),0), coalesce(sum(asset_count),0) FROM workspaces").fetchone()))
    counts["source_files"] = db.execute("SELECT count(*) FROM source_files").fetchone()[0]
    return counts


def _assert_workspace_set(root: Path, names: set[str]) -> None:
    current = {path.name for path in root.iterdir()
               if path.is_dir() and not path.is_symlink()
               and ((path / "chapters.json").is_file()
                    or (path / "knowledge_base.jsonl").is_file())}
    if current != names:
        raise ValueError("Sources changed during sync; rerun: workspace inventory changed")


def sync_outputs(outputs_root: Path | str = DEFAULT_OUTPUTS,
                 db_path: Path | str = DEFAULT_DB,
                 *, require_chinese: bool = True, full_rebuild: bool = False) -> dict[str, Any]:
    root = Path(outputs_root).expanduser().resolve()
    target = Path(db_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Outputs directory does not exist: {root}")
    workspaces = sorted(
        (path for path in root.iterdir()
         if path.is_dir() and not path.is_symlink()
         and ((path / "chapters.json").is_file()
              or (path / "knowledge_base.jsonl").is_file())),
        key=lambda path: path.name,
    )
    if not workspaces and not target.is_file():
        raise ValueError(f"No output workspaces found in {root}")
    if target.is_relative_to(root) and any(target.is_relative_to(path) for path in workspaces):
        raise ValueError("The global database must not overwrite an output workspace")
    inventories = {}
    read_stats = []
    for workspace in workspaces:
        inventory, stats = _workspace_inventory(root, workspace)
        inventories[workspace.name] = inventory
        read_stats.extend(stats)
    previous = {}
    reusable = False
    gate = "enforced" if require_chinese else "allowed"
    if target.is_file() and not full_rebuild:
        with closing(sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)) as old_db:
            version = old_db.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                meta = dict(old_db.execute("SELECT key, value FROM meta"))
                reusable = (meta.get("outputs_root") == str(root)
                            and meta.get("search_normalization") == _NORMALIZATION
                            and meta.get("chinese_gate") == gate
                            and meta.get("short_index_storage") == SHORT_INDEX_STORAGE
                            and old_db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_inventory'").fetchone() is not None)
                if reusable:
                    previous = {name: json.loads(value) for name, value in
                                old_db.execute("SELECT workspace, inventory FROM workspace_inventory")}
                    old_counts = _sync_counts(old_db)
    updated = [path for path in workspaces if not reusable or previous.get(path.name) != inventories[path.name]]
    deleted = sorted(set(previous) - set(inventories))
    result = {"database": str(target), "outputs_root": str(root), "workspaces": len(workspaces),
              "updated_workspaces": [path.name for path in updated],
              "reused_workspaces": sorted(set(inventories) - {path.name for path in updated}),
              "deleted_workspaces": deleted}
    if reusable and not updated and not deleted:
        _assert_sources_stable(read_stats)
        _assert_workspace_set(root, set(inventories))
        return {**result, "counts": old_counts, "mode": "unchanged"}
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".global-kb-", suffix=".sqlite3", dir=target.parent)
    os.close(fd)
    temporary = Path(temp_name)
    totals: Counter[str] = Counter()
    try:
        # A Connection context manager commits but does not close the handle.
        # Windows cannot replace the finished database until it is closed.
        with closing(sqlite3.connect(temporary)) as db, db:
            if reusable:
                with closing(sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)) as old_db:
                    old_db.backup(db)
                db.execute("PRAGMA foreign_keys = ON")
                for name in deleted + [path.name for path in updated if path.name in previous]:
                    _delete_workspace(db, name)
            else:
                _create_schema(db)
                db.execute("CREATE TABLE workspace_inventory(workspace TEXT PRIMARY KEY REFERENCES workspaces(name), inventory TEXT NOT NULL)")
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", (
                ("outputs_root", str(root)), ("built_at", datetime.now(timezone.utc).isoformat()),
                ("search_normalization", _NORMALIZATION), ("chinese_gate", gate),
                ("short_index_storage", SHORT_INDEX_STORAGE)))
            foreign_inventory: dict[str, dict[str, Any]] | None = (
                {} if require_chinese else None
            )
            for workspace in updated:
                counts, stats = _ingest_workspace(
                    db, root, workspace, foreign_inventory=foreign_inventory,
                    source_inventory=inventories[workspace.name]
                )
                totals.update(counts)
                read_stats.extend(stats)
                # The bytes parsed must match the inventory checked for reuse.
                inventory_paths = {root / relative: entry
                                   for relative, entry in inventories[workspace.name].items()}
                for path, digest in db.execute("SELECT path, sha256 FROM source_files WHERE workspace=?", (workspace.name,)):
                    if inventory_paths.get(root / path, [None])[0] != digest:
                        raise ValueError(f"Sources changed during sync; rerun: {path}")
                db.execute("INSERT INTO workspace_inventory VALUES (?, ?)",
                           (workspace.name, json.dumps(inventories[workspace.name], sort_keys=True)))
            if foreign_inventory:
                foreign_inventory = {
                    name: entry for name, entry in foreign_inventory.items()
                    if entry["foreign"] > 0
                }
                if foreign_inventory:
                    listing = ", ".join(
                        f"{name} ({entry['foreign']}/{entry['chunks']} 块)"
                        for name, entry in sorted(
                            foreign_inventory.items(),
                            key=lambda item: -item[1]["foreign"],
                        )
                    )
                    raise ValueError(
                        "Chinese-language quality gate: the following workspaces "
                        f"still hold untranslated foreign reader chunks: {listing}. "
                        "Run translation-agent-kb translate-kb on each workspace, "
                        "or rerun sync with --allow-foreign to index them as-is."
                    )
            _assert_sources_stable(read_stats)
            _assert_workspace_set(root, set(inventories))
            totals = _sync_counts(db)
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
            total_chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            fts_chunks = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            if total_chunks != fts_chunks:
                raise RuntimeError("Search index row count does not match content table")
            if total_chunks != db.execute("SELECT count(*) FROM chunks_short_fts").fetchone()[0]:
                raise RuntimeError("Short-token index row count does not match content table")
            if db.execute("SELECT count(*) FROM chunks c LEFT JOIN chunks_fts f ON f.rowid=c.rowid "
                          "LEFT JOIN chunks_short_fts s ON s.rowid=c.rowid "
                          "WHERE f.rowid IS NULL OR s.rowid IS NULL OR f.id != c.id").fetchone()[0]:
                raise RuntimeError("Search index row IDs do not match content table")
        _assert_sources_stable(read_stats)
        _assert_workspace_set(root, set(inventories))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "counts": dict(totals), "mode": "incremental" if reusable else "full"}


def status(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Global knowledge base does not exist: {path}")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        meta = dict(db.execute("SELECT key, value FROM meta"))
        _require_current_schema(db, path)
        workspaces = [
            dict(zip(("name", "chapters", "reader_chunks", "page_chunks", "archive_chunks",
                      "assets", "report_status"), row))
            for row in db.execute("SELECT * FROM workspaces ORDER BY name")
        ]
        kinds = dict(db.execute("SELECT kind, count(*) FROM chunks GROUP BY kind"))
        assets = dict(db.execute("SELECT kind, count(*) FROM assets GROUP BY kind"))
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    return {"database": str(path), **meta, "schema_version": SCHEMA_VERSION,
            "workspace_count": len(workspaces), "workspaces": workspaces,
            "chunks_by_kind": kinds, "assets_by_kind": assets,
            "integrity": integrity}


def verify_sources(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    """Check that the local index still reflects every indexed source file."""
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Global knowledge base does not exist: {path}")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        root = Path(db.execute("SELECT value FROM meta WHERE key='outputs_root'").fetchone()[0])
        saved = dict(db.execute("SELECT path, sha256 FROM source_files"))
        workspace_names = {row[0] for row in db.execute("SELECT name FROM workspaces")}
        expected_assets = {
            relative: (size, digest)
            for relative, size, digest in db.execute("SELECT path, size_bytes, sha256 FROM assets")
        }
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    changed = []
    missing = []
    for relative, digest in saved.items():
        source = root / relative
        if not source.is_file():
            missing.append(relative)
        elif _sha256(_read_bytes(source)) != digest:
            changed.append(relative)
    actual_sources: set[str] = set()
    actual_workspaces: set[str] = set()
    for workspace in root.iterdir():
        if not workspace.is_dir() or workspace.is_symlink():
            continue
        if not ((workspace / "chapters.json").is_file()
                or (workspace / "knowledge_base.jsonl").is_file()):
            continue
        actual_workspaces.add(workspace.name)
        # Must mirror the file set registered by _ingest_workspace, including
        # the apparatus sidecar and the report/audit dependencies.
        for fixed in ("chapters.json", "knowledge_base.jsonl",
                      "knowledge_base.apparatus.json", "toc.json",
                      "audit/release-report.json",
                      "audit/semantic-review.json",
                      "audit/review-decisions.jsonl"):
            item = workspace / fixed
            if item.is_file():
                actual_sources.add(item.relative_to(root).as_posix())
        for pattern in ("chapters/*.md", "reviewed_chapters/*.md", "pages/page_*.json"):
            actual_sources.update(item.relative_to(root).as_posix()
                                  for item in workspace.glob(pattern) if item.is_file())
    new_sources = sorted(path.relative_to(root).as_posix() for path in
                         ({root / relative for relative in actual_sources}
                          - {root / relative for relative in saved}))
    changed_assets = [relative for relative, (size, digest) in expected_assets.items()
                      if not (root / relative).is_file()
                      or (root / relative).stat().st_size != size
                      or _sha256_file(root / relative) != digest]
    actual_assets = {
        item.relative_to(root).as_posix()
        for workspace_name in actual_workspaces
        for item in (root / workspace_name).rglob("*")
        if item.is_file() and not item.is_symlink()
        and item.suffix.lower() in ASSET_SUFFIXES
    }
    new_assets = sorted(path.relative_to(root).as_posix() for path in
                       ({root / relative for relative in actual_assets}
                        - {root / relative for relative in expected_assets}))
    current = not (changed or missing or new_sources or changed_assets
                   or new_assets or workspace_names != actual_workspaces
                   or integrity != "ok")
    return {
        "current": current, "integrity": integrity,
        "indexed_workspaces": len(workspace_names),
        "changed_sources": changed, "missing_sources": missing,
        "new_sources": new_sources, "changed_assets": changed_assets,
        "new_assets": new_assets,
        "new_workspaces": sorted(actual_workspaces - workspace_names),
        "missing_workspaces": sorted(workspace_names - actual_workspaces),
    }


def _query_terms(query: str) -> tuple[list[str], list[str]]:
    """Split a query into FTS trigram terms and short CJK words.

    FTS5's trigram tokenizer cannot match strings shorter than three
    characters, so one- and two-character CJK/kana/hangul words are returned
    separately for substring predicates instead of being silently dropped —
    a query like ``自然 正式`` used to degrade to a full-string match that
    could never hit ``自然主义正式正文``.
    """
    terms: list[str] = []
    short_terms: list[str] = []
    for part in re.findall(
        r"[A-Za-z0-9_]+|[\u3400-\u9fff]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+",
        query,
    ):
        if re.match(r"[A-Za-z0-9_]", part):
            if len(part) >= 3:
                terms.append(part)
        elif len(part) >= 3:
            terms.extend(part[index:index + 3]
                         for index in range(len(part) - 2))
        else:
            short_terms.append(part)
    # Duplicate n-grams do not add evidence; cap pathological query sizes.
    return list(dict.fromkeys(terms))[:64], list(dict.fromkeys(short_terms))[:16]


def search(query: str, *, db_path: Path | str = DEFAULT_DB,
           scope: str = "reader", workspace: str | None = None,
           limit: int = 10, verified_only: bool = False,
           per_book_cap: int | None = None,
           excerpt: bool = True) -> list[dict[str, Any]]:
    """Return ranked chunks for one query.

    ``excerpt=True`` (the default) replaces each chunk's content with a short
    window around the match and is what a caller wants for cheap discovery.
    ``excerpt=False`` keeps the full chunk text in ``content`` and is required
    by callers that compare *stored bytes* against a passage — a caller
    verifying a quotation against a truncated window would report a verbatim
    quote as a mismatch whenever the quote starts past that window. The
    ``excerpt`` key is still populated in both modes.
    """
    if not query.strip() or limit < 1:
        raise ValueError("A nonempty query and a positive limit are required")
    if scope not in {"reader", "pages", "archive", "all"}:
        raise ValueError(f"Unknown search scope: {scope}")
    if per_book_cap is not None and per_book_cap < 0:
        raise ValueError("per_book_cap must be nonnegative")
    cap = (1 if per_book_cap is None and workspace is None
           and scope in {"reader", "all"} else per_book_cap)
    if cap == 0:
        cap = None
    # The per-book cap is enforced as a window rank inside SQL, so no
    # over-fetching is needed to let other books survive the fetch cut.
    fetch_limit = limit
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Global knowledge base does not exist: {path}")
    conditions = []
    values: list[Any] = []
    if scope != "all":
        kinds = {"reader": READER_KINDS, "pages": PAGE_KINDS,
                 "archive": ARCHIVE_KINDS}[scope]
        conditions.append("c.kind IN (%s)" % ",".join("?" for _ in kinds))
        values.extend(kinds)
    if workspace:
        conditions.append("c.workspace = ?")
        values.append(workspace)
    if verified_only:
        conditions.append("w.report_status = 'passed'")
    filters = (" AND " + " AND ".join(conditions)) if conditions else ""
    # Trigrams retain existing long-query ranking. Pure short CJK queries use
    # an exact unigram/bigram FTS index; punctuation-only queries keep their
    # literal substring fallback.
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        mode = db.execute("SELECT value FROM meta WHERE key='search_normalization'").fetchone()[0]
        _require_current_schema(db, path)
        normalized_query = _normalize_search_text(query, mode)
        terms, short_terms = _query_terms(normalized_query)
        # FTS5's bm25() returns negative scores where smaller is better, so
        # demoting an apparatus chunk means pushing its score toward zero:
        # score + (1 - weight) * |score|.  Demotion must happen inside every
        # ORDER BY — demoting after a fetch limit would already have dropped
        # the prose chunks the TOC pushed past the cut.  Substring-only
        # queries have no meaningful score, so they order apparatus chunks
        # last explicitly.
        score_sql = ("(bm25(chunks_fts, 0, 4, 1) + (1.0 - c.apparatus_weight)"
                     " * abs(bm25(chunks_fts, 0, 4, 1)))")
        demote_sql = ("CASE WHEN c.apparatus_weight < 1.0 THEN 1 ELSE 0 END, "
                      "c.workspace, c.chapter_order, c.id")
        where: list[str] = []
        params: list[Any] = []
        search_table = "chunks_fts"
        if terms:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            where.append("chunks_fts MATCH ?")
            params.append(expression)
            inner_order = f"{score_sql}, c.chapter_order, c.id"
            # Short words alongside trigram terms do NOT filter: a chapter
            # query like ``无器官身体 斥力 引力`` spans chunks, and requiring
            # every two-character word in one chunk drops the best-scoring
            # chunk of the expected chapter (measured on the local fixture).
            # They still guide excerpt highlighting below.
        else:
            inner_order = demote_sql
            if short_terms:
                # Short words carrying the whole query combine with AND:
                # ``自然 正式`` must hit a chunk containing both words, which
                # the old full-string fallback could never match.
                search_table = "chunks_short_fts"
                tokens = set().union(*(_short_search_tokens(term) for term in short_terms))
                # For a two-character word its pair token implies both
                # characters, so only the pair is needed in the posting lookup.
                tokens = {token for token in tokens if token.startswith("b")} | {
                    f"u{ord(term):04x}" for term in short_terms if len(term) == 1
                }
                where.append("chunks_short_fts MATCH ?")
                params.append(" AND ".join(f'"{token}"' for token in sorted(tokens)))
            else:
                where.append("(instr(chunks_fts.content, ?) > 0 "
                             "OR instr(chunks_fts.title, ?) > 0)")
                params.extend([normalized_query, normalized_query])
        base = (f"FROM {search_table} JOIN chunks c ON c.rowid = {search_table}.rowid "
                 "JOIN workspaces w ON w.name = c.workspace WHERE "
                 + " AND ".join(where) + filters)
        # Materialize direct-query BM25 scores before window ranking: calling
        # bm25() inside the window itself is invalid. Keep content out of the
        # intermediate rows and fetch only the final limited result into Python.
        score_select = score_sql if terms else "0.0"
        if cap is None:
            sql = (f"SELECT c.*, w.report_status, {score_select} AS score {base} "
                   f"ORDER BY {inner_order} LIMIT ?")
            rows = db.execute(sql, [*params, *values, fetch_limit]).fetchall()
        else:
            slim = (f"SELECT c.rowid AS chunk_rowid, c.id, c.workspace, c.chapter_order, "
                     f"c.apparatus_weight, {score_select} AS score {base}")
            rank_order = ("score, chapter_order, id" if terms else
                          "CASE WHEN apparatus_weight < 1.0 THEN 1 ELSE 0 END, "
                          "workspace, chapter_order, id")
            output_order = ("p.score, p.chapter_order, p.id" if terms else
                            "CASE WHEN p.apparatus_weight < 1.0 THEN 1 ELSE 0 END, "
                            "p.workspace, p.chapter_order, p.id")
            sql = (
                f"WITH scored AS MATERIALIZED ({slim}), "
                f"ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace "
                f"ORDER BY {rank_order}) AS book_rank FROM scored), "
                f"picked AS MATERIALIZED (SELECT * FROM ranked WHERE book_rank <= ? "
                f"ORDER BY {rank_order} LIMIT ?) "
                f"SELECT c.*, w.report_status, p.score FROM picked p "
                f"JOIN chunks c ON c.rowid = p.chunk_rowid "
                f"JOIN workspaces w ON w.name = c.workspace ORDER BY {output_order}"
            )
            rows = db.execute(sql, [*params, *values, cap, fetch_limit]).fetchall()
    results = []
    by_book: Counter[str] = Counter()
    for row in rows:
        item = dict(row)
        if cap is not None and by_book[item["workspace"]] >= cap:
            continue
        by_book[item["workspace"]] += 1
        content = item.pop("content")
        position = _normalize_search_text(content, mode).casefold().find(
            normalized_query.casefold()
        )
        if position < 0:
            searchable = _normalize_search_text(content, mode).casefold()
            position = next((searchable.find(term.casefold())
                             for term in (*terms, *short_terms)
                             if searchable.find(term.casefold()) >= 0), -1)
        item["excerpt"] = (
            content[max(0, position - 90): position + 230]
            if position >= 0 else f"[标题匹配] {item['title']}"
        )
        item["content"] = content if not excerpt else item["excerpt"]
        item["source_metadata"] = json.loads(item["source_metadata"])
        results.append(item)
        if len(results) == limit:
            break
    return results


def evaluate_retrieval(cases_path: Path | str, *,
                       db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    """Measure Hit@1/5 and MRR@5 against a reviewed, fixed query set."""
    fixture = _read_json(Path(cases_path))
    if not isinstance(fixture, dict) or fixture.get("schema_version") != 1:
        raise ValueError("Unsupported retrieval evaluation fixture")
    cases = fixture.get("cases")
    thresholds = fixture.get("thresholds")
    if not isinstance(cases, list) or not cases or not isinstance(thresholds, dict):
        raise ValueError("Retrieval evaluation needs cases and thresholds")
    top_k = fixture.get("top_k")
    if type(top_k) is not int or top_k < 5:
        raise ValueError("Retrieval evaluation top_k must be at least 5")
    counts: dict[str, Counter[str]] = {}
    reciprocal_ranks: Counter[str] = Counter()
    details: list[dict[str, Any]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Retrieval case must be an object")
        group = str(case.get("group") or "")
        query = str(case.get("query") or "")
        expected_workspace = str(case.get("expected_workspace") or "")
        expected_chapter = case.get("expected_chapter_id")
        if group not in thresholds or not query or not expected_workspace:
            raise ValueError(f"Invalid retrieval case: {case}")
        hits = search(query, db_path=db_path, scope="reader",
                      workspace=case.get("workspace_filter"), limit=top_k)
        rank = next((index for index, hit in enumerate(hits, 1)
                     if hit["workspace"] == expected_workspace
                     and (expected_chapter is None
                          or hit["chapter_id"] == expected_chapter)), None)
        counter = counts.setdefault(group, Counter())
        counter["cases"] += 1
        if rank == 1:
            counter["hit_at_1"] += 1
        if rank is not None and rank <= 5:
            counter["hit_at_5"] += 1
            reciprocal_ranks[group] += 1 / rank
        details.append({
            "id": case.get("id"), "group": group, "query": query,
            "expected_workspace": expected_workspace,
            "expected_chapter_id": expected_chapter,
            "rank": rank,
            "top_hits": [{"workspace": hit["workspace"],
                          "chapter_id": hit["chapter_id"],
                          "source_path": hit["source_path"]} for hit in hits[:5]],
        })
    metrics = {}
    threshold_failures = []
    for group, counter in sorted(counts.items()):
        total = counter["cases"]
        metrics[group] = {
            "cases": total,
            "hit_at_1": counter["hit_at_1"] / total,
            "hit_at_5": counter["hit_at_5"] / total,
            "mrr_at_5": reciprocal_ranks[group] / total,
        }
        for metric in ("hit_at_1", "hit_at_5"):
            floor = float(thresholds[group][metric])
            if metrics[group][metric] < floor:
                threshold_failures.append(
                    f"{group}.{metric}={metrics[group][metric]:.3f} < {floor:.3f}"
                )
    source_audit = verify_sources(db_path)
    database_status = status(db_path)
    expected_workspaces = fixture.get("expected_workspaces")
    # The fixture pins the corpus its queries were reviewed against.  Extra
    # workspaces are allowed — otherwise every newly translated book would
    # permanently fail the gate — while a shrinking corpus still fails.
    if database_status["workspace_count"] < expected_workspaces:
        threshold_failures.append(
            f"workspace_count={database_status['workspace_count']} < {expected_workspaces}"
        )
    if database_status.get("search_normalization") != "nfkc+t2s":
        threshold_failures.append("OpenCC search normalization is not active")
    if not source_audit["current"]:
        threshold_failures.append("Indexed sources changed or are missing")
    return {
        "passed": not threshold_failures,
        "database": str(Path(db_path).resolve()),
        "fixture": str(Path(cases_path).resolve()),
        "thresholds": thresholds,
        "metrics": metrics,
        "workspace_count": database_status["workspace_count"],
        "source_current": source_audit["current"],
        "integrity": source_audit["integrity"],
        "search_normalization": database_status.get("search_normalization"),
        "failures": threshold_failures,
        "cases": details,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local repository-wide knowledge base")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", help="Synchronize changed output workspaces")
    sync.add_argument("--outputs", type=Path, default=DEFAULT_OUTPUTS)
    sync.add_argument("--full-rebuild", action="store_true",
                      help="Rebuild all workspaces instead of reusing unchanged indexed sources")
    sync.add_argument(
        "--allow-foreign",
        action="store_true",
        help="Index untranslated foreign chunks instead of failing the Chinese-language gate",
    )
    commands.add_parser("status", help="Show workspace and content coverage")
    commands.add_parser("verify", help="Check indexed sources for additions or changes")
    evaluate = commands.add_parser("evaluate", help="Measure retrieval hit rate and quality gate")
    evaluate.add_argument("--cases", type=Path, required=True,
                          help="Reviewed JSON query set with expected hits and thresholds")
    evaluate.add_argument("--report", type=Path,
                          default=PROJECT_ROOT / "work" / "global_kb_evaluation.json")
    search_cmd = commands.add_parser("search", help="Search the local index")
    search_cmd.add_argument("query")
    search_cmd.add_argument("--scope", choices=("reader", "pages", "archive", "all"), default="reader")
    search_cmd.add_argument("--workspace")
    search_cmd.add_argument("--limit", type=int, default=10)
    search_cmd.add_argument("--per-book-cap", type=int,
                            help="Max hits per book; default 1 for global reader searches, 0 disables")
    search_cmd.add_argument("--verified-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            result = sync_outputs(args.outputs, args.db,
                                  require_chinese=not args.allow_foreign,
                                  full_rebuild=args.full_rebuild)
        elif args.command == "status":
            result = status(args.db)
        elif args.command == "verify":
            result = verify_sources(args.db)
        elif args.command == "evaluate":
            result = evaluate_retrieval(args.cases, db_path=args.db)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            result = search(args.query, db_path=args.db, scope=args.scope,
                            workspace=args.workspace, limit=args.limit,
                            verified_only=args.verified_only,
                            per_book_cap=args.per_book_cap)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"global-knowledge-base: {exc}\n")
    if args.command == "evaluate":
        print(json.dumps({key: value for key, value in result.items()
                          if key != "cases"}, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
