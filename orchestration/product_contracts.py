"""Stable, JSON-safe contracts shared by CLI, API, Graph and Web UI.

These objects intentionally contain configuration and provenance only.  Raw
credentials are never part of a run specification, event or artifact record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Mapping


APP_VERSION = "0.1.0"
CONTRACT_SCHEMA_VERSION = 1
SOURCE_MODES = frozenset({"scanned-pdf", "text-pdf", "epub"})
ARTIFACT_STATUSES = frozenset({"draft", "released", "blocked"})
EVENT_LEVELS = frozenset({"debug", "info", "warning", "error"})


class ContractError(ValueError):
    """Raised when a public product contract is malformed or unsupported."""


def _strict_fields(
    payload: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    contract: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ContractError(f"{contract} has unknown fields: {unknown}")
    if payload.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        raise ContractError(
            f"{contract} requires schema_version={CONTRACT_SCHEMA_VERSION}"
        )


def _path_text(value: Path | str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _secret_field(name: str) -> bool:
    normalized = name.strip().casefold().replace("-", "_")
    if normalized.endswith("_env"):
        return False
    return normalized in {
        "api_key",
        "apikey",
        "password",
        "passwd",
        "secret",
        "access_token",
        "refresh_token",
        "auth_token",
        "bearer_token",
    } or normalized.endswith(
        ("_api_key", "_password", "_secret", "_access_token", "_refresh_token")
    )


def _json_contract_value(value: Any, *, location: str) -> Any:
    """Return detached JSON data and reject credential-shaped fields."""

    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"{location} must not contain NaN or infinity")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{location} keys must be strings")
            if _secret_field(key) and item is not None and item != "":
                raise ContractError(
                    f"{location}.{key} must not contain a credential value"
                )
            result[key] = _json_contract_value(
                item,
                location=f"{location}.{key}",
            )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _json_contract_value(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ContractError(
        f"{location} must contain JSON values, got {type(value).__name__}"
    )


@dataclass(frozen=True)
class RunSpec:
    """Versioned source-to-publication request without any credential values."""

    schema_version: int = CONTRACT_SCHEMA_VERSION
    source: Path | str | None = None
    source_mode: str = "scanned-pdf"
    output_dir: Path | str = "outputs/book"
    phase: str = "all"
    title: str | None = None
    author: str | None = None
    target_language: str = "简体中文"
    config: Path | str | None = None
    recipe: Path | str | None = None
    targets: tuple[str, ...] = ()
    translate: bool = True
    verify: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "source",
            "source_mode",
            "output_dir",
            "phase",
            "title",
            "author",
            "target_language",
            "config",
            "recipe",
            "targets",
            "translate",
            "verify",
            "options",
        }
    )

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ContractError(
                f"RunSpec requires schema_version={CONTRACT_SCHEMA_VERSION}"
            )
        if self.source_mode not in SOURCE_MODES:
            raise ContractError(
                f"unsupported source_mode {self.source_mode!r}; "
                f"expected one of {sorted(SOURCE_MODES)}"
            )
        if not str(self.output_dir).strip():
            raise ContractError("RunSpec.output_dir must not be empty")
        if not self.phase.strip():
            raise ContractError("RunSpec.phase must not be empty")
        if not self.target_language.strip():
            raise ContractError("RunSpec.target_language must not be empty")
        if not all(isinstance(item, str) and item for item in self.targets):
            raise ContractError("RunSpec.targets must contain non-empty strings")
        if not isinstance(self.options, Mapping):
            raise ContractError("RunSpec.options must be a mapping")
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(
            self,
            "options",
            MappingProxyType(
                _json_contract_value(self.options, location="RunSpec.options")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": _path_text(self.source),
            "source_mode": self.source_mode,
            "output_dir": str(self.output_dir),
            "phase": self.phase,
            "title": self.title,
            "author": self.author,
            "target_language": self.target_language,
            "config": _path_text(self.config),
            "recipe": _path_text(self.recipe),
            "targets": list(self.targets),
            "translate": self.translate,
            "verify": self.verify,
            "options": _json_contract_value(
                self.options,
                location="RunSpec.options",
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunSpec":
        _strict_fields(payload, allowed=cls.FIELDS, contract="RunSpec")
        targets = payload.get("targets", ())
        if not isinstance(targets, (list, tuple)):
            raise ContractError("RunSpec.targets must be an array")
        options = payload.get("options", {})
        if not isinstance(options, Mapping):
            raise ContractError("RunSpec.options must be an object")
        return cls(
            schema_version=int(payload["schema_version"]),
            source=payload.get("source"),
            source_mode=str(payload.get("source_mode") or ""),
            output_dir=str(payload.get("output_dir") or ""),
            phase=str(payload.get("phase") or ""),
            title=(str(payload["title"]) if payload.get("title") is not None else None),
            author=(
                str(payload["author"]) if payload.get("author") is not None else None
            ),
            target_language=str(payload.get("target_language") or ""),
            config=payload.get("config"),
            recipe=payload.get("recipe"),
            targets=tuple(str(item) for item in targets),
            translate=bool(payload.get("translate", True)),
            verify=bool(payload.get("verify", True)),
            options=dict(options),
        )


@dataclass(frozen=True)
class ArtifactRecord:
    schema_version: int
    name: str
    kind: str
    path: Path | str
    status: str
    sha256: str | None = None
    media_type: str | None = None
    report_path: Path | str | None = None

    FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "schema_version",
            "name",
            "kind",
            "path",
            "status",
            "sha256",
            "media_type",
            "report_path",
        }
    )

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ContractError("unsupported ArtifactRecord schema")
        if not self.name or not self.kind or not str(self.path):
            raise ContractError("ArtifactRecord name, kind and path are required")
        if self.status not in ARTIFACT_STATUSES:
            raise ContractError(f"unsupported artifact status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "kind": self.kind,
            "path": str(self.path),
            "status": self.status,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "report_path": _path_text(self.report_path),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactRecord":
        _strict_fields(payload, allowed=cls.FIELDS, contract="ArtifactRecord")
        return cls(
            schema_version=int(payload["schema_version"]),
            name=str(payload.get("name") or ""),
            kind=str(payload.get("kind") or ""),
            path=str(payload.get("path") or ""),
            status=str(payload.get("status") or ""),
            sha256=(str(payload["sha256"]) if payload.get("sha256") else None),
            media_type=(
                str(payload["media_type"]) if payload.get("media_type") else None
            ),
            report_path=payload.get("report_path"),
        )


@dataclass(frozen=True)
class RunEvent:
    schema_version: int
    run_id: str
    event: str
    timestamp: str
    level: str = "info"
    node: str | None = None
    message: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ContractError("unsupported RunEvent schema")
        if not self.run_id or not self.event or not self.timestamp:
            raise ContractError("RunEvent run_id, event and timestamp are required")
        if self.level not in EVENT_LEVELS:
            raise ContractError(f"unsupported event level: {self.level!r}")
        if not isinstance(self.data, Mapping):
            raise ContractError("RunEvent.data must be a mapping")
        object.__setattr__(
            self,
            "data",
            MappingProxyType(
                _json_contract_value(self.data, location="RunEvent.data")
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "event": self.event,
            "timestamp": self.timestamp,
            "level": self.level,
            "node": self.node,
            "message": self.message,
            "data": _json_contract_value(self.data, location="RunEvent.data"),
        }
