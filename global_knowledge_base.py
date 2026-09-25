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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from opencc import OpenCC
except ImportError:  # The standalone script can still run without project extras.
    OpenCC = None


DEFAULT_OUTPUTS = Path.cwd() / "outputs"
DEFAULT_DB = Path.cwd() / "global_knowledge_base.sqlite3"
READER_KINDS = ("knowledge_base", "chapter_fallback")
PAGE_KINDS = ("source_page", "page_translation", "raw_ocr")
ARCHIVE_KINDS = ("chapter_snapshot", "reviewed_chapter")
ASSET_SUFFIXES = {".docx", ".epub", ".pdf", ".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
REQUIRED_KB_FIELDS = {"id", "title", "chapter_id", "chapter_order", "content"}
SCHEMA_VERSION = 1
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


def _manifest(workspace: Path) -> list[dict[str, Any]]:
    path = workspace / "chapters.json"
    if not path.is_file():
        return []
    payload = _read_json(path)
    if not isinstance(payload, list):
        raise ValueError(f"Invalid chapter manifest: {path}")
    return [item for item in payload if isinstance(item, dict)]


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
            FOREIGN KEY(workspace) REFERENCES workspaces(name)
        );
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            id UNINDEXED, title, content, tokenize='trigram'
        );
        CREATE TABLE assets(
            path TEXT PRIMARY KEY, workspace TEXT NOT NULL,
            kind TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            FOREIGN KEY(workspace) REFERENCES workspaces(name)
        );
        CREATE INDEX chunks_workspace_kind ON chunks(workspace, kind);
        CREATE INDEX chunks_content_hash ON chunks(content_sha256);
    """)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _insert_chunk(
    db: sqlite3.Connection, *, workspace: str, kind: str,
    chapter_id: str, chapter_order: int, title: str, content: str,
    source_path: str, source_row_id: str, source_metadata: dict[str, Any],
) -> None:
    chunk_id = hashlib.sha1(
        json.dumps([workspace, kind, source_path, source_row_id], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    db.execute(
        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chunk_id, workspace, kind, chapter_id, chapter_order, title, content,
         _sha256(content.encode("utf-8")), source_path, source_row_id,
         json.dumps(source_metadata, ensure_ascii=False, sort_keys=True)),
    )
    db.execute("INSERT INTO chunks_fts(id, title, content) VALUES (?, ?, ?)",
               (chunk_id, _normalize_search_text(f"{workspace} {title}", _NORMALIZATION),
                _normalize_search_text(content, _NORMALIZATION)))


def _ingest_workspace(db: sqlite3.Connection, root: Path, workspace: Path) -> dict[str, int]:
    name = workspace.name
    manifest = _manifest(workspace)
    counts: Counter[str] = Counter()
    sources: list[tuple[str, str, str, str]] = []
    reader_paths: list[Path] = []
    db.execute("INSERT INTO workspaces VALUES (?, ?, 0, 0, 0, 0, ?)",
               (name, len(manifest), "missing"))
    manifest_path = workspace / "chapters.json"
    if manifest_path.is_file():
        sources.append((manifest_path.relative_to(root).as_posix(), name,
                        "chapter_manifest", _sha256(_read_bytes(manifest_path))))
        reader_paths.append(manifest_path)
    kb_path = workspace / "knowledge_base.jsonl"
    covered: set[str] = set()
    if kb_path.is_file():
        rows, digest = _load_kb(kb_path)
        relative = kb_path.relative_to(root).as_posix()
        sources.append((relative, name, "knowledge_base", digest))
        reader_paths.append(kb_path)
        for row in rows:
            covered.add(row["chapter_id"])
            _insert_chunk(
                db, workspace=name, kind="knowledge_base",
                chapter_id=row["chapter_id"], chapter_order=row["chapter_order"],
                title=row["title"], content=row["content"],
                source_path=relative, source_row_id=row["id"],
                source_metadata={key: value for key, value in row.items()
                                 if key not in REQUIRED_KB_FIELDS},
            )
            counts["reader_chunks"] += 1
        manifest_ids = {str(item.get("id") or "") for item in manifest}
        if manifest and covered - manifest_ids:
            raise ValueError(
                f"Knowledge base references chapters absent from {workspace / 'chapters.json'}: "
                f"{sorted(covered - manifest_ids)}"
            )

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
        kind = "chapter_snapshot" if chapter_id in covered else "chapter_fallback"
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
        markdown = raw.decode("utf-8-sig")
        body = re.sub(r"^#\s+[^\n]*\n?", "", markdown, count=1).strip()
        title = str(item.get("display_title") or item.get("title") or chapter_id)
        order = int(item.get("sequence") or 0)
        for index, chunk in enumerate(_split_text(body), 1):
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
            assets.append((path.relative_to(root).as_posix(), name,
                           path.suffix.lower().lstrip("."), path.stat().st_size,
                           _sha256_file(path)))
    db.execute("UPDATE workspaces SET reader_chunks=?, page_chunks=?, archive_chunks=?, asset_count=?, "
               "report_status=? WHERE name=?",
               (counts["reader_chunks"], counts["page_chunks"], counts["archive_chunks"], len(assets),
                _report_status(workspace, reader_paths), name))
    db.executemany("INSERT INTO source_files VALUES (?, ?, ?, ?)", sources)
    db.executemany("INSERT INTO assets VALUES (?, ?, ?, ?, ?)", assets)
    counts["source_files"] = len(sources)
    counts["assets"] = len(assets)
    return dict(counts)


def sync_outputs(outputs_root: Path | str = DEFAULT_OUTPUTS,
                 db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
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
    if not workspaces:
        raise ValueError(f"No output workspaces found in {root}")
    if target.is_relative_to(root) and any(target.is_relative_to(path) for path in workspaces):
        raise ValueError("The global database must not overwrite an output workspace")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".global-kb-", suffix=".sqlite3", dir=target.parent)
    os.close(fd)
    temporary = Path(temp_name)
    totals: Counter[str] = Counter()
    try:
        with sqlite3.connect(temporary) as db:
            _create_schema(db)
            db.execute("INSERT INTO meta VALUES (?, ?)", ("outputs_root", str(root)))
            db.execute("INSERT INTO meta VALUES (?, ?)",
                       ("built_at", datetime.now(timezone.utc).isoformat()))
            db.execute("INSERT INTO meta VALUES (?, ?)",
                       ("search_normalization", _NORMALIZATION))
            for workspace in workspaces:
                totals.update(_ingest_workspace(db, root, workspace))
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
            total_chunks = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
            fts_chunks = db.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
            if total_chunks != fts_chunks:
                raise RuntimeError("Search index row count does not match content table")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"database": str(target), "outputs_root": str(root),
            "workspaces": len(workspaces), "counts": dict(totals)}


def status(db_path: Path | str = DEFAULT_DB) -> dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Global knowledge base does not exist: {path}")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        meta = dict(db.execute("SELECT key, value FROM meta"))
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
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
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
        for fixed in ("chapters.json", "knowledge_base.jsonl"):
            item = workspace / fixed
            if item.is_file():
                actual_sources.add(item.relative_to(root).as_posix())
        for pattern in ("chapters/*.md", "reviewed_chapters/*.md", "pages/page_*.json"):
            actual_sources.update(item.relative_to(root).as_posix()
                                  for item in workspace.glob(pattern) if item.is_file())
    new_sources = sorted(actual_sources - set(saved))
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
    new_assets = sorted(actual_assets - set(expected_assets))
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


def _query_terms(query: str) -> list[str]:
    """Turn CJK questions into searchable trigrams and keep Latin words."""
    terms: list[str] = []
    for part in re.findall(
        r"[A-Za-z0-9_]+|[\u3400-\u9fff]+|[\u3040-\u30ff]+|[\uac00-\ud7af]+",
        query,
    ):
        if not re.match(r"[A-Za-z0-9_]", part):
            terms.extend(part[index:index + 3]
                         for index in range(max(0, len(part) - 2)))
        elif len(part) >= 3:
            terms.append(part)
    # Duplicate n-grams do not add evidence; cap pathological query sizes.
    return list(dict.fromkeys(terms))[:64]


def search(query: str, *, db_path: Path | str = DEFAULT_DB,
           scope: str = "reader", workspace: str | None = None,
           limit: int = 10, verified_only: bool = False,
           per_book_cap: int | None = None) -> list[dict[str, Any]]:
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
    fetch_limit = max(100, limit * 20) if cap is not None else limit
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
    # FTS5 trigram is useful for CJK. Short queries cannot form a trigram, so
    # they use an indexed-table scan with instr() instead.
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        mode = db.execute("SELECT value FROM meta WHERE key='search_normalization'").fetchone()[0]
        normalized_query = _normalize_search_text(query, mode)
        terms = _query_terms(normalized_query)
        if terms:
            expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
            sql = ("SELECT c.*, w.report_status, bm25(chunks_fts, 0, 4, 1) AS score "
                   "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.id "
                   "JOIN workspaces w ON w.name = c.workspace "
                   "WHERE chunks_fts MATCH ?" + filters + " ORDER BY score LIMIT ?")
            rows = db.execute(sql, [expression, *values, fetch_limit]).fetchall()
        else:
            sql = ("SELECT c.*, w.report_status, 0.0 AS score FROM chunks_fts "
                   "JOIN chunks c ON c.id = chunks_fts.id "
                   "JOIN workspaces w ON w.name = c.workspace "
                   "WHERE (instr(chunks_fts.content, ?) > 0 "
                   "OR instr(chunks_fts.title, ?) > 0)" +
                   filters + " ORDER BY c.workspace, c.chapter_order LIMIT ?")
            rows = db.execute(sql, [normalized_query, normalized_query, *values, fetch_limit]).fetchall()
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
            position = next((searchable.find(term.casefold()) for term in terms
                             if searchable.find(term.casefold()) >= 0), -1)
        item["excerpt"] = (
            content[max(0, position - 90): position + 230]
            if position >= 0 else f"[标题匹配] {item['title']}"
        )
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
    if database_status["workspace_count"] != expected_workspaces:
        threshold_failures.append(
            f"workspace_count={database_status['workspace_count']} != {expected_workspaces}"
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
    sync = commands.add_parser("sync", help="Rebuild the local index from all outputs")
    sync.add_argument("--outputs", type=Path, default=DEFAULT_OUTPUTS)
    commands.add_parser("status", help="Show workspace and content coverage")
    commands.add_parser("verify", help="Check indexed sources for additions or changes")
    evaluate = commands.add_parser("evaluate", help="Measure retrieval hit rate and quality gate")
    evaluate.add_argument("--cases", type=Path, required=True,
                          help="Reviewed JSON query set with expected hits and thresholds")
    evaluate.add_argument("--report", type=Path,
                          default=Path.cwd() / "work" / "global_kb_evaluation.json")
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
            result = sync_outputs(args.outputs, args.db)
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
