from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_SCHEMA_VERSION = 1
TOP_LEVEL_FIELDS = frozenset({"schema_version", "profiles", "pipeline"})
PROFILE_FIELDS = frozenset(
    {
        "adapter",
        "provider",
        "base_url",
        "model",
        "credential_env",
        "timeout",
        "concurrency",
        "thinking",
        "command",
        "reading_direction",
    }
)
PIPELINE_FIELDS = frozenset(
    {
        "ocr_profile",
        "toc_profile",
        "translation_profile",
        "proofread_profile",
    }
)


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
    thinking: str = "disabled"

    @property
    def fingerprint(self) -> str:
        payload = {
            "provider": self.provider.strip().lower(),
            "adapter": self.adapter.strip().lower(),
            "base_url": self.base_url.rstrip("/"),
            "model": self.model.strip(),
            "target_language": self.target_language.strip(),
            "prompt_version": self.prompt_version.strip(),
            "thinking": self.thinking.strip().lower(),
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

    def resolve_credential(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool = True,
    ) -> SecretValue:
        source = os.environ if environ is None else environ
        value = str(source.get(self.credential_env, "") if self.credential_env else "").strip()
        if required and not value:
            raise ValueError(
                f"Profile {self.name!r} requires credential environment variable "
                f"{self.credential_env!r}."
            )
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
            thinking=self.thinking,
        )


@dataclass(frozen=True)
class PipelineProfiles:
    profiles: Mapping[str, ModelProfile]
    ocr_profile: str = ""
    toc_profile: str = ""
    translation_profile: str = ""
    proofread_profile: str = ""
    schema_version: int = PROFILE_SCHEMA_VERSION

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
    unknown_top_level = sorted(set(payload) - TOP_LEVEL_FIELDS)
    if unknown_top_level:
        raise ValueError(
            f"Profile config has unknown top-level fields: {unknown_top_level}"
        )
    schema_version = int(payload.get("schema_version", PROFILE_SCHEMA_VERSION))
    if schema_version != PROFILE_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported Profile schema_version={schema_version}; "
            f"expected {PROFILE_SCHEMA_VERSION}."
        )
    raw_profiles = payload.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("Profile config [profiles] must be a TOML table.")
    profiles: dict[str, ModelProfile] = {}
    for name, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Profile {name!r} must be a TOML table.")
        unknown_profile_fields = sorted(set(raw) - PROFILE_FIELDS)
        if unknown_profile_fields:
            raise ValueError(
                f"Profile {name!r} has unknown fields: {unknown_profile_fields}"
            )
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
        )
    pipeline = payload.get("pipeline", {})
    if not isinstance(pipeline, dict):
        raise ValueError("Profile config [pipeline] must be a TOML table.")
    unknown_pipeline_fields = sorted(set(pipeline) - PIPELINE_FIELDS)
    if unknown_pipeline_fields:
        raise ValueError(
            f"Profile config [pipeline] has unknown fields: "
            f"{unknown_pipeline_fields}"
        )
    result = PipelineProfiles(
        profiles=profiles,
        ocr_profile=str(pipeline.get("ocr_profile") or "").strip(),
        toc_profile=str(pipeline.get("toc_profile") or "").strip(),
        translation_profile=str(pipeline.get("translation_profile") or "").strip(),
        proofread_profile=str(pipeline.get("proofread_profile") or "").strip(),
        schema_version=schema_version,
    )
    for stage in ("ocr", "toc", "translation", "proofread"):
        result.for_stage(stage)
    return result
