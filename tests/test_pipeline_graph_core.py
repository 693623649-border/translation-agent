import json
import multiprocessing
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from pipeline_graph import (
    AmbiguousProviderError,
    GraphContext,
    GraphCycleError,
    GraphExecutor,
    MissingDependencyError,
    NodeContractError,
    NodeExecutionError,
    NodeResult,
    NodeSpec,
    OutputDirectoryLock,
    OutputDirectoryLockedError,
    PipelineGraph,
    UnknownTargetError,
)


def _contend_for_output_lock(
    lock_path: str,
    ready: object,
    start: object,
    counter_guard: object,
    active_owners: object,
    maximum_owners: object,
    results: object,
) -> None:
    """Spawn-safe helper used to exercise the filesystem lock across processes."""

    ready.put(os.getpid())
    if not start.wait(10):
        results.put(("error", "start timeout"))
        return
    try:
        with OutputDirectoryLock(
            Path(lock_path),
            timeout=5.0,
            poll_interval=0.01,
        ):
            with counter_guard:
                active_owners.value += 1
                maximum_owners.value = max(
                    maximum_owners.value,
                    active_owners.value,
                )
            time.sleep(0.15)
            with counter_guard:
                active_owners.value -= 1
        results.put(("ok", os.getpid()))
    except BaseException as exc:  # pragma: no cover - surfaced in parent assertion.
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def node(name, handler, *, requires=(), provides=(), **kwargs):
    return NodeSpec(
        name=name,
        handler=handler,
        requires=frozenset(requires),
        provides=frozenset(provides),
        **kwargs,
    )


class PipelineGraphPlanningTests(unittest.TestCase):
    def test_plans_by_dependencies_not_registration_order(self):
        graph = PipelineGraph(
            [
                node("publish", lambda _ctx: NodeResult(), requires={"chapters"}),
                node("compile", lambda _ctx: NodeResult(), requires={"pages"}, provides={"chapters"}),
                node("ocr", lambda _ctx: NodeResult(), requires={"pdf"}, provides={"pages"}),
            ]
        )
        self.assertEqual(
            [item.name for item in graph.plan(available={"pdf"})],
            ["ocr", "compile", "publish"],
        )

    def test_detects_missing_ambiguous_and_cyclic_dependencies(self):
        missing = PipelineGraph([node("compile", lambda _ctx: NodeResult(), requires={"pages"})])
        with self.assertRaises(MissingDependencyError):
            missing.plan()

        ambiguous = PipelineGraph(
            [
                node("ocr.a", lambda _ctx: NodeResult(), provides={"pages"}),
                node("ocr.b", lambda _ctx: NodeResult(), provides={"pages"}),
            ]
        )
        with self.assertRaises(AmbiguousProviderError):
            ambiguous.plan()

        cyclic = PipelineGraph(
            [
                node("a", lambda _ctx: NodeResult(), requires={"b.value"}, provides={"a.value"}),
                node("b", lambda _ctx: NodeResult(), requires={"a.value"}, provides={"b.value"}),
            ]
        )
        with self.assertRaises(GraphCycleError):
            cyclic.plan()

    def test_targets_select_only_transitive_dependencies(self):
        graph = PipelineGraph(
            [
                node("ocr", lambda _ctx: NodeResult(), requires={"pdf"}, provides={"pages"}),
                node("docx", lambda _ctx: NodeResult(), requires={"pages"}, provides={"docx"}),
                node("epub", lambda _ctx: NodeResult(), requires={"pages"}, provides={"epub"}),
            ]
        )
        self.assertEqual(
            [item.name for item in graph.plan(available={"pdf"}, targets={"docx"})],
            ["ocr", "docx"],
        )

    def test_nodes_can_be_replaced_and_removed(self):
        original = node("ocr", lambda _ctx: NodeResult(outputs={"pages": "old"}), provides={"pages"})
        replacement = node("ocr", lambda _ctx: NodeResult(outputs={"pages": "new"}), provides={"pages"}, version="2")
        graph = PipelineGraph([original])
        self.assertIs(graph.replace("ocr", replacement), graph)
        self.assertIs(graph.nodes[0], replacement)
        self.assertIs(graph.remove("ocr"), replacement)
        self.assertEqual(graph.nodes, ())


