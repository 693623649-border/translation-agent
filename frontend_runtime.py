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
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from product_contracts import (
    APP_VERSION,
    CONTRACT_SCHEMA_VERSION,
    ArtifactRecord,
    RunEvent,
    RunSpec,
)
from run_execution_service import RunExecutionService


DEFAULT_RUNTIME_ROOT = Path.home() / ".translation-agent" / "webui"
DEFAULT_DATABASE = DEFAULT_RUNTIME_ROOT / "jobs.sqlite3"
DEFAULT_JOBS_ROOT = DEFAULT_RUNTIME_ROOT / "jobs"
DEFAULT_UPLOAD_LIMIT = 256 * 1024 * 1024
DATABASE_SCHEMA_VERSION = 3
REDACTION_ENV_NAMES = "TRANSLATION_AGENT_REDACTION_ENV_NAMES"
DISABLE_DOTENV_ENV_NAME = "TRANSLATION_AGENT_DISABLE_DOTENV"
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
        REDACTION_ENV_NAMES,
        DISABLE_DOTENV_ENV_NAME,
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
RELEASE_PROFILES = frozenset({"draft", "draft-only", "word", "full"})
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


class WorkerLeaseLostError(RuntimeError):
    """Raised when a stale worker tries to mutate a newer dispatch generation."""


