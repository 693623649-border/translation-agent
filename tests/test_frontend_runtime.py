import hashlib
import contextlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_frontend
from application_service import ApplicationService
from frontend_runtime import (
    FrontendSettings,
    JobRegistry,
    PathPolicy,
    artifact_catalog,
    child_environment,
    _run_epub_workflow,
    plan_runspec,
    safe_upload_name,
    start_job_process,
    validate_upload_size,
)
from pipeline_profiles import ModelProfile
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
            created = registry.create("a" * 32, workspace, spec)

            with contextlib.closing(sqlite3.connect(database)) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
            self.assertEqual(created.spec.to_dict(), spec.to_dict())
            self.assertEqual(registry.events(created.id)[0]["event"], "job_created")

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
        self.assertNotIn("AMBIENT_API_KEY", environment)
        with self.assertRaisesRegex(ValueError, "runtime control"):
            child_environment({"PYTHONPATH": "/tmp/injected"}, base={})

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
            job = registry.create("b" * 32, workspace, spec)
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
            with self.assertRaisesRegex(ValueError, "reviewable drafts"):
                service.submit_path(spec, start=False)

    def test_epub_adapter_accepts_knowledge_base_target(self) -> None:
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
                targets=("publication.knowledge_base",),
                translate=False,
                verify=False,
            )

            job = service.submit_path(spec, start=False)

            self.assertEqual(job.spec.targets, ("publication.knowledge_base",))
            self.assertIn("core.publish.knowledge_base", plan_runspec(job.spec))

    def test_epub_runner_publishes_knowledge_base_from_nonempty_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            source.write_bytes(b"epub")
            output = root / "output"
            spec = RunSpec(
                source=source,
                source_mode="epub",
                output_dir=output,
                targets=("publication.knowledge_base",),
                translate=False,
                verify=False,
            )

            def fake_import_epub(_source: Path, target: Path) -> dict[str, object]:
                chapter_dir = target / "chapters"
                chapter_dir.mkdir(parents=True)
                (chapter_dir / "001_body.md").write_text(
                    "# Body\n\nUseful content.\n",
                    encoding="utf-8",
                )
                (chapter_dir / "002_cover.md").write_text(
                    "# Cover\n\n",
                    encoding="utf-8",
                )
                (target / "chapters.json").write_text(
                    json.dumps(
                        [
                            {
                                "id": "epub-0001",
                                "sequence": 1,
                                "display_title": "Body",
                                "filename": "001_body.md",
                                "reviewed_override": False,
                            },
                            {
                                "id": "epub-0002",
                                "sequence": 2,
                                "display_title": "Cover",
                                "filename": "002_cover.md",
                                "reviewed_override": False,
                            },
                        ],
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return {"release_blocked": False}

            with patch(
                "epub_semantic_import.import_epub",
                side_effect=fake_import_epub,
            ), patch("translation_agent_api.run_graph") as run_graph:
                run_id = _run_epub_workflow(spec)

            self.assertIsNone(run_id)
            run_graph.assert_not_called()
            rows = [
                json.loads(line)
                for line in (output / "knowledge_base.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual([row["title"] for row in rows], ["Body"])
            self.assertEqual(rows[0]["content"], "Useful content.")
            self.assertTrue((output / "knowledge_base.rag.json").is_file())

    def test_epub_runner_uses_profile_name_in_translation_cache_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            source.write_bytes(b"epub")
            profile = ModelProfile(
                name="academic-v7",
                adapter="openai-chat",
                provider="deepseek",
                model="deepseek-test",
                credential_env="QA_TRANSLATION_KEY",
                base_url="https://example.invalid/v1",
                concurrency=2,
            )
            profiles = type(
                "Profiles",
                (),
                {"for_stage": lambda self, stage, override: profile},
            )()
            spec = RunSpec(
                source=source,
                source_mode="epub",
                output_dir=root / "output",
                config=root / "pipeline.toml",
                translate=True,
                verify=False,
            )
            with patch.dict(
                os.environ,
                {"QA_TRANSLATION_KEY": "secret"},
            ), patch(
                "epub_semantic_import.import_epub",
                return_value={"release_blocked": False},
            ), patch(
                "epub_semantic_import.apply_translations",
            ), patch(
                "pipeline_profiles.load_pipeline_profiles",
                return_value=profiles,
            ), patch(
                "semantic_translation_runner._deepseek_request",
                return_value=lambda prompt: prompt,
            ), patch(
                "semantic_translation_runner.translate_units",
            ) as translate_units:
                _run_epub_workflow(spec)

        self.assertEqual(
            translate_units.call_args.kwargs["prompt_profile"],
            "academic-v7",
        )


if __name__ == "__main__":
    unittest.main()
