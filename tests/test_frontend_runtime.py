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
            audit.mkdir()
            report = audit / "word-release-report.json"
            report.write_text(
                '{"release_ready": true, "mode": "full", '
                '"publication_profile": "word"}\n',
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
                '{"release_ready": false, "mode": "full", '
                '"publication_profile": "word"}\n',
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

    def test_epub_adapter_is_explicitly_draft_until_native_gate_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = FrontendSettings(
                database=root / "jobs.sqlite3",
                jobs_root=root / "jobs",
                source_roots=(root,),
            )
            service = ApplicationService(settings)
            source = root / "book.epub"
            source.write_bytes(b"epub")
            spec = RunSpec(
                source=source,
                source_mode="epub",
                output_dir="ignored",
                targets=("publication.epub",),
                translate=False,
                verify=True,
            )
            with self.assertRaisesRegex(ValueError, "verification is not available"):
                service.submit_path(spec, start=False)

    def test_epub_report_targets_fail_during_planning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            source.write_bytes(b"epub")
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
