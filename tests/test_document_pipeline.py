from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import document_pipeline
from product_contracts import RunSpec
from run_execution_service import (
    SEMANTIC_CACHE_DIRNAME,
    PlanStep,
    RunExecutionError,
    RunExecutionResult,
    RunExecutionService,
    RunPlan,
    RunResolutionError,
)
from tests.test_epub_semantic_import import _write_epub


class DocumentPipelineTests(unittest.TestCase):
    def test_execution_service_propagates_isolated_dotenv_policy(self) -> None:
        spec = RunSpec(
            source_mode="text-pdf",
            output_dir="outputs/book",
            phase="docx",
            targets=("publication.docx",),
            translate=False,
            verify=False,
        )
        prepared = Mock(targets={"publication.docx"})
        prepared.plan.return_value = ()
        graph_request = object()
        with (
            patch(
                "run_execution_service._graph_request",
                return_value=graph_request,
            ) as make_request,
            patch(
                "translation_agent_api.prepare_graph",
                return_value=prepared,
            ) as prepare_graph,
        ):
            plan = RunExecutionService(load_dotenv=False).plan(spec)

        self.assertEqual(plan.targets, ("publication.docx",))
        prepare_graph.assert_called_once_with(graph_request)
        self.assertIs(make_request.call_args.kwargs["load_dotenv"], False)

    def test_epub_plan_exposes_real_adapter_and_graph_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            plan = document_pipeline.plan_spec(
                RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir=root / "output",
                    targets=("publication.epub",),
                    verify=False,
                )
            )

        self.assertEqual(plan["schema_version"], 1)
        self.assertEqual(plan["release_profile"], "draft")
        by_name = {node["name"]: node for node in plan["nodes"]}
        self.assertEqual(by_name["core.source.epub.inspect"]["executor"], "graph")
        self.assertEqual(by_name["core.reconstruct.epub_semantic"]["executor"], "graph")
        self.assertEqual(by_name["core.semantic.translate"]["executor"], "graph")
        self.assertEqual(by_name["core.publish.epub"]["executor"], "graph")

    def test_epub_empty_targets_have_one_documented_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            plan = RunExecutionService().plan(
                RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir=root / "output",
                    translate=False,
                    verify=False,
                )
            )

        self.assertEqual(
            plan.targets,
            ("publication.docx", "publication.epub"),
        )
        self.assertEqual(
            plan.node_names,
            (
                "core.source.epub.inspect",
                "core.reconstruct.epub_semantic",
                "core.semantic.materialize_reader",
                "core.publish.epub",
                "core.publish.docx",
            ),
        )

    def test_cli_accepts_versioned_spec_and_prints_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            spec_path = root / "run.json"
            spec_path.write_text(
                json.dumps(
                    RunSpec(
                        source=source,
                        source_mode="epub",
                        output_dir=root / "output",
                        translate=False,
                        verify=False,
                    ).to_dict()
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = document_pipeline.main(["plan", "--spec", str(spec_path)])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["source_mode"], "epub")

    def test_review_is_blocked_by_any_failed_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            audit = output / "audit"
            audit.mkdir()
            (audit / "semantic-reconstruction.json").write_text(
                json.dumps({"status": "blocked", "release_blocked": True}),
                encoding="utf-8",
            )

            report = document_pipeline.review_status(output)

        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["release_blocked"])

    def test_publish_plan_is_explicitly_draft_without_release_report(self) -> None:
        class FakeRun:
            run_id = "run-test"
            executed = ("core.publish.epub",)
            skipped = ()

        fake_api = types.ModuleType("translation_agent_api")
        fake_api.run_graph = Mock(return_value=FakeRun())
        fake_api.RunRequest = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_api.GraphRunRequest = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch.dict(sys.modules, {"translation_agent_api": fake_api}):
            report = document_pipeline.publish_spec(
                RunSpec(
                    source_mode="epub",
                    output_dir="outputs/book",
                    translate=False,
                ),
                ("epub",),
            )

        self.assertFalse(report["release_ready"])
        self.assertEqual(report["publication_status"], "draft")

    def test_doctor_flags_are_delegated_without_double_dash(self) -> None:
        with patch("doctor.main", return_value=0) as doctor_main:
            code = document_pipeline.main(["doctor", "--json", "--web"])

        self.assertEqual(code, 0)
        doctor_main.assert_called_once_with(["--json", "--web"])

    def test_epub_default_run_targets_native_release_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)
            plan = RunExecutionService().plan(
                RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir=root / "output",
                    translate=False,
                )
            )

        self.assertEqual(plan.targets, ("publication.epub_report",))
        self.assertEqual(plan.release_profile, "epub")
        self.assertIn("core.publication.verify.epub", plan.node_names)

    def test_epub_native_graph_can_issue_a_release_ready_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            _write_epub(source)

            result = RunExecutionService(load_dotenv=False).execute(
                RunSpec(
                    source=source,
                    source_mode="epub",
                    output_dir=root / "output",
                    target_language="en",
                    translate=False,
                    verify=True,
                )
            )

        self.assertTrue(result.release_ready)
        self.assertEqual(result.release_profile, "epub")
        self.assertEqual(result.targets, ("publication.epub_report",))
        self.assertIn("core.publication.verify.epub", result.details["executed"])

    def test_apply_preserves_language_and_glossary_contract(self) -> None:
        spec = RunSpec(
            source_mode="epub",
            output_dir="outputs/book",
            target_language="中文",
            options={"glossary": "terms.json"},
            verify=False,
        )
        with patch(
            "semantic_translation_runner.load_glossary",
            return_value={"state": "国家"},
        ) as load_glossary, patch(
            "epub_semantic_import.apply_translations",
            return_value={"status": "passed"},
        ) as apply_translations:
            report = document_pipeline.apply_spec(
                spec,
                Path("translated.jsonl"),
            )

        load_glossary.assert_called_once_with(Path("terms.json"))
        apply_translations.assert_called_once_with(
            Path("outputs/book").resolve(),
            Path("translated.jsonl"),
            target_language="中文",
            glossary={"state": "国家"},
        )
        self.assertEqual(report["status"], "passed")

    def test_console_boundary_reports_expected_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = document_pipeline.cli(
                [
                    "run",
                    "book.epub",
                    "--source-mode",
                    "epub",
                    "--target",
                    "publication.report",
                    "--no-translate",
                ]
            )

        self.assertEqual(code, 1)
        self.assertIn(
            "[translation-agent-error] RunResolutionError",
            stderr.getvalue(),
        )
        self.assertIn("does not support targets", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_epub_run_stops_when_semantic_ingest_is_blocked(self) -> None:
        prepared = Mock(targets=frozenset({"publication.epub"}))
        prepared.plan.return_value = ()
        prepared.execute.side_effect = RuntimeError(
            "EPUB semantic reconstruction is blocked"
        )
        with patch.object(
            RunExecutionService,
            "_prepare_epub_graph",
            return_value=prepared,
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "semantic reconstruction is blocked",
                ):
                    document_pipeline.main(
                        [
                            "run",
                            "book.epub",
                            "--source-mode",
                            "epub",
                            "--no-translate",
                            "--no-verify",
                        ]
                    )

    def test_incompatible_pdf_report_targets_fail_for_epub(self) -> None:
        for target in ("publication.report", "publication.word_report"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    RunResolutionError,
                    "does not support targets",
                ):
                    RunExecutionService().plan(
                        RunSpec(
                            source="book.epub",
                            source_mode="epub",
                            output_dir="outputs/book",
                            targets=(target,),
                            translate=False,
                            verify=False,
                        )
                    )

    def test_epub_recipe_is_rejected_instead_of_silently_ignored(self) -> None:
        with self.assertRaisesRegex(
            RunResolutionError,
            "do not support Graph Recipe",
        ):
            RunExecutionService().plan(
                RunSpec(
                    source="book.epub",
                    source_mode="epub",
                    output_dir="outputs/book",
                    recipe="recipes/full-publication.toml",
                    targets=("publication.epub",),
                    translate=False,
                    verify=False,
                )
            )

    def test_unified_run_boundary_respects_resolved_targets(self) -> None:
        result = RunExecutionResult(
            status="passed",
            source_mode="epub",
            targets=("publication.epub",),
            release_profile="draft",
        )
        with patch("document_pipeline.execute_runspec", return_value=result) as execute:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = document_pipeline.main(
                    [
                        "run",
                        "book.epub",
                        "--source-mode",
                        "epub",
                        "--target",
                        "publication.epub",
                        "--no-translate",
                        "--no-verify",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(execute.call_args.args[0].targets, ("publication.epub",))
        self.assertEqual(
            json.loads(stdout.getvalue())["targets"],
            ["publication.epub"],
        )

    def test_pdf_standalone_publisher_cannot_claim_verification(self) -> None:
        with self.assertRaisesRegex(RunResolutionError, "unverified draft"):
            RunExecutionService().resolve(
                RunSpec(
                    source_mode="scanned-pdf",
                    source=None,
                    output_dir="outputs/book",
                    phase="docx",
                    verify=True,
                )
            )

    def test_recipe_targets_determine_the_release_profile(self) -> None:
        node = types.SimpleNamespace(
            name="core.publication.verify.word",
            version="1",
            requires=(),
            provides=("publication.word_report",),
            cache=False,
        )
        prepared = types.SimpleNamespace(
            targets={"publication.word_report"},
            plan=lambda: (node,),
        )
        fake_api = types.ModuleType("translation_agent_api")
        fake_api.prepare_graph = lambda request: prepared
        service = RunExecutionService()
        with patch(
            "run_execution_service._graph_request",
            return_value=object(),
        ), patch.dict(sys.modules, {"translation_agent_api": fake_api}):
            plan = service.plan(
                RunSpec(
                    source="book.pdf",
                    source_mode="scanned-pdf",
                    output_dir="outputs/book",
                    recipe="recipes/chinese-pdf-word.toml",
                )
            )

        self.assertEqual(plan.targets, ("publication.word_report",))
        self.assertEqual(plan.release_profile, "word")

    def test_pdf_execute_uses_the_exact_prepared_graph_once(self) -> None:
        node = types.SimpleNamespace(
            name="core.publish.docx",
            version="1",
            requires=(),
            provides=("publication.docx",),
            cache=False,
        )
        graph_result = types.SimpleNamespace(
            run_id="run-one-prepare",
            plan=("core.publish.docx",),
            executed=("core.publish.docx",),
            skipped=(),
            values={"publication.docx": {"path": "book.docx"}},
            state_path=Path("state.json"),
            events_path=Path("events.jsonl"),
        )
        prepared = types.SimpleNamespace(
            targets={"publication.docx"},
            plan=lambda: (node,),
            execute=Mock(return_value=graph_result),
        )
        fake_api = types.ModuleType("translation_agent_api")
        fake_api.prepare_graph = Mock(return_value=prepared)
        with patch(
            "run_execution_service._graph_request",
            return_value=object(),
        ), patch.dict(sys.modules, {"translation_agent_api": fake_api}):
            result = RunExecutionService().execute(
                RunSpec(
                    source="book.pdf",
                    source_mode="scanned-pdf",
                    output_dir="outputs/book",
                    targets=("publication.docx",),
                    verify=False,
                )
            )

        fake_api.prepare_graph.assert_called_once()
        prepared.execute.assert_called_once_with()
        self.assertEqual(result.targets, ("publication.docx",))
        self.assertFalse(result.release_ready)

    def test_pdf_execute_rejects_missing_graph_target(self) -> None:
        node = types.SimpleNamespace(
            name="core.publish.docx",
            version="1",
            requires=(),
            provides=("publication.docx",),
            cache=False,
        )
        prepared = types.SimpleNamespace(
            targets={"publication.docx"},
            plan=lambda: (node,),
            execute=Mock(
                return_value=types.SimpleNamespace(values={})
            ),
        )
        fake_api = types.ModuleType("translation_agent_api")
        fake_api.prepare_graph = Mock(return_value=prepared)
        with patch(
            "run_execution_service._graph_request",
            return_value=object(),
        ), patch.dict(sys.modules, {"translation_agent_api": fake_api}):
            with self.assertRaisesRegex(
                RunExecutionError,
                "without requested plan targets.*publication.docx",
            ):
                RunExecutionService().execute(
                    RunSpec(
                        source="book.pdf",
                        source_mode="scanned-pdf",
                        output_dir="outputs/book",
                        targets=("publication.docx",),
                        verify=False,
                    )
                )

        fake_api.prepare_graph.assert_called_once()
        prepared.execute.assert_called_once_with()

    def test_report_target_fails_when_verifier_report_is_not_ready(self) -> None:
        node = types.SimpleNamespace(
            name="core.publication.verify.word",
            version="1",
            requires=(),
            provides=("publication.word_report",),
            cache=False,
        )
        prepared = types.SimpleNamespace(
            targets={"publication.word_report"},
            plan=lambda: (node,),
            execute=Mock(
                return_value=types.SimpleNamespace(
                    values={"publication.word_report": {"path": "report.json"}}
                )
            ),
        )
        fake_api = types.ModuleType("translation_agent_api")
        fake_api.prepare_graph = Mock(return_value=prepared)
        with tempfile.TemporaryDirectory() as directory, patch(
            "run_execution_service._graph_request",
            return_value=object(),
        ), patch.dict(sys.modules, {"translation_agent_api": fake_api}):
            with self.assertRaisesRegex(
                RunExecutionError,
                "did not produce a release-ready 'word' report",
            ):
                RunExecutionService().execute(
                    RunSpec(
                        source="book.pdf",
                        source_mode="scanned-pdf",
                        output_dir=directory,
                        targets=("publication.word_report",),
                    )
                )

        fake_api.prepare_graph.assert_called_once()
        prepared.execute.assert_called_once_with()

    def test_release_ready_requires_materialized_verifier_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            spec = RunSpec(
                source="book.pdf",
                source_mode="scanned-pdf",
                output_dir=output,
                targets=("publication.word_report",),
            )
            plan = RunPlan(
                source_mode="scanned-pdf",
                targets=("publication.word_report",),
                backend="graph",
                release_profile="word",
                nodes=(PlanStep("core.publication.verify.word", "graph"),),
            )
            self.assertFalse(
                RunExecutionService._verified_release_ready(spec, plan)
            )
            audit = output / "audit"
            audit.mkdir()
            (audit / "word-release-report.json").write_text(
                json.dumps(
                    {
                        "release_ready": True,
                        "mode": "full",
                        "publication_profile": "word",
                    }
                ),
                encoding="utf-8",
            )
            self.assertTrue(
                RunExecutionService._verified_release_ready(spec, plan)
            )

    def test_standalone_translation_uses_shared_cache_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            semantic = output / "semantic"
            semantic.mkdir()
            (semantic / "translation-units.jsonl").write_text("", encoding="utf-8")
            with patch(
                "semantic_translation_runner.main",
                return_value=0,
            ) as semantic_main:
                document_pipeline.translate_spec(
                    RunSpec(
                        source_mode="epub",
                        output_dir=output,
                        verify=False,
                    )
                )

        argv = semantic_main.call_args.args[0]
        self.assertEqual(
            argv[argv.index("--cache-dir") + 1],
            str(output.resolve() / SEMANTIC_CACHE_DIRNAME),
        )

    def test_epub_publisher_must_materialize_requested_artifact(self) -> None:
        fake_run = types.SimpleNamespace(
            run_id="run-missing",
            executed=("core.publish.epub",),
            skipped=(),
            values={},
        )
        prepared = Mock()
        prepared.execute.return_value = fake_run
        plan = RunPlan(
            source_mode="epub",
            targets=("publication.epub",),
            backend="graph",
            release_profile="draft",
            nodes=(PlanStep("core.publish.epub", "graph"),),
        )
        service = RunExecutionService()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            service,
            "_prepare_plan",
            return_value=(plan, prepared),
        ):
            with self.assertRaisesRegex(
                RunExecutionError,
                "without requested plan targets",
            ):
                service.execute(
                    RunSpec(
                        source="book.epub",
                        source_mode="epub",
                        output_dir=directory,
                        targets=("publication.epub",),
                        translate=False,
                        verify=False,
                    )
                )

    def test_standalone_ingest_returns_nonzero_for_blocked_result(self) -> None:
        with patch(
            "document_pipeline.ingest_spec",
            return_value={"status": "blocked", "release_blocked": True},
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = document_pipeline.main(
                    [
                        "ingest",
                        "book.pdf",
                        "--source-mode",
                        "text-pdf",
                    ]
                )

        self.assertEqual(code, 1)
        self.assertTrue(json.loads(stdout.getvalue())["release_blocked"])


if __name__ == "__main__":
    unittest.main()
