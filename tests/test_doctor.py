from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import doctor


class DoctorTests(unittest.TestCase):
    def test_report_never_contains_credential_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "pipeline.toml"
            config.write_text(
                "schema_version=1\n"
                "[profiles.translation]\n"
                "adapter='openai-chat'\nprovider='test'\nmodel='test'\n"
                "credential_env='DOCTOR_TEST_KEY'\n"
                "[pipeline]\ntranslation_profile='translation'\n",
                encoding="utf-8",
            )
            report = doctor.run_doctor(
                config=config,
                output_dir=root / "output",
                environ={"DOCTOR_TEST_KEY": "super-secret-test-value"},
            )

        self.assertNotIn("super-secret-test-value", repr(report))
        credential = next(
            item
            for item in report["checks"]
            if item["name"] == "credential:DOCTOR_TEST_KEY"
        )
        self.assertEqual(credential["detail"], "present")

    def test_missing_required_web_dependency_fails(self) -> None:
        real_find_spec = doctor.importlib.util.find_spec

        def fake_find_spec(name: str):
            if name == "streamlit":
                return None
            return real_find_spec(name)

        with patch.object(doctor.importlib.util, "find_spec", side_effect=fake_find_spec):
            report = doctor.run_doctor(require_web=True)

        self.assertEqual(report["status"], "failed")
        self.assertIn("python:streamlit", report["failed_required"])

    def test_json_mode_contains_no_optional_backend_output(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        def noisy_soffice() -> str:
            print("backend warning on stdout")
            print("backend warning on stderr", file=doctor.sys.stderr)
            return "/test/soffice"

        with patch.object(doctor, "CORE_IMPORTS", {}), patch(
            "docx_render_gate.find_soffice",
            side_effect=noisy_soffice,
        ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = doctor.main(["--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "passed")
        self.assertNotIn("backend warning", stdout.getvalue())
        self.assertNotIn("backend warning", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
