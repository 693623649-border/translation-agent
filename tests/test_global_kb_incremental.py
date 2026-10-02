import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import global_knowledge_base as kb


class IncrementalSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'outputs'
        self.root.mkdir()
        self.db = self.root.parent / 'index.sqlite3'

    def book(self, name, content='自然主义正文内容。'):
        folder = self.root / name
        folder.mkdir(exist_ok=True)
        path = folder / 'knowledge_base.jsonl'
        row = dict(id=hashlib.sha1(name.encode()).hexdigest(), title='正文',
                   chapter_id='chapter', chapter_order=1, content=content)
        path.write_text(json.dumps(row, ensure_ascii=False)+'\n', encoding='utf-8')
        return path

    def sync(self, **kwargs):
        return kb.sync_outputs(self.root, self.db, require_chinese=False, **kwargs)

    def test_unchanged_does_not_ingest_or_write(self):
        self.book('one')
        self.sync()
        before = self.db.read_bytes()
        with patch.object(kb, '_ingest_workspace', side_effect=AssertionError('unchanged')):
            result = self.sync()
        self.assertEqual(result['mode'], 'unchanged')
        self.assertEqual(self.db.read_bytes(), before)

    def test_only_changed_book_and_preserved_mtime_edit(self):
        path = self.book('one')
        self.book('two')
        self.sync()
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace('自然'.encode(), '社会'.encode()))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with patch.object(kb, '_ingest_workspace', wraps=kb._ingest_workspace) as ingest:
            result = self.sync()
        self.assertEqual(ingest.call_count, 1)
        self.assertEqual(result['updated_workspaces'], ['one'])
        self.assertEqual(result['reused_workspaces'], ['two'])
        self.assertEqual(kb.search('社会', db_path=self.db)[0]['workspace'], 'one')

    def test_added_and_deleted_workspaces(self):
        self.book('one')
        self.sync()
        self.book('two')
        shutil.rmtree(self.root / 'one')
        result = self.sync()
        self.assertEqual(result['deleted_workspaces'], ['one'])
        self.assertEqual(result['updated_workspaces'], ['two'])
        self.assertEqual([x['workspace'] for x in kb.search('自然', db_path=self.db)], ['two'])
        shutil.rmtree(self.root / 'two')
        self.assertEqual(self.sync()['workspaces'], 0)

    def test_failed_incremental_preserves_exact_database_bytes(self):
        path = self.book('one')
        self.sync()
        before = self.db.read_bytes()
        path.write_text('invalid', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.sync()
        self.assertEqual(self.db.read_bytes(), before)

    def test_incremental_matches_full_rebuild_including_weights_and_short_search(self):
        self.book('one')
        self.book('two')
        self.sync()
        path = self.book('one', '自然社会自然主义正文内容。')
        row = json.loads(path.read_text(encoding='utf-8'))
        (path.parent / 'knowledge_base.apparatus.json').write_text(json.dumps({
            'schema_version': 1, 'documents_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'annotations': {row['id']: {'is_apparatus': True, 'default_weight': 0.25}}
        }), encoding='utf-8')
        self.sync()
        def logical():
            with closing(sqlite3.connect(self.db)) as db:
                return {table: db.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
                        for table in ('workspaces', 'source_files', 'assets', 'chunks')}
        before = logical()
        hits = kb.search('自然', db_path=self.db)
        self.assertEqual(self.sync(full_rebuild=True)['mode'], 'full')
        self.assertEqual(logical(), before)
        self.assertEqual(kb.search('自然', db_path=self.db), hits)

    def test_unlisted_chapter_addition_is_not_reused(self):
        path = self.book('one')
        self.sync()
        (path.parent / 'chapters').mkdir()
        (path.parent / 'chapters' / 'unexpected.md').write_text('正文', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Unlisted chapter'):
            self.sync()

    def test_gate_change_forces_validation(self):
        self.book('one', 'This is untranslated English text about society and nature.')
        self.sync()
        with self.assertRaisesRegex(ValueError, 'Chinese-language quality gate'):
            kb.sync_outputs(self.root, self.db, require_chinese=True)

    def test_schema_upgrade_rebuilds(self):
        self.book('one')
        self.sync()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('PRAGMA user_version = 3')
        self.assertEqual(self.sync()['mode'], 'full')

    def test_report_dependency_and_asset_changes_are_tracked(self):
        path = self.book('one')
        audit = path.parent / 'audit'
        audit.mkdir()
        (audit / 'release-report.json').write_text('{"status":"passed"}', encoding='utf-8')
        asset = path.parent / 'book.docx'
        asset.write_bytes(b'first asset')
        self.sync()
        (audit / 'semantic-review.json').write_text('{}', encoding='utf-8')
        with patch.object(kb, '_sha256_file', wraps=kb._sha256_file) as digest:
            self.assertEqual(self.sync()['updated_workspaces'], ['one'])
        self.assertEqual(sum(call.args[0] == asset for call in digest.call_args_list), 1)
        asset.write_bytes(b'other asset')
        self.assertEqual(self.sync()['updated_workspaces'], ['one'])

    def test_change_while_ingesting_keeps_old_database(self):
        path = self.book('one')
        self.sync()
        before = self.db.read_bytes()
        self.book('one', '社会正文内容。')
        original_load = kb._load_kb
        def mutate_after_read(source):
            result = original_load(source)
            source.write_bytes(source.read_bytes() + b'\n')
            return result
        with patch.object(kb, '_load_kb', side_effect=mutate_after_read):
            with self.assertRaisesRegex(ValueError, 'Sources changed'):
                self.sync()
        self.assertEqual(self.db.read_bytes(), before)

    def test_database_in_outputs_root_does_not_invalidate_source_snapshot(self):
        self.book('one')
        self.db = self.root / 'index.sqlite3'
        self.sync()
        self.assertEqual(self.sync()['mode'], 'unchanged')

    def test_normalization_change_forces_rebuild(self):
        self.book('one')
        self.sync()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE meta SET value='obsolete' WHERE key='search_normalization'")
        self.assertEqual(self.sync()['mode'], 'full')

    def test_manifest_chapter_extensions_match_ingestion_contract(self):
        for suffix in ('.MD', '.txt'):
            with self.subTest(suffix=suffix):
                name = 'extension_' + suffix[1:]
                path = self.book(name)
                chapters = path.parent / 'chapters'
                chapters.mkdir()
                filename = '001' + suffix
                (chapters / filename).write_text('# 正文\n\n自然主义章节正文。', encoding='utf-8')
                (path.parent / 'chapters.json').write_text(json.dumps([{
                    'id': 'chapter', 'sequence': 1, 'display_title': '正文',
                    'filename': filename,
                }], ensure_ascii=False), encoding='utf-8')
                self.sync()
                with patch.object(kb, '_ingest_workspace', side_effect=AssertionError('unchanged')):
                    self.assertEqual(self.sync()['mode'], 'unchanged')
                (chapters / filename).write_text('# 正文\n\n社会发展章节正文。', encoding='utf-8')
                self.assertEqual(self.sync()['updated_workspaces'], [name])
                hits = kb.search('社会', db_path=self.db, scope='archive', workspace=name)
                self.assertEqual(len(hits), 1)

    @unittest.skipUnless(os.name == 'nt', 'Windows path aliases are case-insensitive')
    def test_uppercase_fixed_dependencies_and_page_directory_match_ingestion(self):
        path = self.book('aliases')
        folder = path.parent
        chapters = folder / 'chapters'
        chapters.mkdir()
        (chapters / '001.md').write_text('# 正文\n\n自然主义正文。', encoding='utf-8')
        (folder / 'chapters.json').write_text(json.dumps([{
            'id': 'chapter', 'sequence': 1, 'display_title': '正文', 'filename': '001.md',
        }]), encoding='utf-8')
        path.rename(folder / 'KNOWLEDGE_BASE.JSONL')
        (folder / 'chapters.json').rename(folder / 'CHAPTERS.JSON')
        pages = folder / 'PAGES'
        pages.mkdir()
        (pages / 'PAGE_0001.JSON').write_text(json.dumps({
            'pdf_page': 1, 'text': '社会正文。', 'language': 'zh',
        }, ensure_ascii=False), encoding='utf-8')
        self.sync()
        self.assertTrue(kb.verify_sources(self.db)['current'])
        self.assertEqual(self.sync()['mode'], 'unchanged')
        self.assertTrue(kb.search('社会', db_path=self.db, scope='pages'))


if __name__ == '__main__':
    unittest.main()
