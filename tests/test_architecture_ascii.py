"""Contracts for the copyable terminal map and its accessible HTML projection."""
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

import architecture_dashboard as dashboard


class DashboardMarkup(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.buttons = []
        self.detail = {}
        self.scripts = []
        self.current_script = None
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "button" and "data-node" in attrs:
            self.buttons.append(attrs)
        if attrs.get("id") == "detail":
            self.detail = attrs
        if tag == "script":
            self.current_script = {"attrs": attrs, "text": ""}

    def handle_endtag(self, tag):
        if tag == "script" and self.current_script is not None:
            self.scripts.append(self.current_script)
            self.current_script = None

    def handle_data(self, data):
        if self.current_script is not None:
            self.current_script["text"] += data


def display_columns(text):
    """Independent width oracle for our chosen CJK/ASCII test repertoire."""
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
               for char in text)


class ArchitectureAsciiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        repo = Path(directory.name)
        (repo / "pipeline_graph").mkdir()
        # A sparse but real architecture: undefined nodes must not become controls.
        definitions = {
            "NODE_SOURCE": "core.source.inspect",
            "NODE_TRANSLATE": "core.pages.translate",
            "NODE_KB": "core.publish.knowledge_base",
            "NODE_VERIFY": "core.publication.verify",
            "NODE_STATUS": "core.pipeline.status",
        }
        (repo / "pipeline_graph/book.py").write_text(
            "\n".join(f'{key} = {value!r}' for key, value in definitions.items()),
            encoding="utf-8")
        self.snapshot = dashboard.collect_snapshot(repo)

    def test_export_is_character_art_without_html_or_ansi(self):
        text = dashboard.render_ascii(self.snapshot)
        for char in "┌─┐│└┘↓":
            self.assertIn(char, text)
        self.assertTrue(any(char in text for char in "├┴→"))
        for markup in ("<button", "<span", "<pre", "<script", "&lt;", "\x1b["):
            self.assertNotIn(markup, text)
        self.assertIn("TRANSLATION", text)
        self.assertIn("未记录", text)

    def test_cjk_combining_long_scope_and_status_keep_columns_aligned(self):
        baseline = dashboard.render_ascii(self.snapshot)
        self.snapshot["scope"] = "全角Ａ知识库e\u0301" * 150 + "\r\n\t\x1b[31m\x00\u202e"
        for node in self.snapshot["nodes"]:
            node["label"] = "知识库Ａe\u0301" * 80
            node["status"] = "自定义未知状态" * 80 + "\r\n\x00"
        text = dashboard.render_ascii(self.snapshot)
        base_widths = [display_columns(line) for line in baseline.splitlines()]
        widths = [display_columns(line) for line in text.splitlines()]
        self.assertEqual(widths, base_widths)
        self.assertLessEqual(max(widths), 118)
        for char in ("\r", "\t", "\x1b", "\x00", "\u202e"):
            self.assertNotIn(char, text)

    def test_malicious_snapshot_is_inert_json_and_preserved_as_data(self):
        malicious = '</script><img src=x onerror="alert(1)">\n\x1b[31m'
        self.snapshot["scope"] = malicious
        self.snapshot["nodes"][0]["label"] = malicious
        self.snapshot["nodes"][0]["evidence"] = malicious
        html = dashboard.render_dashboard(self.snapshot)
        self.assertNotIn("</script><img", html)
        markup = DashboardMarkup(html)
        payload = next(script for script in markup.scripts
                       if script["attrs"].get("id") == "snapshot")
        restored = json.loads(payload["text"])
        self.assertEqual(restored["scope"], malicious)
        self.assertEqual(restored["nodes"][0]["evidence"], malicious)
        self.assertEqual(len(markup.scripts), 2)
        self.assertNotIn("\x1b", html)

    def test_unicode_line_separators_cannot_split_character_rows(self):
        baseline = dashboard.render_ascii(self.snapshot)
        self.snapshot["nodes"][0]["label"] = "主\u2028编\u2029排"
        self.snapshot["events"] = [{"node": "输入\u2028来源", "event": "检查\u2029完成"}]
        text = dashboard.render_ascii(self.snapshot)
        self.assertNotIn("\u2028", text)
        self.assertNotIn("\u2029", text)
        self.assertEqual([display_columns(line) for line in text.splitlines()],
                         [display_columns(line) for line in baseline.splitlines()])

    def test_every_defined_node_has_accessible_control_and_safe_detail_sink(self):
        html = dashboard.render_dashboard(self.snapshot)
        markup = DashboardMarkup(html)
        self.assertEqual({button["data-node"] for button in markup.buttons},
                         {node["id"] for node in self.snapshot["nodes"]})
        for button in markup.buttons:
            self.assertIn("node-link", button.get("class", "").split())
            self.assertTrue(button.get("aria-label"))
            self.assertNotEqual(button.get("tabindex"), "-1")
            self.assertNotIn("disabled", button)
        self.assertEqual(markup.detail.get("role"), "status")
        self.assertEqual(markup.detail.get("aria-live"), "polite")
        script = "\n".join(item["text"] for item in markup.scripts
                           if item["attrs"].get("type") != "application/json")
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)

    def test_recorded_and_unknown_statuses_remain_available_without_fake_live_state(self):
        statuses = ["未记录", "执行完成记录", "复用缓存记录", "索引存在（快照）",
                    "执行失败", "开始记录（当前状态未确认）", "尚无此状态的解释"]
        for node, status in zip(self.snapshot["nodes"], statuses):
            node["status"] = status
        text = dashboard.render_ascii(self.snapshot)
        for marker in ("[?]", "[OK]", "[C]", "[DB]", "[!]", "[~]"):
            self.assertIn(marker, text)
        markup = DashboardMarkup(dashboard.render_dashboard(self.snapshot))
        controls = {button["data-node"]: button for button in markup.buttons}
        for node in self.snapshot["nodes"]:
            self.assertIn(node["status"], controls[node["id"]]["aria-label"])
        for fake in ("$2", "$10", "1,343", "0.97", "gpt-6.1-sol"):
            self.assertNotIn(fake, text)

    def test_unknown_status_keywords_do_not_imply_known_outcomes(self):
        for status in ("尚未开始", "缓存状态未知", "没有失败记录"):
            with self.subTest(status=status):
                for node in self.snapshot["nodes"]:
                    node["status"] = status
                text = dashboard.render_ascii(self.snapshot)
                self.assertIn("[?]", text)
                for marker in ("[~]", "[C]", "[!]"):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
