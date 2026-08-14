"""Application facade shared by the Web UI and other local product clients."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Mapping

from frontend_runtime import (
    FrontendSettings,
    JobRecord,
    JobRegistry,
    PathPolicy,
    artifact_catalog,
    cancel_job,
    plan_runspec,
    safe_upload_name,
    start_job_process,
    tail_log,
)
from product_contracts import ArtifactRecord, RunSpec
from product_paths import resource_root


class ApplicationService:
    """Own job workspaces and expose a credential-free persistent API."""

    def __init__(self, settings: FrontendSettings | None = None) -> None:
        self.settings = settings or FrontendSettings.from_environment()
        self.settings.jobs_root.mkdir(parents=True, exist_ok=True)
        self.registry = JobRegistry(self.settings.database)
        self.source_policy = PathPolicy(self.settings.source_roots)
        self.control_policy = PathPolicy(
            (*self.settings.source_roots, resource_root())
        )

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ApplicationService":
        return cls(FrontendSettings.from_environment(environment))

    def _job_paths(self, job_id: str) -> tuple[Path, Path, Path]:
        workspace = (self.settings.jobs_root / job_id).resolve()
        jobs_root = self.settings.jobs_root.resolve()
        try:
            workspace.relative_to(jobs_root)
        except ValueError as exc:  # defensive: job id is generated, never supplied
            raise ValueError("job workspace escapes configured root") from exc
        return workspace, workspace / "input", workspace / "output"

    def _validate_control_file(
        self,
        value: Path | str | None,
        *,
        suffix: str,
    ) -> Path | None:
        if value is None or not str(value).strip():
            return None
        return self.control_policy.resolve_file(value, suffixes={suffix})

    def _normalized_spec(
        self,
        spec: RunSpec,
        *,
        source: Path,
        output: Path,
    ) -> RunSpec:
        config = self._validate_control_file(spec.config, suffix=".toml")
        recipe = self._validate_control_file(spec.recipe, suffix=".toml")
        if spec.source_mode == "epub" and spec.verify:
            raise ValueError(
                "EPUB-native release verification is not available yet; "
                "EPUB tasks currently produce reviewable drafts only"
            )
        if spec.source_mode == "epub" and spec.targets and not set(spec.targets) <= {
            "publication.epub",
            "publication.docx",
            "publication.report",
            "publication.word_report",
        }:
            raise ValueError(
                "EPUB jobs currently support EPUB, Word, and their release report"
            )
        return replace(
            spec,
            source=source,
            output_dir=output,
            config=config,
            recipe=recipe,
        )

    def submit_path(
        self,
        spec: RunSpec,
        *,
        credentials: Mapping[str, str] | None = None,
        start: bool = True,
    ) -> JobRecord:
        suffixes = {
            ".epub" if spec.source_mode == "epub" else ".pdf"
        }
        source = self.source_policy.resolve_file(
            str(spec.source or ""), suffixes=suffixes
        )
        job_id = uuid.uuid4().hex
        workspace, _input_dir, output = self._job_paths(job_id)
        workspace.mkdir(parents=True, mode=0o700)
        registered = False
        try:
            output.mkdir(parents=True)
            normalized = self._normalized_spec(spec, source=source, output=output)
            self._write_spec(workspace, normalized)
            job = self.registry.create(job_id, workspace, normalized)
            registered = True
        except Exception:
            if not registered:
                shutil.rmtree(workspace)
            raise
        return (
            start_job_process(self.registry, job.id, credentials=credentials)
            if start
            else job
        )

    def submit_upload(
        self,
        spec: RunSpec,
        *,
        filename: str,
        content: bytes | bytearray | memoryview | BinaryIO,
        credentials: Mapping[str, str] | None = None,
        start: bool = True,
    ) -> JobRecord:
        job_id = uuid.uuid4().hex
        workspace, input_dir, output = self._job_paths(job_id)
        workspace.mkdir(parents=True, mode=0o700)
        registered = False
        try:
            input_dir.mkdir()
            output.mkdir()
            source = input_dir / safe_upload_name(filename, source_mode=spec.source_mode)
            self._write_upload(source, content)
            normalized = self._normalized_spec(spec, source=source, output=output)
            self._write_spec(workspace, normalized)
            job = self.registry.create(job_id, workspace, normalized)
            registered = True
        except Exception:
            if not registered:
                shutil.rmtree(workspace)
            raise
        return (
            start_job_process(self.registry, job.id, credentials=credentials)
            if start
            else job
        )

    def _write_upload(
        self,
        destination: Path,
        content: bytes | bytearray | memoryview | BinaryIO,
    ) -> None:
        total = 0
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                if isinstance(content, (bytes, bytearray, memoryview)):
                    chunks = (content,)
                else:
                    chunks = iter(lambda: content.read(1024 * 1024), b"")
                for chunk in chunks:
                    total += len(chunk)
                    if total > self.settings.upload_limit_bytes:
                        raise ValueError(
                            "upload exceeds configured limit of "
                            f"{self.settings.upload_limit_bytes} bytes"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_spec(workspace: Path, spec: RunSpec) -> None:
        destination = workspace / "run-spec.json"
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(spec.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    def preview_plan(self, spec: RunSpec) -> tuple[str, ...]:
        """Plan a server-path request without creating a job."""

        source = self.source_policy.resolve_file(
            str(spec.source or ""),
            suffixes={".epub" if spec.source_mode == "epub" else ".pdf"},
        )
        preview_output = self.settings.jobs_root / ".plan-preview"
        normalized = self._normalized_spec(
            spec,
            source=source,
            output=preview_output,
        )
        return plan_runspec(normalized)

    def preview_upload_plan(self, spec: RunSpec, *, filename: str) -> tuple[str, ...]:
        """Plan an uploaded source without persisting its bytes or a job row."""

        safe_name = safe_upload_name(filename, source_mode=spec.source_mode)
        preview_root = (self.settings.jobs_root / ".plan-preview").resolve()
        normalized = self._normalized_spec(
            spec,
            source=preview_root / safe_name,
            output=preview_root / "output",
        )
        return plan_runspec(normalized)

    def get_job(self, job_id: str) -> JobRecord:
        self.registry.reconcile_workers()
        return self.registry.get(job_id)

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        self.registry.reconcile_workers()
        return self.registry.list(limit=limit)

    def cancel(self, job_id: str) -> JobRecord:
        return cancel_job(self.registry, job_id)

    def resume(
        self,
        job_id: str,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> JobRecord:
        job = self.registry.get(job_id)
        if job.status not in {"failed", "cancelled", "interrupted"}:
            raise RuntimeError(f"task {job_id} cannot resume from {job.status}")
        return start_job_process(self.registry, job_id, credentials=credentials)

    def artifacts(self, job_id: str) -> list[ArtifactRecord]:
        return artifact_catalog(self.registry.get(job_id))

    def log(self, job_id: str, *, max_bytes: int = 128 * 1024) -> str:
        return tail_log(self.registry.get(job_id), max_bytes=max_bytes)


__all__ = ["ApplicationService"]
