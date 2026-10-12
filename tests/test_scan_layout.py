import json
import hashlib
import unittest
from scan_layout import prepare_scan_pages, reader_layout_issues


class ScanLayoutTests(unittest.TestCase):
    def test_numeric_body_section_is_not_another_chapters_folio(self):
        lines = [{'text':'本文甲全文を保存する。','box':[500,130,530,1100]},
                 {'text':'本文乙全文を保存する。','box':[450,130,480,1100]},
                 {'text':'2','box':[100,1300,140,1320]}]
        page = {'pdf_page':20,'text':'raw','notes':{'engine':'paddleocr-local',
                'reading_direction':'vertical','lines':lines}}
        self.assertEqual(reader_layout_issues([page], {'body':'# 正常章节\n\n2\n\n小节正文。'},
                                             {'body':(8,16)}), [])

    def test_reader_rejects_footer_fragments_but_keeps_chapter_title_and_single_number(self):
        page = {'pdf_page': 1, 'text': 'raw', 'notes': {'engine': 'paddleocr-local',
                'reading_direction': 'vertical', 'lines': [
            {'text':'本文甲全文を保存する。','box':[500,130,530,1100]},
            {'text':'本文乙全文を保存する。','box':[450,130,480,1100]},
            {'text':'二','box':[380,1300,400,1320]},
            {'text':'三','box':[400,1300,420,1320]},
            {'text':'章タイトル','box':[450,1300,650,1320]},
            {'text':'65','box':[100,1300,140,1320]},
        ]}}
        self.assertEqual(reader_layout_issues([page], {'ok':'# 章タイトル\n\n二\n\n真正正文。'}), [])
        issues = reader_layout_issues([page], {'bad':'# 正常章题\n\n正文。\n\n二\n\n三\n\n65\n\n章タイトル'})
        self.assertEqual({i['code'] for i in issues}, {'reader_footer_fragments','reader_header_footer_copy'})

    def test_unique_fragmented_footer_is_structure_not_body(self):
        lines = [
            {'text': '本文甲全文を保存する。', 'box': [500, 130, 530, 1100]},
            {'text': '本文乙全文を保存する。', 'box': [450, 130, 480, 1100]},
            {'text': '図の説明', 'box': [300, 1150, 500, 1180]},
            {'text': '近くの図注を保存', 'box': [300, 1250, 500, 1270]},
            {'text': 'って', 'box': [400, 1300, 445, 1320]},
            {'text': 'め', 'box': [380, 1300, 400, 1320]},
            {'text': '193', 'box': [100, 1300, 140, 1320]},
        ]
        raw = '\n'.join(x['text'] for x in lines)
        page = {'pdf_page': 1, 'text': raw, 'notes': {'engine': 'paddleocr-local',
                'reading_direction': 'vertical', 'lines': lines}}
        result = prepare_scan_pages([page])[1]
        self.assertIn('図の説明', result['text'])
        self.assertIn('近くの図注を保存', result['text'])
        self.assertNotIn('って', result['text'])
        self.assertEqual(page['text'], raw)
        self.assertEqual([x['text'] for x in result['structure'] if x['role']=='running_header'], ['って','め'])

    def test_keeps_body_and_captions_but_separates_repeat_header_and_folio(self):
        def page(n):
            lines = [
                {"text": "章タイトル", "box": [800, 130, 840, 600]},
                {"text": "本文甲全文を保存する。", "box": [500, 150, 530, 1100]},
                {"text": "本文乙全文を保存する。", "box": [450, 130, 480, 1100]},
                {"text": "本文丙全文を保存する。", "box": [400, 130, 430, 1100]},
                {"text": "図の説明", "box": [300, 1150, 500, 1180]},
                {"text": "繰り返す書眉", "box": [300, 1300, 550, 1320]},
                {"text": str(n), "box": [800, 1300, 820, 1320]},
            ]
            return {"pdf_page": n, "text": "\n".join(x["text"] for x in lines),
                    "notes": json.dumps({"engine": "paddleocr-local", "reading_direction": "vertical", "lines": lines})}
        records = [page(1), page(2)]
        prepared = prepare_scan_pages(records)
        self.assertIn("図の説明", prepared[1]["text"])
        self.assertNotIn("繰り返す書眉", prepared[1]["text"])
        self.assertNotIn("章タイトル", prepared[1]["text"])
        self.assertEqual(records[0]["text"].splitlines()[-1], "1")
        self.assertIn("folio", [x["role"] for x in prepared[1]["structure"]])

    def test_non_scan_and_human_correction_are_not_geometry_rewritten(self):
        record = {"pdf_page": 1, "text": "raw", "proofread_text": "human",
                  "proofread_source_sha256": hashlib.sha256(b"raw").hexdigest()}
        self.assertEqual(prepare_scan_pages([record])[1]["text"], "human")
