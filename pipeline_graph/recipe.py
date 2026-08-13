"""Strict, low-code recipes and allowlisted graph-node plugins.

Recipes deliberately describe *selection*, not Python implementation.  Node
contracts and handlers live in :class:`~pipeline_graph.core.NodeSpec` objects
registered by trusted code.  This keeps a TOML file from becoming an indirect
``import`` or shell execution mechanism.

External plugins are installed Python distributions exposing the entry-point
group ``translation_agent.graph_nodes``.  Loading a plugin always requires an
explicit, caller-owned allowlist; a recipe's ``required_plugins`` field states
a dependency but never grants permission to import it.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from .core import DuplicateNodeError, NodeSpec, PipelineGraph, stable_fingerprint


SCHEMA_VERSION: Final = 1
ENTRY_POINT_GROUP: Final = "translation_agent.graph_nodes"
MAX_RECIPE_BYTES: Final = 64 * 1024

_RECIPE_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "targets",
        "enable",
        "disable",
        "required_plugins",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "import",
        "module",
        "callable",
        "command",
        "apikey",
        "token",
        "baseurl",
        "credentialenv",
        "password",
        "secret",
    }
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RecipeError(ValueError):
    """Base error for an invalid recipe or recipe selection."""


class RecipeSchemaError(RecipeError):
    """Raised when TOML does not conform to the strict recipe schema."""


class RecipeSecurityError(RecipeSchemaError):
    """Raised when a recipe attempts to describe executable or secret data."""


class PluginError(RuntimeError):
    """Base error for graph plugin discovery or registration."""


class PluginNotAllowedError(PluginError):
    """Raised before discovery when a recipe requests a non-allowlisted plugin."""


class PluginNotFoundError(PluginError):
    """Raised when an allowlisted required entry point is not installed."""


class PluginRegistrationError(PluginError):
    """Raised when an entry point cannot safely register its node contracts."""


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _find_sensitive_key(value: object, *, path: str = "recipe") -> str | None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = _normalized_key(key)
            if normalized in _SENSITIVE_KEYS or any(
                marker in normalized
                for marker in (
                    "apikey",
                    "credentialenv",
                    "baseurl",
                    "callable",
                    "command",
                    "password",
                    "secret",
                    "token",
                )
            ):
                return f"{path}.{key}"
            nested = _find_sensitive_key(item, path=f"{path}.{key}")
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for index, item in enumerate(value):
            nested = _find_sensitive_key(item, path=f"{path}[{index}]")
            if nested is not None:
                return nested
    return None


def _strict_string_list(
    payload: Mapping[str, Any],
    field_name: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    raw = payload.get(field_name)
    if raw is None:
        if required:
            raise RecipeSchemaError(f"recipe field {field_name!r} is required")
        return ()
    if not isinstance(raw, list):
        raise RecipeSchemaError(f"recipe field {field_name!r} must be a string array")
    values: list[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not _IDENTIFIER_PATTERN.fullmatch(item):
            raise RecipeSecurityError(
                f"recipe field {field_name!r}[{index}] must be a safe identifier; "
                "paths, import references, and callables are not allowed"
            )
        values.append(item)
    if len(set(values)) != len(values):
        raise RecipeSchemaError(f"recipe field {field_name!r} contains duplicates")
    if required and not values:
        raise RecipeSchemaError(f"recipe field {field_name!r} must not be empty")
    return tuple(values)


@dataclass(frozen=True)
class Recipe:
    """A validated graph selection with no executable configuration."""

    id: str
    targets: tuple[str, ...]
    enable: tuple[str, ...] = ()
    disable: tuple[str, ...] = ()
    required_plugins: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise RecipeSchemaError(
                f"unsupported recipe schema_version {self.schema_version!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        if not isinstance(self.id, str) or not _IDENTIFIER_PATTERN.fullmatch(self.id):
            raise RecipeSchemaError("recipe id must be a safe non-empty identifier")
        for field_name in ("targets", "enable", "disable", "required_plugins"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                raise RecipeSchemaError(f"recipe field {field_name!r} must be a tuple")
            if len(set(value)) != len(value):
                raise RecipeSchemaError(f"recipe field {field_name!r} contains duplicates")
            for item in value:
                if not isinstance(item, str) or not _IDENTIFIER_PATTERN.fullmatch(item):
                    raise RecipeSecurityError(
                        f"recipe field {field_name!r} contains an unsafe identifier"
                    )
        if not self.targets:
            raise RecipeSchemaError("recipe targets must not be empty")
        overlap = sorted(set(self.enable) & set(self.disable))
        if overlap:
            raise RecipeSchemaError(
                "recipe cannot both enable and disable nodes: " + ", ".join(overlap)
            )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "Recipe":
        if not isinstance(payload, Mapping):
            raise RecipeSchemaError("recipe root must be a TOML table")
        if any(not isinstance(key, str) for key in payload):
            raise RecipeSchemaError("recipe field names must be strings")
        sensitive = _find_sensitive_key(payload)
        if sensitive is not None:
            raise RecipeSecurityError(
                f"executable, endpoint, or credential field is forbidden: {sensitive}"
            )
        unknown = sorted(set(payload) - _RECIPE_FIELDS)
        if unknown:
            raise RecipeSchemaError("unknown recipe fields: " + ", ".join(unknown))
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
            raise RecipeSchemaError(
                f"unsupported recipe schema_version {schema_version!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        recipe_id = payload.get("id")
        if not isinstance(recipe_id, str) or not _IDENTIFIER_PATTERN.fullmatch(recipe_id):
            raise RecipeSchemaError("recipe id must be a safe non-empty identifier")
        return cls(
            id=recipe_id,
            targets=_strict_string_list(payload, "targets", required=True),
            enable=_strict_string_list(payload, "enable"),
            disable=_strict_string_list(payload, "disable"),
            required_plugins=_strict_string_list(payload, "required_plugins"),
            schema_version=schema_version,
        )


def parse_recipe(source: str | bytes | Mapping[str, Any]) -> Recipe:
    """Parse a TOML string/bytes or validate an already decoded mapping."""

    if isinstance(source, Mapping):
        return Recipe.from_mapping(source)
    if isinstance(source, str):
        encoded = source.encode("utf-8")
        if len(encoded) > MAX_RECIPE_BYTES:
            raise RecipeSchemaError("recipe exceeds the 64 KiB size limit")
        try:
            payload = tomllib.loads(source)
        except tomllib.TOMLDecodeError as exc:
            raise RecipeSchemaError(f"invalid recipe TOML: {exc}") from exc
    elif isinstance(source, bytes):
        if len(source) > MAX_RECIPE_BYTES:
            raise RecipeSchemaError("recipe exceeds the 64 KiB size limit")
        try:
            payload = tomllib.loads(source.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RecipeSchemaError(f"invalid recipe TOML: {exc}") from exc
    else:
        raise TypeError("recipe source must be TOML text, bytes, or a mapping")
    return Recipe.from_mapping(payload)


def load_recipe(path: str | Path) -> Recipe:
    """Load a bounded UTF-8 TOML recipe from disk."""

    recipe_path = Path(path).expanduser().resolve()
    try:
        size = recipe_path.stat().st_size
    except OSError as exc:
        raise RecipeSchemaError(f"cannot stat recipe {recipe_path}: {exc}") from exc
    if size > MAX_RECIPE_BYTES:
        raise RecipeSchemaError("recipe exceeds the 64 KiB size limit")
    try:
        raw = recipe_path.read_bytes()
    except OSError as exc:
        raise RecipeSchemaError(f"cannot read recipe {recipe_path}: {exc}") from exc
    return parse_recipe(raw)


@dataclass(frozen=True)
class _RegisteredNode:
    spec: NodeSpec
    origin: str
    enabled_by_default: bool


class _ExternalRegistrar:
    """Restricted registration surface handed to one external plugin."""

    def __init__(self, plugin_name: str) -> None:
        self.__plugin_name = plugin_name
        self.__specs: list[NodeSpec] = []

    @property
    def specs(self) -> tuple[NodeSpec, ...]:
        return tuple(self.__specs)

    def register(self, spec: NodeSpec, *, enabled: bool = False) -> None:
        if enabled:
            raise PluginRegistrationError(
                "external nodes cannot enable themselves; list the node in recipe.enable"
            )
        if not isinstance(spec, NodeSpec):
            raise PluginRegistrationError("plugin registrations must be NodeSpec objects")
        if spec.name.startswith("core."):
            raise PluginRegistrationError(
                f"external plugin {self.__plugin_name!r} cannot register or override "
                f"core node {spec.name!r}"
            )
        if any(existing.name == spec.name for existing in self.__specs):
            raise PluginRegistrationError(
                f"external plugin {self.__plugin_name!r} registered duplicate node "
                f"{spec.name!r}"
            )
        self.__specs.append(spec)


class NodeRegistry:
    """Trusted core-node registry plus explicitly allowlisted external plugins."""

    def __init__(self, nodes: Iterable[NodeSpec] = ()) -> None:
        self._entries: dict[str, _RegisteredNode] = {}
        self._loaded_plugins: set[str] = set()
        for spec in nodes:
            self.register_core(spec)

    @property
    def nodes(self) -> tuple[NodeSpec, ...]:
        return tuple(entry.spec for entry in self._entries.values())

    @property
    def loaded_plugins(self) -> frozenset[str]:
        return frozenset(self._loaded_plugins)

    @property
    def origins(self) -> Mapping[str, str]:
        return MappingProxyType(
            {name: entry.origin for name, entry in self._entries.items()}
        )

    def get(self, name: str) -> NodeSpec:
        try:
            return self._entries[name].spec
        except KeyError:
            raise KeyError(name) from None

    @staticmethod
    def _require_core_name(spec: NodeSpec) -> None:
        if not spec.name.startswith("core."):
            raise ValueError("trusted core node names must start with 'core.'")

    def register_core(
        self,
        spec: NodeSpec,
        *,
        enabled_by_default: bool = True,
    ) -> NodeSpec:
        """Register a trusted ``core.*`` contract without implicit replacement."""

        if not isinstance(spec, NodeSpec):
            raise TypeError("registered node must be a NodeSpec")
        self._require_core_name(spec)
        if spec.name in self._entries:
            raise DuplicateNodeError(f"duplicate node name: {spec.name!r}")
        self._entries[spec.name] = _RegisteredNode(
            spec=spec,
            origin="core",
            enabled_by_default=bool(enabled_by_default),
        )
        return spec

    # Short aliases make the common trusted-code API pleasant without exposing
    # replacement semantics to plugin registrars.
    register = register_core

    def replace_core(self, spec: NodeSpec) -> NodeSpec:
        """Replace one existing core node while preserving its default state/order."""

        if not isinstance(spec, NodeSpec):
            raise TypeError("replacement node must be a NodeSpec")
        self._require_core_name(spec)
        existing = self._entries.get(spec.name)
        if existing is None:
            raise KeyError(spec.name)
        if existing.origin != "core":
            raise PluginRegistrationError("only a registered core node may be replaced")
        self._entries[spec.name] = _RegisteredNode(
            spec=spec,
            origin="core",
            enabled_by_default=existing.enabled_by_default,
        )
        return spec

    replace = replace_core

    def _register_external(
        self,
        spec: NodeSpec,
        *,
        plugin_name: str,
        provenance: str,
    ) -> None:
        if not isinstance(spec, NodeSpec):
            raise PluginRegistrationError("plugin registrations must be NodeSpec objects")
        if spec.name.startswith("core."):
            raise PluginRegistrationError(
                f"external plugin {plugin_name!r} cannot register or override core node "
                f"{spec.name!r}"
            )
        if "pipeline.argv" in spec.provides:
            raise PluginRegistrationError(
                f"external plugin {plugin_name!r} cannot provide reserved private "
                "artifact 'pipeline.argv'"
            )
        if spec.name in self._entries:
            raise PluginRegistrationError(
                f"external plugin {plugin_name!r} registered duplicate node {spec.name!r}"
            )
        # Include installed entry-point provenance in the node identity so a
        # different distribution/version cannot inherit cached outputs merely
        # by reusing the same node name and declared version.
        bound_spec = replace(
            spec,
            version=f"{spec.version}|plugin={provenance}",
        )
        self._entries[spec.name] = _RegisteredNode(
            spec=bound_spec,
            origin=f"plugin:{plugin_name}",
            enabled_by_default=False,
        )

    @staticmethod
    def _entry_points() -> tuple[Any, ...]:
        discovered = importlib_metadata.entry_points()
        if hasattr(discovered, "select"):
            return tuple(discovered.select(group=ENTRY_POINT_GROUP))
        if isinstance(discovered, Mapping):  # pragma: no cover - old Python API
            return tuple(discovered.get(ENTRY_POINT_GROUP, ()))
        return tuple(
            item
            for item in discovered
            if getattr(item, "group", None) == ENTRY_POINT_GROUP
        )

    def load_plugins(
        self,
        required_plugins: Iterable[str],
        *,
        allowlist: Iterable[str],
    ) -> tuple[str, ...]:
        """Load required entry points after checking a separate explicit allowlist.

        Registration is transactional per plugin.  A broken plugin cannot leave
        a half-registered graph behind.
        """

        required = _safe_identifier_iterable(required_plugins, "required_plugins")
        allowed = frozenset(_safe_identifier_iterable(allowlist, "plugin allowlist"))
        denied = sorted(set(required) - allowed)
        if denied:
            raise PluginNotAllowedError(
                "plugins are not explicitly allowlisted: " + ", ".join(denied)
            )
        pending = [name for name in required if name not in self._loaded_plugins]
        if not pending:
            return ()

        by_name: dict[str, list[Any]] = {}
        for entry_point in self._entry_points():
            by_name.setdefault(str(entry_point.name), []).append(entry_point)
        missing = sorted(name for name in pending if name not in by_name)
        if missing:
            raise PluginNotFoundError(
                "allowlisted graph plugins are not installed: " + ", ".join(missing)
            )
        ambiguous = sorted(name for name in pending if len(by_name[name]) != 1)
        if ambiguous:
            raise PluginRegistrationError(
                "multiple graph entry points use the same name: " + ", ".join(ambiguous)
            )

        loaded_names: list[str] = []
        for name in pending:
            entries_before = dict(self._entries)
            plugins_before = set(self._loaded_plugins)
            registrar = _ExternalRegistrar(name)
            try:
                entry_point = by_name[name][0]
                provider = entry_point.load()
                if isinstance(provider, NodeSpec):
                    registrar.register(provider)
                elif callable(provider):
                    returned = provider(registrar)
                    if returned is not None:
                        self._register_plugin_result(returned, registrar)
                else:
                    self._register_plugin_result(provider, registrar)
                if not registrar.specs:
                    raise PluginRegistrationError(
                        f"graph plugin {name!r} did not register any nodes"
                    )
                distribution = getattr(entry_point, "dist", None)
                distribution_name = str(
                    getattr(distribution, "name", "unknown-distribution")
                )
                distribution_version = str(
                    getattr(distribution, "version", "unknown-version")
                )
                provenance = stable_fingerprint(
                    {
                        "entry_point": name,
                        "value": str(getattr(entry_point, "value", "")),
                        "distribution": distribution_name,
                        "version": distribution_version,
                    }
                )[:20]
                for spec in registrar.specs:
                    self._register_external(
                        spec,
                        plugin_name=name,
                        provenance=provenance,
                    )
                self._loaded_plugins.add(name)
                loaded_names.append(name)
            except BaseException as exc:
                self._entries = entries_before
                self._loaded_plugins = plugins_before
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, PluginError):
                    raise
                raise PluginRegistrationError(
                    f"failed to register graph plugin {name!r}: {exc}"
                ) from exc
        return tuple(loaded_names)

    @staticmethod
    def _register_plugin_result(result: object, registrar: _ExternalRegistrar) -> None:
        if isinstance(result, NodeSpec):
            registrar.register(result)
            return
        if isinstance(result, (str, bytes, Mapping)) or not isinstance(result, Iterable):
            raise PluginRegistrationError(
                "plugin entry point must register nodes or return NodeSpec objects"
            )
        for spec in result:
            registrar.register(spec)

    def build_graph(self, recipe: Recipe) -> PipelineGraph:
        """Select default core nodes and explicitly enabled plugin nodes."""

        if not isinstance(recipe, Recipe):
            raise TypeError("recipe must be a Recipe")
        missing_plugins = sorted(
            set(recipe.required_plugins) - self._loaded_plugins
        )
        if missing_plugins:
            raise PluginNotAllowedError(
                "recipe plugins have not been explicitly loaded: "
                + ", ".join(missing_plugins)
            )
        referenced = set(recipe.enable) | set(recipe.disable)
        unknown = sorted(referenced - set(self._entries))
        if unknown:
            raise RecipeSchemaError("recipe refers to unknown nodes: " + ", ".join(unknown))
        undeclared_plugins = sorted(
            {
                entry.origin.removeprefix("plugin:")
                for name, entry in self._entries.items()
                if name in recipe.enable
                and entry.origin.startswith("plugin:")
                and entry.origin.removeprefix("plugin:")
                not in recipe.required_plugins
            }
        )
        if undeclared_plugins:
            raise RecipeSchemaError(
                "enabled external nodes require declarations in required_plugins: "
                + ", ".join(undeclared_plugins)
            )
        enabled = {
            name
            for name, entry in self._entries.items()
            if entry.enabled_by_default
        }
        enabled.update(recipe.enable)
        enabled.difference_update(recipe.disable)
        return PipelineGraph(
            entry.spec
            for name, entry in self._entries.items()
            if name in enabled
        )

    def prepare_graph(
        self,
        recipe: Recipe,
        *,
        plugin_allowlist: Iterable[str] = (),
    ) -> PipelineGraph:
        """Allowlist-load recipe dependencies, then build its selected graph."""

        self.load_plugins(
            recipe.required_plugins,
            allowlist=plugin_allowlist,
        )
        return self.build_graph(recipe)


def _safe_identifier_iterable(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{field_name} must be an iterable of identifiers, not a string")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
            raise RecipeSecurityError(f"{field_name} contains an unsafe identifier")
        result.append(value)
    if len(set(result)) != len(result):
        raise RecipeSchemaError(f"{field_name} contains duplicates")
    return tuple(result)


__all__ = [
    "ENTRY_POINT_GROUP",
    "MAX_RECIPE_BYTES",
    "SCHEMA_VERSION",
    "NodeRegistry",
    "PluginError",
    "PluginNotAllowedError",
    "PluginNotFoundError",
    "PluginRegistrationError",
    "Recipe",
    "RecipeError",
    "RecipeSchemaError",
    "RecipeSecurityError",
    "load_recipe",
    "parse_recipe",
]