class GraphExecutorTests(unittest.TestCase):
    def test_uncacheable_state_is_not_restored_after_node_becomes_cacheable(self):
        calls: list[int] = []

        def produce(_context):
            calls.append(len(calls) + 1)
            return NodeResult(outputs={"artifact": f"run-{calls[-1]}"})

        with tempfile.TemporaryDirectory() as directory:
            first = GraphExecutor(
                PipelineGraph(
                    [
                        node(
                            "produce",
                            produce,
                            provides={"artifact"},
                            fingerprint="stable",
                            cache=False,
                        )
                    ]
                )
            ).execute(GraphContext(directory), targets={"artifact"})
            second = GraphExecutor(
                PipelineGraph(
                    [
                        node(
                            "produce",
                            produce,
                            provides={"artifact"},
                            fingerprint="stable",
                            cache=True,
                        )
                    ]
                )
            ).execute(GraphContext(directory), targets={"artifact"})

        self.assertEqual(first.values["artifact"], "run-1")
        self.assertEqual(second.values["artifact"], "run-2")
        self.assertEqual(second.executed, ("produce",))
        self.assertEqual(second.skipped, ())
        self.assertEqual(calls, [1, 2])

    def test_node_cannot_provide_a_private_initial_value(self):
        calls: list[str] = []
        graph = PipelineGraph(
            [
                node(
                    "replace-control",
                    lambda _context: (
                        calls.append("called")
                        or NodeResult(outputs={"pipeline.argv": ["unsafe"]})
                    ),
                    provides={"pipeline.argv"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            context = GraphContext(
                directory,
                values={"pipeline.argv": ["safe"]},
                private_value_names=frozenset({"pipeline.argv"}),
            )
            with self.assertRaisesRegex(
                NodeContractError,
                "private initial values.*pipeline.argv",
            ):
                GraphExecutor(graph).execute(
                    context,
                    targets={"pipeline.argv"},
                )

        self.assertEqual(calls, [])

    def test_reused_context_does_not_treat_removed_node_output_as_input(self):
        graph = PipelineGraph(
            [
                node(
                    "produce",
                    lambda _ctx: NodeResult(outputs={"middle": "value"}),
                    provides={"middle"},
                ),
                node(
                    "consume",
                    lambda ctx: NodeResult(outputs={"result": ctx["middle"]}),
                    requires={"middle"},
                    provides={"result"},
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            context = GraphContext(directory)
            executor = GraphExecutor(graph)
            executor.execute(context, targets={"result"})
            graph.remove("produce")
            with self.assertRaises(UnknownTargetError) as raised:
                executor.execute(context, targets={"result"})
        self.assertIn("middle", str(raised.exception))

    def test_reused_context_restores_initial_value_overwritten_by_node(self):
        graph = PipelineGraph(
            [
                node(
                    "override",
                    lambda _ctx: NodeResult(outputs={"value": "produced"}),
                    provides={"value"},
                ),
                node(
                    "consume",
                    lambda ctx: NodeResult(outputs={"result": ctx["value"]}),
                    requires={"value"},
                    provides={"result"},
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            context = GraphContext(directory, {"value": "initial"})
            executor = GraphExecutor(graph)
            first = executor.execute(context, targets={"result"})
            self.assertEqual(first.values["result"], "produced")
            graph.remove("override")
            second = executor.execute(context, targets={"result"}, force={"consume"})
        self.assertEqual(second.values["result"], "initial")

    def test_private_initial_values_are_not_exposed_in_run_result(self):
        graph = PipelineGraph(
            [
                node(
                    "consume",
                    lambda ctx: NodeResult(outputs={"result": len(ctx["secret"])}),
                    requires={"secret"},
                    provides={"result"},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = GraphExecutor(graph).execute(
                GraphContext(
                    directory,
                    {"secret": "do-not-return"},
                    private_value_names=frozenset({"secret"}),
                )
            )
        self.assertEqual(result.values, {"result": 13})
        self.assertNotIn("do-not-return", repr(result))

    def test_executes_then_restores_matching_fingerprint_cache(self):
        calls = []

        def first(ctx):
            calls.append("ocr")
            return NodeResult(outputs={"pages": ctx["pdf"] + ".pages"})

        def second(ctx):
            calls.append("compile")
            return NodeResult(outputs={"docx": ctx["pages"] + ".docx"})

        graph = PipelineGraph(
            [
                node("ocr", first, requires={"pdf"}, provides={"pages"}),
                node("compile", second, requires={"pages"}, provides={"docx"}),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            executor = GraphExecutor(graph)
            first_run = executor.execute(GraphContext(directory, {"pdf": "book.pdf"}))
            second_run = executor.execute(GraphContext(directory, {"pdf": "book.pdf"}))
            self.assertEqual(first_run.executed, ("ocr", "compile"))
            self.assertEqual(second_run.skipped, ("ocr", "compile"))
            self.assertEqual(second_run.values["docx"], "book.pdf.pages.docx")
            self.assertEqual(calls, ["ocr", "compile"])
            state = json.loads(first_run.state_path.read_text(encoding="utf-8"))
            self.assertEqual(set(state["nodes"]), {"ocr", "compile"})
            events = [
                json.loads(line)
                for line in first_run.events_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("node_succeeded", {event["event"] for event in events})
            self.assertIn("node_skipped", {event["event"] for event in events})

    def test_changed_input_invalidates_node_and_downstream(self):
        calls = []
        graph = PipelineGraph(
            [
                node(
                    "uppercase",
                    lambda ctx: (calls.append("uppercase") or NodeResult(outputs={"upper": ctx["source"].upper()})),
                    requires={"source"},
                    provides={"upper"},
                ),
                node(
                    "suffix",
                    lambda ctx: (calls.append("suffix") or NodeResult(outputs={"result": ctx["upper"] + "!"})),
                    requires={"upper"},
                    provides={"result"},
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            executor = GraphExecutor(graph)
            executor.execute(GraphContext(directory, {"source": "a"}))
            result = executor.execute(GraphContext(directory, {"source": "b"}))
        self.assertEqual(result.executed, ("uppercase", "suffix"))
        self.assertEqual(calls, ["uppercase", "suffix", "uppercase", "suffix"])

    def test_custom_node_fingerprint_and_cache_validator(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.txt"

            def produce(_ctx):
                calls.append(1)
                artifact.write_text("ok", encoding="utf-8")
                return NodeResult(outputs={"artifact": artifact})

            graph = PipelineGraph(
                [
                    node(
                        "publish",
                        produce,
                        provides={"artifact"},
                        fingerprint=lambda ctx: ctx.config["profile"],
                        cache_validator=lambda _ctx, outputs: Path(outputs["artifact"]).exists(),
                    )
                ]
            )
            executor = GraphExecutor(graph)
            executor.execute(GraphContext(directory, config={"profile": "v1"}))
            executor.execute(GraphContext(directory, config={"profile": "v1"}))
            artifact.unlink()
            result = executor.execute(GraphContext(directory, config={"profile": "v1"}))
            self.assertEqual(result.executed, ("publish",))
            self.assertEqual(calls, [1, 1])

    def test_failure_is_attributed_and_does_not_leave_lock(self):
        def fail(_ctx):
            raise ValueError("boom")

        with tempfile.TemporaryDirectory() as directory:
            executor = GraphExecutor(PipelineGraph([node("bad", fail)]))
            with self.assertRaises(NodeExecutionError) as raised:
                executor.execute(GraphContext(directory))
            self.assertEqual(raised.exception.node_name, "bad")
            self.assertIsInstance(raised.exception.__cause__, ValueError)
            executor.graph.replace("bad", node("bad", lambda _ctx: NodeResult()))
            self.assertEqual(executor.execute(GraphContext(directory)).executed, ("bad",))

    def test_output_directory_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "lock"
            with OutputDirectoryLock(lock_path):
                with self.assertRaises(OutputDirectoryLockedError):
                    OutputDirectoryLock(lock_path).acquire()
            with OutputDirectoryLock(lock_path):
                self.assertTrue(lock_path.exists())

    def test_output_directory_lock_keeps_advisory_file_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "output.lock"
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 2_000_000_000,
                        "hostname": socket.gethostname(),
                        "token": "dead-owner",
                    }
                ),
                encoding="utf-8",
            )

            first = OutputDirectoryLock(lock_path)
            first.acquire()
            try:
                with self.assertRaises(OutputDirectoryLockedError):
                    OutputDirectoryLock(lock_path).acquire()
            finally:
                first.release()

            # Advisory locks live on the open file descriptor.  The metadata
            # file is durable diagnostics and must not be unlinked on release.
            self.assertTrue(lock_path.is_file())
            json.loads(lock_path.read_text(encoding="utf-8"))
            with OutputDirectoryLock(lock_path):
                self.assertTrue(lock_path.is_file())
            self.assertTrue(lock_path.is_file())

    def test_stale_metadata_cannot_create_two_multiprocess_lock_owners(self):
        process_context = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "output.lock"
            # This simulates the durable owner JSON left by a process that is
            # no longer alive.  Both contenders must lock the same inode;
            # neither may unlink-and-recreate it as a stale-file recovery step.
            lock_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 2_000_000_000,
                        "hostname": socket.gethostname(),
                        "token": "dead-owner",
                    }
                ),
                encoding="utf-8",
            )
            ready = process_context.Queue()
            start = process_context.Event()
            counter_guard = process_context.Lock()
            active_owners = process_context.Value("i", 0)
            maximum_owners = process_context.Value("i", 0)
            results = process_context.Queue()
            processes = [
                process_context.Process(
                    target=_contend_for_output_lock,
                    args=(
                        str(lock_path),
                        ready,
                        start,
                        counter_guard,
                        active_owners,
                        maximum_owners,
                        results,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            try:
                self.assertEqual(len({ready.get(timeout=10) for _ in processes}), 2)
                start.set()
                outcomes = [results.get(timeout=10) for _ in processes]
                for process in processes:
                    process.join(timeout=10)
                self.assertEqual(len(outcomes), 2)
                self.assertTrue(all(status == "ok" for status, _ in outcomes), outcomes)
                self.assertTrue(all(process.exitcode == 0 for process in processes))
                self.assertEqual(active_owners.value, 0)
                self.assertEqual(maximum_owners.value, 1)
                self.assertTrue(lock_path.is_file())
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
