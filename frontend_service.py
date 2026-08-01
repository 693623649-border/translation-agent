from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from translation_agent_api import RunRequest, status_for_request


PROJECT_ROOT = Path(__file__).resolve().parent
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ARTIFACT_SUFFIXES = {".epub", ".docx", ".pdf", ".jsonl"}


@dataclass(frozen=True)
class PipelineJob:
    """A UI-submitted job whose credentials are never represented in argv."""

    request: RunRequest
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)

    def command(self) -> list[str]:
        return [
            sys.executable,
            str(PROJECT_ROOT / "book_pipeline.py"),
            *self.request.to_argv(),
        ]

    def environment(
        self,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        for name, value in self.credentials.items():
            if not ENV_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"Invalid credential environment variable: {name!r}")
            if value:
                environment[name] = value
        return environment


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
        status=status_for_request(job.request),
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