class JobCancellationRequested(RuntimeError):
    """Raised cooperatively when the registry records a cancellation request."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plan_metadata(
    targets: Sequence[str],
    release_profile: str | None,
) -> tuple[str, str | None]:
    if isinstance(targets, (str, bytes)) or not isinstance(targets, Sequence):
        raise ValueError("resolved targets must be a sequence of strings")
    normalized_targets = tuple(targets)
    if (
        not all(isinstance(target, str) and target for target in normalized_targets)
        or len(set(normalized_targets)) != len(normalized_targets)
    ):
        raise ValueError("resolved targets must be unique non-empty strings")
    if release_profile is not None and release_profile not in RELEASE_PROFILES:
        raise ValueError(f"unsupported release profile: {release_profile!r}")
    return json.dumps(list(normalized_targets), separators=(",", ":")), release_profile


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
    worker_token: str | None = None
    worker_started_at: str | None = None
    exit_code: int | None = None
    run_id: str | None = None
    error: str | None = None
    resolved_targets: tuple[str, ...] = ()
    release_profile: str | None = None

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
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(
                    "job database schema is newer than this application "
                    f"({version} > {DATABASE_SCHEMA_VERSION})"
                )
            if version < 1:
                self._migrate_to_v1(connection)
                connection.execute("PRAGMA user_version = 1")
                version = 1
            if version < 2:
                self._migrate_to_v2(connection)
                connection.execute("PRAGMA user_version = 2")
                version = 2
            if version < 3:
                self._migrate_to_v3(connection)
                connection.execute("PRAGMA user_version = 3")

    @staticmethod
    def _migrate_to_v1(connection: sqlite3.Connection) -> None:
        """Create the original registry schema, including legacy databases."""

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
    def _migrate_to_v2(connection: sqlite3.Connection) -> None:
        """Bind a PID to a per-dispatch identity before it may be signalled."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "worker_token" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN worker_token TEXT")
        if "worker_started_at" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN worker_started_at TEXT")

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection) -> None:
        """Persist the plan actually resolved from RunSpec plus Recipe defaults."""

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "resolved_targets_json" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN resolved_targets_json TEXT"
            )
        if "release_profile" not in columns:
            connection.execute("ALTER TABLE jobs ADD COLUMN release_profile TEXT")

    @staticmethod
    def _row(row: sqlite3.Row) -> JobRecord:
        raw_targets = row["resolved_targets_json"]
        resolved_targets: tuple[str, ...] = ()
        if raw_targets:
            try:
                decoded_targets = json.loads(str(raw_targets))
            except json.JSONDecodeError as exc:
                raise RuntimeError("job has corrupt resolved target metadata") from exc
            if (
                not isinstance(decoded_targets, list)
                or not all(
                    isinstance(target, str) and target for target in decoded_targets
                )
                or len(set(decoded_targets)) != len(decoded_targets)
            ):
                raise RuntimeError("job has corrupt resolved target metadata")
            resolved_targets = tuple(decoded_targets)
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
            worker_token=(
                str(row["worker_token"]) if row["worker_token"] else None
            ),
            worker_started_at=(
                str(row["worker_started_at"])
                if row["worker_started_at"]
                else None
            ),
            exit_code=(
                int(row["exit_code"]) if row["exit_code"] is not None else None
            ),
            run_id=str(row["run_id"]) if row["run_id"] else None,
            error=str(row["error"]) if row["error"] else None,
            resolved_targets=resolved_targets,
            release_profile=(
                str(row["release_profile"]) if row["release_profile"] else None
            ),
        )

    def create(
        self,
        job_id: str,
        workspace: Path,
        spec: RunSpec,
        *,
        resolved_targets: Sequence[str] = (),
        release_profile: str | None = None,
    ) -> JobRecord:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("job id must be a lowercase UUID hex value")
        now = _utc_now()
        source = Path(str(spec.source or "")).expanduser().resolve()
        target_json, normalized_profile = _plan_metadata(
            resolved_targets,
            release_profile,
        )
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
                    source_mode, source_path, spec_json,
                    resolved_targets_json, release_profile
                ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    now,
                    now,
                    str(workspace.resolve()),
                    spec.source_mode,
                    str(source),
                    _canonical_json(spec.to_dict()),
                    target_json,
                    normalized_profile,
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
        worker_token: str | None | object = ...,
        worker_started_at: str | None | object = ...,
        exit_code: int | None | object = ...,
        run_id: str | None | object = ...,
        error: str | None | object = ...,
        expected_worker_token: str | None = None,
        expected_statuses: Iterable[str] | None = None,
        resolved_targets: Sequence[str] | object = ...,
        release_profile: str | None | object = ...,
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
            ("worker_token", worker_token),
            ("worker_started_at", worker_started_at),
            ("exit_code", exit_code),
            ("run_id", run_id),
            ("error", error),
        ):
            if value is not ...:
                fields.append(f"{name} = ?")
                values.append(value)
        if resolved_targets is not ... or release_profile is not ...:
            current = self.get(job_id)
            target_value = (
                current.resolved_targets
                if resolved_targets is ...
                else resolved_targets
            )
            profile_value = (
                current.release_profile if release_profile is ... else release_profile
            )
            target_json, normalized_profile = _plan_metadata(
                target_value,
                profile_value,
            )
            fields.extend(
                ["resolved_targets_json = ?", "release_profile = ?"]
            )
            values.extend([target_json, normalized_profile])
        where = "id = ?"
        values.append(job_id)
        if expected_worker_token is not None:
            where += " AND worker_token = ?"
            values.append(expected_worker_token)
        normalized_statuses: tuple[str, ...] | None = None
        if expected_statuses is not None:
            normalized_statuses = tuple(dict.fromkeys(expected_statuses))
            if not normalized_statuses or not set(normalized_statuses) <= JOB_STATUSES:
                raise ValueError("expected_statuses must contain valid job statuses")
            placeholders = ", ".join("?" for _ in normalized_statuses)
            where += f" AND status IN ({placeholders})"
            values.extend(normalized_statuses)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE {where}", values
            )
        if cursor.rowcount != 1:
            if expected_worker_token is not None or normalized_statuses is not None:
                raise WorkerLeaseLostError(
                    f"worker lease or expected status is no longer active for task {job_id}"
                )
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
            identity = _worker_identity_status(job)
            if identity in {"missing", "mismatch"}:
                cancellation = job.status == "cancel_requested"
                detail = (
                    "background worker exited after cancellation request"
                    if cancellation
                    else "background worker is no longer running"
                    if identity == "missing"
                    else "stored worker PID no longer belongs to this task"
                )
                try:
                    self.update(
                        job.id,
                        status="cancelled" if cancellation else "interrupted",
                        pid=None,
                        worker_token=None,
                        worker_started_at=None,
                        exit_code=130 if cancellation else None,
                        error=None if cancellation else detail,
                        expected_worker_token=job.worker_token,
                        expected_statuses={job.status},
                    )
                except WorkerLeaseLostError:
                    continue
                self.add_event(
                    job.id,
                    "job_cancelled" if cancellation else "job_interrupted",
                    level="warning",
                    message=detail.capitalize(),
                    data={"identity_status": identity},
                )


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
    environment[DISABLE_DOTENV_ENV_NAME] = "1"
    explicit_names: list[str] = []
    for name, value in credentials.items():
        if not ENV_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid credential environment variable: {name!r}")
        if name in FORBIDDEN_CREDENTIAL_ENVIRONMENT_NAMES:
            raise ValueError(
                f"credential environment variable may not override runtime control: {name!r}"
            )
        if value:
            environment[name] = str(value)
            explicit_names.append(name)
    if explicit_names:
        environment[REDACTION_ENV_NAMES] = json.dumps(
            sorted(explicit_names),
            ensure_ascii=True,
            separators=(",", ":"),
        )
    return environment


