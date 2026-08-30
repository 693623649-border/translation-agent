"""Local-first job runtime for the Streamlit product shell.

The module deliberately keeps Streamlit out of the execution boundary.  UI
sessions submit versioned :class:`product_contracts.RunSpec` values to a
SQLite registry, while each job runs in its own process and UUID workspace.
No credential value is serialized to SQLite, JSON, argv, or the job log.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from product_contracts import (
    APP_VERSION,
    CONTRACT_SCHEMA_VERSION,
    ArtifactRecord,
    RunEvent,
    RunSpec,
)
DEFAULT_RUNTIME_ROOT = Path.home() / ".translation-agent" / "webui"
DEFAULT_DATABASE = DEFAULT_RUNTIME_ROOT / "jobs.sqlite3"
DEFAULT_JOBS_ROOT = DEFAULT_RUNTIME_ROOT / "jobs"
DEFAULT_UPLOAD_LIMIT = 256 * 1024 * 1024
SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "FONTCONFIG_FILE",
        "FONTCONFIG_PATH",
        "JAVA_HOME",
    }
)
FORBIDDEN_CREDENTIAL_ENVIRONMENT_NAMES = SAFE_ENVIRONMENT_NAMES | frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
    }
)
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
JOB_STATUSES = frozenset(
    {
        "queued",
        "running",
        "cancel_requested",
        "cancelled",
        "succeeded",
        "failed",
        "interrupted",
    }
)
TERMINAL_STATUSES = frozenset({"cancelled", "succeeded", "failed", "interrupted"})
SOURCE_EXTENSIONS = {
    "scanned-pdf": frozenset({".pdf"}),
    "text-pdf": frozenset({".pdf"}),
    "epub": frozenset({".epub"}),
}
PUBLICATION_KINDS = {
    "publication.epub": "epub",
    "publication.docx": "docx",
    "publication.knowledge_base": "knowledge_base",
    "publication.reference_pdf": "reference_pdf",
    "publication.report": "release_report",
    "publication.word_report": "release_report",
}
MEDIA_TYPES = {
    "epub": "application/epub+zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "knowledge_base": "application/x-ndjson",
    "reference_pdf": "application/pdf",
    "release_report": "application/json",
    "semantic_units": "application/x-ndjson",
    "semantic_audit": "application/json",
}


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        result = super().__exit__(exc_type, exc, tb)
        self.close()
        return bool(result)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def safe_upload_name(name: str, *, source_mode: str) -> str:
    """Return a bounded basename with the extension dictated by source mode."""

    if source_mode not in SOURCE_EXTENSIONS:
        raise ValueError(f"unsupported source mode: {source_mode!r}")
    suffix = Path(Path(name).name).suffix.lower()
    if suffix not in SOURCE_EXTENSIONS[source_mode]:
        expected = ", ".join(sorted(SOURCE_EXTENSIONS[source_mode]))
        raise ValueError(f"source file must use one of: {expected}")
    stem = re.sub(
        r"[^\w\u3400-\u9fff.-]+",
        "_",
        Path(Path(name).name).stem,
    ).strip("._")
    return f"{(stem or 'uploaded_book')[:120]}{suffix}"


def validate_upload_size(size: int, *, limit: int) -> None:
    """Fail before a Streamlit upload is copied into another memory buffer."""

    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("upload size must be a non-negative integer")
    if size > limit:
        raise ValueError(f"upload exceeds configured limit of {limit} bytes")


@dataclass(frozen=True)
class FrontendSettings:
    database: Path
    jobs_root: Path
    source_roots: tuple[Path, ...]
    upload_limit_bytes: int = DEFAULT_UPLOAD_LIMIT

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "FrontendSettings":
        values = os.environ if environment is None else environment
        runtime_root = Path(
            values.get("TRANSLATION_AGENT_WEBUI_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT)
        ).expanduser().resolve()
        database = Path(
            values.get("TRANSLATION_AGENT_WEBUI_DATABASE", runtime_root / "jobs.sqlite3")
        ).expanduser().resolve()
        jobs_root = Path(
            values.get("TRANSLATION_AGENT_WEBUI_JOBS_ROOT", runtime_root / "jobs")
        ).expanduser().resolve()
        configured_roots = values.get("TRANSLATION_AGENT_WEBUI_SOURCE_ROOTS", "")
        roots = tuple(
            Path(item).expanduser().resolve()
            for item in configured_roots.split(os.pathsep)
            if item.strip()
        ) or (Path.cwd().resolve(),)
        try:
            upload_limit = int(
                values.get(
                    "TRANSLATION_AGENT_WEBUI_UPLOAD_LIMIT_BYTES",
                    DEFAULT_UPLOAD_LIMIT,
                )
            )
        except ValueError as exc:
            raise ValueError("upload limit must be an integer") from exc
        if upload_limit < 1:
            raise ValueError("upload limit must be positive")
        return cls(
            database=database,
            jobs_root=jobs_root,
            source_roots=roots,
            upload_limit_bytes=upload_limit,
        )


class PathPolicy:
    """Resolve user paths without allowing them to escape configured roots."""

    def __init__(self, roots: Iterable[Path | str]) -> None:
        self.roots = tuple(Path(root).expanduser().resolve() for root in roots)
        if not self.roots:
            raise ValueError("at least one workspace root is required")

    def resolve_file(
        self,
        value: Path | str,
        *,
        suffixes: Iterable[str] | None = None,
    ) -> Path:
        path = Path(value).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError(f"not a regular file: {path}")
        if not any(_is_relative_to(path, root) for root in self.roots):
            raise ValueError(f"path is outside configured workspace roots: {path}")
        allowed = frozenset(item.lower() for item in suffixes or ())
        if allowed and path.suffix.lower() not in allowed:
            raise ValueError(
                f"unsupported file extension {path.suffix!r}; expected {sorted(allowed)}"
            )
        return path


@dataclass(frozen=True)
class JobRecord:
    id: str
    status: str
    created_at: str
    updated_at: str
    workspace: Path
    source_mode: str
    source_path: Path
    spec: RunSpec
    pid: int | None = None
    exit_code: int | None = None
    run_id: str | None = None
    error: str | None = None

    @property
    def log_path(self) -> Path:
        return self.workspace / "job.log"


class JobRegistry:
    """Small SQLite WAL registry safe across Streamlit reruns and workers."""

    def __init__(self, database: Path | str) -> None:
        self.database = Path(database).expanduser().resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=10,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    pid INTEGER,
                    exit_code INTEGER,
                    run_id TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS jobs_updated_at_idx
                    ON jobs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS job_events_job_idx
                    ON job_events(job_id, sequence);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            workspace=Path(str(row["workspace"])).resolve(),
            source_mode=str(row["source_mode"]),
            source_path=Path(str(row["source_path"])).resolve(),
            spec=RunSpec.from_dict(json.loads(str(row["spec_json"]))),
            pid=int(row["pid"]) if row["pid"] is not None else None,
            exit_code=(
                int(row["exit_code"]) if row["exit_code"] is not None else None
            ),
            run_id=str(row["run_id"]) if row["run_id"] else None,
            error=str(row["error"]) if row["error"] else None,
        )

    def create(self, job_id: str, workspace: Path, spec: RunSpec) -> JobRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("job id must be a lowercase UUID hex value")
        now = _utc_now()
        source = Path(str(spec.source or "")).expanduser().resolve()
        event = RunEvent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            run_id=job_id,
            event="job_created",
            timestamp=now,
            message="Task created",
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, status, created_at, updated_at, workspace,
                    source_mode, source_path, spec_json
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    now,
                    now,
                    str(workspace.resolve()),
                    spec.source_mode,
                    str(source),
                    _canonical_json(spec.to_dict()),
                ),
            )
            connection.execute(
                "INSERT INTO job_events(job_id, created_at, payload_json) VALUES (?, ?, ?)",
                (job_id, now, _canonical_json(event.to_dict())),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown job: {job_id}")
        return self._row(row)

    def list(self, *, limit: int = 100) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row(row) for row in rows]

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        pid: int | None | object = ...,
        exit_code: int | None | object = ...,
        run_id: str | None | object = ...,
        error: str | None | object = ...,
    ) -> JobRecord:
        fields: list[str] = ["updated_at = ?"]
        values: list[Any] = [_utc_now()]
        if status is not None:
            if status not in JOB_STATUSES:
                raise ValueError(f"unsupported job status: {status!r}")
            fields.append("status = ?")
            values.append(status)
        for name, value in (
            ("pid", pid),
            ("exit_code", exit_code),
            ("run_id", run_id),
            ("error", error),
        ):
            if value is not ...:
                fields.append(f"{name} = ?")
                values.append(value)
        values.append(job_id)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown job: {job_id}")
        return self.get(job_id)

    def add_event(
        self,
        job_id: str,
        event: str,
        *,
        level: str = "info",
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> RunEvent:
        payload = RunEvent(
            schema_version=CONTRACT_SCHEMA_VERSION,
            run_id=job_id,
            event=event,
            timestamp=_utc_now(),
            level=level,
            message=message,
            data=dict(data or {}),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id, created_at, payload_json) VALUES (?, ?, ?)",
                (job_id, payload.timestamp, _canonical_json(payload.to_dict())),
            )
        return payload

    def events(self, job_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM job_events
                WHERE job_id = ? ORDER BY sequence DESC LIMIT ?
                """,
                (job_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in reversed(rows)]

    def reconcile_workers(self) -> None:
        """Mark vanished background workers resumable after a server restart."""

        for job in self.list(limit=500):
            if job.status not in {"running", "cancel_requested"} or not job.pid:
                continue
            try:
                os.kill(job.pid, 0)
            except ProcessLookupError:
                self.update(
                    job.id,
                    status="interrupted",
                    pid=None,
                    exit_code=None,
                    error="background worker is no longer running",
                )
                self.add_event(
                    job.id,
                    "job_interrupted",
                    level="warning",
                    message="Background worker is no longer running",
                )
            except PermissionError:
                # A process exists but is owned by another user.  Do not send
                # signals or reinterpret its status from this UI instance.
                continue


