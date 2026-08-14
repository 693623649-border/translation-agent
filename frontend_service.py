from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from frontend_runtime import child_environment
from translation_agent_api import GraphRunRequest, RunRequest, status_for_request


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARTIFACT_SUFFIXES = {".epub", ".docx", ".pdf", ".jsonl"}


@dataclass(frozen=True)
class PipelineJob:
    """Compatibility wrapper for callers that still run one job synchronously.

    New Web UI code uses :class:`application_service.ApplicationService`, but
    this boundary now delegates to ``graph_pipeline.py`` instead of the legacy
    monolith and keeps credentials out of argv.
    """

    request: GraphRunRequest | RunRequest
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def graph_request(self) -> GraphRunRequest:
        return (
            self.request
            if isinstance(self.request, GraphRunRequest)
            else GraphRunRequest(pipeline=self.request)
        )

    def command(self) -> list[str]:
        request = self.graph_request
        controls: list[str] = []
        pairs = (
            ("--recipe", request.recipe),
            ("--toc-source", request.toc_source),
            ("--source-mode", request.source_mode),
            (
                "--text-pdf-strip-leading-page-number-offset",
                request.text_pdf_strip_leading_page_number_offset,
            ),
        )
        for option, value in pairs:
            if value is not None:
                controls.extend([option, str(value)])
        for option, values in (
            ("--target", request.targets),
            ("--enable-node", request.enable_nodes),
            ("--disable-node", request.disable_nodes),
            ("--force-node", request.force_nodes),
            ("--allow-plugin", request.plugin_allowlist),
        ):
            for value in values:
                controls.extend([option, value])
        for enabled, option in (
            (request.include_proofread, "--include-proofread"),
            (request.force_all, "--force-graph"),
            (request.adopt_existing_output, "--adopt-existing-output"),
            (request.text_pdf_sort, "--text-pdf-sort"),
            (request.text_pdf_reflow, "--text-pdf-reflow"),
        ):
            if enabled:
                controls.append(option)
        return [
            sys.executable,
            str(PROJECT_ROOT / "graph_pipeline.py"),
            *controls,
            *request.pipeline.to_argv(),
        ]

    def environment(
        self,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        return child_environment(self.credentials, base=base)


@dataclass(frozen=True)
class PipelineJobResult:
    exit_code: int
    status: dict

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_pipeline_job(
    job: PipelineJob,
    *,
    on_output: Callable[[str], None] | None = None,
) -> PipelineJobResult:
    """Run a pipeline in an isolated child process and stream sanitized stdout."""

    process = subprocess.Popen(
        job.command(),
        cwd=PROJECT_ROOT,
        env=job.environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    secrets = [value for value in job.credentials.values() if value]
    for raw_line in process.stdout:
        line = raw_line.rstrip()
        for secret in secrets:
            line = line.replace(secret, "<redacted>")
        if on_output is not None:
            on_output(line)
    exit_code = process.wait()
    return PipelineJobResult(
        exit_code=exit_code,
        status=status_for_request(job.graph_request.pipeline),
    )


def discover_artifacts(output_dir: Path | str) -> list[Path]:
    root = Path(output_dir).expanduser().resolve()
    if not root.exists():
        return []
    return sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in ARTIFACT_SUFFIXES
        ),
        key=lambda path: path.name,
    )


def artifact_mime_type(path: Path) -> str:
    return {
        ".epub": "application/epub+zip",
        ".docx": (
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        ".pdf": "application/pdf",
        ".jsonl": "application/x-ndjson",
    }.get(path.suffix.lower(), "application/octet-stream")


def safe_uploaded_pdf_name(name: str) -> str:
    basename = Path(name).name
    stem = re.sub(r"[^\w\u3400-\u9fff.-]+", "_", Path(basename).stem).strip("._")
    if not stem:
        stem = "uploaded_book"
    return f"{stem[:120]}.pdf"