def _explicit_redaction_names(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment.get(REDACTION_ENV_NAMES, "")
    if not raw:
        return ()
    try:
        names = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(names, list):
        return ()
    return tuple(
        name
        for name in names
        if isinstance(name, str) and ENV_NAME_PATTERN.fullmatch(name)
    )


def _redaction_values(
    environment: Mapping[str, str],
    *,
    explicit_names: Iterable[str] = (),
) -> tuple[str, ...]:
    values: set[str] = set()
    direct_names = set(explicit_names) | set(_explicit_redaction_names(environment))
    for name, value in environment.items():
        upper_name = name.upper()
        if not value:
            continue
        if (
            name in direct_names
            or "KEY" in upper_name
            or "TOKEN" in upper_name
            or "SECRET" in upper_name
            or "PASSWORD" in upper_name
            or ("PROXY" in upper_name and "@" in value)
        ):
            values.add(value)
    return tuple(sorted(values, key=len, reverse=True))


def _worker_identity_status(job: JobRecord) -> str:
    """Return match/mismatch/missing/unknown without ever signalling a PID."""

    if not job.pid or not job.worker_token:
        return "mismatch"
    try:
        os.kill(job.pid, 0)
    except ProcessLookupError:
        return "missing"
    except PermissionError:
        return "unknown"

    command: str | None = None
    proc_command = Path(f"/proc/{job.pid}/cmdline")
    if proc_command.is_file():
        try:
            command = proc_command.read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", errors="replace"
            )
        except OSError:
            return "unknown"
    elif os.name == "posix":
        try:
            completed = subprocess.run(
                ["ps", "-ww", "-p", str(job.pid), "-o", "command="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        if completed.returncode != 0:
            return "missing"
        command = completed.stdout.strip()
    if command is None:
        return "unknown"

    job_pattern = re.compile(
        rf"(?:^|\s)--job-id(?:=|\s+){re.escape(job.id)}(?:\s|$)"
    )
    token_pattern = re.compile(
        rf"(?:^|\s)--worker-token(?:=|\s+){re.escape(job.worker_token)}(?:\s|$)"
    )
    return (
        "match"
        if job_pattern.search(command) and token_pattern.search(command)
        else "mismatch"
    )


def _redact(value: str, secrets: Iterable[str] | None = None) -> str:
    result = value
    selected = _redaction_values(os.environ) if secrets is None else tuple(secrets)
    for secret in selected:
        result = result.replace(secret, "<redacted>")
    return result


class _RedactingWriter:
    def __init__(self, stream: Any, secrets: Iterable[str]) -> None:
        self._stream = stream
        self._secrets = tuple(
            sorted(
                {str(secret) for secret in secrets if str(secret)},
                key=len,
                reverse=True,
            )
        )
        self._pending = ""
        self._closed = False
        self._lock = threading.RLock()

    def _pending_prefix_length(self, value: str) -> int:
        length = 0
        for secret in self._secrets:
            upper = min(len(value), len(secret))
            for size in range(upper, length, -1):
                if value.endswith(secret[:size]):
                    length = size
                    break
        return length

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("redacting writer accepts text only")
        with self._lock:
            if self._closed:
                raise ValueError("I/O operation on closed redacting writer")
            combined = self._pending + value
            pending_length = self._pending_prefix_length(combined)
            if pending_length:
                safe = combined[:-pending_length]
                self._pending = combined[-pending_length:]
            else:
                safe = combined
                self._pending = ""
            if safe:
                self._stream.write(_redact(safe, self._secrets))
            self._stream.flush()
            return len(value)

    def flush(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._pending:
                # Pending text is a credential or one of its prefixes.  Once
                # the caller requests a hard flush there is no future chunk
                # with which to disambiguate it, so fail closed and mask it.
                self._stream.write("<redacted>")
                self._pending = ""
            self._stream.flush()

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.flush()
            self._closed = True
            self._stream.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def plan_runspec(spec: RunSpec) -> tuple[str, ...]:
    """Compatibility facade that delegates to the single product compiler."""

    return RunExecutionService(load_dotenv=False).plan(spec).node_names


def execute_job(database: Path | str, job_id: str, worker_token: str) -> int:
    registry = JobRegistry(database)
    # Wait for the dispatcher to persist our PID.  Without this handshake a
    # very short task can finish before the parent updates the row, leaving a
    # succeeded task incorrectly marked as running.
    for _attempt in range(200):
        job = registry.get(job_id)
        if job.pid == os.getpid() and job.worker_token == worker_token:
            break
        if job.status in {"cancel_requested", "cancelled"}:
            return 130
        time.sleep(0.01)
    else:
        try:
            registry.update(
                job_id,
                status="failed",
                pid=None,
                worker_token=None,
                worker_started_at=None,
                exit_code=1,
                error="dispatcher did not register worker identity",
                expected_worker_token=worker_token,
                expected_statuses={"running"},
            )
        except WorkerLeaseLostError:
            return 130
        return 1
    registry.update(
        job_id,
        status="running",
        error=None,
        expected_worker_token=worker_token,
        expected_statuses={"running"},
    )
    registry.add_event(job_id, "job_started", data={"pid": os.getpid()})
    try:
        execution = RunExecutionService(load_dotenv=False)
        plan = execution.plan(job.spec)
        registry.update(
            job_id,
            resolved_targets=plan.targets,
            release_profile=plan.release_profile,
            expected_worker_token=worker_token,
            expected_statuses={"running"},
        )

        def progress(payload: Mapping[str, Any]) -> None:
            current = registry.get(job_id)
            if current.worker_token != worker_token:
                raise WorkerLeaseLostError(
                    f"worker lease is no longer active for task {job_id}"
                )
            if current.status == "cancel_requested":
                raise JobCancellationRequested(
                    f"cancellation requested for task {job_id}"
                )
            if current.status != "running":
                raise WorkerLeaseLostError(
                    f"worker status is no longer active for task {job_id}"
                )
            event = str(payload.get("event") or "execution_progress")
            data = {
                str(name): value
                for name, value in payload.items()
                if name not in {"event", "schema_version"}
            }
            registry.add_event(job_id, event, data=data)

        result = execution.execute(job.spec, progress=progress)
        if result.status != "passed":
            raise RuntimeError(
                f"execution service returned non-success status: {result.status}"
            )
        if tuple(result.targets) != tuple(plan.targets):
            raise RuntimeError(
                "execution result does not satisfy the resolved target contract: "
                f"expected {list(plan.targets)}, got {list(result.targets)}"
            )
        if plan.release_profile in {"word", "full"} and not result.release_ready:
            raise RuntimeError(
                "execution completed without a release-ready verifier report for "
                f"profile {plan.release_profile!r}"
            )
        run_id = result.run_id
        registry.update(
            job_id,
            status="succeeded",
            pid=None,
            worker_token=None,
            worker_started_at=None,
            exit_code=0,
            run_id=run_id,
            error=None,
            expected_worker_token=worker_token,
            expected_statuses={"running"},
        )
        registry.add_event(
            job_id,
            "job_succeeded",
            data={
                "run_id": run_id,
                "targets": list(result.targets),
                "release_profile": result.release_profile,
            },
        )
        return 0
    except (KeyboardInterrupt, JobCancellationRequested):
        try:
            registry.update(
                job_id,
                status="cancelled",
                pid=None,
                worker_token=None,
                worker_started_at=None,
                exit_code=130,
                expected_worker_token=worker_token,
                expected_statuses={"running", "cancel_requested"},
            )
        except WorkerLeaseLostError:
            return 130
        registry.add_event(job_id, "job_cancelled", level="warning")
        return 130
    except WorkerLeaseLostError:
        return 130
    except BaseException as exc:  # worker boundary records a resumable failure
        message = _redact(str(exc).replace("\x00", ""))[:2000]
        print(f"[job-error] {type(exc).__name__}: {message}", file=sys.stderr)
        traceback.print_exc()
        try:
            registry.update(
                job_id,
                status="failed",
                pid=None,
                worker_token=None,
                worker_started_at=None,
                exit_code=1,
                error=message,
                expected_worker_token=worker_token,
                expected_statuses={"running"},
            )
        except WorkerLeaseLostError:
            return 130
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
    worker_token = uuid.uuid4().hex
    worker_started_at = _utc_now()
    registry.update(
        job_id,
        status="running",
        pid=None,
        worker_token=worker_token,
        worker_started_at=worker_started_at,
        exit_code=None,
        error=None,
        expected_statuses={job.status},
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
        "--worker-token",
        worker_token,
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
        secrets = tuple(str(value) for value in (credentials or {}).values() if value)
        registry.update(
            job_id,
            status="failed",
            pid=None,
            worker_token=None,
            worker_started_at=None,
            exit_code=1,
            error=_redact(str(exc), secrets)[:2000],
            expected_worker_token=worker_token,
            expected_statuses={"running"},
        )
        raise
    finally:
        log_handle.close()
    registry.update(
        job_id,
        pid=process.pid,
        expected_worker_token=worker_token,
        expected_statuses={"running"},
    )
    registry.add_event(
        job_id,
        "job_dispatched",
        data={"pid": process.pid, "worker_started_at": worker_started_at},
    )
    return registry.get(job_id)


def cancel_job(registry: JobRegistry, job_id: str) -> JobRecord:
    job = registry.get(job_id)
    if job.status == "queued":
        try:
            registry.update(
                job_id,
                status="cancelled",
                pid=None,
                worker_token=None,
                worker_started_at=None,
                exit_code=130,
                expected_statuses={"queued"},
            )
        except WorkerLeaseLostError:
            return registry.get(job_id)
        registry.add_event(job_id, "job_cancelled", level="warning")
        return registry.get(job_id)
    if job.status not in {"running", "cancel_requested"}:
        return job
    if job.status == "running":
        try:
            job = registry.update(
                job_id,
                status="cancel_requested",
                expected_worker_token=job.worker_token,
                expected_statuses={"running"},
            )
        except WorkerLeaseLostError:
            return registry.get(job_id)
    identity = _worker_identity_status(job)
    signalled = False
    if identity == "match" and job.pid:
        try:
            if os.name == "posix":
                os.killpg(job.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows compatibility
                os.kill(job.pid, signal.SIGTERM)
            signalled = True
        except ProcessLookupError:
            identity = "missing"
    elif identity == "unknown":
        registry.add_event(
            job_id,
            "job_cancel_signal_refused",
            level="warning",
            message="Worker identity could not be verified; PID was not signalled",
        )
        raise RuntimeError(
            "worker identity could not be verified; refusing to signal its PID"
        )

    if signalled:
        registry.add_event(
            job_id,
            "job_cancel_requested",
            level="warning",
            data={"worker_signalled": True, "identity_status": identity},
        )
        # SIGTERM is only a request.  Keep the lease until the worker exits
        # cooperatively or reconciliation proves that its process disappeared.
        return registry.get(job_id)

    try:
        registry.update(
            job_id,
            status="cancelled",
            pid=None,
            worker_token=None,
            worker_started_at=None,
            exit_code=130,
            expected_worker_token=job.worker_token,
            expected_statuses={"cancel_requested"},
        )
    except WorkerLeaseLostError:
        return registry.get(job_id)
    registry.add_event(
        job_id,
        "job_cancelled",
        level="warning",
        data={"worker_signalled": False, "identity_status": identity},
    )
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
    has_resolved_plan = job.release_profile is not None
    requested = set(
        job.resolved_targets if has_resolved_plan else job.spec.targets
    )
    expected_profile = (
        "full"
        if job.release_profile == "full" and "publication.report" in requested
        else "word"
        if job.release_profile == "word"
        and "publication.word_report" in requested
        else "full"
        if not has_resolved_plan and "publication.report" in requested
        else "word"
        if not has_resolved_plan and "publication.word_report" in requested
        else None
    )
    report_names = (
        ("publication.report",)
        if expected_profile == "full"
        else ("publication.word_report",)
        if expected_profile == "word"
        else ()
    )
    _report_artifact, report_path, report = _release_report(
        output,
        identities,
        report_names,
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
        if expected_profile == "word"
        else set(PUBLICATION_KINDS)
        - {"publication.report", "publication.word_report"}
        if expected_profile == "full"
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
            if covered and not release_ready
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
    execute.add_argument("--worker-token", required=True)
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
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(
                    writer
                ):
                    return execute_job(args.database, args.job_id, args.worker_token)
            finally:
                writer.flush()
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
