from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from publication_checks import check_runtime_hygiene


class PublicationRuntimeHygieneTests(unittest.TestCase):
    def _check_without_host_proc(self, output_dir: Path) -> dict:
        with mock.patch(
            "publication_checks.runtime_hygiene._PROC_ROOT",
            output_dir / "missing-proc",
        ):
            return check_runtime_hygiene(output_dir)

    def test_clean_output_matches_the_golden_check_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()

            result = self._check_without_host_proc(output)

        self.assertEqual(
            result,
            {
                "summary": "未发现临时产物或残留流水线进程。",
                "metrics": {
                    "temporary_path_count": 0,
                    "active_stage_lock_count": 0,
                    "active_process_count": 0,
                },
                "issues": [],
                "warnings": [],
            },
        )

    def test_temporary_path_patterns_preserve_issue_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            (output / "tmp").mkdir()
            (output / "chapter.part").write_bytes(b"partial")
            (output / ".chapter.tmp-state").write_bytes(b"partial")
            (output / "_page_images_1234_deadbeef").mkdir()
            (output / "chapter.md").write_text("正文", encoding="utf-8")

            result = self._check_without_host_proc(output)

        self.assertEqual(
            set(result),
            {"summary", "metrics", "issues", "warnings"},
        )
        self.assertEqual(result["summary"], "发现临时产物或残留流水线进程。")
        self.assertEqual(result["metrics"]["temporary_path_count"], 4)
        self.assertEqual(result["metrics"]["active_stage_lock_count"], 0)
        self.assertEqual(result["metrics"]["active_process_count"], 0)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["issues"]), 1)
        issue = result["issues"][0]
        self.assertEqual(issue["code"], "temporary_artifacts_present")
        self.assertEqual(issue["path"], str(output))
        self.assertEqual(issue["evidence"]["count"], 4)
        self.assertEqual(len(issue["evidence"]["values"]), 4)

    @unittest.skipUnless(os.name == "posix", "POSIX flock contract")
    def test_active_stage_lock_preserves_metrics_and_issue_code(self) -> None:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - non-POSIX interpreter.
            self.skipTest("fcntl is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            lock_dir = output / ".stage_locks"
            lock_dir.mkdir()
            lock_path = lock_dir / "compile.lock"
            with lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    result = self._check_without_host_proc(output)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        self.assertEqual(result["metrics"]["active_stage_lock_count"], 1)
        self.assertEqual(
            [issue["code"] for issue in result["issues"]],
            ["active_stage_locks"],
        )
        self.assertEqual(
            result["issues"][0]["evidence"]["values"],
            [str(lock_path)],
        )

    def test_process_evidence_never_copies_command_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            output = root / "output"
            output.mkdir()
            proc_root = root / "proc"
            current = proc_root / "101"
            current.mkdir(parents=True)
            (current / "stat").write_text("101 (python) S 1 0 0\n", encoding="utf-8")
            sibling = proc_root / "202"
            sibling.mkdir()
            secret = "sensitive-test-token"
            (sibling / "cmdline").write_bytes(
                b"/usr/bin/python3\x00book_pipeline.py\x00--output\x00"
                + str(output).encode("utf-8")
                + b"\x00--api-key\x00"
                + secret.encode("utf-8")
            )

            with (
                mock.patch(
                    "publication_checks.runtime_hygiene._PROC_ROOT",
                    proc_root,
                ),
                mock.patch(
                    "publication_checks.runtime_hygiene.os.getpid",
                    return_value=101,
                ),
            ):
                result = check_runtime_hygiene(output)

        self.assertEqual(result["metrics"]["active_process_count"], 1)
        issue = result["issues"][0]
        self.assertEqual(issue["code"], "active_pipeline_processes")
        self.assertEqual(
            issue["evidence"]["values"],
            [{"pid": 202, "executable": "python3"}],
        )
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("--api-key", serialized)


if __name__ == "__main__":
    unittest.main()
