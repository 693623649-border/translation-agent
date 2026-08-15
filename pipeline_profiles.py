from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOML_INT_MIN = -(2**63)
_TOML_INT_MAX = 2**63 - 1
_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api_token",
        "auth_header",
        "authorization",
        "bearer_token",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "token",
        "access_key",
        "access_token",
    }
)
_SECRET_FIELD_SUFFIXES = tuple(f"_{name}" for name in _SECRET_FIELD_NAMES)


def _normalise_field_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _looks_like_secret_field(value: str) -> bool:
    """Return whether a config key is likely to contain credential material.

    The check deliberately does not reject ordinary inference settings such as
    ``max_tokens`` or ``tokenizer``.  Credentials belong in ``credential_env``;
    allowing them in the serialisable settings maps would leak them into cache
    fingerprints, logs, and graph reports.
    """

    normalised = _normalise_field_name(value)
    return normalised in _SECRET_FIELD_NAMES or normalised.endswith(
        _SECRET_FIELD_SUFFIXES
    )


def _freeze_config_value(value: object, *, path: str) -> object:
    """Validate and deeply freeze a JSON/TOML-safe configuration value."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if not _TOML_INT_MIN <= value <= _TOML_INT_MAX:
            raise ValueError(f"{path} integer is outside TOML's signed 64-bit range.")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity.")
        return value
    if isinstance(value, MappingABC):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings, got {type(key).__name__}.")
            if _looks_like_secret_field(key):
                raise ValueError(
                    f"{path}.{key} looks like a secret field; put only its environment "
                    "variable name in credential_env."
                )
            frozen[key] = _freeze_config_value(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_config_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise ValueError(
        f"{path} contains unsupported {type(value).__name__}; only nested mappings, "
        "arrays, strings, booleans, finite numbers, and null are allowed."
    )


def _freeze_config_mapping(value: object, *, path: str) -> Mapping[str, object]:
    if not isinstance(value, MappingABC):
        raise ValueError(f"{path} must be a mapping/TOML table.")
    frozen = _freeze_config_value(value, path=path)
    assert isinstance(frozen, MappingABC)
    return frozen


def _plain_config_value(value: object) -> object:
    """Convert a validated frozen value into canonical JSON-compatible data."""

    if isinstance(value, MappingABC):
        return {
            key: _plain_config_value(value[key])
            for key in sorted(value)
        }
    if isinstance(value, tuple):
        return [_plain_config_value(item) for item in value]
    return value


def _config_fingerprint(value: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _plain_config_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SecretValue:
    """A resolved credential whose repr/str never exposes the secret."""

    _value: str = field(repr=False)

    def get_secret_value(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "<redacted>"

    def __repr__(self) -> str:
        return "SecretValue(<redacted>)"


@dataclass(frozen=True)
class ModelIdentity:
    provider: str
    adapter: str
    base_url: str
    model: str
    target_language: str
    prompt_version: str

    @property
    def fingerprint(self) -> str:
        payload = {
            "provider": self.provider.strip().lower(),
            "adapter": self.adapter.strip().lower(),
            "base_url": self.base_url.rstrip("/"),
            "model": self.model.strip(),
            "target_language": self.target_language.strip(),
            "prompt_version": self.prompt_version.strip(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ModelProfile:
    name: str
    adapter: str
    provider: str
    model: str
    credential_env: str = ""
    base_url: str = ""
    timeout: int = 120
    concurrency: int = 1
    thinking: str = "disabled"
    command: tuple[str, ...] = ()
    reading_direction: str = ""
    # MappingProxyType is deliberately unhashable.  Excluding these maps from
    # the generated dataclass hash preserves ModelProfile's historic hashable
    # behaviour; their explicit fingerprints are used where settings identity
    # matters.
    content: Mapping[str, object] = field(default_factory=dict, hash=False)
    runtime: Mapping[str, object] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        if self.credential_env and not ENV_NAME_PATTERN.fullmatch(self.credential_env):
            raise ValueError(
                f"Profile {self.name!r} credential_env must be an environment "
                "variable name, not a raw credential."
            )
        if self.reading_direction not in {"", "horizontal", "vertical"}:
            raise ValueError(
                f"Profile {self.name!r} reading_direction must be horizontal or vertical."
            )
        object.__setattr__(
            self,
            "content",
            _freeze_config_mapping(self.content, path=f"profiles.{self.name}.content"),
        )
        object.__setattr__(
            self,
            "runtime",
            _freeze_config_mapping(self.runtime, path=f"profiles.{self.name}.runtime"),
        )

    @property
    def content_fingerprint(self) -> str:
        """Stable identity for settings that can change OCR output semantics."""

        return _config_fingerprint(self.content)

    @property
    def runtime_fingerprint(self) -> str:
        """Stable identity for deployment settings, useful for observability only."""

        return _config_fingerprint(self.runtime)

    def resolve_credential(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool = True,
    ) -> SecretValue:
        source = os.environ if environ is None else environ
        value = str(source.get(self.credential_env, "") if self.credential_env else "").strip()
        credential_free_local = (
            self.provider.strip().lower() == "local"
            and self.adapter.strip().lower() == "paddleocr-local"
        )
        if required and not value and not credential_free_local:
            if self.credential_env:
                detail = f"environment variable {self.credential_env!r}"
            else:
                detail = "a credential_env setting"
            raise ValueError(f"Profile {self.name!r} requires {detail}.")
        return SecretValue(value)

    def identity(
        self,
        *,
        target_language: str,
        prompt_version: str,
    ) -> ModelIdentity:
        return ModelIdentity(
            provider=self.provider,
            adapter=self.adapter,
            base_url=self.base_url,
            model=self.model,
            target_language=target_language,
            prompt_version=prompt_version,
        )


@dataclass(frozen=True)
class PipelineProfiles:
    profiles: Mapping[str, ModelProfile]
    ocr_profile: str = ""
    toc_profile: str = ""
    translation_profile: str = ""
    proofread_profile: str = ""

    def get(self, name: str) -> ModelProfile:
        try:
            return self.profiles[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.profiles)) or "<none>"
            raise ValueError(
                f"Unknown model profile {name!r}; available profiles: {available}"
            ) from exc

    def for_stage(self, stage: str, override: str | None = None) -> ModelProfile | None:
        selected = override or {
            "ocr": self.ocr_profile,
            "toc": self.toc_profile,
            "translation": self.translation_profile,
            # OCR proofreading is an optional text-model stage.  Reusing the
            # translation profile keeps old profile files useful and avoids a
            # second credential setting for the common DeepSeek setup.
            "proofread": self.proofread_profile or self.translation_profile,
        }.get(stage, "")
        return self.get(selected) if selected else None


def load_pipeline_profiles(path: str | Path) -> PipelineProfiles:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle)
    raw_profiles = payload.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("Profile config [profiles] must be a TOML table.")
    profiles: dict[str, ModelProfile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Profile {name!r} must be a TOML table.")
        adapter = str(raw.get("adapter") or "").strip()
        provider = str(raw.get("provider") or "").strip()
        model = str(raw.get("model") or "").strip()
        if not adapter or not provider or not model:
            raise ValueError(
                f"Profile {name!r} requires adapter, provider, and model."
            )
        command_value = raw.get("command", ())
        if isinstance(command_value, str):
            command = (command_value,)
        elif isinstance(command_value, (list, tuple)) and all(
            isinstance(item, str) for item in command_value
        ):
            command = tuple(command_value)
        else:
            raise ValueError(f"Profile {name!r} command must be a string array.")
        timeout = int(raw.get("timeout", 120))
        concurrency = int(raw.get("concurrency", 1))
        if timeout < 1 or concurrency < 1:
            raise ValueError(
                f"Profile {name!r} timeout and concurrency must be positive."
            )
        thinking = str(raw.get("thinking", "disabled")).strip().lower()
        if thinking not in {"enabled", "disabled", "omit"}:
            raise ValueError(
                f"Profile {name!r} thinking must be enabled, disabled, or omit."
            )
        profiles[name] = ModelProfile(
            name=name,
            adapter=adapter,
            provider=provider,
            base_url=str(raw.get("base_url") or "").strip(),
            model=model,
            credential_env=str(raw.get("credential_env") or "").strip(),
            timeout=timeout,
            concurrency=concurrency,
            thinking=thinking,
            command=command,
            reading_direction=str(raw.get("reading_direction") or "").strip().lower(),
            content=raw.get("content", {}),
            runtime=raw.get("runtime", {}),
        )
    pipeline = payload.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ValueError("Profile config [pipeline] must be a TOML table.")
    result = PipelineProfiles(
        profiles=profiles,
        ocr_profile=str(pipeline.get("ocr_profile") or "").strip(),
        toc_profile=str(pipeline.get("toc_profile") or "").strip(),
        translation_profile=str(pipeline.get("translation_profile") or "").strip(),
        proofread_profile=str(pipeline.get("proofread_profile") or "").strip(),
    )
    for stage in ("ocr", "toc", "translation", "proofread"):
        result.for_stage(stage)
    return result
