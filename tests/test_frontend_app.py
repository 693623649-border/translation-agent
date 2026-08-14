import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class FrontendAppTests(unittest.TestCase):
    def test_default_page_renders_without_exception(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"TRANSLATION_AGENT_WEBUI_RUNTIME_ROOT": directory},
        ):
            app = AppTest.from_file(str(root / "streamlit_app.py")).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, ":material/translate: 翻译出版工作台")
        self.assertEqual(app.header[0].value, "创建翻译任务")
        self.assertIn("创建并运行", [button.label for button in app.button])

    def test_streamlit_sources_use_native_layout_without_deprecated_width(self) -> None:
        root = Path(__file__).resolve().parents[1]
        sources = [root / "streamlit_app.py", *sorted((root / "app_pages").glob("*.py"))]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in sources)
        self.assertNotIn("use_container_width", combined)
        self.assertNotIn("unsafe_allow_html", combined)
        self.assertIn("st.navigation", combined)


if __name__ == "__main__":
    unittest.main()
