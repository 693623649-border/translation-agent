import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class NativeOCRFrontendTests(unittest.TestCase):
    def test_native_only_job_submits_without_cloud_credentials(self):
        import fitz
        from application_service import ApplicationService
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.pdf'
            with fitz.open() as document:
                document.new_page()
                document.save(source)
            with patch.dict(os.environ, {
                'TRANSLATION_AGENT_WEBUI_RUNTIME_ROOT': folder,
                'TRANSLATION_AGENT_WEBUI_SOURCE_ROOTS': os.pathsep.join([str(root), folder]),
            }), patch('paddle_native.readiness', return_value=(True, 'ready')):
                from app_pages._shared import application_service
                application_service.clear()
                app = AppTest.from_file(str(root / 'streamlit_app.py')).run()
                app.button_group(key='new_source_mode').set_value('扫描 PDF').run()
                app.button_group(key='new_task_scope').set_value('仅 OCR').run()
                app.selectbox(key='new_ocr_mode').select('本机 PaddleOCR（CPU）').run()
                app.selectbox(key='new_native_variant').select('server').run()
                app.button_group(key='new_source_origin').set_value('工作区路径').run()
                path_input = next(item for item in app.text_input if item.label == '工作区内文件路径')
                path_input.set_value(str(source))
                self.assertFalse(any('API_KEY' in item.label for item in app.text_input))
                with patch.object(ApplicationService, 'submit_path') as submit:
                    submit.return_value.id = 'test-native-job'
                    next(button for button in app.button if button.label == '创建并运行').click().run()
                self.assertEqual(len(app.exception), 0)
                self.assertFalse(app.error)
                spec = submit.call_args.args[0]
                self.assertEqual(spec.phase, 'ocr')
                self.assertEqual(spec.targets, ('pages.raw',))
                self.assertFalse(spec.translate)
                self.assertFalse(spec.verify)
                self.assertEqual(spec.options['ocr_backend'], 'paddleocr-native')
                self.assertEqual(spec.options['paddle_native_variant'], 'server')
                self.assertEqual(submit.call_args.kwargs['credentials'], {})
                application_service.clear()

    def test_cancelled_native_ocr_can_resume_without_credentials(self):
        from frontend_runtime import JobRecord
        from product_contracts import RunSpec

        root = Path(__file__).resolve().parents[1]
        job = JobRecord(
            id="native-resume", status="cancelled", created_at="now", updated_at="now",
            workspace=root, source_mode="scanned-pdf", source_path=root / "input.pdf",
            spec=RunSpec(phase="ocr", options={"ocr_backend": "paddleocr-native"}),
        )
        service = MagicMock()
        service.list_jobs.return_value = [job]
        service.get_job.return_value = job
        service.ocr_pages.return_value = []
        service.log.return_value = "cancelled"
        service.registry.events.return_value = []
        with patch("app_pages._shared.application_service", return_value=service), patch(
            "app_pages._shared.load_profiles_for_ui"
        ) as profiles:
            app = AppTest.from_file(str(root / "app_pages/jobs.py")).run()
            self.assertFalse(app.exception)
            self.assertFalse(app.text_input)
            profiles.assert_not_called()
            # Avoid the rerun loop while asserting the actual credential handoff.
            service.resume.side_effect = RuntimeError("resume captured")
            next(button for button in app.button if button.label == "恢复运行").click().run()
            service.resume.assert_called_once_with(job.id, credentials={})
            self.assertFalse(app.exception)


if __name__ == "__main__":
    unittest.main()
