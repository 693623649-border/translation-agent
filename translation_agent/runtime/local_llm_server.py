"""Workflow-managed local llama.cpp translation server.

Lifecycle of one stage (PDF page translation or EPUB semantic translation)::

    first request ──> acquire heavy-model lease (shared with native OCR)
                  ──> verify pinned binary/model SHA-256
                  ──> spawn supervisor -> llama-server on 127.0.0.1:<free port>
                  ──> wait /health, then serve requests on one slot
    stage end     ──> close control pipe -> supervisor stops and waits server
                  ──> confirm both PIDs are gone -> release lease

Nothing is loaded at plan/status time or when every batch is a cache hit,
because the server only starts on the first real request.  The lock
descriptor is inherited by the supervisor and llama-server, so the lease is
held for as long as any process that may still map the weights is alive.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Mapping
import urllib.error
import urllib.request

from translation_agent.runtime.model_lease import (
    acquire_heavy_model_lease,
    lease_log,
    release_heavy_model_lease,
)
from translation_agent.runtime.pipeline_profiles import LocalModelSettings, ModelProfile

COMPONENT = "local-llm"
SUPERVISOR = Path(__file__).with_name("_llama_supervisor.py")
HEALTH_TIMEOUT_SECONDS = 180.0
STOP_TIMEOUT_SECONDS = 20.0
# Chat-template tokens added around the user/system content (measured: the
# Hy-MT2 template adds < 10 tokens; keep headroom).
TEMPLATE_TOKEN_MARGIN = 48
PRESSURE_NAMES = {1: "normal", 2: "warning", 4: "critical"}
# Requests go to 127.0.0.1 only; an http_proxy from the environment must not
# receive the bearer token or book text.
_LOOPBACK = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class LocalModelError(RuntimeError):
    """Base error for the managed local model."""


class LocalContextOverflowError(LocalModelError):
    """The request cannot fit in the configured context window."""


class LocalOutputTruncatedError(LocalModelError):
    """Generation hit the output limit; the text would be incomplete."""


class LocalOutputContractError(LocalModelError):
    """The model returned empty or reasoning-prefixed content."""


class LocalServerUnavailableError(LocalModelError):
    """The backend exited, failed to start, or was stopped by the memory guard."""


def state_path() -> Path:
    return Path.home() / ".translation-agent" / "local-llm" / "server-state.json"


# --------------------------------------------------------------------------
# Memory metrics (macOS).  phys_footprint is what Activity Monitor reports as
# "Memory"; on Apple silicon it includes Metal buffers owned by the process.
# --------------------------------------------------------------------------

_RUSAGE_INFO_V4 = 4
_RESIDENT_SIZE_OFFSET = 64
_PHYS_FOOTPRINT_OFFSET = 72
_LIFETIME_MAX_FOOTPRINT_OFFSET = 240


@dataclass
class _Libproc:
    function: Any = None

    def __post_init__(self) -> None:
        name = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
        if sys.platform == "darwin":
            library = ctypes.CDLL(name, use_errno=True)
            self.function = library.proc_pid_rusage
            self.function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
            self.function.restype = ctypes.c_int


_LIBPROC = _Libproc()


def process_footprint(pid: int) -> tuple[int, int, int] | None:
    """Return (phys_footprint, lifetime_max_phys_footprint, resident) bytes.

    phys_footprint counts dirty/anonymous memory incl. Metal buffers; the
    mmap'd GGUF weights are clean file-backed pages and appear only in the
    resident size.
    """

    if _LIBPROC.function is None:
        return None
    buffer = ctypes.create_string_buffer(512)
    if _LIBPROC.function(pid, _RUSAGE_INFO_V4, buffer) != 0:
        return None
    raw = buffer.raw
    return (
        int.from_bytes(raw[_PHYS_FOOTPRINT_OFFSET:_PHYS_FOOTPRINT_OFFSET + 8], "little"),
        int.from_bytes(raw[_LIFETIME_MAX_FOOTPRINT_OFFSET:_LIFETIME_MAX_FOOTPRINT_OFFSET + 8], "little"),
        int.from_bytes(raw[_RESIDENT_SIZE_OFFSET:_RESIDENT_SIZE_OFFSET + 8], "little"),
    )


def _sysctl(name: str) -> str:
    return subprocess.run(
        ["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, check=True
    ).stdout.strip()


def system_memory_snapshot() -> dict[str, Any]:
    """Memory-pressure level and swap usage of the whole machine."""

    if sys.platform != "darwin":
        return {}
    level = int(_sysctl("kern.memorystatus_vm_pressure_level"))
    swap = _sysctl("vm.swapusage")  # "total = 5120.00M  used = 3846.31M  free = ..."
    fields = swap.replace("=", " ").split()
    used = float(fields[fields.index("used") + 1].rstrip("M"))
    return {"pressure_level": level, "pressure": PRESSURE_NAMES.get(level, str(level)), "swap_used_mib": used}


def _mib(value: int | None) -> float | None:
    return None if value is None else round(value / 1048576, 1)


@dataclass
class ServerMetrics:
    start_system: dict[str, Any] = field(default_factory=dict)
    end_system: dict[str, Any] = field(default_factory=dict)
    load_seconds: float | None = None
    footprint_after_load: int | None = None
    footprint_peak: int = 0
    footprint_end: int | None = None
    resident_peak: int = 0
    pressure_seen: set[str] = field(default_factory=set)
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    generation_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        swap_delta = None
        if self.start_system and self.end_system:
            swap_delta = round(self.end_system["swap_used_mib"] - self.start_system["swap_used_mib"], 1)
        return {
            "load_seconds": self.load_seconds,
            "footprint_after_load_mib": _mib(self.footprint_after_load),
            "footprint_peak_mib": _mib(self.footprint_peak),
            "footprint_end_mib": _mib(self.footprint_end),
            "resident_peak_mib": _mib(self.resident_peak),
            "system_start": self.start_system,
            "system_end": self.end_system,
            "swap_delta_mib": swap_delta,
            "pressure_seen": sorted(self.pressure_seen),
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "decode_tokens_per_second": (
                round(self.completion_tokens / self.generation_seconds, 1)
                if self.generation_seconds else None
            ),
        }


# --------------------------------------------------------------------------
# Verification of pinned artifacts
# --------------------------------------------------------------------------


def sha256_path(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_artifacts(settings: LocalModelSettings) -> None:
    for label, path, expected in (
        ("server_binary", settings.server_binary, settings.server_sha256),
        ("model_file", settings.model_file, settings.model_sha256),
    ):
        if not path.is_file():
            raise LocalServerUnavailableError(f"{label} not found: {path}")
        actual = sha256_path(path)
        if actual != expected:
            raise LocalServerUnavailableError(
                f"{label} SHA-256 mismatch for {path}: expected {expected}, got {actual}"
            )


# --------------------------------------------------------------------------
# Orphan recovery
# --------------------------------------------------------------------------


def _signal(pid: int, signum: int) -> None:
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        pass  # Already gone (another job may have recovered it).


def _unlink_state_if(alias: str) -> None:
    path = state_path()
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if current.get("alias") == alias:
        path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_command(pid: int) -> str:
    result = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return result.stdout.strip()


def recover_orphaned_server() -> int | None:
    """Stop a llama-server that this project started for a now-dead owner.

    Only acts when the state file names a dead owner and the recorded PID still
    runs the recorded binary with the recorded session tag.  Returns the PID
    that was stopped.
    """

    path = state_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    owner = int(state.get("owner_pid") or 0)
    supervisor = int(state.get("supervisor_pid") or 0)
    server = int(state.get("server_pid") or 0)
    # A live owner or supervisor is still responsible for stopping the server.
    if not server or any(pid and _pid_alive(pid) for pid in (owner, supervisor)):
        return None
    alias = str(state.get("alias"))
    command = _pid_command(server)
    # The per-session alias is random, so a match cannot be another service.
    if not (str(state.get("binary")) in command and f"--alias {alias}" in command):
        _unlink_state_if(alias)
        return None
    lease_log(COMPONENT, f"recovering orphaned llama-server pid={server} (owner pid={owner} is gone)")
    _signal(server, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while _pid_alive(server) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _pid_alive(server):
        _signal(server, signal.SIGKILL)
        while _pid_alive(server):
            time.sleep(0.05)
    _unlink_state_if(alias)
    return server


# --------------------------------------------------------------------------
# Managed server
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class ManagedLlamaServer:
    """One llama-server process for one workflow stage; not thread-shared across stages."""

    def __init__(self, settings: LocalModelSettings, *, label: str) -> None:
        self.settings = settings
        self.label = label
        self.metrics = ServerMetrics()
        self._cancelled = threading.Event()
        self._start_lock = threading.Lock()
        self._lease = None
        self._supervisor: subprocess.Popen | None = None
        self._control_write: int | None = None
        self._server_pid: int | None = None
        self._port: int | None = None
        self._api_key = secrets.token_urlsafe(24)
        self._alias = f"ta-{label}-{secrets.token_hex(4)}"
        self._log_path: Path | None = None
        self._sampler: threading.Thread | None = None
        self._stopped_by_guard = threading.Event()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._supervisor is not None

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise LocalServerUnavailableError("local server is not running")
        return f"http://127.0.0.1:{self._port}"

    def ensure_started(self) -> None:
        with self._start_lock:
            if self._stopped_by_guard.is_set():
                raise LocalServerUnavailableError(
                    "local server was stopped by the memory-pressure guard"
                )
            if self._closed:
                raise LocalServerUnavailableError("local server session is closed")
            if self._supervisor is None:
                self._start()

    def _start(self) -> None:
        self._lease = acquire_heavy_model_lease(COMPONENT, cancelled=self._cancelled)
        try:
            # Holding the lease, so any recorded server is dead or orphaned.
            recover_orphaned_server()
            verify_artifacts(self.settings)
            self.metrics.start_system = system_memory_snapshot()
            self._spawn()
            self._wait_healthy()
        except BaseException:
            self._shutdown()
            raise

    def _spawn(self) -> None:
        settings = self.settings
        port = settings.port or _free_port()
        if _port_in_use(port):
            raise LocalServerUnavailableError(f"127.0.0.1:{port} is already in use; not reusing another service")
        log_dir = Path.home() / ".translation-agent" / "local-llm" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"{self._alias}.log"
        command = [
            str(settings.server_binary),
            "--model", str(settings.model_file),
            "--alias", self._alias,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--ctx-size", str(settings.context_size),
            "--parallel", "1",
            "--n-gpu-layers", str(settings.gpu_layers),
            "--threads", str(settings.threads),
            "--cache-ram", "0",
            "--no-webui",
            "--jinja",
            "--offline",
            "--log-file", str(self._log_path),
        ]
        lock_fd = self._lease.fileno()
        environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().endswith(("_API_KEY", "_TOKEN", "_SECRET"))
        }
        environment["LLAMA_API_KEY"] = self._api_key
        environment["TA_SUPERVISOR_PASS_FDS"] = str(lock_fd)
        lease_log(COMPONENT, f"loading {settings.model_file.name} on 127.0.0.1:{port} (ctx={settings.context_size}, slots=1)")
        started = time.monotonic()
        control_read, control_write = os.pipe()
        try:
            with self._log_path.with_suffix(".stderr").open("wb") as stderr:
                self._supervisor = subprocess.Popen(
                    [sys.executable, "-I", str(SUPERVISOR), "--control-fd", str(control_read), "--", *command],
                    pass_fds=(control_read, lock_fd),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=stderr,
                    env=environment,
                    text=True,
                )
        except BaseException:
            os.close(control_write)
            raise
        finally:
            os.close(control_read)
        self._control_write = control_write
        self._port = port
        pid_line = self._supervisor.stdout.readline().strip()
        if not pid_line.isdigit():
            raise LocalServerUnavailableError(
                f"llama-server supervisor failed to start the server; see {self._log_path.with_suffix('.stderr')}"
            )
        self._server_pid = int(pid_line)
        self._load_started = started
        state_path().write_text(
            json.dumps({
                "owner_pid": os.getpid(),
                "supervisor_pid": self._supervisor.pid,
                "server_pid": self._server_pid,
                "binary": str(settings.server_binary),
                "alias": self._alias,
                "port": port,
                "model_sha256": settings.model_sha256,
                "started_at": time.time(),
            }) + "\n",
            encoding="utf-8",
        )

    def _wait_healthy(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._supervisor.poll() is not None:
                raise LocalServerUnavailableError(
                    f"llama-server exited during load (code {self._supervisor.returncode}); see {self._log_path}"
                )
            if self._cancelled.is_set():
                raise LocalServerUnavailableError("local server start cancelled")
            try:
                with _LOOPBACK.open(f"{self.base_url}/health", timeout=1) as response:
                    if response.status == 200:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(0.1)
        else:
            raise LocalServerUnavailableError(f"llama-server did not become healthy in {HEALTH_TIMEOUT_SECONDS:g}s")
        self.metrics.load_seconds = round(time.monotonic() - self._load_started, 2)
        sample = process_footprint(self._server_pid)
        self.metrics.footprint_after_load = sample[0] if sample else None
        lease_log(
            COMPONENT,
            f"ready in {self.metrics.load_seconds}s pid={self._server_pid} "
            f"footprint={_mib(self.metrics.footprint_after_load)}MiB",
        )
        self._sampler = threading.Thread(target=self._sample_memory, name="local-llm-memory", daemon=True)
        self._sampler.start()

    def _sample_memory(self) -> None:
        try:
            self._sample_memory_loop()
        except Exception as exc:  # noqa: BLE001 - report, then stop the backend we can no longer guard
            lease_log(COMPONENT, f"memory sampler failed ({exc}); stopping this task's backend")
            self._stopped_by_guard.set()
            self.close()

    def _sample_memory_loop(self) -> None:
        critical_streak = 0
        while self._supervisor is not None and not self._closed:
            sample = process_footprint(self._server_pid) if self._server_pid else None
            if sample:
                self.metrics.footprint_peak = max(self.metrics.footprint_peak, sample[0], sample[1])
                self.metrics.resident_peak = max(self.metrics.resident_peak, sample[2])
            system = system_memory_snapshot()
            if system:
                self.metrics.pressure_seen.add(system["pressure"])
                critical_streak = critical_streak + 1 if system["pressure_level"] >= 4 else 0
                if critical_streak >= self.settings.critical_pressure_seconds:
                    lease_log(COMPONENT, "memory pressure stayed critical; stopping this task's backend only")
                    self._stopped_by_guard.set()
                    threading.Thread(target=self.close, daemon=True).start()
                    return
            time.sleep(1.0)

    def close(self) -> None:
        # Signal first: a start may be holding _start_lock while it waits in
        # the lease queue, and only this event lets it give up.
        self._cancelled.set()
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown()

    def _shutdown(self) -> None:
        try:
            self._stop_processes()
        finally:
            self._port = None
            if self._lease is not None:
                release_heavy_model_lease(COMPONENT, self._lease)
                self._lease = None

    def _stop_processes(self) -> None:
        supervisor = self._supervisor
        if supervisor is None:
            return
        if self._server_pid:
            sample = process_footprint(self._server_pid)
            if sample:
                self.metrics.footprint_end = sample[0]
                self.metrics.footprint_peak = max(self.metrics.footprint_peak, sample[0], sample[1])
                self.metrics.resident_peak = max(self.metrics.resident_peak, sample[2])
        lease_log(COMPONENT, f"unloading pid={self._server_pid}")
        if self._control_write is not None:
            os.close(self._control_write)
            self._control_write = None
        try:
            code = supervisor.wait(STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            supervisor.kill()
            code = supervisor.wait()
        if self._server_pid and _pid_alive(self._server_pid):
            # The supervisor was killed before it could stop its child.
            _signal(self._server_pid, signal.SIGKILL)
            while _pid_alive(self._server_pid):
                time.sleep(0.05)
        supervisor.stdout.close()
        self._supervisor = None
        _unlink_state_if(self._alias)
        self.metrics.end_system = system_memory_snapshot()
        lease_log(COMPONENT, f"unloaded (supervisor exit={code}); metrics={json.dumps(self.metrics.to_dict(), ensure_ascii=False)}")

    # -- requests ----------------------------------------------------------

    def _post(self, path: str, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._api_key}"},
            method="POST",
        )
        try:
            with _LOOPBACK.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:600]
            if exc.code == 400 and "exceed_context_size" in detail:
                raise LocalContextOverflowError(detail) from exc
            raise LocalModelError(f"llama-server HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            supervisor = self._supervisor
            if supervisor is None or supervisor.poll() is not None:
                raise LocalServerUnavailableError(f"llama-server is not running: {exc}") from exc
            raise LocalModelError(f"llama-server request failed: {exc}") from exc

    def count_tokens(self, text: str) -> int:
        return len(self._post("/tokenize", {"content": text}, timeout=30)["tokens"])

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int, timeout: float) -> str:
        """Run one chat completion; never returns truncated or empty text."""

        self.ensure_started()
        prompt_tokens = self.count_tokens("\n".join(message["content"] for message in messages)) + TEMPLATE_TOKEN_MARGIN
        available = self.settings.context_size - prompt_tokens
        if available < 64:
            raise LocalContextOverflowError(
                f"prompt needs ~{prompt_tokens} tokens; context_size={self.settings.context_size}"
            )
        budget = min(max_tokens, self.settings.max_output_tokens, available)
        started = time.monotonic()
        payload = self._post(
            "/v1/chat/completions",
            {
                "model": self._alias,
                "messages": messages,
                "max_tokens": budget,
                "temperature": self.settings.temperature,
                "top_p": self.settings.top_p,
                "top_k": self.settings.top_k,
                "repeat_penalty": self.settings.repeat_penalty,
                "stream": False,
            },
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        timings = payload.get("timings") or {}
        self.metrics.requests += 1
        self.metrics.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.metrics.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.metrics.generation_seconds += float(timings.get("predicted_ms") or 0) / 1000
        lease_log(
            COMPONENT,
            f"request {self.metrics.requests}: prompt={usage.get('prompt_tokens')} "
            f"completion={usage.get('completion_tokens')} {elapsed:.2f}s "
            f"decode={timings.get('predicted_per_second', 0):.1f} tok/s",
        )
        if choice.get("finish_reason") == "length":
            raise LocalOutputTruncatedError(
                f"output reached max_tokens={budget}; split the input instead of accepting partial text"
            )
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content.strip():
            raise LocalOutputContractError("local model returned empty content")
        if "<think>" in content or choice["message"].get("reasoning_content"):
            raise LocalOutputContractError("local model returned reasoning text")
        return content


class LocalLlamaChatClient:
    """TextChatBackend for PDF page translation backed by a managed server."""

    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile
        self.settings = profile.local
        self.server = ManagedLlamaServer(self.settings, label="page-translate")

    def chat_text(self, prompt: str, *, system: str, max_tokens: int = 16384) -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        return self.server.chat(messages, max_tokens=max_tokens, timeout=self.profile.timeout)

    def model_identity(self, *, target_language: str, prompt_version: str):
        return self.profile.identity(target_language=target_language, prompt_version=prompt_version)

    def close(self) -> None:
        self.server.close()


class LocalSemanticRequest:
    """``Callable[[str], str]`` for the semantic UNIT runner; ``close`` ends the stage."""

    def __init__(self, profile: ModelProfile, *, system_prompt: str) -> None:
        self.profile = profile
        self.system_prompt = system_prompt
        self.server = ManagedLlamaServer(profile.local, label="semantic-translate")

    def __call__(self, prompt: str) -> str:
        from translation_agent.semantic.semantic_translation_runner import (
            SemanticOutputError,
            SemanticRequestError,
        )

        try:
            return self.server.chat(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.profile.local.max_output_tokens,
                timeout=self.profile.timeout,
            )
        except (LocalContextOverflowError, LocalOutputTruncatedError, LocalOutputContractError) as exc:
            # The runner retries, then halves the batch; a single unit that
            # still does not fit fails the stage instead of being truncated.
            raise SemanticOutputError(str(exc)) from exc
        except LocalServerUnavailableError as exc:
            raise SemanticRequestError(str(exc), retryable=False) from exc
        except LocalModelError as exc:
            raise SemanticRequestError(str(exc), retryable=True) from exc

    def close(self) -> None:
        self.server.close()


def main(argv: list[str] | None = None) -> int:
    """``translation-agent-local-llm``: status, artifact check, or a foreground server."""

    import argparse

    from translation_agent.runtime.pipeline_profiles import load_pipeline_profiles
    from translation_agent.runtime.model_lease import heavy_model_lock_path

    parser = argparse.ArgumentParser(description="Manage the local llama.cpp translation server.")
    parser.add_argument("command", choices=("status", "check", "serve"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", required=True, help="llama-cpp-local translation profile name")
    args = parser.parse_args(argv)
    profile = load_pipeline_profiles(args.config).get(args.profile)
    if profile.local is None:
        parser.error(f"profile {profile.name!r} is not a llama-cpp-local profile")
    if args.command == "status":
        lock = heavy_model_lock_path()
        holder = lock.read_text(encoding="utf-8").strip() if lock.is_file() else ""
        print(json.dumps({
            "profile": profile.name,
            "model_file": str(profile.local.model_file),
            "server_binary": str(profile.local.server_binary),
            "lease_file": str(lock),
            "last_lease_holder": holder or None,
            "server_state": json.loads(state_path().read_text()) if state_path().is_file() else None,
            "system": system_memory_snapshot(),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "check":
        verify_artifacts(profile.local)
        print(f"ok: {profile.local.model_file.name} and {profile.local.server_binary} match pinned SHA-256")
        return 0
    server = ManagedLlamaServer(profile.local, label="serve")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        server.ensure_started()
        # Per-session bearer token; the server listens on 127.0.0.1 only.
        print(json.dumps({"base_url": f"{server.base_url}/v1", "model": server._alias,
                          "authorization": f"Bearer {server._api_key}"}), flush=True)
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
