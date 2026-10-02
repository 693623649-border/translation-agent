import json
from pathlib import Path
import tempfile
import unittest
from architecture_dashboard import collect_snapshot, discover_workspaces, render_dashboard

class ArchitectureDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pipeline_graph").mkdir()
        (self.repo / "pipeline_graph/book.py").write_text('NODE_VERIFY = "core.publication.verify"\nNODE_KB = "core.publish.knowledge_base"', encoding="utf-8")

    def test_empty_snapshot_and_escaping(self):
        repo = self.repo
        data = collect_snapshot(repo)
        assert all(n['status'] == '未记录' for n in data['nodes'])
        assert not data['review']
        data['scope'] = '</script><img src=x onerror=alert(1)>'
        rendered = render_dashboard(data)
        assert '</script><img' not in rendered
        assert '\\u003c/script' in rendered
        assert 'textContent' in rendered

    def test_event_success_does_not_imply_release(self):
        repo = self.repo
        workspace = repo / 'outputs/book'
        graph = workspace / '.pipeline_graph'
        graph.mkdir(parents=True)
        (graph / 'state.json').write_text(json.dumps({'nodes': {'core.publication.verify': {'completed_at': '2026-10-01'}}}))
        (graph / 'events.jsonl').write_text(json.dumps({'node': 'core.publication.verify', 'event': 'node_failed', 'timestamp': '2026-10-02', 'error': 'SECRET'}) + '\n')
        data = collect_snapshot(repo, workspace=workspace)
        assert next(n for n in data['nodes'] if n['id'] == 'core.publication.verify')['status'] == '执行失败'
        assert not data['review']
        assert 'SECRET' not in json.dumps(data)

    def test_bad_json_and_escape_rejected(self):
        repo = self.repo
        workspace = repo / 'outputs/book'
        (workspace / '.pipeline_graph').mkdir(parents=True)
        (workspace / '.pipeline_graph/state.json').write_text('oops')
        assert collect_snapshot(repo, workspace=workspace)['warnings']
        with self.assertRaises(ValueError):
            collect_snapshot(repo, workspace=repo.parent)
        assert discover_workspaces(repo) == [workspace]

    def test_directory_limit(self):
        repo = self.repo
        (repo / 'outputs').mkdir()
        for i in range(5):
            folder = repo / 'outputs' / str(i)
            folder.mkdir()
            (folder / 'knowledge_base.jsonl').write_text('{}')
        (repo / 'outputs' / '.run-logs').mkdir()
        (repo / 'outputs' / 'unrelated').mkdir()
        assert len(discover_workspaces(repo, limit=2)) == 2
        assert len(discover_workspaces(repo)) == 5
    def test_sqlite_counts_and_benchmark_whitelist(self):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.repo / 'global_knowledge_base.sqlite3')) as db:
            db.executescript("CREATE TABLE meta(key TEXT,value TEXT); INSERT INTO meta VALUES ('secret','private'); CREATE TABLE workspaces(name TEXT); INSERT INTO workspaces VALUES ('book'); CREATE TABLE chunks(id TEXT); INSERT INTO chunks VALUES ('one');")
            db.commit()
        report = self.repo / 'work/kb-core-optimization'
        report.mkdir(parents=True)
        (report / 'benchmark.json').write_text(json.dumps({'equivalent': True, 'evaluation': {'passed': True}, 'token': 'private'}))
        data = collect_snapshot(self.repo)
        self.assertEqual(data['knowledge']['chunks'], 1)
        self.assertTrue(data['benchmark']['passed'])
        self.assertNotIn('private', json.dumps(data))
        index = next(node for node in data['nodes'] if node['id'] == 'global_index')
        self.assertEqual(index['status'], '索引存在（快照）')
        self.assertIn('1 块', index['evidence'])
        self.assertIn('未在本次刷新重新验证来源', index['evidence'])

    def test_cached_event_is_runtime_skipped_event(self):
        workspace = self.repo / 'outputs/book'
        graph = workspace / '.pipeline_graph'
        graph.mkdir(parents=True)
        (graph / 'events.jsonl').write_text(json.dumps({'node': 'core.publication.verify', 'event': 'node_skipped'}))
        data = collect_snapshot(self.repo, workspace=workspace)
        node = next(node for node in data['nodes'] if node['id'] == 'core.publication.verify')
        self.assertEqual(node['status'], '复用缓存记录')
        self.assertFalse(data['review'])
