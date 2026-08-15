import hashlib
import io
import json
import os
import signal
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import document_pipeline
import launch_frontend
from application_service import ApplicationService
from frontend_runtime import (
    FrontendSettings,
    JobRegistry,
    PathPolicy,
    WorkerLeaseLostError,
    _RedactingWriter,
    artifact_catalog,
    cancel_job,
    child_environment,
    execute_job,
    plan_runspec as frontend_plan_runspec,
    _redaction_values,
    safe_upload_name,
    start_job_process,
    validate_upload_size,
)
from product_contracts import RunSpec
from semantic_review_policy import refresh_semantic_review
from tests.test_epub_semantic_import import _write_epub


def _write_pdf_reconstruction(
    output: Path,
    *,
    blocked: bool = False,
) -> Path:
    output = output.resolve()
    issue = {
        "code": "semantic_structure_invalid",
        "message": "Structural evidence is incomplete",
        "blocking": True,
        "source_page": "pdf-0001-0001",
        "note_label": None,
        "evidence": {"page": 1},
    }
    chapter_issues = [issue] if blocked else []
    reconstruction = {
        "schema_version": 1,
        "status": "blocked" if blocked else "passed",
        "release_blocked": blocked,
        "generated_by": "test",
        "contract_mode": "born-digital-pdf-text-layer",
        "importer_version": "test-v1",
        "source": {
            "path": "/source/book.pdf",
            "sha256": hashlib.sha256(b"source").hexdigest(),
        },
        "issues": [],
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "filename": "chapter-1.md",
                "markdown_sha256": hashlib.sha256(b"chapter").hexdigest(),
                "footnote_count": 0,
                "issues": chapter_issues,
                "release_blocked": blocked,
            }
        ],
        "summary": {
            "chapter_count": 1,
            "footnote_count": 0,
            "issue_count": len(chapter_issues),
            "blocking_issue_count": len(chapter_issues),
            "release_blocked": blocked,
        },
    }
    reconstruction_path = output / "audit" / "semantic-reconstruction.json"
    reconstruction_path.parent.mkdir(parents=True, exist_ok=True)
    reconstruction_path.write_text(
        json.dumps(reconstruction, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return reconstruction_path


def _write_pdf_semantic_review(
    output: Path,
    *,
    blocked: bool = False,
) -> tuple[dict[str, object], Path, Path]:
    output = output.resolve()
    _write_pdf_reconstruction(output, blocked=blocked)
    artifact = refresh_semantic_review(output, create_decision_log=True)
    semantic = {
        "reconstruction_sha256": artifact.resolution.reconstruction_sha256,
        "review_required": True,
        "review_sha256": artifact.audit_sha256,
        "review_decision_log_sha256": (
            artifact.resolution.decision_log_sha256
        ),
        "review_policy_fingerprint": (
            artifact.resolution.review_policy_fingerprint
        ),
        "review_issue_set_sha256": artifact.resolution.issue_set_sha256,
    }
    return (
        semantic,
        artifact.audit_path,
        output / "audit" / "review-decisions.jsonl",
    )


def _pdf_catalog_fixture(
    root: Path,
    *,
    profile: str,
    blocked_review: bool = False,
    include_binding: bool = True,
    review_required: bool = True,
) -> SimpleNamespace:
    root = root.resolve()
    workspace = root / "job"
    output = workspace / "output"
    output.mkdir(parents=True)
    source = workspace / "book.pdf"
    source.write_bytes(b"pdf")
    report_artifact = (
        "publication.word_report" if profile == "word" else "publication.report"
    )
    report_name = (
        "word-release-report.json" if profile == "word" else "release-report.json"
    )
    registry = JobRegistry(root / "jobs.sqlite3")
    job = registry.create(
        "b" * 32,
        workspace,
        RunSpec(
            source=source,
            source_mode="text-pdf",
            output_dir=output,
            targets=(report_artifact,),
        ),
        resolved_targets=(report_artifact,),
        release_profile=profile,
    )
    publication = output / "book.docx"
    publication.write_bytes(b"docx")
    publication_sha256 = hashlib.sha256(publication.read_bytes()).hexdigest()
    if review_required:
        semantic, review_audit, decision_log = _write_pdf_semantic_review(
            output,
            blocked=blocked_review,
        )
    else:
        reconstruction = _write_pdf_reconstruction(
            output,
            blocked=blocked_review,
        )
        legacy_payload = json.loads(reconstruction.read_text(encoding="utf-8"))
        for key in ("contract_mode", "importer_version", "source"):
            legacy_payload.pop(key, None)
        reconstruction.write_text(
            json.dumps(legacy_payload, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        semantic = {
            "reconstruction_sha256": hashlib.sha256(
                reconstruction.read_bytes()
            ).hexdigest(),
            "review_required": False,
            "review_sha256": None,
            "review_decision_log_sha256": None,
            "review_policy_fingerprint": None,
            "review_issue_set_sha256": None,
        }
        review_audit = output / "audit" / "semantic-review.json"
        decision_log = output / "audit" / "review-decisions.jsonl"
    report = output / "audit" / report_name
    payload: dict[str, object] = {
        "release_ready": True,
        "mode": "full",
        "publication_profile": profile,
    }
    if include_binding:
        payload["semantic"] = semantic
    report.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    metadata = output / ".pipeline_graph"
    metadata.mkdir()
    state_path = metadata / "state.json"

    def write_state() -> None:
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "nodes": {
                        "core.publish.docx": {
                            "outputs": {
                                "publication.docx": {
                                    "path": str(publication),
                                    "sha256": publication_sha256,
                                }
                            }
                        },
                        f"core.publication.verify.{profile}": {
                            "outputs": {
                                report_artifact: {
                                    "path": str(report),
                                    "sha256": hashlib.sha256(
                                        report.read_bytes()
                                    ).hexdigest(),
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    write_state()
    return SimpleNamespace(
        job=job,
        report=report,
        publication=publication,
        review_audit=review_audit,
        decision_log=decision_log,
        write_state=write_state,
    )


class FrontendRuntimeTests(unittest.TestCase):
    def test_dispatcher_keeps_credentials_out_of_argv_and_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "c" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    translate=False,
                    verify=False,
                ),
            )
            fake_process = type("Process", (), {"pid": 43210})()
            with patch(
                "frontend_runtime.subprocess.Popen", return_value=fake_process
            ) as popen:
                started = start_job_process(
                    registry,
                    job.id,
                    credentials={"MODEL_API_KEY": "secret-value"},
                )
            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(popen.call_args.kwargs["cwd"], workspace.resolve())
            self.assertNotIn("secret-value", " ".join(command))
            self.assertEqual(environment["MODEL_API_KEY"], "secret-value")
            self.assertNotIn("secret-value", registry.get(job.id).spec.to_dict().values())
            self.assertEqual(started.pid, 43210)
            self.assertRegex(started.worker_token or "", r"^[0-9a-f]{32}$")
            self.assertIn("--worker-token", command)

    def test_launcher_rejects_non_loopback_bind_address(self) -> None:
        with patch("launch_frontend.subprocess.Popen") as popen:
            with self.assertRaises(SystemExit) as caught:
                launch_frontend.main(["--host", "0.0.0.0", "--no-browser"])
        self.assertEqual(caught.exception.code, 2)
        popen.assert_not_called()

    def test_launcher_overrides_global_streamlit_development_mode(self) -> None:
        with patch("launch_frontend.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            self.assertEqual(
                launch_frontend.main(["--port", "8507", "--no-browser"]),
                0,
            )
        command = popen.call_args.args[0]
        self.assertIn("--global.developmentMode=false", command)
        self.assertIn("--server.port=8507", command)

    def test_sqlite_registry_uses_wal_and_round_trips_versioned_spec(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "jobs.sqlite3"
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            workspace = root / "job"
            workspace.mkdir()
            output = workspace / "output"
            output.mkdir()
            registry = JobRegistry(database)
            spec = RunSpec(
                source=source,
                source_mode="text-pdf",
                output_dir=output,
                targets=("publication.docx",),
                translate=False,
                verify=False,
            )
            created = registry.create(
                "a" * 32,
                workspace,
                spec,
                resolved_targets=("publication.docx",),
                release_profile="draft",
            )

            with sqlite3.connect(database) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
            self.assertEqual(mode, "wal")
            self.assertEqual(version, 3)
            self.assertIn("worker_token", columns)
            self.assertIn("worker_started_at", columns)
            self.assertIn("resolved_targets_json", columns)
            self.assertIn("release_profile", columns)
            self.assertEqual(created.spec.to_dict(), spec.to_dict())
            self.assertEqual(created.resolved_targets, ("publication.docx",))
            self.assertEqual(created.release_profile, "draft")
            self.assertEqual(registry.events(created.id)[0]["event"], "job_created")

    def test_sqlite_registry_migrates_unversioned_job_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, status TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        workspace TEXT NOT NULL, source_mode TEXT NOT NULL,
                        source_path TEXT NOT NULL, spec_json TEXT NOT NULL,
                        pid INTEGER, exit_code INTEGER, run_id TEXT, error TEXT
                    )
                    """
                )
            JobRegistry(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 3
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
            self.assertIn("worker_token", columns)
            self.assertIn("worker_started_at", columns)
            self.assertIn("resolved_targets_json", columns)
            self.assertIn("release_profile", columns)

    def test_sqlite_registry_migrates_v1_job_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v1.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, status TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        workspace TEXT NOT NULL, source_mode TEXT NOT NULL,
                        source_path TEXT NOT NULL, spec_json TEXT NOT NULL,
                        pid INTEGER, exit_code INTEGER, run_id TEXT, error TEXT
                    );
                    CREATE TABLE job_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL, payload_json TEXT NOT NULL
                    );
                    PRAGMA user_version = 1;
                    """
                )
            JobRegistry(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 3
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
            self.assertIn("worker_token", columns)
            self.assertIn("worker_started_at", columns)
            self.assertIn("resolved_targets_json", columns)
            self.assertIn("release_profile", columns)

    def test_sqlite_registry_migrates_v2_plan_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "v2.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, status TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                        workspace TEXT NOT NULL, source_mode TEXT NOT NULL,
                        source_path TEXT NOT NULL, spec_json TEXT NOT NULL,
                        pid INTEGER, exit_code INTEGER, run_id TEXT, error TEXT,
                        worker_token TEXT, worker_started_at TEXT
                    );
                    CREATE TABLE job_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL, payload_json TEXT NOT NULL
                    );
                    PRAGMA user_version = 2;
                    """
                )
            JobRegistry(database)
            with sqlite3.connect(database) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 3
                )
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
                }
            self.assertIn("resolved_targets_json", columns)
            self.assertIn("release_profile", columns)

    def test_path_policy_rejects_files_outside_allowlisted_roots(self) -> None:
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as denied:
            accepted = Path(allowed) / "accepted.pdf"
            accepted.write_bytes(b"pdf")
            rejected = Path(denied) / "rejected.pdf"
            rejected.write_bytes(b"pdf")
            policy = PathPolicy([allowed])
            self.assertEqual(
                policy.resolve_file(accepted, suffixes={".pdf"}),
                accepted.resolve(),
            )
            with self.assertRaisesRegex(ValueError, "outside configured"):
                policy.resolve_file(rejected, suffixes={".pdf"})

    def test_upload_is_bounded_sanitized_and_uses_uuid_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FrontendSettings(
                database=root / "jobs.sqlite3",
                jobs_root=root / "jobs",
                source_roots=(root,),
                upload_limit_bytes=8,
            )
            service = ApplicationService(settings)
            spec = RunSpec(
                source_mode="text-pdf",
                output_dir="ignored",
                targets=("publication.docx",),
                translate=False,
                verify=False,
            )
            job = service.submit_upload(
                spec,
                filename="../../危险 书?.PDF",
                content=b"12345678",
                start=False,
            )
            self.assertRegex(job.id, r"^[0-9a-f]{32}$")
            self.assertEqual(job.source_path.name, "危险_书.pdf")
            self.assertTrue(job.source_path.is_relative_to(job.workspace))
            self.assertTrue(Path(job.spec.output_dir).is_relative_to(job.workspace))
            self.assertEqual(job.resolved_targets, ("publication.docx",))
            self.assertEqual(job.release_profile, "draft")
            run_spec = json.loads(
                (job.workspace / "run-spec.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("credentials", run_spec)

            with self.assertRaisesRegex(ValueError, "upload exceeds"):
                service.submit_upload(
                    spec,
                    filename="large.pdf",
                    content=b"123456789",
                    start=False,
                )
            self.assertEqual(
                sorted(path.name for path in settings.jobs_root.iterdir()),
                [job.id],
            )

    def test_child_environment_is_allowlisted_and_credentials_are_explicit(self) -> None:
        environment = child_environment(
            {"MODEL_API_KEY": "secret"},
            base={
                "PATH": "/usr/bin",
                "HOME": "/tmp/home",
                "AMBIENT_API_KEY": "must-not-leak",
            },
        )
        self.assertEqual(environment["MODEL_API_KEY"], "secret")
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertEqual(environment["TRANSLATION_AGENT_DISABLE_DOTENV"], "1")
        self.assertNotIn("AMBIENT_API_KEY", environment)
        with self.assertRaisesRegex(ValueError, "runtime control"):
            child_environment({"PYTHONPATH": "/tmp/injected"}, base={})
        with self.assertRaisesRegex(ValueError, "runtime control"):
            child_environment(
                {"TRANSLATION_AGENT_DISABLE_DOTENV": "0"}, base={}
            )

    def test_explicit_credential_values_are_redacted_without_name_guessing(self) -> None:
        environment = child_environment(
            {"MODEL_AUTH": "opaque-value"},
            base={"PATH": "/usr/bin"},
        )
        self.assertIn("opaque-value", _redaction_values(environment))

    def test_redacting_writer_masks_secrets_split_across_writes(self) -> None:
        stream = io.StringIO()
        writer = _RedactingWriter(stream, ("secret-value",))
        writer.write("before secret-")
        self.assertEqual(stream.getvalue(), "before ")
        writer.write("value after")
        writer.flush()
        self.assertEqual(stream.getvalue(), "before <redacted> after")
        self.assertNotIn("secret-value", stream.getvalue())

    def test_redacting_writer_flush_and_close_mask_partial_secret(self) -> None:
        class CloseTrackingStream(io.StringIO):
            was_closed = False

            def close(self) -> None:
                self.was_closed = True

        stream = CloseTrackingStream()
        writer = _RedactingWriter(stream, ("secret-value",))
        writer.write("partial secret-")
        writer.flush()
        self.assertEqual(stream.getvalue(), "partial <redacted>")
        writer.write("tail secret-")
        writer.close()
        self.assertTrue(writer.closed)
        self.assertTrue(stream.was_closed)
        self.assertEqual(
            stream.getvalue(),
            "partial <redacted>tail <redacted>",
        )

    def test_cancel_refuses_to_signal_a_reused_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "e" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    translate=False,
                    verify=False,
                ),
            )
            registry.update(
                job.id,
                status="running",
                pid=32123,
                worker_token="f" * 32,
                worker_started_at="2026-08-15T00:00:00+00:00",
            )
            with patch(
                "frontend_runtime._worker_identity_status", return_value="mismatch"
            ), patch("frontend_runtime.os.killpg") as killpg:
                cancelled = cancel_job(registry, job.id)
            killpg.assert_not_called()
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNone(cancelled.worker_token)
            self.assertFalse(
                registry.events(job.id)[-1]["data"]["worker_signalled"]
            )

    def test_cancel_fails_closed_when_worker_identity_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "4" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    translate=False,
                    verify=False,
                ),
            )
            token = "3" * 32
            registry.update(
                job.id,
                status="running",
                pid=32124,
                worker_token=token,
                worker_started_at="2026-08-15T00:00:00+00:00",
            )
            with patch(
                "frontend_runtime._worker_identity_status", return_value="unknown"
            ), patch("frontend_runtime.os.killpg") as killpg, self.assertRaisesRegex(
                RuntimeError, "refusing to signal"
            ):
                cancel_job(registry, job.id)
            killpg.assert_not_called()
            current = registry.get(job.id)
            self.assertEqual(current.status, "cancel_requested")
            self.assertEqual(current.worker_token, token)

    def test_cancel_keeps_worker_lease_until_reconcile_confirms_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "2" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    translate=False,
                    verify=False,
                ),
            )
            token = "1" * 32
            registry.update(
                job.id,
                status="running",
                pid=32125,
                worker_token=token,
                worker_started_at="2026-08-15T00:00:00+00:00",
            )
            with patch(
                "frontend_runtime._worker_identity_status", return_value="match"
            ), patch("frontend_runtime.os.killpg") as killpg:
                requested = cancel_job(registry, job.id)
            killpg.assert_called_once_with(32125, signal.SIGTERM)
            self.assertEqual(requested.status, "cancel_requested")
            self.assertEqual(requested.pid, 32125)
            self.assertEqual(requested.worker_token, token)
            with self.assertRaises(WorkerLeaseLostError):
                registry.update(
                    job.id,
                    status="succeeded",
                    expected_worker_token=token,
                    expected_statuses={"running"},
                )
            with self.assertRaises(WorkerLeaseLostError):
                registry.update(
                    job.id,
                    status="failed",
                    expected_worker_token=token,
                    expected_statuses={"running"},
                )

            with patch(
                "frontend_runtime._worker_identity_status", return_value="missing"
            ):
                registry.reconcile_workers()
            cancelled = registry.get(job.id)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNone(cancelled.pid)
            self.assertIsNone(cancelled.worker_token)
            self.assertEqual(cancelled.exit_code, 130)

    def test_artifact_catalog_requires_graph_identity_and_release_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            output = workspace / "output"
            output.mkdir(parents=True)
            source = workspace / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            spec = RunSpec(
                source=source,
                source_mode="text-pdf",
                output_dir=output,
                targets=("publication.word_report",),
            )
            job = registry.create(
                "b" * 32,
                workspace,
                spec,
                resolved_targets=("publication.word_report",),
                release_profile="word",
            )
            docx = output / "book.docx"
            docx.write_bytes(b"docx")
            sha256 = hashlib.sha256(b"docx").hexdigest()
            audit = output / "audit"
            semantic, _review_audit, _decision_log = _write_pdf_semantic_review(
                output
            )
            report = audit / "word-release-report.json"
            report.write_text(
                json.dumps(
                    {
                        "release_ready": True,
                        "mode": "full",
                        "publication_profile": "word",
                        "semantic": semantic,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            metadata = output / ".pipeline_graph"
            metadata.mkdir()

            def write_state() -> None:
                (metadata / "state.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "nodes": {
                                "core.publish.docx": {
                                    "outputs": {
                                        "publication.docx": {
                                            "path": str(docx),
                                            "sha256": sha256,
                                        }
                                    }
                                },
                                "core.publication.verify.word": {
                                    "outputs": {
                                        "publication.word_report": {
                                            "path": str(report),
                                            "sha256": hashlib.sha256(
                                                report.read_bytes()
                                            ).hexdigest(),
                                        }
                                    }
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )

            write_state()

            records = artifact_catalog(job)
            publication = next(record for record in records if record.kind == "docx")
            self.assertEqual(publication.status, "released")

            # Recipe/default targets are absent from the original RunSpec.
            # Catalog decisions must follow the plan persisted by the service.
            resolved_job = registry.create(
                "5" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=output,
                    targets=(),
                    verify=True,
                ),
                resolved_targets=("publication.word_report",),
                release_profile="word",
            )
            resolved_publication = next(
                record
                for record in artifact_catalog(resolved_job)
                if record.kind == "docx"
            )
            self.assertEqual(resolved_publication.status, "released")
            self.assertEqual(resolved_publication.report_path, report.resolve())

            stale_epub = output / "old.epub"
            stale_epub.write_bytes(b"epub")
            state_path = metadata / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["nodes"]["old.epub"] = {
                "outputs": {
                    "publication.epub": {
                        "path": str(stale_epub),
                        "sha256": hashlib.sha256(b"epub").hexdigest(),
                    }
                }
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            epub = next(
                record for record in artifact_catalog(job) if record.kind == "epub"
            )
            self.assertEqual(epub.status, "draft")
            self.assertIsNone(epub.report_path)
            docx_after_epub = next(
                record for record in artifact_catalog(job) if record.kind == "docx"
            )
            self.assertEqual(docx_after_epub.status, "released")

            report.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "mode": "full",
                        "publication_profile": "word",
                        "semantic": semantic,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            write_state()
            publication = next(
                record for record in artifact_catalog(job) if record.kind == "docx"
            )
            self.assertEqual(publication.status, "blocked")

            # A direct draft target must not inherit an old report merely
            # because both files remain in the same resumable Graph state.
            draft_job = registry.create(
                "d" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=output,
                    targets=("publication.docx",),
                    verify=False,
                ),
            )
            draft_publication = next(
                record
                for record in artifact_catalog(draft_job)
                if record.kind == "docx"
            )
            self.assertEqual(draft_publication.status, "draft")
            self.assertIsNone(draft_publication.report_path)

    def test_pdf_release_catalog_requires_report_bound_central_review(self) -> None:
        for profile in ("word", "full"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                fixture = _pdf_catalog_fixture(Path(directory), profile=profile)
                publication = next(
                    record
                    for record in artifact_catalog(fixture.job)
                    if record.kind == "docx"
                )
                self.assertEqual(publication.status, "released")

            with (
                self.subTest(profile=profile, evidence="legacy-without-review"),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = _pdf_catalog_fixture(
                    Path(directory),
                    profile=profile,
                    review_required=False,
                )
                publication = next(
                    record
                    for record in artifact_catalog(fixture.job)
                    if record.kind == "docx"
                )
                self.assertEqual(publication.status, "released")

            with (
                self.subTest(profile=profile, evidence="report-binding-missing"),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = _pdf_catalog_fixture(
                    Path(directory),
                    profile=profile,
                    include_binding=False,
                )
                publication = next(
                    record
                    for record in artifact_catalog(fixture.job)
                    if record.kind == "docx"
                )
                self.assertEqual(publication.status, "blocked")

    def test_pdf_release_catalog_rejects_stale_or_blocked_review_evidence(self) -> None:
        scenarios = (
            "backdated-review-audit",
            "backdated-decision-log",
            "missing-review-audit",
            "missing-decision-log",
            "report-binding-mismatch",
            "blocked-resolution",
        )
        for profile in ("word", "full"):
            for scenario in scenarios:
                with (
                    self.subTest(profile=profile, scenario=scenario),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = _pdf_catalog_fixture(
                        Path(directory),
                        profile=profile,
                        blocked_review=scenario == "blocked-resolution",
                    )
                    older = max(
                        1,
                        fixture.report.stat().st_mtime_ns - 1_000_000_000,
                    )
                    if scenario == "backdated-review-audit":
                        payload = json.loads(
                            fixture.review_audit.read_text(encoding="utf-8")
                        )
                        payload["summary"]["resolved_issue_count"] = 99
                        fixture.review_audit.write_text(
                            json.dumps(payload),
                            encoding="utf-8",
                        )
                        os.utime(fixture.review_audit, ns=(older, older))
                    elif scenario == "backdated-decision-log":
                        fixture.decision_log.write_text("{}\n", encoding="utf-8")
                        os.utime(fixture.decision_log, ns=(older, older))
                    elif scenario == "missing-review-audit":
                        fixture.review_audit.unlink()
                    elif scenario == "missing-decision-log":
                        fixture.decision_log.unlink()
                    elif scenario == "report-binding-mismatch":
                        payload = json.loads(fixture.report.read_text(encoding="utf-8"))
                        payload["semantic"]["review_sha256"] = "0" * 64
                        fixture.report.write_text(
                            json.dumps(payload) + "\n",
                            encoding="utf-8",
                        )
                        fixture.write_state()

                    publication = next(
                        record
                        for record in artifact_catalog(fixture.job)
                        if record.kind == "docx"
                    )
                    self.assertEqual(publication.status, "blocked")

    def test_legacy_pdf_release_is_invalidated_by_any_review_evidence(self) -> None:
        evidence_names = (
            "semantic-review.json",
            f"semantic-review.{'1' * 64}.json",
            "review-decisions.jsonl",
            "review-decisions.jsonl.lock",
        )
        for profile in ("word", "full"):
            for evidence_name in evidence_names:
                with (
                    self.subTest(profile=profile, evidence=evidence_name),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    fixture = _pdf_catalog_fixture(
                        Path(directory),
                        profile=profile,
                        review_required=False,
                    )
                    evidence = fixture.report.parent / evidence_name
                    evidence.write_text("{}\n", encoding="utf-8")
                    older = max(
                        1,
                        fixture.report.stat().st_mtime_ns - 1_000_000_000,
                    )
                    os.utime(evidence, ns=(older, older))
                    publication = next(
                        record
                        for record in artifact_catalog(fixture.job)
                        if record.kind == "docx"
                    )
                    self.assertEqual(publication.status, "blocked")

            with (
                self.subTest(profile=profile, evidence="broken-snapshot-symlink"),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = _pdf_catalog_fixture(
                    Path(directory),
                    profile=profile,
                    review_required=False,
                )
                snapshot = fixture.report.parent / "semantic-review.broken.json"
                snapshot.symlink_to(fixture.report.parent / "missing-review-target")
                publication = next(
                    record
                    for record in artifact_catalog(fixture.job)
                    if record.kind == "docx"
                )
                self.assertEqual(publication.status, "blocked")

    def test_upload_name_has_mode_specific_extension(self) -> None:
        self.assertEqual(
            safe_upload_name("Book.EPUB", source_mode="epub"),
            "Book.epub",
        )
        with self.assertRaises(ValueError):
            safe_upload_name("Book.pdf", source_mode="epub")

    def test_upload_size_gate_runs_before_buffer_copy(self) -> None:
        validate_upload_size(8, limit=8)
        with self.assertRaisesRegex(ValueError, "upload exceeds"):
            validate_upload_size(9, limit=8)

    def test_epub_native_release_plan_is_persisted_for_web_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FrontendSettings(
                database=root / "jobs.sqlite3",
                jobs_root=root / "jobs",
                source_roots=(root,),
            )
            service = ApplicationService(settings)
            source = root / "book.epub"
            _write_epub(source)
            spec = RunSpec(
                source=source,
                source_mode="epub",
                output_dir="ignored",
                targets=("publication.epub_report",),
                translate=False,
                verify=True,
            )
            job = service.submit_path(spec, start=False)

            self.assertEqual(job.release_profile, "epub")
            self.assertEqual(job.resolved_targets, ("publication.epub_report",))

    def test_server_path_job_snapshots_source_and_control_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            config = root / "pipeline.toml"
            config.write_text("[pipeline]\nname = 'original'\n", encoding="utf-8")
            recipe = root / "recipe.toml"
            recipe.write_text("targets = ['publication.epub']\n", encoding="utf-8")
            glossary = root / "glossary.json"
            glossary.write_text(
                json.dumps({"injury": "伤害"}, ensure_ascii=False),
                encoding="utf-8",
            )
            original_source = source.read_bytes()
            service = ApplicationService(
                FrontendSettings(
                    database=root / "jobs.sqlite3",
                    jobs_root=root / "jobs",
                    source_roots=(root,),
                )
            )
            plan = SimpleNamespace(
                targets=("semantic.translation_units",),
                release_profile="draft",
            )
            with patch.object(service.execution, "plan", return_value=plan):
                job = service.submit_path(
                    RunSpec(
                        source=source,
                        source_mode="epub",
                        output_dir="ignored",
                        config=config,
                        recipe=recipe,
                        targets=("semantic.translation_units",),
                        translate=False,
                        verify=False,
                        options={"glossary": str(glossary)},
                    ),
                    start=False,
                )

            self.assertTrue(job.source_path.is_relative_to(job.workspace / "input"))
            self.assertEqual(job.source_path.read_bytes(), original_source)
            self.assertEqual(
                Path(job.spec.config or "").read_text(encoding="utf-8"),
                "[pipeline]\nname = 'original'\n",
            )
            self.assertEqual(
                Path(job.spec.recipe or "").read_text(encoding="utf-8"),
                "targets = ['publication.epub']\n",
            )
            snapshot_glossary = Path(str(job.spec.options["glossary"]))
            self.assertTrue(snapshot_glossary.is_relative_to(job.workspace / "control"))
            self.assertEqual(
                json.loads(snapshot_glossary.read_text(encoding="utf-8")),
                {"injury": "伤害"},
            )

            source.write_bytes(b"changed source")
            config.write_text("changed config", encoding="utf-8")
            recipe.write_text("changed recipe", encoding="utf-8")
            glossary.write_text("{}", encoding="utf-8")
            self.assertEqual(job.source_path.read_bytes(), original_source)
            self.assertIn("name = 'original'", Path(job.spec.config or "").read_text())
            self.assertIn("publication.epub", Path(job.spec.recipe or "").read_text())
            self.assertEqual(
                json.loads(snapshot_glossary.read_text(encoding="utf-8")),
                {"injury": "伤害"},
            )

    def test_epub_upload_is_written_before_native_graph_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.epub"
            _write_epub(source)
            service = ApplicationService(
                FrontendSettings(
                    database=root / "jobs.sqlite3",
                    jobs_root=root / "jobs",
                    source_roots=(root,),
                )
            )
            job = service.submit_upload(
                RunSpec(
                    source_mode="epub",
                    output_dir="ignored",
                    targets=("semantic.translation_units",),
                    translate=False,
                    verify=False,
                ),
                filename="book.epub",
                content=source.read_bytes(),
                start=False,
            )

            self.assertTrue(job.source_path.is_file())
            self.assertEqual(job.resolved_targets, ("semantic.translation_units",))

    def test_epub_native_report_releases_only_the_graph_epub_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            service = ApplicationService(
                FrontendSettings(
                    database=root / "jobs.sqlite3",
                    jobs_root=root / "jobs",
                    source_roots=(root,),
                )
            )
            job = service.submit_path(
                RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir="ignored",
                    targets=("publication.epub_report",),
                    target_language="en",
                    translate=False,
                    verify=True,
                ),
                start=False,
            )
            result = service.execution.execute(job.spec)

            self.assertTrue(result.release_ready)
            records = service.artifacts(job.id)
            publication = next(record for record in records if record.kind == "epub")
            self.assertEqual(publication.status, "released")
            self.assertEqual(
                publication.report_path,
                Path(job.spec.output_dir) / "audit" / "epub-release-report.json",
            )

            state_path = Path(job.spec.output_dir) / ".pipeline_graph" / "state.json"
            state_bytes = state_path.read_bytes()
            state = json.loads(state_bytes)
            replacement = Path(job.spec.output_dir) / "replacement.epub"
            replacement.write_bytes(Path(publication.path).read_bytes() + b"\n")
            replacement_sha256 = hashlib.sha256(replacement.read_bytes()).hexdigest()
            replaced = False
            for node in state["nodes"].values():
                outputs = node.get("outputs", {})
                if "publication.epub" in outputs:
                    outputs["publication.epub"]["path"] = str(replacement)
                    outputs["publication.epub"]["sha256"] = replacement_sha256
                    replaced = True
            self.assertTrue(replaced)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            stale_report = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(stale_report.path, replacement.resolve())
            self.assertEqual(stale_report.status, "blocked")
            state_path.write_bytes(state_bytes)
            replacement.unlink()
            restored_identity = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(restored_identity.status, "released")

            draft_audit = (
                Path(job.spec.output_dir)
                / ".pipeline_graph"
                / "draft-semantic-audit.json"
            )
            reconstruction_audit = (
                draft_audit
                if draft_audit.is_file()
                else Path(job.spec.output_dir)
                / "audit"
                / "semantic-reconstruction.json"
            )
            audit_bytes = reconstruction_audit.read_bytes()
            audit_stat = reconstruction_audit.stat()
            reconstruction_audit.unlink()
            missing_evidence = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(missing_evidence.status, "blocked")
            reconstruction_audit.write_bytes(audit_bytes)
            os.utime(
                reconstruction_audit,
                ns=(audit_stat.st_atime_ns, audit_stat.st_mtime_ns),
            )
            restored = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(restored.status, "released")

            report_mtime = publication.report_path.stat().st_mtime_ns
            review_audit = (
                Path(job.spec.output_dir) / "audit" / "semantic-review.json"
            )
            review_bytes = review_audit.read_bytes()
            review_payload = json.loads(review_bytes)
            review_payload["summary"]["resolved_issue_count"] = 99
            review_audit.write_text(json.dumps(review_payload), encoding="utf-8")
            older = max(1, report_mtime - 1_000_000_000)
            os.utime(review_audit, ns=(older, older))
            review_tampered = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(review_tampered.status, "blocked")
            review_audit.write_bytes(review_bytes)

            chapter = next((Path(job.spec.output_dir) / "chapters").glob("*.md"))
            chapter.write_text(
                chapter.read_text(encoding="utf-8") + "\nchanged after release\n",
                encoding="utf-8",
            )
            os.utime(chapter, ns=(older, older))
            tampered = next(
                record
                for record in service.artifacts(job.id)
                if record.kind == "epub"
            )
            self.assertEqual(tampered.status, "blocked")

    def test_epub_report_targets_fail_during_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            service = ApplicationService(
                FrontendSettings(
                    database=root / "jobs.sqlite3",
                    jobs_root=root / "jobs",
                    source_roots=(root,),
                )
            )
            for target in ("publication.report", "publication.word_report"):
                spec = RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir=root / "output",
                    targets=(target,),
                    translate=False,
                    verify=False,
                )
                with self.subTest(target=target), self.assertRaisesRegex(
                    ValueError, "does not support targets"
                ):
                    service.preview_plan(spec)

    def test_web_and_cli_compile_the_same_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            service = ApplicationService(
                FrontendSettings(
                    database=root / "jobs.sqlite3",
                    jobs_root=root / "jobs",
                    source_roots=(root,),
                )
            )
            spec = RunSpec(
                source=source,
                source_mode="text-pdf",
                output_dir=root / "output",
                targets=("publication.docx",),
                translate=False,
                verify=False,
            )
            with patch("pipeline_graph.book.legacy.load_env_file") as load_env:
                web_nodes = service.preview_plan(spec)
                compatibility_nodes = frontend_plan_runspec(spec)
            load_env.assert_not_called()
            cli_plan = document_pipeline.plan_spec(spec)
        self.assertEqual(
            web_nodes,
            tuple(node["name"] for node in cli_plan["nodes"]),
        )
        self.assertEqual(compatibility_nodes, web_nodes)
        self.assertEqual(cli_plan["targets"], ["publication.docx"])

    def test_worker_does_not_succeed_when_result_misses_resolved_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "9" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    targets=("publication.docx",),
                    translate=False,
                    verify=False,
                ),
            )
            token = "8" * 32
            registry.update(
                job.id,
                status="running",
                pid=os.getpid(),
                worker_token=token,
                worker_started_at="2026-08-15T00:00:00+00:00",
            )
            fake_service = SimpleNamespace(
                plan=lambda spec: SimpleNamespace(
                    targets=("publication.docx",),
                    release_profile="draft",
                ),
                execute=lambda spec, progress=None: SimpleNamespace(
                    status="passed",
                    targets=(),
                    run_id="graph-run",
                    release_profile="draft",
                ),
            )
            with patch(
                "frontend_runtime.RunExecutionService", return_value=fake_service
            ):
                exit_code = execute_job(registry.database, job.id, token)
            failed = registry.get(job.id)
        self.assertEqual(exit_code, 1)
        self.assertEqual(failed.status, "failed")
        self.assertIn("does not satisfy", failed.error or "")

    def test_worker_does_not_succeed_without_requested_release_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "job"
            workspace.mkdir()
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            registry = JobRegistry(root / "jobs.sqlite3")
            job = registry.create(
                "7" * 32,
                workspace,
                RunSpec(
                    source=source,
                    source_mode="text-pdf",
                    output_dir=workspace / "output",
                    targets=("publication.word_report",),
                    translate=False,
                    verify=True,
                ),
            )
            token = "6" * 32
            registry.update(
                job.id,
                status="running",
                pid=os.getpid(),
                worker_token=token,
                worker_started_at="2026-08-15T00:00:00+00:00",
            )
            fake_service = SimpleNamespace(
                plan=lambda spec: SimpleNamespace(
                    targets=("publication.word_report",),
                    release_profile="word",
                ),
                execute=lambda spec, progress=None: SimpleNamespace(
                    status="passed",
                    targets=("publication.word_report",),
                    run_id="graph-run",
                    release_profile="word",
                    release_ready=False,
                ),
            )
            with patch(
                "frontend_runtime.RunExecutionService", return_value=fake_service
            ):
                exit_code = execute_job(registry.database, job.id, token)
            failed = registry.get(job.id)
        self.assertEqual(exit_code, 1)
        self.assertEqual(failed.status, "failed")
        self.assertIn("release-ready", failed.error or "")


if __name__ == "__main__":
    unittest.main()
