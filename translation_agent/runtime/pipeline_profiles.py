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
        "local",
    }
)
LOCAL_LLAMA_ADAPTER = "llama-cpp-local"
# Prompt contract used by the PDF page path for the local translation model
# (Hy-MT2 official default instruction).  Bump when that prompt changes.
LOCAL_PAGE_PROMPT_STYLE = "hy-mt-default-v1"
LOCAL_FIELDS = frozenset(
    {
        "server_binary",
        "server_sha256",
        "model_file",
        "model_sha256",
        "context_size",
        "max_output_tokens",
        "max_input_chars",
        "gpu_layers",
        "threads",
        "port",
        "temperature",
        "top_p",
        "top_k",
        "repeat_penalty",
        "critical_pressure_seconds",
    }
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PIPELINE_FIELDS = frozenset(
    {
        "ocr_profile",
        "toc_profile",
        "translation_profile",
        "proofread_profile",
    }
)
# ``thinking`` speaks two provider dialects.  ``enabled``/``disabled``/``omit``
# keep the original ``thinking.type`` payload, while ``low``/``high``/``max``
# select an OpenAI-style ``reasoning_effort`` level.  DeepSeek ``deepseek-flash``
# and ``deepseek-v4-pro`` expose exactly these three reasoning levels.
REASONING_EFFORT_VALUES = ("low", "high", "max")
THINKING_VALUES = ("enabled", "disabled", "omit") + REASONING_EFFORT_VALUES


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
class LocalModelSettings:
    """Pinned llama.cpp runtime/model and bounded generation for local translation."""

    server_binary: Path
    server_sha256: str
    model_file: Path
    model_sha256: str
    context_size: int = 4096
    max_output_tokens: int = 2048
    max_input_chars: int = 1500
    gpu_layers: int = 99
    threads: int = 4
    port: int = 0
    # Hy-MT2 1.8B official sampling recommendation.
    temperature: float = 0.7
    top_p: float = 0.6
    top_k: int = 20
    repeat_penalty: float = 1.05
    critical_pressure_seconds: int = 5

    def __post_init__(self) -> None:
        for name in ("server_sha256", "model_sha256"):
            if not SHA256_PATTERN.fullmatch(getattr(self, name)):
                raise ValueError(f"local.{name} must be a lowercase SHA-256 hex digest")
        if not 1024 <= self.context_size <= 8192:
            raise ValueError("local.context_size must be between 1024 and 8192")
        if not 64 <= self.max_output_tokens < self.context_size or self.max_output_tokens > 4096:
            raise ValueError("local.max_output_tokens must be 64-4096 and below context_size")
        if not 200 <= self.max_input_chars <= self.context_size:
            raise ValueError("local.max_input_chars must be between 200 and context_size")
        if not 0 <= self.gpu_layers <= 999 or not 1 <= self.threads <= 8:
            raise ValueError("local.gpu_layers must be 0-999 and local.threads 1-8")
        if self.port and not 1024 <= self.port <= 65535:
            raise ValueError("local.port must be 0 (pick a free port) or 1024-65535")
        if not 0 <= self.temperature <= 2 or not 0 < self.top_p <= 1 or self.top_k < 0:
            raise ValueError("local sampling values are out of range")
        if self.critical_pressure_seconds < 1:
            raise ValueError("local.critical_pressure_seconds must be positive")

    def semantics(self) -> dict[str, object]:
        """Cache-relevant identity: pinned hashes and generation settings."""

        return {
            "server_sha256": self.server_sha256,
            "model_sha256": self.model_sha256,
            "context_size": self.context_size,
            "max_output_tokens": self.max_output_tokens,
            "max_input_chars": self.max_input_chars,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "repeat_penalty": self.repeat_penalty,
        }


def _local_settings(name: str, raw: object) -> LocalModelSettings:
    if not isinstance(raw, dict):
        raise ValueError(f"Profile {name!r} adapter {LOCAL_LLAMA_ADAPTER!r} requires a [local] table.")
    unknown = sorted(set(raw) - LOCAL_FIELDS)
    if unknown:
        raise ValueError(f"Profile {name!r} [local] has unknown fields: {unknown}")
    missing = sorted({"server_binary", "server_sha256", "model_file", "model_sha256"} - set(raw))
    if missing:
        raise ValueError(f"Profile {name!r} [local] requires: {missing}")
    values = dict(raw)
    for key in ("server_binary", "model_file"):
        values[key] = Path(str(values[key])).expanduser()
    try:
        return LocalModelSettings(**values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Profile {name!r} [local]: {exc}") from exc


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
    local: LocalModelSettings | None = None

    def __post_init__(self) -> None:
        if (self.adapter == LOCAL_LLAMA_ADAPTER) != (self.local is not None):
            raise ValueError(
                f"Profile {self.name!r}: a [local] table is required for and only "
                f"valid with adapter={LOCAL_LLAMA_ADAPTER!r}."
            )
        if self.local is not None and (self.concurrency != 1 or self.credential_env or self.base_url):
            raise ValueError(
                f"Profile {self.name!r}: local profiles use concurrency=1 and no "
                "base_url/credential_env (the server is managed by the workflow)."
            )
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
        if self.local is not None:
            # Pin the exact weights/runtime/sampling instead of a URL.
            local_digest = hashlib.sha256(
                json.dumps(self.local.semantics(), sort_keys=True).encode("utf-8")
            ).hexdigest()
            return ModelIdentity(
                provider=self.provider,
                adapter=self.adapter,
                base_url=f"local://llama.cpp/{local_digest}",
                model=f"{self.model}@sha256:{self.local.model_sha256}",
                target_language=target_language,
                prompt_version=f"{prompt_version}+{LOCAL_PAGE_PROMPT_STYLE}",
                thinking=self.thinking,
            )
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
        profile = self.get(selected) if selected else None
        if profile is not None and profile.local is not None and stage != "translation":
            # The local model is validated for translation only.
            raise ValueError(
                f"Profile {profile.name!r} ({profile.adapter}) is translation-only; "
                f"set an explicit {stage}_profile."
            )
        return profile


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
        if thinking not in THINKING_VALUES:
            raise ValueError(
                f"Profile {name!r} thinking must be one of "
                f"{', '.join(THINKING_VALUES)}."
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
            local=(
                _local_settings(name, raw.get("local"))
                if adapter == LOCAL_LLAMA_ADAPTER or "local" in raw
                else None
            ),
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
    for stage in ("ocr", "toc", "translation"):
        result.for_stage(stage)
    if result.proofread_profile:
        result.for_stage("proofread")
    return result
