"""Process-level lifecycle tests for the managed local LLM server.

Every test uses ``tests/fixtures/stub_llama_server.py`` as the server binary
(a real child process that binds 127.0.0.1 and inherits the lease descriptor)
instead of a model; real-model checks are run separately.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

from translation_agent.pipeline.book_pipeline import ChatTranslator
from translation_agent.pipeline.graph.core import OutputDirectoryLock, OutputDirectoryLockedError
from translation_agent.runtime import local_llm_server as server_module
from translation_agent.runtime.local_llm_server import (
    LocalContextOverflowError,
    LocalLlamaChatClient,
    LocalOutputTruncatedError,
    LocalSemanticRequest,
    LocalServerUnavailableError,
    ManagedLlamaServer,
    recover_orphaned_server,
)
from translation_agent.runtime.model_lease import (
    HeavyModelLeaseCancelled,
    acquire_heavy_model_lease,
    heavy_model_lock_path,
)
from translation_agent.runtime.pipeline_profiles import (
    LocalModelSettings,
    ModelProfile,
    load_pipeline_profiles,
)
from translation_agent.semantic.semantic_translation_runner import SYSTEM_PROMPT, translate_units
from tests.test_semantic_translation_runner import _unit

STUB = Path(__file__).with_name("fixtures") / "stub_llama_server.py"
REPOSITORY = Path(__file__).resolve().parents[1]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _lock_is_free() -> bool:
    lock = OutputDirectoryLock(heavy_model_lock_path())
    try:
        lock.acquire()
    except OutputDirectoryLockedError:
        return False
    lock.release()
    return True


def _assert_no_overlap(case: unittest.TestCase, timeline: Path) -> list[tuple[str, float, float]]:
    """Pair load/unload events per pid and require disjoint intervals."""

    events = [json.loads(line) for line in timeline.read_text(encoding="utf-8").splitlines()]
    opened: dict[int, tuple[str, float]] = {}
    intervals: list[tuple[str, float, float]] = []
    for event in events:
        if event["event"] == "load":
            opened[event["pid"]] = (event["who"], event["t"])
        else:
            who, start = opened.pop(event["pid"])
            intervals.append((who, start, event["t"]))
    case.assertEqual(opened, {}, "every heavy model must have been unloaded")
    intervals.sort(key=lambda item: item[1])
    for first, second in zip(intervals, intervals[1:]):
        case.assertLessEqual(first[2], second[1], f"overlapping heavy-model intervals: {first} {second}")
    return intervals


class LocalServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.timeline = self.root / "timeline.jsonl"
        environment = mock.patch.dict(
            os.environ,
            {"HOME": str(self.root), "STUB_TIMELINE": str(self.timeline)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        # An executable whose command line contains its own path, like the
        # real llama-server binary does.
        self.binary = self.root / "bin" / "llama-server"
        self.binary.parent.mkdir()
        self.binary.write_text(f"#!{sys.executable} -I\n" + STUB.read_text(encoding="utf-8"), encoding="utf-8")
        self.binary.chmod(0o755)
        self.model = self.root / "model.gguf"
        self.model.write_bytes(b"GGUF stub weights")
        self.settings = LocalModelSettings(
            server_binary=self.binary,
            server_sha256=_sha(self.binary),
            model_file=self.model,
            model_sha256=_sha(self.model),
            context_size=1024,
            max_output_tokens=256,
            max_input_chars=600,
        )
        self.profile = ModelProfile(
            name="local",
            adapter="llama-cpp-local",
            provider="tencent-hunyuan",
            model="stub",
            timeout=30,
            local=self.settings,
        )

    def ocr_holder(self, *, hold_seconds: float) -> subprocess.Popen:
        """Separate process that takes the lease like native OCR does."""

        script = textwrap.dedent(
            f"""
            import json, os, sys, threading, time
            sys.path.insert(0, {str(REPOSITORY)!r})
            from translation_agent.runtime.model_lease import acquire_heavy_model_lease, release_heavy_model_lease
            def record(event):
                with open(os.environ["STUB_TIMELINE"], "a") as stream:
                    stream.write(json.dumps({{"event": event, "pid": os.getpid(), "t": time.time(), "who": "ocr"}}) + "\\n")
            lock = acquire_heavy_model_lease("paddleocr-native", cancelled=threading.Event())
            record("load"); print("held", flush=True)
            time.sleep({hold_seconds})
            record("unload")
            release_heavy_model_lease("paddleocr-native", lock)
            """
        )
        return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)


class ManagedServerLifecycleTests(LocalServerTestCase):
    def test_construction_and_status_do_not_load_and_stage_end_unloads(self) -> None:
        client = LocalLlamaChatClient(self.profile)
        self.assertFalse(client.server.running)
        self.assertFalse(self.timeline.exists())
        self.assertTrue(_lock_is_free())

        with mock.patch.dict(os.environ, {"STUB_REPLY": "中文译文。"}):
            self.assertEqual(client.chat_text("Hello.", system=""), "中文译文。")
            self.assertEqual(client.chat_text("Again.", system=""), "中文译文。")
        server_pid = client.server._server_pid
        self.assertFalse(_lock_is_free(), "the lease is held while the model is loaded")
        state = json.loads(server_module.state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["server_pid"], server_pid)

        client.close()
        self.assertFalse(_alive(server_pid))
        self.assertTrue(_lock_is_free())
        self.assertFalse(server_module.state_path().exists())
        events = [json.loads(line)["event"] for line in self.timeline.read_text().splitlines()]
        self.assertEqual(events, ["load", "unload"], "one load per stage, reused across requests")
        self.assertEqual(client.server.metrics.requests, 2)

    def test_listens_on_loopback_with_per_session_key(self) -> None:
        server = ManagedLlamaServer(self.settings, label="test")
        self.addCleanup(server.close)
        server.ensure_started()
        self.assertTrue(server.base_url.startswith("http://127.0.0.1:"))
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{server.base_url}/v1/chat/completions", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)

    def test_environment_proxy_is_never_used_for_loopback(self) -> None:
        with mock.patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:9", "STUB_REPLY": "译文。"}):
            server = ManagedLlamaServer(self.settings, label="test")
            self.addCleanup(server.close)
            self.assertEqual(server.chat([{"role": "user", "content": "Hi"}], max_tokens=32, timeout=5), "译文。")

    def test_occupied_fixed_port_is_not_reused(self) -> None:
        import socket

        with socket.socket() as other_service:
            other_service.bind(("127.0.0.1", 0))
            other_service.listen()
            port = other_service.getsockname()[1]
            settings = LocalModelSettings(**{**self.settings.__dict__, "port": port})
            server = ManagedLlamaServer(settings, label="test")
            with self.assertRaisesRegex(LocalServerUnavailableError, "already in use"):
                server.ensure_started()
        self.assertTrue(_lock_is_free())
        self.assertFalse(self.timeline.exists())

    def test_hash_mismatch_fails_before_loading(self) -> None:
        self.model.write_bytes(b"different weights")
        server = ManagedLlamaServer(self.settings, label="test")
        with self.assertRaisesRegex(LocalServerUnavailableError, "SHA-256 mismatch"):
            server.ensure_started()
        self.assertTrue(_lock_is_free())
        self.assertFalse(self.timeline.exists())

    def test_overflow_and_truncation_are_errors_not_partial_text(self) -> None:
        server = ManagedLlamaServer(self.settings, label="test")
        self.addCleanup(server.close)
        with self.assertRaises(LocalContextOverflowError):
            # The stub counts 1 token per 3 characters; 1024-token context.
            server.chat([{"role": "user", "content": "x" * 3200}], max_tokens=64, timeout=10)
        with mock.patch.dict(os.environ, {"STUB_FINISH": "length"}):
            server.close()
            server = ManagedLlamaServer(self.settings, label="test")
            self.addCleanup(server.close)
            with self.assertRaises(LocalOutputTruncatedError):
                server.chat([{"role": "user", "content": "Hello"}], max_tokens=64, timeout=10)

    def test_backend_crash_during_load_releases_the_lease(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_EXIT_AFTER_LOAD": "1"}):
            server = ManagedLlamaServer(self.settings, label="test")
            with self.assertRaisesRegex(LocalServerUnavailableError, "exited during load"):
                server.ensure_started()
        self.assertTrue(_lock_is_free())
        self.assertFalse(server_module.state_path().exists())


class MemoryGuardTests(LocalServerTestCase):
    def test_sustained_critical_pressure_stops_only_this_backend(self) -> None:
        settings = LocalModelSettings(**{**self.settings.__dict__, "critical_pressure_seconds": 1})
        server = ManagedLlamaServer(settings, label="test")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(unrelated.kill)
        critical = {"pressure_level": 4, "pressure": "critical", "swap_used_mib": 0.0}
        with mock.patch.object(server_module, "system_memory_snapshot", return_value=critical):
            server.ensure_started()
            server_pid = server._server_pid
            self.assertTrue(_wait_gone(server_pid, timeout=10))
        deadline = time.monotonic() + 5
        while not _lock_is_free() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(_lock_is_free())
        self.assertIsNone(unrelated.poll(), "other processes are never stopped")
        with self.assertRaisesRegex(LocalServerUnavailableError, "memory-pressure guard"):
            server.chat([{"role": "user", "content": "Hello"}], max_tokens=32, timeout=5)
        self.assertIn("critical", server.metrics.pressure_seen)


class LeaseExclusionTests(LocalServerTestCase):
    def test_llm_waits_for_ocr_then_ocr_waits_for_llm(self) -> None:
        ocr = self.ocr_holder(hold_seconds=1.0)
        self.assertEqual(ocr.stdout.readline().strip(), "held")
        server = ManagedLlamaServer(self.settings, label="test")
        with mock.patch.dict(os.environ, {"STUB_REPLY": "译文。"}):
            server.chat([{"role": "user", "content": "Hello"}], max_tokens=32, timeout=10)
        ocr.wait(10)
        follower = self.ocr_holder(hold_seconds=0.1)
        time.sleep(0.5)  # the follower must still be queued
        self.assertIsNone(follower.poll())
        server.close()
        self.assertEqual(follower.wait(10), 0)
        ocr.stdout.close()
        follower.stdout.close()
        intervals = _assert_no_overlap(self, self.timeline)
        self.assertEqual([who for who, *_ in intervals], ["ocr", "llm", "ocr"])

    def test_cancel_while_queued_never_loads(self) -> None:
        holder = OutputDirectoryLock(heavy_model_lock_path(), purpose="paddleocr-native")
        holder.acquire()
        self.addCleanup(holder.release)
        server = ManagedLlamaServer(self.settings, label="test")
        errors: list[BaseException] = []

        def run() -> None:
            try:
                server.ensure_started()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the assertion below
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.4)
        server.close()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], HeavyModelLeaseCancelled)
        self.assertFalse(self.timeline.exists(), "no model may load while another holds the lease")

    def test_waiting_lease_wait_can_be_cancelled(self) -> None:
        holder = OutputDirectoryLock(heavy_model_lock_path())
        holder.acquire()
        self.addCleanup(holder.release)
        cancelled = threading.Event()
        threading.Timer(0.3, cancelled.set).start()
        with self.assertRaises(HeavyModelLeaseCancelled):
            acquire_heavy_model_lease("local-llm", cancelled=cancelled)


class ParentExitTests(LocalServerTestCase):
    def owner_process(self) -> subprocess.Popen:
        """Worker that loads the server and then waits to be killed."""

        settings = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in self.settings.__dict__.items()
        }
        script = textwrap.dedent(
            f"""
            import os, sys, time
            from pathlib import Path
            sys.path.insert(0, {str(REPOSITORY)!r})
            from translation_agent.runtime.pipeline_profiles import LocalModelSettings
            from translation_agent.runtime.local_llm_server import ManagedLlamaServer
            values = {settings!r}
            values["server_binary"] = Path(values["server_binary"]); values["model_file"] = Path(values["model_file"])
            server = ManagedLlamaServer(LocalModelSettings(**values), label="crash")
            server.ensure_started()
            print(server._supervisor.pid, server._server_pid, flush=True)
            time.sleep(120)
            """
        )
        return subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)

    def test_killed_worker_does_not_leave_a_resident_model(self) -> None:
        owner = self.owner_process()
        supervisor_pid, server_pid = map(int, owner.stdout.readline().split())
        self.assertFalse(_lock_is_free())
        owner.send_signal(signal.SIGKILL)
        owner.wait(5)
        owner.stdout.close()
        self.assertTrue(_wait_gone(server_pid), "server must stop when its worker dies")
        self.assertTrue(_wait_gone(supervisor_pid))
        # Kernel lock is released once every inheriting process is gone; the
        # next OCR job can then load.
        follower = self.ocr_holder(hold_seconds=0.1)
        self.assertEqual(follower.wait(15), 0)
        follower.stdout.close()
        _assert_no_overlap(self, self.timeline)

    def test_orphan_holding_the_lease_is_recovered_by_the_next_job(self) -> None:
        owner = self.owner_process()
        supervisor_pid, server_pid = map(int, owner.stdout.readline().split())
        # Kill both owner and supervisor before the supervisor can react.
        os.kill(supervisor_pid, signal.SIGKILL)
        owner.send_signal(signal.SIGKILL)
        owner.wait(5)
        owner.stdout.close()
        self.assertTrue(_wait_gone(supervisor_pid))
        self.assertTrue(_alive(server_pid), "server orphan still holds the inherited lock")
        self.assertFalse(_lock_is_free())
        follower = self.ocr_holder(hold_seconds=0.1)
        self.assertEqual(follower.wait(20), 0)
        follower.stdout.close()
        self.assertFalse(_alive(server_pid))
        self.assertFalse(server_module.state_path().exists())
        _assert_no_overlap(self, self.timeline)

    def test_recovery_ignores_processes_that_are_not_ours(self) -> None:
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(unrelated.kill)
        server_module.state_path().parent.mkdir(parents=True, exist_ok=True)
        server_module.state_path().write_text(json.dumps({
            "owner_pid": 0, "supervisor_pid": 0, "server_pid": unrelated.pid,
            "binary": str(self.binary), "alias": "ta-x-deadbeef",
        }))
        self.assertIsNone(recover_orphaned_server())
        self.assertIsNone(unrelated.poll())


def _read_until(stream, marker: str, timeout: float) -> str:
    """Read a pipe until ``marker`` appears; fail instead of hanging."""

    import select

    seen = ""
    deadline = time.monotonic() + timeout
    while marker not in seen:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"{marker!r} not seen within {timeout}s; got {seen!r}")
        readable, _, _ = select.select([stream], [], [], remaining)
        if readable:
            chunk = os.read(stream.fileno(), 4096).decode("utf-8", errors="replace")
            if not chunk:
                raise AssertionError(f"stream closed before {marker!r}; got {seen!r}")
            seen += chunk
    return seen


class ServeCliSignalTests(LocalServerTestCase):
    """Real ``python -m ... serve`` subprocess receiving SIGTERM (stub backend)."""

    def serve(self) -> subprocess.Popen:
        config = LocalProfileTests.write_config(self)
        process = subprocess.Popen(
            [sys.executable, "-m", "translation_agent.runtime.local_llm_server",
             "serve", "--config", str(config), "--profile", "local"],
            cwd=REPOSITORY, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stderr.close)
        return process

    def assert_terminated(self, process: subprocess.Popen, *, within: float) -> None:
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(within), 128 + signal.SIGTERM)

    def test_sigterm_while_queued_exits_without_loading(self) -> None:
        holder = OutputDirectoryLock(heavy_model_lock_path(), purpose="paddleocr-native")
        holder.acquire()
        process = self.serve()
        try:
            _read_until(process.stderr, "queued:", timeout=20)
            # Bounded exit while the OCR lease is still held.
            self.assert_terminated(process, within=3)
        finally:
            holder.release()
        time.sleep(0.5)
        self.assertFalse(self.timeline.exists(), "a cancelled queued serve must never load")
        self.assertTrue(_lock_is_free())

    def test_sigterm_while_loading_reaps_backend_and_releases_lease(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_LOAD_SECONDS": "30"}):
            process = self.serve()
            _read_until(process.stderr, "loading ", timeout=20)
        state = server_module.state_path()
        deadline = time.monotonic() + 10
        while not state.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        recorded = json.loads(state.read_text(encoding="utf-8"))
        self.assert_terminated(process, within=15)
        for pid in (recorded["supervisor_pid"], recorded["server_pid"]):
            self.assertFalse(_alive(pid), f"pid {pid} survived serve cancellation")
        self.assertFalse(state.exists())
        self.assertTrue(_lock_is_free())
        self.assertFalse(self.timeline.exists(), "the stub was stopped before it finished loading")

    def test_sigterm_while_serving_unloads(self) -> None:
        process = self.serve()
        _read_until(process.stdout, "base_url", timeout=20)
        recorded = json.loads(server_module.state_path().read_text(encoding="utf-8"))
        self.assert_terminated(process, within=15)
        for pid in (recorded["supervisor_pid"], recorded["server_pid"]):
            self.assertFalse(_alive(pid))
        self.assertTrue(_lock_is_free())
        events = [json.loads(line)["event"] for line in self.timeline.read_text().splitlines()]
        self.assertEqual(events, ["load", "unload"])


class SemanticAndPageSeamTests(LocalServerTestCase):
    REPLY = (
        "⟦UNIT:c-u1:START⟧\n"
        "译文⟦SEMANTIC_TOKEN_0000⟧。\n\n"
        "⟦SEMANTIC_TOKEN_0001⟧: 注释。\n"
        "⟦UNIT:c-u1:END⟧"
    )

    def test_semantic_cache_hit_never_starts_the_server(self) -> None:
        units = self.root / "units.jsonl"
        units.write_text(json.dumps(_unit("c-u1", "c", 1, "Text[^n].\n\n[^n]: Note.")) + "\n", encoding="utf-8")
        identity = self.profile.identity(target_language="简体中文", prompt_version="v")
        common = dict(cache_dir=self.root / "cache", model=identity.model, base_url=identity.base_url,
                      provider="tencent-hunyuan", concurrency=1, max_chars=600)
        with mock.patch.dict(os.environ, {"STUB_REPLY": self.REPLY}):
            first = LocalSemanticRequest(self.profile, system_prompt=SYSTEM_PROMPT)
            try:
                result = translate_units(units, self.root / "one.jsonl", request=first, **common)
            finally:
                first.close()
        self.assertEqual(result["cache_hits"], 0)
        second = LocalSemanticRequest(self.profile, system_prompt=SYSTEM_PROMPT)
        try:
            result = translate_units(units, self.root / "two.jsonl", request=second, **common)
        finally:
            second.close()
        self.assertEqual(result["cache_hits"], 1)
        self.assertFalse(second.server.running)
        events = [json.loads(line)["event"] for line in self.timeline.read_text().splitlines()]
        self.assertEqual(events, ["load", "unload"])
        self.assertTrue(_lock_is_free())

    def test_page_translator_uses_short_prompt_and_keeps_numbering_gate(self) -> None:
        calls: list[tuple[str, str]] = []

        class Client:
            def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
                calls.append((prompt, system))
                return "1. 注释。" if prompt.rstrip().endswith("Note.") else "正文。"

        translator = ChatTranslator(Client(), max_chars=600, prompt_style="hy-mt")
        with self.assertRaisesRegex(RuntimeError, "omitted numbered footnote"):
            translator.translate("Body text.\n1 Note.", source_language="en", target_language="简体中文")
        self.assertTrue(calls[0][0].startswith("将以下文本翻译为简体中文，注意只需要输出翻译后的结果"))
        self.assertEqual(calls[0][1], "")


class LocalProfileTests(LocalServerTestCase):
    def write_config(self, extra: str = "") -> Path:
        path = self.root / "pipeline.toml"
        path.write_text(textwrap.dedent(f"""
            schema_version = 1
            [profiles.remote]
            adapter = "openai-chat"
            provider = "deepseek"
            base_url = "https://api.deepseek.com"
            model = "deepseek-v4-flash"
            credential_env = "DEEPSEEK_API_KEY"
            concurrency = 16
            [profiles.local]
            adapter = "llama-cpp-local"
            provider = "tencent-hunyuan"
            model = "stub"
            {extra}
            [profiles.local.local]
            server_binary = "{self.binary}"
            server_sha256 = "{self.settings.server_sha256}"
            model_file = "{self.model}"
            model_sha256 = "{self.settings.model_sha256}"
            [pipeline]
            toc_profile = "remote"
            translation_profile = "local"
            """), encoding="utf-8")
        return path

    def test_local_profile_is_single_slot_and_not_used_for_proofreading(self) -> None:
        profiles = load_pipeline_profiles(self.write_config())
        local = profiles.for_stage("translation")
        self.assertEqual(local.local.context_size, 4096)
        self.assertEqual(local.local.max_output_tokens, 2048)
        # Proofreading would fall back to the translation profile; the local
        # model is translation-only, so that is an explicit error.
        with self.assertRaisesRegex(ValueError, "translation-only"):
            profiles.for_stage("proofread")
        self.assertEqual(profiles.for_stage("proofread", "remote").name, "remote")
        with self.assertRaisesRegex(ValueError, "concurrency=1"):
            load_pipeline_profiles(self.write_config("concurrency = 16"))

    def test_identity_pins_model_hash(self) -> None:
        first = self.profile.identity(target_language="简体中文", prompt_version="v")
        changed = ModelProfile(**{**self.profile.__dict__, "local": LocalModelSettings(
            **{**self.settings.__dict__, "model_sha256": "0" * 64})})
        self.assertIn(self.settings.model_sha256, first.model)
        self.assertNotEqual(first.fingerprint, changed.identity(target_language="简体中文", prompt_version="v").fingerprint)

    def test_local_settings_reject_unbounded_generation(self) -> None:
        for change in ({"context_size": 32768}, {"max_output_tokens": 32768}, {"server_sha256": "abc"}):
            with self.assertRaises(ValueError):
                LocalModelSettings(**{**self.settings.__dict__, **change})

    def test_epub_runspec_plan_needs_no_credentials_and_does_not_load(self) -> None:
        from tests.test_epub_graph import _write_epub
        from translation_agent.runtime.product_contracts import RunSpec
        from translation_agent.runtime.run_execution_service import plan_runspec

        source = self.root / "book.epub"
        _write_epub(source)
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            plan = plan_runspec(RunSpec(
                source=str(source), source_mode="epub", output_dir=str(self.root / "out"),
                config=str(self.write_config()), translate=True, verify=False,
                targets=("publication.epub",), options={"translation_profile": "local"},
            ))
        self.assertIn("core.semantic.translate", plan.node_names)
        self.assertFalse(self.timeline.exists())
        self.assertTrue(_lock_is_free())


if __name__ == "__main__":
    unittest.main()
