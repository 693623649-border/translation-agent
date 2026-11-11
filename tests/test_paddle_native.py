from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import traceback
import types
import weakref
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import pymupdf as fitz

import translation_agent.pipeline.book_pipeline as book
from translation_agent.ocr.paddle_native import (BACKEND, MODEL_FILES, NativeOptions, PaddleNativeOCR,
                           identity_from_args, model_identity, options_from_args,
                           ordered_text, readiness)
from translation_agent.pipeline.graph.core import OutputDirectoryLock


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
        with patch('translation_agent.ocr.paddle_native.readiness', return_value=(True, 'ready')):
            return PaddleNativeOCR(self.options, model_id=model_identity(self.options))

    def test_readiness_checks_dependencies_and_complete_models_without_importing(self):
        with patch('translation_agent.ocr.paddle_native.importlib.util.find_spec', return_value=None):
            self.assertFalse(readiness(self.options)[0])
        with patch('translation_agent.ocr.paddle_native.importlib.util.find_spec', return_value=object()):
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

    def test_auto_prefers_gpu_then_cpu_and_an_explicit_backend_wins(self):
        args = book.build_parser().parse_args([])
        with patch('translation_agent.pipeline.book_pipeline.paddle_local_available', return_value=True):
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], 'paddleocr-local')
        with patch('translation_agent.pipeline.book_pipeline.paddle_local_available', return_value=False), patch('translation_agent.pipeline.book_pipeline.paddle_native_available', return_value=True):
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], BACKEND)
            args.ocr_backend = 'openai-vision'
            self.assertEqual(book.resolve_ocr_backend_name(args)[0], 'openai-vision')
        # With neither a local engine nor an OCR profile there is nothing left
        # to run: the removed cloud MCP fallback is gone, so a run fails loudly
        # instead of silently routing to a backend this release cannot build.
        args.ocr_backend = 'auto'
        with patch('translation_agent.pipeline.book_pipeline.paddle_local_available', return_value=False), patch('translation_agent.pipeline.book_pipeline.paddle_native_available', return_value=False):
            with self.assertRaisesRegex(ValueError, 'No OCR backend is available'):
                book.resolve_ocr_backend_name(args)

    def test_horizontal_and_vertical_ordering_and_malformed_results(self):
        result = {'rec_texts': ['left bottom', 'right top', 'left top'],
                  'rec_boxes': [[0, 50, 20, 70], [100, 0, 120, 20], [0, 0, 20, 20]]}
        self.assertEqual(ordered_text(result, 'horizontal'), 'left top\nright top\nleft bottom')
        self.assertEqual(ordered_text(result, 'vertical'), 'right top\nleft top\nleft bottom')
        with self.assertRaises(RuntimeError):
            ordered_text({'rec_texts': ['lost box'], 'rec_boxes': []}, 'horizontal')

    def test_vertical_two_tier_page_reads_upper_tier_before_lower(self):
        # Two-tier tategaki: columns 20 wide, upper tier y 40-300, lower y 400-700.
        # A wide heading sits at the right edge; a folio and running head sit
        # above the text block on the left and are dropped as page frame.
        result = {
            'rec_texts': ['lower right', 'upper left', 'heading', 'lower left',
                          'upper right', 'author', '37'],
            'rec_boxes': [[100, 400, 120, 700], [40, 40, 60, 300], [140, 50, 190, 600],
                          [40, 400, 60, 700], [100, 40, 120, 300], [30, 10, 80, 30],
                          [2, 10, 28, 30]],
        }
        self.assertEqual(
            ordered_text(result, 'vertical'),
            'heading\nupper right\nupper left\nlower right\nlower left',
        )

    def test_vertical_content_above_first_tier_is_kept(self):
        # A tall vertical title above the body is content, not page frame, and
        # ruby boxes beside the columns must not set the column width.
        result = {
            'rec_texts': ['title', 'col R', 'ruby a', 'ruby b', 'ruby c', 'col L'],
            'rec_boxes': [[500, 40, 560, 330], [300, 400, 320, 700],
                          [322, 420, 330, 440], [322, 500, 330, 520], [322, 600, 330, 620],
                          [260, 400, 280, 700]],
        }
        lines = ordered_text(result, 'vertical').split('\n')
        self.assertEqual(sorted(lines), sorted(result['rec_texts']))
        self.assertEqual(lines[0], 'title')
        self.assertLess(lines.index('col R'), lines.index('col L'))

    def test_vertical_heading_spanning_both_tiers_is_read_first(self):
        result = {
            'rec_texts': ['l1', 'u1', 'TITLE', 'u2', 'l2'],
            'rec_boxes': [[200, 400, 220, 720], [200, 40, 220, 300], [300, 40, 360, 720],
                          [160, 40, 180, 300], [160, 400, 180, 720]],
        }
        self.assertEqual(ordered_text(result, 'vertical'), 'TITLE\nu1\nu2\nl1\nl2')

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

    def test_vertical_emphasis_columns_keep_only_higher_scoring_rereads(self):
        # Body columns are 40 wide; boxes 1 and 2 are widened by emphasis dots.
        result = {
            'rec_texts': ['body 1', 'body 2', 'body 3', 'dotted misread', 'heading'],
            'rec_scores': [0.99, 0.99, 0.99, 0.80, 0.99],
            'rec_boxes': [[500, 0, 540, 400], [440, 0, 480, 400], [380, 0, 420, 400],
                          [300, 0, 360, 400], [200, 0, 280, 400]],
        }
        engine = Mock()
        engine.predict.return_value = [result]
        # The heading reread scores higher but loses glyphs (radicals only).
        rec = Mock(return_value=[{'rec_text': 'dotted fixed', 'rec_score': 0.97},
                                 {'rec_text': 'hea', 'rec_score': 0.999}])
        engine.paddlex_pipeline.text_rec_model = rec
        page = Image.new('RGB', (600, 400), 'white')
        page.paste((255, 0, 0), (300, 0, 343, 400))  # body glyphs of the dotted line
        page.paste((0, 0, 255), (345, 0, 360, 400))  # emphasis dots on its right
        page.save(self.image)
        backend = PaddleNativeOCR(replace(self.options, reading_direction='vertical'),
                                  model_id='test')
        backend._engine = engine
        backend._process_lock = object()
        text = backend.ocr_image(self.image)[0]
        self.assertEqual(text, 'body 1\nbody 2\nbody 3\ndotted fixed\nheading')
        crops = rec.call_args.args[0]
        # Only the two wide boxes are re-read, each as a rotated left strip.
        self.assertEqual([crop.shape[:2] for crop in crops], [(42, 400), (42, 400)])
        # The strip is the left part of the box: the dotted box (x 300-360)
        # has red ink at x 300-340 and blue emphasis dots at x 345-360.
        self.assertTrue((crops[0][..., 2] > 200).all() and (crops[0][..., 0] < 50).all())
        backend._process_lock = None

    def test_no_text_on_nonblank_page_is_failure_not_a_cached_success(self):
        backend = self.backend()
        backend._engine = Mock()
        backend._engine.predict.return_value = [{'rec_texts': [], 'rec_boxes': []}]
        with patch('translation_agent.pipeline.book_pipeline.is_visually_blank_page', return_value=(False, .5)):
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

    def lifecycle_backend(self, predict):
        """Backend whose factory builds a weakref-able engine (no Mock caches).

        Records the order of engine finalization and lease release so a test
        can prove the engine is gone before another job may take the lease.
        """

        events = []
        engines = []

        class Engine:
            def __init__(self, **_kwargs):
                engines.append(weakref.ref(self, lambda _ref: events.append('engine-freed')))

            def predict(self, array):
                return predict(self, array)

        from translation_agent.runtime import model_lease

        real_release = model_lease.release_heavy_model_lease

        def release(purpose, lock):
            events.append('lease-released')
            real_release(purpose, lock)

        patches = (
            patch.dict('sys.modules', {'paddleocr': types.SimpleNamespace(PaddleOCR=Engine)}),
            patch.object(model_lease, 'release_heavy_model_lease', release),
        )
        for active in patches:
            active.start()
            self.addCleanup(active.stop)
        return self.backend(), engines, events

    @staticmethod
    def page_result():
        return [{'rec_texts': ['text'], 'rec_boxes': [[0, 0, 10, 10]]}]

    def test_cancel_during_predict_frees_engine_before_lease_even_if_error_is_kept(self):
        backend = None

        def predict(_engine, _array):
            backend.close()  # another thread cancels mid-inference
            return self.page_result()

        backend, engines, events = self.lifecycle_backend(predict)
        kept = []
        try:
            backend.ocr_image(self.image)
        except RuntimeError as exc:
            kept.append(exc)  # a caller that keeps the error and its traceback
        self.assertEqual(str(kept[0]), 'Native OCR cancelled')
        self.assertIsNone(backend._engine)
        self.assertIsNone(engines[0](), 'kept traceback must not keep the model alive')
        self.assertEqual(events, ['engine-freed', 'lease-released'])
        self.assertIsNotNone(kept[0].__traceback__, 'the traceback itself is preserved')

    def test_predict_failure_kept_by_caller_does_not_retain_engine_after_close(self):
        def predict(_engine, _array):
            raise ValueError('native inference failed')

        backend, engines, events = self.lifecycle_backend(predict)
        kept = []
        try:
            backend.ocr_image(self.image)
        except ValueError as exc:
            kept.append(exc)
        # A failed page does not unload: the stage may continue with the next page.
        self.assertIsNotNone(engines[0]())
        self.assertIs(backend._engine, engines[0]())
        backend.close()
        self.assertIsNone(engines[0](), 'traceback frames must not pin the engine')
        self.assertEqual(events, ['engine-freed', 'lease-released'])
        self.assertEqual(str(kept[0]), 'native inference failed')
        self.assertIn('predict', ''.join(traceback.format_exception(kept[0])))

    def test_load_failure_does_not_retain_partial_engine_in_kept_error(self):
        from translation_agent.runtime import model_lease

        partial = []
        events = []

        class Engine:
            def __init__(self, **_kwargs):
                partial.append(weakref.ref(self, lambda _ref: events.append('engine-freed')))
                raise OSError('weights unreadable')

        real_release = model_lease.release_heavy_model_lease

        def release(purpose, lock):
            events.append('lease-released')
            real_release(purpose, lock)

        with patch.dict('sys.modules', {'paddleocr': types.SimpleNamespace(PaddleOCR=Engine)}), \
                patch.object(model_lease, 'release_heavy_model_lease', release):
            backend = self.backend()
            kept = []
            try:
                backend.ocr_image(self.image)
            except OSError as exc:
                kept.append(exc)
        self.assertIsNone(partial[0]())
        self.assertEqual(events, ['engine-freed', 'lease-released'])
        self.assertIsNone(backend._process_lock)

    def unreadable_images(self):
        corrupt = self.root / 'corrupt.png'
        corrupt.write_bytes(b'\x89PNG\r\n\x1a\n not an image')
        return (
            (self.root / 'missing.png', FileNotFoundError, 'missing.png'),
            (corrupt, Image.UnidentifiedImageError, 'corrupt.png'),
        )

    def test_unreadable_image_on_cold_backend_never_loads_or_takes_the_lease(self):
        for path, error, detail in self.unreadable_images():
            with self.subTest(image=path.name):
                backend, engines, events = self.lifecycle_backend(lambda _engine, _array: self.page_result())
                with self.assertRaisesRegex(error, detail):
                    backend.ocr_image(path)
                self.assertEqual(engines, [], 'no model load for an unreadable page')
                self.assertIsNone(backend._process_lock)
                lock = OutputDirectoryLock(self.root / '.translation-agent/paddleocr/cpu.lock')
                lock.acquire()  # the lease was never taken
                lock.release()
                backend.close()
                self.assertEqual(events, [])

    def test_unreadable_image_on_warm_backend_kept_error_does_not_retain_engine(self):
        for path, error, detail in self.unreadable_images():
            with self.subTest(image=path.name):
                backend, engines, events = self.lifecycle_backend(lambda _engine, _array: self.page_result())
                self.assertEqual(backend.ocr_image(self.image)[0], 'text')
                kept = []
                try:
                    backend.ocr_image(path)
                except error as exc:
                    kept.append(exc)  # the caller keeps the error and its traceback
                self.assertIn(detail, str(kept[0]), 'the real error is preserved')
                # A bad page does not unload: the stage may continue.
                self.assertIs(backend._engine, engines[0]())
                self.assertEqual(backend.ocr_image(self.image)[0], 'text')
                self.assertEqual(len(engines), 1, 'the warm engine is reused after a bad page')
                backend.close()
                self.assertIsNone(engines[0](), 'kept image error must not pin the engine')
                self.assertEqual(events, ['engine-freed', 'lease-released'])

    def test_success_path_reuses_one_engine_until_close(self):
        backend, engines, events = self.lifecycle_backend(lambda _engine, _array: self.page_result())
        self.assertEqual(backend.ocr_image(self.image)[0], 'text')
        self.assertEqual(backend.ocr_image(self.image)[0], 'text')
        self.assertEqual(len(engines), 1, 'the model is loaded once per stage')
        self.assertIs(backend._engine, engines[0]())
        backend.close()
        self.assertIsNone(engines[0]())
        self.assertEqual(events, ['engine-freed', 'lease-released'])

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
        with patch('translation_agent.ocr.paddle_native.readiness', return_value=(True, 'ready')), patch.dict('sys.modules', {'paddleocr': types.SimpleNamespace(PaddleOCR=factory)}):
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
        from translation_agent.cli.document_pipeline import build_parser, _spec_from_args
        from translation_agent.runtime.run_execution_service import _graph_request
        args = build_parser().parse_args(['run', 'input.pdf', '--phase', 'ocr', '--no-translate', '--no-verify',
            '--ocr-backend', BACKEND, '--paddle-native-variant', 'server', '--paddle-native-det-limit', '640'])
        spec = _spec_from_args(args)
        self.assertEqual(_graph_request(spec, tuple(spec.targets)).pipeline.ocr_backend, BACKEND)
        self.assertEqual(_graph_request(spec, tuple(spec.targets)).pipeline.paddle_native_variant, "server")
        argv = _graph_request(spec, tuple(spec.targets)).pipeline.to_argv()
        parsed = book.build_parser().parse_args(argv)
        self.assertEqual(parsed.ocr_backend, BACKEND)
        self.assertEqual(options_from_args(parsed).variant, 'server')
        self.assertEqual(options_from_args(parsed).det_limit, 640)

    def test_graph_and_execution_share_native_profile_identity(self):
        from translation_agent.pipeline.graph.book import _ocr_stage_semantics, _selected_model_profiles
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
