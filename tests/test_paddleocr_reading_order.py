"""Reading-order regressions for local PaddleOCR without GPU dependencies."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy' / 'paddleocr'))
sys.path.insert(0, str(ROOT / 'tools'))
import batch_ocr as batch
import local_paddleocr_import as importer


class ReadingOrderTests(unittest.TestCase):
    def test_vertical_columns_and_fragment_jitter(self):
        texts = ['left-bottom', 'right-bottom', 'left-top', 'right-top']
        boxes = [[10, 80, 30, 150], [102, 80, 122, 150], [12, 10, 32, 70], [100, 10, 120, 70]]
        ordered, scores, output_boxes = batch._ordered_lines(texts, [.1, .2, .3, .4], boxes, 'vertical')
        self.assertEqual(ordered, ['right-top', 'right-bottom', 'left-top', 'left-bottom'])
        self.assertEqual(scores, [.4, .2, .3, .1])
        self.assertEqual(output_boxes[0], boxes[3])

    def test_vertical_two_regions_read_upper_before_lower(self):
        boxes = [[100, 0, 120, 100], [50, 0, 70, 95],
                 [100, 150, 120, 250], [50, 150, 70, 250]]
        self.assertEqual(batch._ordered_lines(['upper-right', 'upper-left', 'lower-right', 'lower-left'], [], boxes, 'vertical')[0],
                         ['upper-right', 'upper-left', 'lower-right', 'lower-left'])

    def test_short_column_does_not_split_continuous_neighbour(self):
        boxes = [[100, 0, 120, 40], [100, 90, 120, 150], [50, 0, 70, 150]]
        self.assertEqual(batch._ordered_lines(['right-top', 'right-bottom', 'left'], [], boxes, 'vertical')[0],
                         ['right-top', 'right-bottom', 'left'])

    def test_horizontal_default_unchanged(self):
        texts = ['bottom', 'right', 'left']
        boxes = [[0, 60, 100, 80], [110, 0, 210, 20], [0, 1, 100, 21]]
        self.assertEqual(batch._ordered_lines(texts, [], boxes)[0], ['left', 'right', 'bottom'])

    def test_auto_requires_clear_vertical_geometry(self):
        self.assertEqual(batch._resolve_direction([[0, 0, 20, 200], [30, 0, 50, 200]], 'auto'), 'vertical')
        self.assertEqual(batch._resolve_direction([[0, 0, 200, 20]], 'auto'), 'horizontal')
        self.assertEqual(batch._resolve_direction([[0, 0, 20, 20]], 'auto'), 'horizontal')
        self.assertEqual(batch._resolve_direction([], 'auto'), 'horizontal')

    def test_language_and_audit_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            notes = {'ordering_version': 3, 'reading_direction_requested': 'vertical', 'lines': [{'text': 'これは本文', 'score': .9, 'box': [0, 0, 10, 100]}]}
            batch._write_page(Path(folder), 1, 'これは本文', 'test-model', notes)
            path = batch._checkpoint_path(Path(folder), 1)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['language'], 'ja')
            self.assertTrue(batch._checkpoint_matches(path, 'test-model', 'vertical'))
            self.assertFalse(batch._checkpoint_matches(path, 'test-model', 'horizontal'))
            self.assertFalse(batch._checkpoint_matches(path, 'other-model', 'vertical'))
            batch._write_page(Path(folder), 1, '旧本文', 'test-model', {})
            self.assertFalse(batch._checkpoint_matches(path, 'test-model', 'vertical'))

    def test_manifest_and_model_change_with_direction(self):
        horizontal = importer._parse_args(['source.pdf', '--output-dir', 'out'])
        vertical = importer._parse_args(['source.pdf', '--output-dir', 'out', '--reading-direction', 'vertical'])
        self.assertNotEqual(importer._model_id(horizontal), importer._model_id(vertical))
        expected = importer._expected_manifest(horizontal, {'sha256': 'test'})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            path.write_text(json.dumps(expected), encoding='utf-8')
            self.assertTrue(importer._manifest_reuse_matches(path, expected))
            self.assertFalse(importer._manifest_reuse_matches(path, importer._expected_manifest(vertical, {'sha256': 'test'})))


if __name__ == '__main__':
    unittest.main()
