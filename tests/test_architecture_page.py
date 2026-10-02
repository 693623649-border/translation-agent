import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from streamlit.testing.v1 import AppTest

class ArchitecturePageTests(unittest.TestCase):
    def test_page_and_empty_job_scope(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TRANSLATION_AGENT_WEBUI_RUNTIME_ROOT": directory}):
            app = AppTest.from_file(str(root / "app_pages/architecture.py")).run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            self.assertEqual(app.header[0].value, "架构与审查")
            app.selectbox[0].select("已有任务").run(timeout=20)
            self.assertEqual(len(app.exception), 0)
            self.assertTrue(app.info)
