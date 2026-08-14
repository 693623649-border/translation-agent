"""Shared Streamlit-only helpers; business logic remains in application_service."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import streamlit as st

from application_service import ApplicationService
from pipeline_profiles import ModelProfile, PipelineProfiles, load_pipeline_profiles
from product_contracts import RunSpec
from product_paths import default_profile_path


OCR_ADAPTERS = frozenset({"coding-plan-mcp", "glm-ocr", "tesseract"})
TEXT_ADAPTERS = frozenset({"openai-chat", "glm-chat"})


@st.cache_resource
def application_service() -> ApplicationService:
    return ApplicationService.from_environment()


@st.cache_data(ttl="30s", max_entries=20)
def cached_profiles(path_text: str, modified_ns: int) -> PipelineProfiles:
    del modified_ns
    return load_pipeline_profiles(path_text)


def configured_profile_path() -> Path:
    value = str(st.session_state.get("frontend_config_path") or "").strip()
    if value:
        return Path(value).expanduser().resolve()
    return default_profile_path()


def load_profiles_for_ui() -> tuple[Path, PipelineProfiles | None, str | None]:
    path = configured_profile_path()
    try:
        profiles = cached_profiles(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError) as exc:
        return path, None, str(exc)
    return path, profiles, None


def profiles_for_stage(
    profiles: PipelineProfiles,
    stage: str,
) -> list[ModelProfile]:
    adapters = OCR_ADAPTERS if stage == "ocr" else TEXT_ADAPTERS
    return sorted(
        (profile for profile in profiles.profiles.values() if profile.adapter in adapters),
        key=lambda profile: profile.name,
    )


def profile_label(profile: ModelProfile) -> str:
    return f"{profile.name} · {profile.provider}/{profile.model}"


def profile_default_index(options: list[ModelProfile], name: str | None) -> int:
    return next(
        (index for index, profile in enumerate(options) if profile.name == name),
        0,
    )


def secret_default(name: str) -> str:
    try:
        value = st.secrets.get(name, "")
    except (FileNotFoundError, RuntimeError):
        value = ""
    return str(value) if value else ""


def credential_inputs(
    environment_names: Iterable[str | None],
    *,
    key_prefix: str,
) -> dict[str, str]:
    credentials: dict[str, str] = {}
    for name in sorted({item for item in environment_names if item}):
        value = st.text_input(
            f"{name}",
            type="password",
            value=secret_default(name),
            key=f"{key_prefix}_{name}",
            help="只传给本次任务子进程，不写入任务数据库或运行参数。",
        )
        if value:
            credentials[name] = value
    return credentials


def job_option_rows(jobs: Iterable[object]) -> list[str]:
    return [str(getattr(job, "id")) for job in jobs]


def job_label(job_id: str, jobs: Iterable[object]) -> str:
    by_id = {str(getattr(job, "id")): job for job in jobs}
    job = by_id.get(job_id)
    if job is None:
        return job_id
    title = getattr(job, "spec").title or getattr(job, "source_path").stem
    return f"{title} · {job_id[:8]} · {getattr(job, 'status')}"


def build_spec(
    *,
    source: str | Path | None,
    source_mode: str,
    config: Path,
    recipe: str | Path | None,
    title: str,
    author: str,
    targets: tuple[str, ...],
    translate: bool,
    verify: bool,
    options: dict,
) -> RunSpec:
    return RunSpec(
        source=source,
        source_mode=source_mode,
        output_dir="outputs/webui-pending",
        phase="all",
        title=title.strip() or None,
        author=author.strip() or None,
        target_language="简体中文",
        config=config,
        recipe=recipe,
        targets=targets,
        translate=translate,
        verify=verify,
        options=options,
    )
