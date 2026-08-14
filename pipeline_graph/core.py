"""Small, dependency-free DAG runtime for the translation pipeline.

The graph deals in named values.  A node declares the values it requires and
provides; the planner derives the execution order from those declarations.
Handlers remain ordinary synchronous Python callables, which lets the current
pipeline functions be wrapped without rewriting their checkpoint formats.
"""

from __future__ import annotations

import copy
import dataclasses
import errno
import hashlib
import json
import math
import os
import socket
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

try:  # pragma: no cover - platform-specific import.
    import fcntl
except ImportError:  # pragma: no cover - Windows.
    fcntl = None

try:  # pragma: no cover - platform-specific import.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX.
    msvcrt = None


JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)
NodeHandler: TypeAlias = Callable[["GraphContext"], "NodeResult"]
FingerprintFactory: TypeAlias = Callable[["GraphContext"], Any]
CacheValidator: TypeAlias = Callable[["GraphContext", Mapping[str, JsonValue]], bool]
ValueValidator: TypeAlias = Callable[["GraphContext", Any], bool]


class GraphError(RuntimeError):
    """Base class for graph construction, planning, and execution failures."""


class DuplicateNodeError(GraphError):
    """Raised when two nodes use the same name."""


class AmbiguousProviderError(GraphError):
    """Raised when more than one node provides the same value."""


class MissingDependencyError(GraphError):
    """Raised when no node or initial graph value satisfies a requirement."""


class GraphCycleError(GraphError):
    """Raised when the selected nodes contain a dependency cycle."""


class UnknownTargetError(GraphError):
    """Raised when an execution target has no provider."""


class NodeContractError(GraphError):
    """Raised when a node returns data inconsistent with its declaration."""


class _DeclaredValueView(MutableMapping[str, Any]):
    """Isolated node-local view of values declared in ``NodeSpec.requires``.

    The executor owns the canonical value store.  Handlers receive a private
    copy so mutating a nested list or mapping cannot silently change an
    upstream artifact while retaining its old fingerprint.  Looking up a name
    outside the declared contract is an error instead of an accidental hidden
    DAG dependency.
    """

    def __init__(
        self,
        *,
        node_name: str,
        allowed: Iterable[str],
        values: Mapping[str, Any],
    ) -> None:
        self._node_name = node_name
        self._allowed = frozenset(allowed)
        self._values = {
            name: _isolated_copy(value)
            for name, value in values.items()
            if name in self._allowed
        }

    def _check(self, name: object) -> None:
        if name not in self._allowed:
            raise NodeContractError(
                f"node {self._node_name!r} accessed undeclared input {name!r}; "
                "add it to NodeSpec.requires"
            )

    def __getitem__(self, name: str) -> Any:
        self._check(name)
        return self._values[name]

    def __setitem__(self, name: str, value: Any) -> None:
        self._check(name)
        self._values[name] = value

    def __delitem__(self, name: str) -> None:
        self._check(name)
        del self._values[name]

    def __iter__(self):  # type annotation is inferred by collections.abc.
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __contains__(self, name: object) -> bool:
        self._check(name)
        return name in self._values


