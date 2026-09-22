from __future__ import annotations

from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import fitz

import book_pipeline as book
from paddle_native import (BACKEND, MODEL_FILES, NativeOptions, PaddleNativeOCR,
                           identity_from_args, model_identity, options_from_args,
                           ordered_text, readiness)
from pipeline_graph.core import OutputDirectoryLock


class NativeOCRTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = patch('pathlib.Path.home', return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.options = NativeOptions(models_dir=self.root / 'models')
        for name in self.options.model_names:
            directory = self.options.models_dir / name
            directory.mkdir(parents=True)
            for file in MODEL_FILES:
                (directory / file).write_text('fixture', encoding='utf-8')
        self.image = self.root / 'page.png'
        Image.new('RGB', (2200, 3000), 'white').save(self.image)

    def backend(self):
        with patch('paddle_native.readiness', return_value=(True, 'ready')):
            return PaddleNativeOCR(self.options, model_id=model_identity(self.options))

    def test_readiness_checks_dependencies_and_complete_models_without_importing(self):
        with patch('paddle_native.importlib.util.find_spec', return_value=None):
            self.assertFalse(readiness(self.options)[0])
        with patch('paddle_native.importlib.util.find_spec', return_value=object()):
            self.assertTrue(readiness(self.options)[0])
            (self.options.models_dir / self.options.model_names[0] / 'inference.pdiparams').unlink()
            self.assertFalse(readiness(self.options)[0])

    def test_resource_limits_reject_unsafe_values(self):
        for changes in ({'threads': 0}, {'threads': 20}, {'det_limit': 2000}, {'variant': 'unknown'}):
            with self.assertRaises(ValueError):
                replace(self.options, **changes)

    def test_identity_changes_with_weights_rendering_and_reading_direction(self):
        original = model_identity(self.options)
        self.assertEqual(original, model_identity(self.options))
        for changes in ({'det_limit': 640}, {'threads': 2}, {'reading_direction': 'vertical'}):
            self.assertNotEqual(original, model_identity(replace(self.options, **changes)))
        self.assertNotEqual(original, model_identity(self.options, quality=70))
        weight = self.options.models_dir / self.options.model_names[0] / 'inference.pdiparams'
        weight.write_text('updated weights')
        self.assertNotEqual(original, model_identity(self.options))

    def test_auto_prefers_gpu_then_cpu_then_profile_and_explicit_cloud_wins(self):
        args = book.build_parser().parse_args([])
        with patch('book_pipeline.paddle_local_available', return_value=True):
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], 'paddleocr-local')
        with patch('book_pipeline.paddle_local_available', return_value=False), patch('book_pipeline.paddle_native_available', return_value=True):
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], BACKEND)
            args.ocr_backend = 'glm-ocr'
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], 'glm-ocr')
        args.ocr_backend = 'auto'
        with patch('book_pipeline.paddle_local_available', return_value=False), patch('book_pipeline.paddle_native_available', return_value=False):
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], 'coding-plan-mcp')

    def test_horizontal_and_vertical_ordering_and_malformed_results(self):
        result = {'rec_texts': ['left bottom', 'right top', 'left top'],
                  'rec_boxes': [[0, 50, 20, 70], [100, 0, 120, 20], [0, 0, 20, 20]]}
        self.assertEqual(ordered_text(result, 'horizontal'), 'left top\nright top\nleft bottom')
        self.assertEqual(ordered_text(result, 'vertical'), 'right top\nleft top\nleft bottom')
        with self.assertRaises(RuntimeError):
            ordered_text({'rec_texts': ['lost box'], 'rec_boxes': []}, 'horizontal')

    def test_engine_is_reused_and_images_are_bounded(self):
        result = {'rec_texts': ['test'], 'rec_boxes': [[0, 0, 20, 20]]}
        engine = Mock()
        engine.predict.return_value = [result]
        factory = Mock(return_value=engine)
        backend = self.backend()
        with patch.dict('sys.modules', {'paddleocr': types.SimpleNamespace(PaddleOCR=factory)}):
            self.assertEqual(backend.ocr_image(self.image)[0], 'test')
            backend.ocr_image(self.image)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(engine.predict.call_count, 2)
        self.assertLessEqual(max(engine.predict.call_args.args[0].shape[:2]), 2000)
        self.assertEqual(factory.call_args.kwargs['device'], 'cpu')
        self.assertEqual(factory.call_args.kwargs['text_recognition_batch_size'], 1)
        self.assertFalse(factory.call_args.kwargs['enable_mkldnn'])
        backend.close()
        self.assertIsNone(backend._engine)
        self.assertIsNone(backend._process_lock)

    def test_no_text_on_nonblank_page_is_failure_not_a_cached_success(self):
        backend = self.backend()
        backend._engine = Mock()
        backend._engine.predict.return_value = [{'rec_texts': [], 'rec_boxes': []}]
        with patch('book_pipeline.is_visually_blank_page', return_value=(False, .5)):
            with self.assertRaisesRegex(RuntimeError, 'nonblank'):
                backend.ocr_image(self.image)
        backend.close()

    def test_waiting_job_can_be_cancelled_without_loading_a_second_model(self):
        lock = OutputDirectoryLock(self.root / '.translation-agent/paddleocr/cpu.lock')
        lock.acquire()
        self.addCleanup(lock.release)
        backend = self.backend()
        entered = threading.Event()
        errors = []
        def run():
            entered.set()
            try:
                backend.ocr_image(self.image)
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=run)
        thread.start()
        entered.wait(2)
        backend.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIsNone(backend._engine)

    def test_legacy_cli_resume_blank_cache_and_weight_invalidation(self):
        pdf = self.root / 'input.pdf'
        with fitz.open() as document:
            document.new_page()
            document.new_page()
            document.save(pdf)
        output = self.root / 'out'
        args = [str(pdf), '-o', str(output), '--phase', 'ocr', '--ocr-backend', BACKEND,
                '--paddle-native-models-dir', str(self.options.models_dir)]
        engine = Mock()
        engine.predict.return_value = [{'rec_texts': [], 'rec_boxes': []}]
        factory = Mock(return_value=engine)
        with patch('paddle_native.readiness', return_value=(True, 'ready')), patch.dict('sys.modules', {'paddleocr': types.SimpleNamespace(PaddleOCR=factory)}):
            self.assertEqual(book.main(args), 0)
            records = book.load_page_records(output)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].text, '[空白页]')
            self.assertTrue(records[0].ocr_model.startswith(BACKEND + '/'))
            self.assertEqual(book.main(args), 0)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(engine.predict.call_count, 2)
            (self.options.models_dir / self.options.model_names[0] / 'inference.yml').write_text('new config')
            self.assertEqual(book.main(args), 0)
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(engine.predict.call_count, 4)

    def test_product_cli_and_runspec_preserve_native_options(self):
        from document_pipeline import build_parser, _spec_from_args, _graph_request
        from frontend_runtime import _runspec_to_graph_request
        args = build_parser().parse_args(['run', 'input.pdf', '--phase', 'ocr', '--no-translate', '--no-verify',
            '--ocr-backend', BACKEND, '--paddle-native-variant', 'server', '--paddle-native-det-limit', '640'])
        spec = _spec_from_args(args)
        self.assertEqual(_graph_request(spec).pipeline.ocr_backend, BACKEND)
        self.assertEqual(_graph_request(spec).pipeline.paddle_native_variant, "server")
        argv = _runspec_to_graph_request(spec).pipeline.to_argv()
        parsed = book.build_parser().parse_args(argv)
        self.assertEqual(parsed.ocr_backend, BACKEND)
        self.assertEqual(options_from_args(parsed).variant, 'server')
        self.assertEqual(options_from_args(parsed).det_limit, 640)

    def test_graph_and_execution_share_native_profile_identity(self):
        from pipeline_graph.book import _ocr_stage_semantics, _selected_model_profiles
        config = self.root / 'profiles.toml'
        config.write_text('''schema_version = 1
[profiles.local]
adapter = "paddleocr-native"
provider = "paddleocr"
model = "PP-OCRv5-server"
reading_direction = "vertical"
[pipeline]
ocr_profile = "local"
''')
        args = book.build_parser().parse_args(['--config', str(config), '--ocr-backend', BACKEND])
        profile = _selected_model_profiles(args)['ocr']
        self.assertEqual(options_from_args(args, profile).variant, 'server')
        self.assertEqual(_ocr_stage_semantics(args)['identity']['model'], identity_from_args(args, profile))


if __name__ == '__main__':
    unittest.main()
