import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


class FrontendAppTests(unittest.TestCase):
    def test_default_page_renders_without_exception(self) -> None:
        app = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "frontend_app.py")
        ).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.title[0].value, "📚 影印书转换台")
        self.assertEqual(
            [button.label for button in app.button],
            ["▶ 开始 / 继续任务", "↻ 刷新状态"],
        )


if __name__ == "__main__":
    unittest.main()