def child_environment(
    credentials: Mapping[str, str],
    *,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal child environment and add validated credential names."""

    source = os.environ if base is None else base
    environment = {
        name: str(source[name])
        for name in SAFE_ENVIRONMENT_NAMES
        if source.get(name)
    }
    environment["PYTHONUNBUFFERED"] = "1"
    for name, value in credentials.items():
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid credential environment variable: {name!r}")
        if name in FORBIDDEN_CREDENTIAL_ENVIRONMENT_NAMES:
            raise ValueError(
                f"credential environment variable may not override runtime control: {name!r}"
            )
        if value:
            environment[name] = str(value)
    return environment


def _redaction_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    values: set[str] = set()
    for name, value in environment.items():
        upper_name = name.upper()
        if not value:
            continue
        if (
            "KEY" in upper_name
            or "TOKEN" in upper_name
            or "SECRET" in upper_name
            or "PASSWORD" in upper_name
            or ("PROXY" in upper_name and "@" in value)
        ):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _redact(value: str, secrets: Iterable[str] | None = None) -> str:
    result = value
    for secret in secrets or _redaction_values(os.environ):
        result = result.replace(secret, "<redacted>")
    return result


class _RedactingWriter:
    def __init__(self, stream: Any, secrets: Iterable[str]) -> None:
        self._stream = stream
        self._secrets = tuple(secrets)

    def write(self, value: str) -> int:
        redacted = _redact(value, self._secrets)
        self._stream.write(redacted)
        self._stream.flush()
        return len(value)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _runspec_to_graph_request(spec: RunSpec, *, phase: str | None = None) -> Any:
    """Translate the stable public contract into the current Graph API lazily."""

    from translation_agent_api import GraphRunRequest, RunRequest

    options = dict(spec.options)
    targets = tuple(spec.targets)
    requested_publications = set(targets)
    generate_epub = "publication.epub" in requested_publications
    generate_docx = "publication.docx" in requested_publications
    generate_kb = "publication.knowledge_base" in requested_publications
    generate_pdf = "publication.reference_pdf" in requested_publications
    if "publication.report" in requested_publications:
        generate_epub = bool(options.get("generate_epub", True))
        generate_docx = bool(options.get("generate_docx", True))
        generate_kb = bool(options.get("generate_knowledge_base", True))
        generate_pdf = bool(options.get("generate_reference_pdf", True))
    if "publication.word_report" in requested_publications:
        generate_docx = True

    pipeline = RunRequest(
        output_dir=spec.output_dir,
        input_pdf=(
            spec.source
            if spec.source is not None and spec.source_mode != "epub"
            else None
        ),
        phase=phase or spec.phase,
        config=spec.config,
        ocr_profile=options.get("ocr_profile"),
        toc_profile=options.get("toc_profile"),
        proofread_profile=options.get("proofread_profile"),
        translation_profile=options.get("translation_profile"),
        title=spec.title,
        author=spec.author,
        start_page=options.get("start_page"),
        end_page=options.get("end_page"),
        translate_non_chinese=spec.translate,
        source_language=str(options.get("source_language") or "auto"),
        target_language=spec.target_language,
        ocr_concurrency=options.get("ocr_concurrency"),
        proofread_concurrency=options.get("proofread_concurrency"),
        translation_concurrency=options.get("translation_concurrency"),
        granularity=options.get("granularity"),
        toc_pages=options.get("toc_pages"),
        page_offset=options.get("page_offset"),
        printed_pages_per_pdf_page=options.get("printed_pages_per_pdf_page"),
        front_matter_pages=options.get("front_matter_pages"),
        ocr_reading_direction=options.get("ocr_reading_direction"),
        keep_page_images=bool(options.get("keep_page_images", False)),
        force=bool(options.get("force_legacy", False)),
        require_complete_ocr=bool(options.get("require_complete_ocr", True)),
        require_translation=bool(options.get("require_translation", spec.translate)),
        generate_epub=generate_epub,
        generate_docx=generate_docx,
        generate_knowledge_base=generate_kb,
        generate_bookmarked_pdf=generate_pdf,
        verify_publication=spec.verify,
        require_all_reviewed=bool(options.get("require_all_reviewed", False)),
    )
    return GraphRunRequest(
        pipeline=pipeline,
        recipe=spec.recipe,
        targets=targets,
        include_proofread=bool(options.get("include_proofread", False)),
        toc_source=str(options.get("toc_source") or "pipeline"),
        force_nodes=tuple(options.get("force_nodes", ())),
        force_all=bool(options.get("force_all", False)),
        adopt_existing_output=bool(options.get("adopt_existing_output", False)),
        source_mode=(spec.source_mode if spec.source_mode != "epub" else None),
        text_pdf_sort=bool(options.get("text_pdf_sort", False)),
        text_pdf_reflow=bool(options.get("text_pdf_reflow", False)),
        text_pdf_strip_leading_page_number_offset=options.get(
            "text_pdf_strip_leading_page_number_offset"
        ),
    )


def plan_runspec(spec: RunSpec) -> tuple[str, ...]:
    if spec.source_mode == "epub":
        if spec.verify:
            raise ValueError(
                "EPUB-native release verification is not available yet; "
                "run this adapter as a draft workflow"
            )
        nodes = ["adapter.epub.semantic_import"]
        if spec.translate:
            nodes.extend(
                ["semantic.translate", "adapter.epub.apply_translations"]
            )
        if "publication.epub" in spec.targets:
            nodes.append("core.publish.epub")
        if "publication.docx" in spec.targets or "publication.word_report" in spec.targets:
            nodes.append("core.publish.docx")
        if "publication.knowledge_base" in spec.targets:
            nodes.append("core.publish.knowledge_base")
        return tuple(nodes)
    from translation_agent_api import plan_graph

    return plan_graph(_runspec_to_graph_request(spec))


def _publish_epub_knowledge_base(source: Path, output: Path) -> dict[str, Any]:
    from book_pipeline import build_knowledge_rows_from_manifest, write_knowledge_base

    manifest_path = output / "chapters.json"
    chapter_dir = output / "chapters"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list):
        raise RuntimeError("EPUB chapter manifest must be a list")
    rows = [
        row
        for row in build_knowledge_rows_from_manifest(source, chapter_dir, manifest)
        if str(row.get("content") or "").strip()
    ]
    path = output / "knowledge_base.jsonl"
    write_knowledge_base(path, rows)
    return {"path": path, "rows": len(rows), "sha256": _sha256_file(path)}


def _run_epub_workflow(spec: RunSpec) -> str | None:
    """Run the bounded EPUB adapter until it becomes a first-class Graph source."""

    from epub_semantic_import import apply_translations, import_epub
    from semantic_translation_runner import _deepseek_request, load_glossary, translate_units
    from pipeline_profiles import load_pipeline_profiles
    from translation_agent_api import run_graph

    output = Path(spec.output_dir).resolve()
    if spec.verify:
        raise RuntimeError(
            "EPUB-native release verification is not available; "
            "the adapter may only produce draft artifacts"
        )
    imported = import_epub(Path(str(spec.source)), output)
    if imported.get("release_blocked"):
        raise RuntimeError("EPUB semantic reconstruction is blocked; review its audit")
    options = dict(spec.options)
    if spec.translate:
        if spec.config is None:
            raise RuntimeError("EPUB translation requires a model profile config")
        profile = load_pipeline_profiles(spec.config).for_stage(
            "translation", options.get("translation_profile")
        )
        credential = os.getenv(profile.credential_env or "", "")
        if not credential:
            raise RuntimeError(
                f"missing credential environment variable: {profile.credential_env}"
            )
        request = _deepseek_request(
            api_key=credential,
            base_url=profile.base_url or "https://api.deepseek.com",
            model=profile.model,
            timeout=profile.timeout,
            thinking=profile.thinking,
        )
        translations = output / "semantic" / "translations.jsonl"
        glossary_value = options.get("glossary")
        glossary = load_glossary(Path(glossary_value)) if glossary_value else {}
        translate_units(
            output / "semantic" / "translation-units.jsonl",
            translations,
            target_language=spec.target_language,
            glossary=glossary,
            model=profile.model,
            provider=profile.provider,
            base_url=profile.base_url or "https://api.deepseek.com",
            prompt_profile=profile.name,
            thinking=profile.thinking,
            cache_dir=output / ".translation_cache",
            request=request,
            max_chars=int(options.get("translation_max_chars", 9000)),
            concurrency=int(
                options.get("translation_concurrency") or profile.concurrency
            ),
            retries=int(options.get("translation_retries", 3)),
            progress=lambda done, total, cached: print(
                f"[semantic-translate] completed={done}/{total} "
                f"source={'cache' if cached else 'model'}",
                flush=True,
            ),
        )
        apply_translations(
            output,
            translations,
            target_language=spec.target_language,
            glossary=glossary,
        )

    run_id: str | None = None
    publication_targets = set(spec.targets)
    phases: list[str] = []
    if "publication.epub" in publication_targets:
        phases.append("epub")
    if (
        "publication.docx" in publication_targets
        or "publication.word_report" in publication_targets
    ):
        phases.append("docx")
    if "publication.knowledge_base" in publication_targets:
        _publish_epub_knowledge_base(Path(str(spec.source)).resolve(), output)
    unsupported = publication_targets & {
        "publication.reference_pdf",
    }
    if unsupported:
        raise RuntimeError(
            "EPUB adapter does not provide these publication targets yet: "
            + ", ".join(sorted(unsupported))
        )
    for phase in phases:
        phase_spec = replace(spec, phase=phase, targets=())
        result = run_graph(_runspec_to_graph_request(phase_spec, phase=phase))
        run_id = result.run_id

    return run_id


def execute_job(database: Path | str, job_id: str) -> int:
    registry = JobRegistry(database)
    # Wait for the dispatcher to persist our PID.  Without this handshake a
    # very short task can finish before the parent updates the row, leaving a
    # succeeded task incorrectly marked as running.
    for _attempt in range(200):
        job = registry.get(job_id)
        if job.pid == os.getpid():
            break
        if job.status in {"cancel_requested", "cancelled"}:
            return 130
        time.sleep(0.01)
    else:
        registry.update(
            job_id,
            status="failed",
            pid=None,
            exit_code=1,
            error="dispatcher did not register worker pid",
        )
        return 1
    registry.update(job_id, status="running", error=None)
    registry.add_event(job_id, "job_started", data={"pid": os.getpid()})
    try:
        if job.spec.source_mode == "epub":
            run_id = _run_epub_workflow(job.spec)
        else:
            from translation_agent_api import run_graph

            result = run_graph(_runspec_to_graph_request(job.spec))
            run_id = result.run_id
        registry.update(
            job_id,
            status="succeeded",
            pid=None,
            exit_code=0,
            run_id=run_id,
            error=None,
        )
        registry.add_event(job_id, "job_succeeded", data={"graph_run_id": run_id})
        return 0
    except KeyboardInterrupt:
        registry.update(job_id, status="cancelled", pid=None, exit_code=130)
        registry.add_event(job_id, "job_cancelled", level="warning")
        return 130
    except BaseException as exc:  # worker boundary records a resumable failure
        message = _redact(str(exc).replace("\x00", ""))[:2000]
        print(f"[job-error] {type(exc).__name__}: {message}", file=sys.stderr)
        traceback.print_exc()
        registry.update(
            job_id,
            status="failed",
            pid=None,
            exit_code=1,
            error=message,
        )
        registry.add_event(
            job_id,
            "job_failed",
            level="error",
            message=message,
        )
        return 1


def start_job_process(
    registry: JobRegistry,
    job_id: str,
    *,
    credentials: Mapping[str, str] | None = None,
) -> JobRecord:
    job = registry.get(job_id)
    if job.status in {"running", "cancel_requested"}:
        raise RuntimeError(f"task {job_id} is already {job.status}")
    job.workspace.mkdir(parents=True, exist_ok=True)
    registry.update(
        job_id,
        status="running",
        pid=None,
        exit_code=None,
        error=None,
    )
    log_path = job.log_path
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    log_handle = os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "execute",
        "--database",
        str(registry.database),
        "--job-id",
        job_id,
    ]
    try:
        process = subprocess.Popen(
            command,
            # Never execute a product job from site-packages or the source
            # checkout.  The UUID workspace is writable and isolates any
            # relative files created by third-party tools.
            cwd=job.workspace,
            env=child_environment(credentials or {}),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except Exception as exc:
        registry.update(
            job_id,
            status="failed",
            pid=None,
            exit_code=1,
            error=str(exc)[:2000],
        )
        raise
    finally:
        log_handle.close()
    registry.update(
        job_id,
        pid=process.pid,
    )
    registry.add_event(job_id, "job_dispatched", data={"pid": process.pid})
    return registry.get(job_id)


def cancel_job(registry: JobRegistry, job_id: str) -> JobRecord:
    job = registry.get(job_id)
    if job.status == "queued":
        registry.update(job_id, status="cancelled", pid=None, exit_code=130)
        registry.add_event(job_id, "job_cancelled", level="warning")
        return registry.get(job_id)
    if job.status not in {"running", "cancel_requested"}:
        return job
    registry.update(job_id, status="cancel_requested")
    if job.pid:
        try:
            if os.name == "posix":
                os.killpg(job.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows compatibility
                os.kill(job.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    registry.update(job_id, status="cancelled", pid=None, exit_code=130)
    registry.add_event(job_id, "job_cancelled", level="warning")
    return registry.get(job_id)


def _graph_artifacts(output_dir: Path) -> dict[str, dict[str, Any]]:
    state_path = output_dir / ".pipeline_graph" / "state.json"
    if not state_path.is_file() or state_path.is_symlink():
        return {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    nodes = state.get("nodes") if isinstance(state, dict) else None
    if not isinstance(nodes, dict):
        return {}
    artifacts: dict[str, dict[str, Any]] = {}
    for node in nodes.values():
        outputs = node.get("outputs") if isinstance(node, dict) else None
        if not isinstance(outputs, dict):
            continue
        for name, value in outputs.items():
            if name in PUBLICATION_KINDS and isinstance(value, dict):
                artifacts[name] = value
    return artifacts


def _release_report(
    output_dir: Path,
    identities: Mapping[str, Mapping[str, Any]],
    report_names: Iterable[str],
) -> tuple[str | None, Path | None, dict[str, Any] | None]:
    for artifact_name in report_names:
        identity = identities.get(artifact_name)
        raw_path = identity.get("path") if isinstance(identity, Mapping) else None
        expected_hash = identity.get("sha256") if isinstance(identity, Mapping) else None
        if not isinstance(raw_path, str) or not isinstance(expected_hash, str):
            continue
        path = Path(raw_path).expanduser().resolve()
        if (
            _is_relative_to(path, output_dir)
            and path.is_file()
            and not path.is_symlink()
            and _sha256_file(path) == expected_hash
        ):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return artifact_name, path, None
            return (
                artifact_name,
                path,
                payload if isinstance(payload, dict) else None,
            )
    return None, None, None


def _report_is_fresh(
    output_dir: Path,
    report_path: Path,
    *,
    publication_profile: str,
) -> bool:
    """Mirror the pipeline's release-report freshness boundary."""

    report_mtime = report_path.stat().st_mtime_ns
    watched = [
        output_dir / "toc.json",
        output_dir / "chapters.json",
        *output_dir.glob("*.docx"),
        *(output_dir / "chapters").glob("*.md"),
        *(output_dir / "reviewed_chapters").glob("*.md"),
        *(output_dir / "pages").glob("page_*.json"),
    ]
    if publication_profile == "full":
        watched.extend(
            [
                output_dir / "knowledge_base.jsonl",
                *output_dir.glob("*.epub"),
                *output_dir.glob("*_带目录.pdf"),
            ]
        )
    return not any(
        path.is_file() and path.stat().st_mtime_ns > report_mtime
        for path in watched
    )


def artifact_catalog(job: JobRecord) -> list[ArtifactRecord]:
    """Return only graph-identified publications plus known semantic drafts."""

    output = Path(job.spec.output_dir).resolve()
    identities = _graph_artifacts(output)
    requested = set(job.spec.targets)
    report_names = (
        ("publication.report",)
        if "publication.report" in requested
        else ("publication.word_report",)
        if "publication.word_report" in requested
        else ()
    )
    report_artifact, report_path, report = _release_report(
        output,
        identities,
        report_names,
    )
    expected_profile = (
        "full"
        if report_artifact == "publication.report"
        else "word"
        if report_artifact == "publication.word_report"
        else None
    )
    release_ready = bool(
        report
        and report.get("release_ready") is True
        and report.get("mode") == "full"
        and report.get("publication_profile") == expected_profile
        and report_path is not None
        and _report_is_fresh(
            output,
            report_path,
            publication_profile=expected_profile,
        )
    )
    covered_artifacts = (
        {"publication.docx"}
        if report_artifact == "publication.word_report"
        else set(PUBLICATION_KINDS)
        - {"publication.report", "publication.word_report"}
        if report_artifact == "publication.report"
        else set()
    )
    records: list[ArtifactRecord] = []
    for artifact_name, identity in sorted(identities.items()):
        if artifact_name in {"publication.report", "publication.word_report"}:
            continue
        raw_path = identity.get("path")
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path).expanduser().resolve()
        if not _is_relative_to(path, output) or not path.is_file() or path.is_symlink():
            continue
        expected_hash = identity.get("sha256")
        actual_hash = _sha256_file(path)
        identity_valid = isinstance(expected_hash, str) and expected_hash == actual_hash
        covered = artifact_name in covered_artifacts
        status = (
            "released"
            if covered and release_ready and identity_valid
            else "blocked"
            if covered and report_path is not None and not release_ready
            else "draft"
        )
        kind = PUBLICATION_KINDS[artifact_name]
        records.append(
            ArtifactRecord(
                schema_version=CONTRACT_SCHEMA_VERSION,
                name=path.name,
                kind=kind,
                path=path,
                status=status,
                sha256=actual_hash,
                media_type=MEDIA_TYPES[kind],
                report_path=report_path if covered else None,
            )
        )
    for artifact_name, filename in (
        ("publication.knowledge_base", "knowledge_base.jsonl"),
    ):
        if artifact_name not in requested or artifact_name in identities:
            continue
        path = (output / filename).resolve()
        if _is_relative_to(path, output) and path.is_file() and not path.is_symlink():
            kind = PUBLICATION_KINDS[artifact_name]
            records.append(
                ArtifactRecord(
                    schema_version=CONTRACT_SCHEMA_VERSION,
                    name=path.name,
                    kind=kind,
                    path=path,
                    status="draft",
                    sha256=_sha256_file(path),
                    media_type=MEDIA_TYPES[kind],
                    report_path=None,
                )
            )
    if report_path is not None and report_path.is_file():
        records.append(
            ArtifactRecord(
                schema_version=CONTRACT_SCHEMA_VERSION,
                name=report_path.name,
                kind="release_report",
                path=report_path,
                status="released" if release_ready else "blocked",
                sha256=_sha256_file(report_path),
                media_type=MEDIA_TYPES["release_report"],
                report_path=report_path,
            )
        )
    for name, kind in (
        ("semantic/translation-units.jsonl", "semantic_units"),
        ("audit/semantic-reconstruction.json", "semantic_audit"),
        ("audit/semantic-translation.json", "semantic_audit"),
    ):
        path = (output / name).resolve()
        if _is_relative_to(path, output) and path.is_file() and not path.is_symlink():
            records.append(
                ArtifactRecord(
                    schema_version=CONTRACT_SCHEMA_VERSION,
                    name=path.name,
                    kind=kind,
                    path=path,
                    status="draft",
                    sha256=_sha256_file(path),
                    media_type=MEDIA_TYPES[kind],
                    report_path=report_path,
                )
            )
    return records


def tail_log(job: JobRecord, *, max_bytes: int = 128 * 1024) -> str:
    path = job.log_path
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read().decode("utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Translation-agent Web UI job worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--database", required=True)
    execute.add_argument("--job-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "execute":
        registry = JobRegistry(args.database)
        job = registry.get(args.job_id)
        job.workspace.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            job.log_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        secrets = _redaction_values(os.environ)
        with os.fdopen(descriptor, "a", encoding="utf-8", buffering=1) as log:
            writer = _RedactingWriter(log, secrets)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                return execute_job(args.database, args.job_id)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "APP_VERSION",
    "FrontendSettings",
    "JobRecord",
    "JobRegistry",
    "PathPolicy",
    "artifact_catalog",
    "cancel_job",
    "child_environment",
    "execute_job",
    "plan_runspec",
    "safe_upload_name",
    "start_job_process",
    "tail_log",
    "validate_upload_size",
]
