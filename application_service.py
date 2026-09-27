"""Application facade shared by the Web UI and other local product clients."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, BinaryIO, Mapping

from frontend_runtime import (
    FrontendSettings,
    JobRecord,
    JobRegistry,
    PathPolicy,
    artifact_catalog,
    cancel_job,
    safe_upload_name,
    start_job_process,
    tail_log,
)
from product_contracts import ArtifactRecord, RunSpec
from product_paths import resource_root
from run_execution_service import RunExecutionService
from semantic_review_policy import ACCEPT_AS_TEXT_REASON as REVIEW_ACCEPT_REASON


class ApplicationService:
    """Own job workspaces and expose a credential-free persistent API."""

    def __init__(self, settings: FrontendSettings | None = None) -> None:
        self.settings = settings or FrontendSettings.from_environment()
        self.settings.jobs_root.mkdir(parents=True, exist_ok=True)
        self.registry = JobRegistry(self.settings.database)
        self.execution = RunExecutionService(load_dotenv=False)
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
        options = dict(spec.options)
        glossary_value = options.get("glossary")
        if glossary_value is not None and str(glossary_value).strip():
            glossary = self._validate_control_file(
                str(glossary_value),
                suffix=".json",
            )
            options["glossary"] = str(glossary)
        return replace(
            spec,
            source=source,
            output_dir=output,
            config=config,
            recipe=recipe,
            options=options,
        )

    @staticmethod
    def _snapshot_file(source: Path, destination: Path) -> None:
        """Copy one regular input through a stable descriptor.

        Product jobs must not keep reading mutable server paths after they have
        been accepted.  Opening with ``O_NOFOLLOW`` and comparing the source
        descriptor before and after the copy also rejects a file that changes
        while the snapshot is being materialized.
        """

        source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, source_flags)
        try:
            before = os.fstat(source_descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"not a regular file: {source}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(destination_descriptor, "wb") as target:
                    while True:
                        chunk = os.read(source_descriptor, 1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            after = os.fstat(source_descriptor)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            if identity_after != identity_before:
                destination.unlink(missing_ok=True)
                raise ValueError(f"input changed while being snapshotted: {source}")
        finally:
            os.close(source_descriptor)

    def _snapshot_control_inputs(
        self,
        spec: RunSpec,
        *,
        workspace: Path,
    ) -> RunSpec:
        """Bind config, recipe and glossary bytes to the registered job."""

        control_dir = workspace / "control"
        config: Path | None = None
        recipe: Path | None = None
        options = dict(spec.options)
        if spec.config is not None:
            config = control_dir / "pipeline.toml"
            self._snapshot_file(Path(spec.config), config)
        if spec.recipe is not None:
            recipe = control_dir / "recipe.toml"
            self._snapshot_file(Path(spec.recipe), recipe)
        glossary_value = options.get("glossary")
        if glossary_value:
            glossary = control_dir / "glossary.json"
            self._snapshot_file(Path(str(glossary_value)), glossary)
            options["glossary"] = str(glossary)
        return replace(
            spec,
            config=config,
            recipe=recipe,
            options=options,
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
        workspace, input_dir, output = self._job_paths(job_id)
        workspace.mkdir(parents=True, mode=0o700)
        registered = False
        try:
            input_dir.mkdir()
            output.mkdir(parents=True)
            snapshot = input_dir / safe_upload_name(
                source.name,
                source_mode=spec.source_mode,
            )
            self._snapshot_file(source, snapshot)
            normalized = self._normalized_spec(
                spec,
                source=snapshot,
                output=output,
            )
            normalized = self._snapshot_control_inputs(
                normalized,
                workspace=workspace,
            )
            plan = self.execution.plan(normalized)
            self._write_spec(workspace, normalized)
            job = self.registry.create(
                job_id,
                workspace,
                normalized,
                resolved_targets=plan.targets,
                release_profile=plan.release_profile,
            )
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
            normalized = self._snapshot_control_inputs(
                normalized,
                workspace=workspace,
            )
            plan = self.execution.plan(normalized)
            self._write_spec(workspace, normalized)
            job = self.registry.create(
                job_id,
                workspace,
                normalized,
                resolved_targets=plan.targets,
                release_profile=plan.release_profile,
            )
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
        return self.execution.plan(normalized).node_names

    def preview_upload_plan(
        self,
        spec: RunSpec,
        *,
        filename: str,
        content: bytes | bytearray | memoryview | BinaryIO,
    ) -> tuple[str, ...]:
        """Plan against temporary uploaded bytes without creating a task row."""

        safe_name = safe_upload_name(filename, source_mode=spec.source_mode)
        with tempfile.TemporaryDirectory(
            prefix=".plan-preview-",
            dir=self.settings.jobs_root,
        ) as temporary:
            preview_root = Path(temporary).resolve()
            source = preview_root / safe_name
            output = preview_root / "output"
            self._write_upload(source, content)
            normalized = self._normalized_spec(
                spec,
                source=source,
                output=output,
            )
            return self.execution.plan(normalized).node_names

    def get_job(self, job_id: str) -> JobRecord:
        self.registry.reconcile_workers()
        return self.registry.get(job_id)

    def list_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        self.registry.reconcile_workers()
        return self.registry.list(limit=limit)

    def ocr_pages(self, job_id: str) -> tuple[int, ...]:
        job = self.registry.get(job_id)
        directory = job.workspace / "output" / "pages"
        return tuple(sorted(int(path.stem[5:]) for path in directory.glob("page_*.json")
                            if path.stem[5:].isdigit() and not path.is_symlink()))

    def ocr_page(self, job_id: str, page: int) -> dict:
        if page < 1:
            raise ValueError("page must be positive")
        job = self.registry.get(job_id)
        path = job.workspace / "output" / "pages" / f"page_{page:04d}.json"
        path.resolve(strict=True).relative_to(job.workspace.resolve())
        return json.loads(path.read_text(encoding="utf-8"))

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

    def _review_output(self, job_id: str) -> Path:
        """Return the canonical output owned by a registered Web task."""

        job = self.registry.get(job_id)
        workspace, _input_dir, expected_output = self._job_paths(job.id)
        if job.workspace.resolve() != workspace:
            raise ValueError("task workspace does not match its registered identity")
        output = Path(job.spec.output_dir).expanduser().resolve()
        if output != expected_output.resolve():
            raise ValueError("task review output does not match its UUID workspace")
        return output

    def review_status(self, job_id: str) -> dict[str, Any]:
        """Read current review state through the shared product contract."""

        from document_pipeline import review_status

        return review_status(self._review_output(job_id), include_issues=True)

    def review_report(self, job_id: str) -> dict[str, Any]:
        """Initialize/refresh and return the complete shared review report."""

        from document_pipeline import review_report

        return review_report(self._review_output(job_id))

    def accept_review_issue_as_text(
        self,
        job_id: str,
        *,
        issue_id: str,
        reviewer: str,
    ) -> dict[str, Any]:
        """Record the sole non-structural decision exposed by Web UI v1."""

        from document_pipeline import review_report

        return review_report(
            self._review_output(job_id),
            issue_id=issue_id,
            reviewer=reviewer,
            decision="accepted",
            reason=REVIEW_ACCEPT_REASON,
        )


__all__ = ["ApplicationService", "REVIEW_ACCEPT_REASON"]
