import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import fitz
from book_pipeline import PageRecord, save_page_record
from tools.jp_vertical_kb_plugin import vertical_ingest as v


class VerticalIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.pdf'
        doc = fitz.open()
        for _ in range(4):
            doc.new_page()
        doc.save(self.source)
        doc.close()
        self.ws = self.root / 'work'
        self.args = dict(source=str(self.source), workspace=str(self.ws), title='测试书', author='作者')
        v.prepare(self.args)

    def populate(self):
        for n in range(1, 5):
            record = PageRecord(n, 'これは文学についての文章です。', language='ja', translated_text='这是一段关于文学的文字。', translation_provider='provider', translation_model='model' + str(n), translation_target_language='zh-CN', translation_prompt_version='test-v1', translation_fingerprint='test-fingerprint', notes=json.dumps(dict(ordering_version=3, reading_direction='vertical', lines=[dict(text='文字', score=.99, box=[0, 0, 10, 100])])), ocr_model='paddleocr-local/test')
            record.translation_source_sha256 = record.effective_text_sha256
            save_page_record(self.ws, record)
        toc = dict(book_title='测试书', author='作者', page_offset=0, printed_pages_per_pdf_page=1, entries=[dict(id='one', index=1, level=1, kind='chapter', title='文学', source_title='文學', pdf_page=1)])
        v.write(self.ws / 'toc.json', toc)
        state, manifest, records = v.snapshot(self.ws)
        review = dict(source_sha256=state['source_sha256'], toc_sha256=state['toc_sha256'], page_records_sha256=state['page_records_sha256'], title='测试书', author='作者', reviewer='test reviewer', note='Inspected synthetic pages', content_starts=[dict(pdf_page=1, note='heading image')], excluded_pages=[])
        v.write(self.ws / 'source-review.json', review)
        return state, manifest, records

    def gate(self):
        state, manifest, records = v.snapshot(self.ws)
        return v.source_gate(self.ws, state, manifest, records, self.ws / 'toc.json', self.ws / 'source-review.json')

    def test_prepare_identity_and_dirty_workspace(self):
        self.assertEqual(v.prepare(self.args)['status'], 'prepared')
        with self.assertRaisesRegex(ValueError, 'another source'):
            v.prepare({**self.args, 'title': 'different'})
        dirty = self.root / 'dirty'
        dirty.mkdir()
        (dirty / 'keep.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'nonempty'):
            v.prepare({**self.args, 'workspace': str(dirty)})
        self.assertEqual((dirty / 'keep.txt').read_text(), 'keep')

    def test_exact_disjoint_pages(self):
        self.populate()
        with patch.object(v, 'execute') as execute:
            v.run(dict(workspace=str(self.ws), stage='translate', pages=[4, 1, 2]))
        self.assertIn('--config', execute.call_args.args[0])
        bounds = [(c.args[0][c.args[0].index('--start-page')+1], c.args[0][c.args[0].index('--end-page')+1]) for c in execute.call_args_list]
        self.assertEqual(bounds, [('1', '2'), ('4', '4')])
        self.assertNotIn('--force', execute.call_args.args[0])
        for invalid in [[], [True], [0], [5], '1-4']:
            with self.assertRaises(ValueError):
                v.ranges(invalid, 4)

    def test_mixed_models_and_source_hash_independent_of_translation(self):
        before, _, records = self.populate()
        self.gate()
        records[0].translated_text += '补充。'
        save_page_record(self.ws, records[0])
        after, _, _ = v.snapshot(self.ws)
        self.assertEqual(before['page_records_sha256'], after['page_records_sha256'])
        self.assertNotEqual(before['artifact_sha256'], after['artifact_sha256'])
        self.gate()

    def test_stale_source_review(self):
        _, _, records = self.populate()
        records[0].text += '追加'
        save_page_record(self.ws, records[0])
        with self.assertRaisesRegex(ValueError, 'page_records_sha256'):
            self.gate()

    def test_translation_failures(self):
        for change, message in [('empty', 'translations'), ('stale', 'translations'), ('foreign', 'foreign'), ('ellipsis', 'ellipsis')]:
            with self.subTest(change=change):
                _, _, records = self.populate()
                record = records[0]
                if change == 'empty': record.translated_text = ''
                if change == 'stale': record.translation_source_sha256 = 'old'
                if change == 'foreign': record.translated_text += '\nThis entire paragraph remains in English and has not been translated into Chinese at all.'
                if change == 'ellipsis': record.translated_text += '……'
                save_page_record(self.ws, record)
                with self.assertRaisesRegex(ValueError, message):
                    self.gate()

    def test_geometry_missing_and_source_replaced(self):
        _, _, records = self.populate()
        records[0].notes = '{}'
        save_page_record(self.ws, records[0])
        with self.assertRaisesRegex(ValueError, 'geometry'):
            self.gate()
        self.source.write_bytes(b'changed')
        self.assertFalse(v.snapshot(self.ws)[0]['source_current'])
        with self.assertRaisesRegex(ValueError, 'Source PDF'):
            v.run(dict(workspace=str(self.ws), stage='ocr', pages=[1]))

    def test_no_registration_without_release_receipt(self):
        self.populate()
        with patch.object(v, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'receipt'):
                v.run(dict(workspace=str(self.ws), stage='register'))
        execute.assert_not_called()

    def test_compile_verify_register_review_lifecycle(self):
        self.populate()
        def stage(command, timeout):
            if '--phase' in command:
                (self.ws / 'test.docx').write_bytes(b'synthetic-docx')
                audit = self.ws / 'audit'
                audit.mkdir(exist_ok=True)
                v.write(audit / 'release-report.json', dict(release_ready=True, mode='full', docx_render_required=True))
            elif 'register' in command:
                (self.ws / 'knowledge_base.rag.json').write_text('{}')
                (self.ws / 'knowledge_base.vectors.jsonl').write_text('{}')
            elif '-DocxPath' in command:
                render_dir = Path(command[command.index('-OutputDirectory') + 1])
                render_dir.mkdir(parents=True, exist_ok=True)
                pdf = fitz.open()
                pdf.new_page()
                pdf.save(render_dir / 'test.pdf')
                pdf.close()
        with patch.object(v, 'execute', side_effect=stage) as execute:
            v.run(dict(workspace=str(self.ws), stage='compile'))
            v.run(dict(workspace=str(self.ws), stage='verify'))
            receipt = v.read(self.ws / v.MANIFEST)['verified']
            layout = dict(artifact_sha256=receipt['artifact_sha256'], rendered_pdf=receipt['rendered_pdf'], rendered_pdf_sha256=receipt['rendered_pdf_sha256'], reviewer='reviewer', note='contact sheet plus detailed inspection', pages=[dict(pdf_page=1, note='full resolution checked')])
            layout_file = self.root / 'layout.json'
            v.write(layout_file, layout)
            result = v.run(dict(workspace=str(self.ws), stage='register', layout_review_file=str(layout_file)))
            self.assertEqual(result['status'], 'passed')
            self.assertTrue(v.snapshot(self.ws)[0]['registration_current'])
            (self.ws / 'knowledge_base.vectors.jsonl').write_text('changed')
            self.assertFalse(v.snapshot(self.ws)[0]['registration_current'])
            self.assertEqual(execute.call_args.args[0][-2:], ['register', str(self.ws.resolve())])
            (self.ws / 'test.docx').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                v.run(dict(workspace=str(self.ws), stage='register', layout_review_file=str(layout_file)))

    def test_unreadable_is_not_blank(self):
        _, _, records = self.populate()
        records[0].text = '[无法辨认]'
        save_page_record(self.ws, records[0])
        self.assertTrue(any('Unreadable' in x for x in v.snapshot(self.ws)[0]['issues']))

    def test_empty_source_blocks_and_status_is_inspection(self):
        _, _, records = self.populate()
        records[0].text = ''
        save_page_record(self.ws, records[0])
        state = v.snapshot(self.ws)[0]
        self.assertEqual(state['status'], 'inspected')
        self.assertFalse(state['publication_ready'])
        with self.assertRaisesRegex(ValueError, 'Empty OCR source'):
            self.gate()

    def test_translation_provenance_is_required(self):
        _, _, records = self.populate()
        records[0].translation_prompt_version = ''
        save_page_record(self.ws, records[0])
        with self.assertRaisesRegex(ValueError, 'translations'):
            self.gate()

    def test_bookmarked_pdf_binds_release_hash(self):
        self.populate()
        before = v.snapshot(self.ws)[0]['artifact_sha256']
        publication = self.ws / 'book_带目录.pdf'
        publication.write_bytes(b'bookmark artifact')
        self.assertNotEqual(before, v.snapshot(self.ws)[0]['artifact_sha256'])

    def test_exclusion_of_real_text_is_rejected(self):
        self.populate()
        review_path = self.ws / 'source-review.json'
        review = v.read(review_path)
        review['excluded_pages'] = [dict(pdf_page=2, note='divider')]
        v.write(review_path, review)
        with self.assertRaisesRegex(ValueError, 'Excluded nonprose'):
            self.gate()


if __name__ == '__main__':
    unittest.main()