class NodeExecutionError(GraphError):
    """Wrap a handler failure while retaining the original exception as cause."""

    def __init__(
        self,
        node_name: str,
        cause: BaseException,
        *,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(f"node {node_name!r} failed: {safe_message or cause}")
        self.node_name = node_name
        self.cause = cause


class GraphStateError(GraphError):
    """Raised when a graph state sidecar is malformed or unsupported."""


class OutputDirectoryLockedError(GraphError):
    """Raised when another executor owns an output directory."""


def _name_set(values: Iterable[str], *, field_name: str) -> frozenset[str]:
    result = frozenset(values)
    for value in result:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
    return result


def _isolated_copy(value: Any) -> Any:
    """Copy graph-compatible containers, including non-picklable proxies."""

    if isinstance(value, Mapping):
        return {key: _isolated_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_isolated_copy(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_isolated_copy(item) for item in value)
    if isinstance(value, set):
        return {_isolated_copy(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_isolated_copy(item) for item in value)
    return copy.deepcopy(value)


def _json_value(value: Any, *, location: str = "value") -> JsonValue:
    """Convert supported Python values to a deterministic JSON representation."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{location} contains a non-finite float")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value, location=location)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_value(dataclasses.asdict(value), location=location)
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{location} contains a non-string mapping key")
            normalized[key] = _json_value(item, location=f"{location}.{key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _json_value(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        items = [_json_value(item, location=f"{location}[]") for item in value]
        return sorted(items, key=_canonical_json)
    raise TypeError(
        f"{location} has unsupported type {type(value).__name__}; "
        "graph values must be JSON-compatible (Path, Enum, and dataclass are "
        "also accepted)"
    )


def _canonical_json(value: JsonValue) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def stable_fingerprint(value: Any) -> str:
    """Return a stable SHA-256 fingerprint for a graph-compatible value."""

    normalized = _json_value(value)
    return hashlib.sha256(_canonical_json(normalized).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NodeResult:
    """Values produced by one node plus optional audit metadata.

    Outputs are normalized to JSON-compatible values immediately so a first
    run and a restored run expose identical types.  A node may supply a custom
    content fingerprint for an output (for example a file checksum); otherwise
    the runtime fingerprints the normalized value itself.
    """

    outputs: Mapping[str, Any] = field(default_factory=dict)
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_outputs = _json_value(dict(self.outputs), location="outputs")
        normalized_metadata = _json_value(dict(self.metadata), location="metadata")
        assert isinstance(normalized_outputs, dict)
        assert isinstance(normalized_metadata, dict)
        fingerprints = dict(self.fingerprints)
        if not set(fingerprints).issubset(normalized_outputs):
            unknown = sorted(set(fingerprints) - set(normalized_outputs))
            raise ValueError(f"fingerprints refer to unknown outputs: {unknown}")
        for name, value in fingerprints.items():
            if not isinstance(name, str) or not isinstance(value, str) or not value:
                raise ValueError("output fingerprints must be non-empty strings")
        object.__setattr__(
            self, "outputs", MappingProxyType(normalized_outputs)
        )
        object.__setattr__(self, "fingerprints", MappingProxyType(fingerprints))
        object.__setattr__(
            self, "metadata", MappingProxyType(normalized_metadata)
        )


@dataclass(frozen=True)
class NodeSpec:
    """Declarative contract for a replaceable graph node."""

    name: str
    handler: NodeHandler
    requires: frozenset[str] = field(default_factory=frozenset)
    fingerprint_requires: frozenset[str] | None = None
    provides: frozenset[str] = field(default_factory=frozenset)
    version: str = "1"
    fingerprint: str | FingerprintFactory | None = None
    resources: frozenset[str] = field(
        default_factory=lambda: frozenset({"output_dir"})
    )
    cache: bool = True
    cache_validator: CacheValidator | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("node name must be a non-empty string")
        if not callable(self.handler):
            raise TypeError("node handler must be callable")
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("node version must be a non-empty string")
        if self.fingerprint is not None and not (
            isinstance(self.fingerprint, str) or callable(self.fingerprint)
        ):
            raise TypeError("fingerprint must be a string, callable, or None")
        if self.cache_validator is not None and not callable(self.cache_validator):
            raise TypeError("cache_validator must be callable or None")
        object.__setattr__(
            self, "requires", _name_set(self.requires, field_name="requires")
        )
        fingerprint_requires = (
            self.requires
            if self.fingerprint_requires is None
            else _name_set(
                self.fingerprint_requires,
                field_name="fingerprint_requires",
            )
        )
        if not set(fingerprint_requires).issubset(self.requires):
            unknown = sorted(set(fingerprint_requires) - set(self.requires))
            raise ValueError(
                "fingerprint_requires must be a subset of requires: "
                f"{unknown}"
            )
        object.__setattr__(self, "fingerprint_requires", fingerprint_requires)
        object.__setattr__(
            self, "provides", _name_set(self.provides, field_name="provides")
        )
        object.__setattr__(
            self, "resources", _name_set(self.resources, field_name="resources")
        )


@dataclass
class GraphContext:
    """Mutable values and immutable-ish configuration shared by graph nodes."""

    output_dir: Path | str
    values: MutableMapping[str, Any] = field(default_factory=dict)
    config: Mapping[str, Any] = field(default_factory=dict)
    fingerprints: MutableMapping[str, str] = field(default_factory=dict)
    private_value_names: frozenset[str] = field(default_factory=frozenset)
    redaction_values: tuple[str, ...] = field(
        default_factory=tuple,
        repr=False,
    )
    value_validators: Mapping[str, ValueValidator] = field(
        default_factory=dict,
        repr=False,
    )
    run_id: str | None = field(default=None, init=False)
    node_name: str | None = field(default=None, init=False)
    _initial_value_names: frozenset[str] = field(
        default_factory=frozenset,
        init=False,
        repr=False,
    )
    _initial_values: dict[str, Any] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _initial_fingerprints: dict[str, str] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _produced_value_names: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.values = _isolated_copy(dict(self.values))
        self.config = MappingProxyType(_isolated_copy(dict(self.config)))
        self.fingerprints = dict(self.fingerprints)
        self.private_value_names = _name_set(
            self.private_value_names,
            field_name="private_value_names",
        )
        self.redaction_values = tuple(
            sorted(
                {
                    value
                    for value in self.redaction_values
                    if isinstance(value, str) and len(value) >= 4
                },
                key=len,
                reverse=True,
            )
        )
        self.value_validators = MappingProxyType(dict(self.value_validators))
        for name, validator in self.value_validators.items():
            if not isinstance(name, str) or not name or not callable(validator):
                raise ValueError("value_validators must map names to callables")
        self._initial_value_names = frozenset(self.values)
        self._initial_values = _isolated_copy(dict(self.values))
        self._initial_fingerprints = dict(self.fingerprints)
        _name_set(self.values, field_name="values")
        _name_set(self.fingerprints, field_name="fingerprints")
        if not set(self.fingerprints).issubset(self.values):
            unknown = sorted(set(self.fingerprints) - set(self.values))
            raise ValueError(f"fingerprints refer to unknown values: {unknown}")
        if not set(self.private_value_names).issubset(self.values):
            unknown = sorted(set(self.private_value_names) - set(self.values))
            raise ValueError(
                f"private_value_names refer to unknown values: {unknown}"
            )
        for name, value in self.fingerprints.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"fingerprint for {name!r} must be a non-empty string")

    def require(self, name: str) -> Any:
        """Return a required value with a useful error for handler authors."""

        try:
            return self.values[name]
        except KeyError as exc:
            raise MissingDependencyError(
                f"node {self.node_name!r} requires unavailable value {name!r}"
            ) from exc

    def __getitem__(self, name: str) -> Any:
        return self.require(name)

    def _for_node(self, node: NodeSpec) -> "GraphContext":
        """Build the isolated contract view passed to one node callback."""

        required_values = {
            name: self.values[name]
            for name in node.requires
            if name in self.values
        }
        required_fingerprints = {
            name: self.fingerprints[name]
            for name in node.requires
            if name in self.fingerprints
        }
        invocation = GraphContext(
            self.output_dir,
            values=required_values,
            config=self.config,
            fingerprints=required_fingerprints,
            private_value_names=frozenset(
                set(self.private_value_names) & set(node.requires)
            ),
            redaction_values=self.redaction_values,
            value_validators=self.value_validators,
        )
        invocation.values = _DeclaredValueView(
            node_name=node.name,
            allowed=node.requires,
            values=required_values,
        )
        invocation.fingerprints = _DeclaredValueView(
            node_name=node.name,
            allowed=node.requires,
            values=required_fingerprints,
        )
        invocation.run_id = self.run_id
        invocation.node_name = node.name
        return invocation

    def _reset_produced_values(self) -> None:
        """Drop values restored or produced by a previous executor run."""

        for name in self._produced_value_names:
            if name in self._initial_value_names:
                self.values[name] = _isolated_copy(self._initial_values[name])
                if name in self._initial_fingerprints:
                    self.fingerprints[name] = self._initial_fingerprints[name]
                else:
                    self.fingerprints.pop(name, None)
            else:
                self.values.pop(name, None)
                self.fingerprints.pop(name, None)
        self._produced_value_names.clear()


class PipelineGraph:
    """Mutable node registry with deterministic dependency planning."""

    def __init__(self, nodes: Iterable[NodeSpec] = ()) -> None:
        self._nodes: dict[str, NodeSpec] = {}
        for node in nodes:
            self.add(node)

    @property
    def nodes(self) -> tuple[NodeSpec, ...]:
        return tuple(self._nodes.values())

    def add(self, node: NodeSpec) -> "PipelineGraph":
        if node.name in self._nodes:
            raise DuplicateNodeError(f"duplicate node name: {node.name!r}")
        self._nodes[node.name] = node
        return self

    def replace(self, name: str, node: NodeSpec) -> "PipelineGraph":
        """Replace a node in place, preserving deterministic registry order."""

        if name not in self._nodes:
            raise KeyError(name)
        if node.name != name:
            raise ValueError(
                f"replacement node must retain name {name!r}, got {node.name!r}"
            )
        self._nodes[name] = node
        return self

    def remove(self, name: str) -> NodeSpec:
        """Remove and return a node; planning reports any resulting gap."""

        try:
            return self._nodes.pop(name)
        except KeyError:
            raise KeyError(name) from None

    def copy(self) -> "PipelineGraph":
        return PipelineGraph(self.nodes)

    def _providers(self) -> dict[str, NodeSpec]:
        providers: dict[str, NodeSpec] = {}
        duplicates: dict[str, list[str]] = {}
        for node in self._nodes.values():
            for value in node.provides:
                existing = providers.get(value)
                if existing is not None:
                    duplicates.setdefault(value, [existing.name]).append(node.name)
                else:
                    providers[value] = node
        if duplicates:
            details = "; ".join(
                f"{value!r}: {', '.join(names)}"
                for value, names in sorted(duplicates.items())
            )
            raise AmbiguousProviderError(f"ambiguous graph providers: {details}")
        return providers

    def plan(
        self,
        *,
        available: Iterable[str] = (),
        targets: Iterable[str] | None = None,
    ) -> tuple[NodeSpec, ...]:
        """Build a stable topological plan for all nodes or selected outputs."""

        available_values = _name_set(available, field_name="available")
        providers = self._providers()
        selected: set[str]

        if targets is None:
            selected = set(self._nodes)
        else:
            requested = _name_set(targets, field_name="targets")
            selected = set()

            def include_for(value: str, chain: tuple[str, ...]) -> None:
                provider = providers.get(value)
                if provider is None:
                    if value in available_values:
                        return
                    raise UnknownTargetError(
                        f"target or dependency {value!r} has no provider "
                        f"(resolution chain: {' -> '.join(chain + (value,))})"
                    )
                if provider.name in selected:
                    return
                selected.add(provider.name)
                for requirement in provider.requires:
                    # A registered provider takes precedence over an initial
                    # fallback value.  Removing that provider later naturally
                    # exposes the initial value on the next reusable run.
                    include_for(requirement, chain + (value,))

            for target in requested:
                include_for(target, ())

        dependencies: dict[str, set[str]] = {name: set() for name in selected}
        consumers: dict[str, set[str]] = {name: set() for name in selected}
        for node_name in selected:
            node = self._nodes[node_name]
            for requirement in node.requires:
                provider = providers.get(requirement)
                if provider is None:
                    if requirement not in available_values:
                        raise MissingDependencyError(
                            f"node {node.name!r} requires {requirement!r}, but no "
                            "node or initial context provides it"
                        )
                    continue
                if provider.name not in selected:
                    raise MissingDependencyError(
                        f"node {node.name!r} requires {requirement!r} from "
                        f"unselected node {provider.name!r}"
                    )
                dependencies[node.name].add(provider.name)
                consumers[provider.name].add(node.name)

        order_index = {name: index for index, name in enumerate(self._nodes)}
        ready = sorted(
            (name for name, deps in dependencies.items() if not deps),
            key=order_index.__getitem__,
        )
        ordered: list[NodeSpec] = []
        while ready:
            name = ready.pop(0)
            ordered.append(self._nodes[name])
            for consumer in sorted(consumers[name], key=order_index.__getitem__):
                dependencies[consumer].discard(name)
                if not dependencies[consumer] and all(
                    candidate.name != consumer for candidate in ordered
                ) and consumer not in ready:
                    ready.append(consumer)
            ready.sort(key=order_index.__getitem__)

        if len(ordered) != len(selected):
            cyclic = sorted(
                (name for name, deps in dependencies.items() if deps),
                key=order_index.__getitem__,
            )
            raise GraphCycleError(
                "graph contains a dependency cycle involving: " + ", ".join(cyclic)
            )
        return tuple(ordered)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise GraphStateError(f"refusing to replace symlinked graph metadata: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write a complete payload even on platforms that return short writes."""

    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - defensive OS failure path.
            raise OSError("short write while persisting graph metadata")
        view = view[written:]


class OutputDirectoryLock:
    """Kernel-backed process lock shared by legacy and Graph entry points.

    The lock file is deliberately persistent.  Ownership is carried by the
    open file descriptor (``flock``/``msvcrt.locking``), so a crashed process
    releases the lock in the kernel without any stale-file deletion race.
    The JSON stored in the file is diagnostic metadata only.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = 0.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("lock timeout must be non-negative and poll interval positive")
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.token = uuid.uuid4().hex
        self._owned = False
        self._handle: Any | None = None

    @staticmethod
    def _try_lock(handle: Any) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                raise
            return True
        if msvcrt is not None:  # pragma: no cover - exercised on Windows.
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            return True
        raise RuntimeError("no supported cross-process file locking API")

    @staticmethod
    def _unlock(handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:  # pragma: no cover - exercised on Windows.
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise GraphStateError(f"refusing symlinked output lock: {self.path}")
        deadline = time.monotonic() + self.timeout
        owner = {
            "schema_version": 1,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "token": self.token,
            "acquired_at": _utc_now(),
        }
        payload = (_canonical_json(_json_value(owner)) + "\n").encode("utf-8")
        while True:
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise GraphStateError(
                        f"refusing symlinked output lock: {self.path}"
                    ) from exc
                raise
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise GraphStateError(
                    f"output lock must be a regular file: {self.path}"
                )
            handle = os.fdopen(descriptor, "r+b")
            try:
                acquired = self._try_lock(handle)
            except Exception:
                handle.close()
                raise
            if not acquired:
                handle.close()
                if time.monotonic() >= deadline:
                    raise OutputDirectoryLockedError(
                        f"output directory is locked ({self.path})"
                    )
                time.sleep(
                    min(self.poll_interval, max(0.0, deadline - time.monotonic()))
                )
                continue
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                try:
                    self._unlock(handle)
                finally:
                    handle.close()
                raise
            self._handle = handle
            self._owned = True
            return

    def release(self) -> None:
        if not self._owned:
            return
        handle = self._handle
        try:
            if handle is not None:
                self._unlock(handle)
        finally:
            if handle is not None:
                handle.close()
            self._handle = None
            self._owned = False

    def __enter__(self) -> "OutputDirectoryLock":
        self.acquire()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


@dataclass(frozen=True)
class GraphRunResult:
    run_id: str
    plan: tuple[str, ...]
    executed: tuple[str, ...]
    skipped: tuple[str, ...]
    values: Mapping[str, Any]
    state_path: Path
    events_path: Path
    schema_version: int = 1


class GraphExecutor:
    """Execute a graph sequentially with resumable fingerprint checkpoints."""

    STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        graph: PipelineGraph,
        *,
        metadata_dir_name: str = ".pipeline_graph",
        lock_timeout: float = 0.0,
    ) -> None:
        if not metadata_dir_name or Path(metadata_dir_name).is_absolute():
            raise ValueError("metadata_dir_name must be a non-empty relative path")
        self.graph = graph
        self.metadata_dir_name = metadata_dir_name
        self.lock_timeout = lock_timeout

    @staticmethod
    def _load_state(path: Path) -> dict[str, Any]:
        if path.is_symlink():
            raise GraphStateError(f"refusing symlinked graph state: {path}")
        if not path.exists():
            return {"schema_version": GraphExecutor.STATE_SCHEMA_VERSION, "nodes": {}}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GraphStateError(f"cannot read graph state {path}: {exc}") from exc
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise GraphStateError(f"unsupported graph state schema in {path}")
        if not isinstance(state.get("nodes"), dict):
            raise GraphStateError(f"graph state has invalid nodes mapping in {path}")
        return state

    @staticmethod
    def _append_event(path: Path, event: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise GraphStateError(f"refusing symlinked graph event log: {path}")
        versioned_event = {**event, "schema_version": 1}
        payload = (_canonical_json(_json_value(versioned_event)) + "\n").encode(
            "utf-8"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise GraphStateError(
                    f"graph event log must be a regular file: {path}"
                )
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _value_fingerprint(context: GraphContext, name: str) -> str:
        explicit = context.fingerprints.get(name)
        if explicit is not None:
            return explicit
        return stable_fingerprint(context.values[name])

    @staticmethod
    def _safe_error_text(context: GraphContext, error: BaseException) -> str:
        value = str(error)
        for secret in context.redaction_values:
            value = value.replace(secret, "<redacted>")
        return value[:2000]

    @classmethod
    def _node_fingerprint(cls, node: NodeSpec, context: GraphContext) -> str:
        declared = node.fingerprint(context) if callable(node.fingerprint) else node.fingerprint
        requirements = {
            name: cls._value_fingerprint(context, name)
            for name in sorted(node.fingerprint_requires or ())
        }
        return stable_fingerprint(
            {
                "node": node.name,
                "version": node.version,
                "declared": declared,
                "requires": requirements,
            }
        )

    @staticmethod
    def _required_input_snapshot(
        node: NodeSpec,
        context: GraphContext,
    ) -> dict[str, tuple[str, str | None]]:
        """Capture both content and declared fingerprints for node inputs."""

        return {
            name: (
                stable_fingerprint(context.values[name]),
                context.fingerprints.get(name),
            )
            for name in sorted(node.requires)
        }

    @classmethod
    def _assert_required_inputs_unchanged(
        cls,
        node: NodeSpec,
        context: GraphContext,
        before: Mapping[str, tuple[str, str | None]],
    ) -> None:
        after = cls._required_input_snapshot(node, context)
        if after != before:
            changed = sorted(
                name
                for name in set(before) | set(after)
                if before.get(name) != after.get(name)
            )
            raise NodeContractError(
                f"node {node.name!r} mutated required inputs: {changed}"
            )

    @staticmethod
    def _cached_outputs(
        node: NodeSpec,
        record: Any,
        fingerprint: str,
        context: GraphContext,
    ) -> tuple[dict[str, JsonValue], dict[str, str]] | None:
        if not node.cache or not isinstance(record, dict):
            return None
        if record.get("cacheable") is not True:
            return None
        if record.get("fingerprint") != fingerprint:
            return None
        outputs = record.get("outputs")
        output_fingerprints = record.get("output_fingerprints")
        if not isinstance(outputs, dict) or set(outputs) != set(node.provides):
            return None
        if not isinstance(output_fingerprints, dict) or set(output_fingerprints) != set(
            node.provides
        ):
            return None
        if node.cache_validator is not None and not node.cache_validator(context, outputs):
            return None
        for name, value in outputs.items():
            validator = context.value_validators.get(name)
            if validator is not None:
                try:
                    if not validator(context, value):
                        return None
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    return None
        return outputs, output_fingerprints

    def execute(
        self,
        context: GraphContext,
        *,
        targets: Iterable[str] | None = None,
        force: bool | Iterable[str] = False,
    ) -> GraphRunResult:
        """Execute selected graph outputs and return a run summary.

        ``force=True`` reruns every selected node.  An iterable reruns only the
        named nodes; downstream nodes naturally invalidate when their required
        output fingerprints change.
        """

        context.output_dir.mkdir(parents=True, exist_ok=True)
        metadata_candidate = context.output_dir / self.metadata_dir_name
        if metadata_candidate.is_symlink():
            raise GraphStateError(
                f"refusing symlinked graph metadata directory: {metadata_candidate}"
            )
        metadata_candidate.mkdir(parents=True, exist_ok=True)
        metadata_dir = metadata_candidate.resolve()
        try:
            metadata_dir.relative_to(context.output_dir)
        except ValueError as exc:
            raise GraphStateError(
                f"graph metadata directory escapes output root: {metadata_dir}"
            ) from exc
        state_path = metadata_dir / "state.json"
        events_path = metadata_dir / "events.jsonl"
        lock_path = metadata_dir / "output.lock"
        run_id = uuid.uuid4().hex
        if isinstance(force, bool):
            forced = None if force else frozenset()
        else:
            forced = _name_set(force, field_name="force")
            unknown_forced = forced - {node.name for node in self.graph.nodes}
            if unknown_forced:
                raise KeyError(f"unknown forced nodes: {sorted(unknown_forced)}")

        with OutputDirectoryLock(lock_path, timeout=self.lock_timeout):
            # A Prepared graph is reusable, but its previous outputs must not
            # masquerade as external inputs after nodes are removed/replaced.
            context._reset_produced_values()
            state = self._load_state(state_path)
            plan = self.graph.plan(available=context.values, targets=targets)
            private_providers = {
                node.name: sorted(set(node.provides) & set(context.private_value_names))
                for node in plan
                if set(node.provides) & set(context.private_value_names)
            }
            if private_providers:
                raise NodeContractError(
                    "nodes cannot provide private initial values: "
                    + "; ".join(
                        f"{name}={values}"
                        for name, values in sorted(private_providers.items())
                    )
                )
            plan_names = tuple(node.name for node in plan)
            self._append_event(
                events_path,
                {
                    "event": "run_started",
                    "run_id": run_id,
                    "timestamp": _utc_now(),
                    "plan": list(plan_names),
                },
            )
            context.run_id = run_id
            executed: list[str] = []
            skipped: list[str] = []
            try:
                for node in plan:
                    context.node_name = node.name
                    required_snapshot = self._required_input_snapshot(node, context)
                    fingerprint = self._node_fingerprint(
                        node,
                        context._for_node(node),
                    )
                    self._assert_required_inputs_unchanged(
                        node,
                        context,
                        required_snapshot,
                    )
                    is_forced = forced is None or node.name in forced
                    cached = None if is_forced else self._cached_outputs(
                        node,
                        state["nodes"].get(node.name),
                        fingerprint,
                        context._for_node(node),
                    )
                    self._assert_required_inputs_unchanged(
                        node,
                        context,
                        required_snapshot,
                    )
                    if cached is not None:
                        outputs, output_fingerprints = cached
                        context.values.update(_isolated_copy(outputs))
                        context.fingerprints.update(output_fingerprints)
                        context._produced_value_names.update(node.provides)
                        skipped.append(node.name)
                        self._append_event(
                            events_path,
                            {
                                "event": "node_skipped",
                                "run_id": run_id,
                                "node": node.name,
                                "fingerprint": fingerprint,
                                "timestamp": _utc_now(),
                            },
                        )
                        continue

                    state["nodes"].pop(node.name, None)
                    _atomic_write_json(state_path, state)
                    started = time.monotonic()
                    self._append_event(
                        events_path,
                        {
                            "event": "node_started",
                            "run_id": run_id,
                            "node": node.name,
                            "fingerprint": fingerprint,
                            "resources": sorted(node.resources),
                            "timestamp": _utc_now(),
                        },
                    )
                    try:
                        invocation_context = context._for_node(node)
                        invocation_snapshot = self._required_input_snapshot(
                            node,
                            invocation_context,
                        )
                        result = node.handler(invocation_context)
                        self._assert_required_inputs_unchanged(
                            node,
                            invocation_context,
                            invocation_snapshot,
                        )
                        self._assert_required_inputs_unchanged(
                            node,
                            context,
                            required_snapshot,
                        )
                        if not isinstance(result, NodeResult):
                            raise NodeContractError(
                                f"node {node.name!r} must return NodeResult, got "
                                f"{type(result).__name__}"
                            )
                        if set(result.outputs) != set(node.provides):
                            missing = sorted(set(node.provides) - set(result.outputs))
                            extra = sorted(set(result.outputs) - set(node.provides))
                            raise NodeContractError(
                                f"node {node.name!r} output contract mismatch; "
                                f"missing={missing}, extra={extra}"
                            )
                        for name, value in result.outputs.items():
                            validator = context.value_validators.get(name)
                            if validator is not None and not validator(
                                context._for_node(node),
                                value,
                            ):
                                raise NodeContractError(
                                    f"node {node.name!r} produced invalid artifact {name!r}"
                                )
                    except Exception as exc:
                        try:
                            self._assert_required_inputs_unchanged(
                                node,
                                context,
                                required_snapshot,
                            )
                        except NodeContractError as mutation_error:
                            exc = mutation_error
                        safe_error = self._safe_error_text(context, exc)
                        self._append_event(
                            events_path,
                            {
                                "event": "node_failed",
                                "run_id": run_id,
                                "node": node.name,
                                "error_type": type(exc).__name__,
                                "error": safe_error,
                                "duration_seconds": round(time.monotonic() - started, 6),
                                "timestamp": _utc_now(),
                            },
                        )
                        if isinstance(exc, NodeContractError):
                            raise exc
                        raise NodeExecutionError(
                            node.name,
                            exc,
                            safe_message=safe_error,
                        ) from exc

                    outputs = _isolated_copy(dict(result.outputs))
                    output_fingerprints = {
                        name: result.fingerprints.get(
                            name, stable_fingerprint(outputs[name])
                        )
                        for name in node.provides
                    }
                    context.values.update(outputs)
                    context.fingerprints.update(output_fingerprints)
                    context._produced_value_names.update(node.provides)
                    state["nodes"][node.name] = {
                        "fingerprint": fingerprint,
                        "version": node.version,
                        "outputs": outputs,
                        "output_fingerprints": output_fingerprints,
                        "metadata": dict(result.metadata),
                        "cacheable": node.cache,
                        "completed_at": _utc_now(),
                    }
                    _atomic_write_json(state_path, state)
                    executed.append(node.name)
                    self._append_event(
                        events_path,
                        {
                            "event": "node_succeeded",
                            "run_id": run_id,
                            "node": node.name,
                            "fingerprint": fingerprint,
                            "duration_seconds": round(time.monotonic() - started, 6),
                            "timestamp": _utc_now(),
                        },
                    )

                self._append_event(
                    events_path,
                    {
                        "event": "run_succeeded",
                        "run_id": run_id,
                        "executed": executed,
                        "skipped": skipped,
                        "timestamp": _utc_now(),
                    },
                )
                return GraphRunResult(
                    run_id=run_id,
                    plan=plan_names,
                    executed=tuple(executed),
                    skipped=tuple(skipped),
                    values=MappingProxyType(
                        _isolated_copy({
                            name: value
                            for name, value in context.values.items()
                            if name not in context.private_value_names
                        })
                    ),
                    state_path=state_path,
                    events_path=events_path,
                )
            except Exception as exc:
                self._append_event(
                    events_path,
                    {
                        "event": "run_failed",
                        "run_id": run_id,
                        "error_type": type(exc).__name__,
                        "error": self._safe_error_text(context, exc),
                        "timestamp": _utc_now(),
                    },
                )
                raise
            finally:
                context.node_name = None
