import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from frontend_service import (
    PipelineJob,
    artifact_mime_type,
    discover_artifacts,
    run_pipeline_job,
    safe_uploaded_pdf_name,
)
from translation_agent_api import RunRequest


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = iter(["starting\n", "provider echoed secret-value\n"])

    def wait(self) -> int:
        return 0


class FrontendServiceTests(unittest.TestCase):
    def test_credentials_are_only_in_child_environment(self) -> None:
        request = RunRequest(
            output_dir="outputs/book",
            phase="translate",
            config="pipeline.toml",
            translation_profile="deepseek_pro",
            translate_non_chinese=True,
        )
        job = PipelineJob(
            request=request,
            credentials={"DEEPSEEK_API_KEY": "secret-value"},
        )
        self.assertNotIn("secret-value", " ".join(job.command()))
        self.assertEqual(
            job.environment(base={})["DEEPSEEK_API_KEY"],
            "secret-value",
        )
        self.assertNotIn("secret-value", repr(job))

    def test_invalid_credential_environment_name_is_rejected(self) -> None:
        job = PipelineJob(
            request=RunRequest(output_dir="outputs/book", phase="status"),
            credentials={"BAD-NAME": "secret"},
        )
        with self.assertRaises(ValueError):
            job.environment(base={})

    def test_streamed_output_redacts_credentials(self) -> None:
        lines: list[str] = []
        job = PipelineJob(
            request=RunRequest(output_dir="outputs/book", phase="status"),
            credentials={"TEST_API_KEY": "secret-value"},
        )
        with (
            patch("frontend_service.subprocess.Popen", return_value=_FakeProcess()),
            patch("frontend_service.status_for_request", return_value={"pages": 1}),
        ):
            result = run_pipeline_job(job, on_output=lines.append)
        self.assertTrue(result.ok)
        self.assertEqual(result.status["pages"], 1)
        self.assertEqual(lines[-1], "provider echoed <redacted>")

    def test_artifact_discovery_and_mime_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epub = root / "book.epub"
            epub.write_bytes(b"epub")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            self.assertEqual(discover_artifacts(root), [epub])
            self.assertEqual(artifact_mime_type(epub), "application/epub+zip")

    def test_uploaded_pdf_name_is_sanitized(self) -> None:
        self.assertEqual(
            safe_uploaded_pdf_name("../../危险 书名?.PDF"),
            "危险_书名.pdf",
        )


if __name__ == "__main__":
    unittest.main()
