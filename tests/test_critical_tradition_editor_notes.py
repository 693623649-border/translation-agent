import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import book_pipeline as b
from work import critical_tradition_selection_edition as edition


class InlineEditorNoteTests(unittest.TestCase):
    def convert(self, page, text, count):
        record = b.PageRecord(page, ({110: "Aeschylus, Oreithia. ", 103: "Choerilus ", 191: "1665 "}.get(page, "")) + "Source note. [Ed.] " * count, translated_text=text)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "audit").mkdir()
            with patch.object(edition, "STAGING", root):
                summary = edition.separate_inline_editor_notes([record], {page})
            audit = json.loads((root / "audit/selection-editorial-footnotes.json").read_text(encoding="utf-8"))
        return record.translated_text, summary, audit

    def test_page64_nested_bracket_preserves_prose(self):
        text, summary, _ = self.convert(64, "选文是否如此？[战争背景说明。[编者注]]继续选文。", 1)
        self.assertEqual(summary["inline_editor_notes_separated"], 1)
        self.assertIn("选文是否如此？[^inline-editor-pdf0064-1]", text)
        self.assertIn("继续选文。", text)
        self.assertNotIn("]]", text)

    def test_page97_four_adjacent_notes(self):
        text, summary, audit = self.convert(97, "正文。\n\n^第一注。[编者注]第二注。[编者注]第三注。[编者注]第四注。[编者注]", 4)
        self.assertEqual(summary["inline_editor_notes_separated"], 4)
        self.assertEqual([item["content"] for item in audit["items"]], ["第一注。", "第二注。", "第三注。", "第四注。"])

    def test_page99_short_adjacent_note_and_following_prose(self):
        text, summary, _ = self.convert(99, "前面的选文。^人物背景。[编者注]“请鼓掌。”[编者注]后面的选文。", 2)
        self.assertEqual(summary["inline_editor_notes_separated"], 2)
        body = text.split("\n\n")[0]
        self.assertIn("前面的选文。", body)
        self.assertIn("后面的选文。", body)
        self.assertNotIn("人物背景", body)
        self.assertNotIn("请鼓掌", body)

    def test_page482_eleven_note_run(self):
        text, summary, _ = self.convert(482, "正文。\n\n*" + "".join(f"第{i}条说明。[编者注]" for i in range(11)), 11)
        self.assertEqual(summary["inline_editor_notes_separated"], 11)
        self.assertEqual(summary["ambiguous_editor_note_labels_preserved"], 0)

    def test_page110_reviewed_note_preserves_surrounding_prose(self):
        source = "选择作品的论证和诗句。'埃斯库罗斯，《俄瑞提亚》。[编者注]下一段选文。"
        text, summary, _ = self.convert(110, source, 1)
        self.assertEqual(summary["inline_editor_notes_separated"], 1)
        self.assertIn("选择作品的论证和诗句。[^inline-editor-pdf0110-1]", text)
        self.assertIn("下一段选文。", text.split("\n\n")[0])

    def test_reviewed_p103_and_p191_boundaries(self):
        text, summary, _ = self.convert(103, "原作论述[原文缺损]“科里卢斯是古代诗人。[编者注]", 1)
        self.assertEqual(summary["inline_editor_notes_separated"], 1)
        self.assertIn("原作论述[原文缺损]", text)
        text, summary, _ = self.convert(191, "原作正文。\n'1665年6月3日。[编者注]詹姆斯，约克公爵。[编者注]关于名字见第160页。[编者注]", 3)
        self.assertEqual(summary["inline_editor_notes_separated"], 3)

    def test_arbitrary_quotation_not_a_footnote_boundary(self):
        source = "论述中的‘人物’与普通引文。[编者注]"
        text, summary, _ = self.convert(110, source, 1)
        self.assertEqual(text, source)
        self.assertEqual(summary["ambiguous_editor_note_labels_preserved"], 1)

    def test_numeric_markers_and_standalone_apparatus(self):
        for marker in ("[3]", "3", "3. "):
            with self.subTest(marker=marker):
                _, summary, _ = self.convert(200, f"正文。\n\n{marker}注释说明。[编者注]", 1)
                self.assertEqual(summary["inline_editor_notes_separated"], 1)
        text, summary, _ = self.convert(201, "正文。\n\n---\n\n没有标号的说明。[编者注]\n第二条说明。[编者注]", 2)
        self.assertEqual(summary["inline_editor_notes_separated"], 2)

    def test_circled_and_explicit_note_markers(self):
        for marker in ("①", "②", "[脚注1]", "[脚注2]", "[脚注3]"):
            with self.subTest(marker=marker):
                _, summary, _ = self.convert(421 if marker in "①②" else 931, f"正文。\n{marker}注释说明。[编者注]", 1)
                self.assertEqual(summary["inline_editor_notes_separated"], 1)

    def test_editor_source_ocr_variants_exclude_author_labels(self):
        self.assertEqual(len(edition._SOURCE_EDITOR_LABEL.findall("[Ed.] [Ed. J [Ed.| [Ed. [Au.] [Editorial]")), 4)

    def test_bottom_geometry_requires_independent_note_line(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pages = root / "source/pages"
            pages.mkdir(parents=True)
            note = "参见克里斯蒂娃，第1075页。[编者注]"
            (pages / "page_1343.json").write_text(json.dumps({"translated_text": "完整正文。\n\n" + note}), encoding="utf-8")
            record = b.PageRecord(1343, "Source. [Ed. J", translated_text=note, notes=json.dumps({"lines": [
                {"text": "Body", "box": [10, 10, 100, 40]},
                {"text": "1 See Kristeva. [Ed. J", "box": [10, 850, 100, 880]},
                {"text": "1315", "box": [10, 950, 100, 980]},
            ]}))
            with patch.object(edition, "SOURCE_OUT", root / "source"), patch.object(edition, "ROOT", root):
                self.assertEqual(edition._standalone_editor_note_geometry(record), {note})
                record.notes = json.dumps({"lines": [{"text": "[Ed.]", "box": [10, 20, 100, 50]}, {"text": "footer", "box": [10, 950, 100, 980]}]})
                self.assertEqual(edition._standalone_editor_note_geometry(record), set())
                (pages / "page_1343.json").write_text(json.dumps({"translated_text": "原作正文仍在此处。" + note}), encoding="utf-8")
                record.notes = json.dumps({"lines": [{"text": "[Ed.]", "box": [10, 850, 100, 880]}, {"text": "footer", "box": [10, 950, 100, 980]}]})
                self.assertEqual(edition._standalone_editor_note_geometry(record), set())

    def test_reviewed_page_spans_require_source_and_exact_translation(self):
        for page, spans in edition._REVIEWED_EDITOR_SPANS.items():
            for source_anchor, note in spans:
                with self.subTest(page=page, note=note):
                    r = b.PageRecord(page, source_anchor + " [Ed.]", translated_text="原作正文。" + note + "[编者注]继续正文。")
                    body, recovered = edition._apply_reviewed_editor_spans(r)
                    self.assertEqual(len(recovered), 1)
                    self.assertIn("原作正文。", body)
                    self.assertIn("继续正文。", body)
                    self.assertNotIn(note, body)
                    r.text = "No matching source note. [Au.]"
                    self.assertEqual(edition._apply_reviewed_editor_spans(r), (r.translated_text, []))
        r = b.PageRecord(237, "Figures of speech. [Ed.]", translated_text="诗句修辞格。[编者注]")
        self.assertEqual(edition._apply_reviewed_editor_spans(r), (r.translated_text, []))

    def test_p1218_split_label_and_p1428_author_note_stay_separate(self):
        note = edition._REVIEWED_EDITOR_SPANS[1218][0][1]
        r = b.PageRecord(1218, "See the introduction to Structuralism [Ed. J", translated_text=note + "\n\n[编者注] 德里达并非以哲学史中熟悉视角的方式来关注这些有争议的问题。")
        body, notes = edition._apply_reviewed_editor_spans(r)
        self.assertEqual(len(notes), 1)
        self.assertIn("德里达并非以哲学史中熟悉视角", body)
        self.assertNotIn("[编者注]", body)
        note = edition._REVIEWED_EDITOR_SPANS[1428][0][1]
        r = b.PageRecord(1428, "Author explanation [Au.] Nicholas Vachel Lindsay [Ed. i", translated_text="马库斯的作者说明。[作者注] " + note + "[编者注]继续选文。")
        body, notes = edition._apply_reviewed_editor_spans(r)
        self.assertEqual(len(notes), 1)
        self.assertIn("马库斯的作者说明。[作者注]", body)
        self.assertIn("继续选文。", body)

    def test_p507_and_p511_relocate_note_to_proven_body_anchor(self):
        fixtures = [
            (507, 'The "Ode to Joy." [Ed.]', '席勒的一首诗，其后论述著名的《神曲》和《“欢乐颂”》。[编者注]', '著名的《神曲》和'),
            (511, '[Ed.] ^Cathexis is the investment of libido energy in an activity.', '贯注于他的玩耍世界，他仍然[编者注] ^贯注是指将力比多能量投入到一项活动中。', '贯注于他的玩耍世界'),
        ]
        fixtures.append((511, fixtures[1][1], fixtures[1][2].replace(" ^", " "), fixtures[1][3]))
        for page, source, translated, anchor in fixtures:
            with self.subTest(page=page):
                body, notes = edition._apply_reviewed_editor_spans(b.PageRecord(page, source, translated_text=translated))
                self.assertEqual(len(notes), 1)
                self.assertIn(anchor + "[^reviewed-editor-", body)
                self.assertNotIn("[编者注]", body)

    def test_p907_restores_three_notes_without_explanatory_noise(self):
        source = 'Beginning as end. [Ed.] Study of last things. [Ed.] The six preceding Greek terms mean form. [Ed.]'
        translated = 'arche也可以被称为telos；末世论；eidos、aletheia。\n（原文缺损：脚注部分错误说明）'
        body, notes = edition._apply_reviewed_editor_spans(b.PageRecord(907, source, translated_text=translated))
        self.assertEqual(len(notes), 3)
        self.assertNotIn("原文缺损", body)
        for anchor in ('telos', '末世论', 'aletheia'):
            self.assertIn(anchor + '[^reviewed-editor-', body)

    def test_reviewed_footer_repairs_preserve_main_prose(self):
        for page, printed, footer, title in [(1218, 1190, "II9O 马克思主义批评", "马克思主义批评"), (1428, 1400, "第1400页 女性主义文学批评", "女性主义文学批评")]:
            with self.subTest(page=page):
                record = b.PageRecord(page, "source", translated_text="选文论述。 " + footer)
                cleaned, removed = edition.strip_running_page_labels(record, printed, [title])
                self.assertEqual(cleaned.translated_text.strip(), "选文论述。")
                self.assertTrue(removed)
        record = b.PageRecord(1428, "source", translated_text="正文。 第1399页 女性主义文学批评")
        cleaned, removed = edition.strip_running_page_labels(record, 1400, ["女性主义文学批评"])
        self.assertIsNone(removed)
        self.assertEqual(cleaned.translated_text, record.translated_text)

    def test_p221_existing_reference_is_not_an_ocr_marker(self):
        original = "原作正文。^^ [^reviewed-editor-pdf0221-1] 《穆斯塔法》的说明。[编者注]贺拉斯说：后续选文。"
        text, summary, audit = self.convert(221, original, 1)
        self.assertEqual(text, original)
        self.assertEqual(summary["inline_editor_notes_separated"], 0)
        self.assertFalse(audit["items"])
        self.assertIn("贺拉斯说：后续选文。", text)
        self.assertFalse(list(edition._INLINE_EDITOR_NOTE_MARKER.finditer("[^reviewed-editor-pdf0221-1]")))

    def test_existing_markdown_definition_is_not_parsed_again(self):
        original = "选文[^reviewed-editor-pdf0221-1]。\n\n[^reviewed-editor-pdf0221-1]: [编者注] 已整理的说明。\n"
        text, summary, audit = self.convert(221, original, 1)
        self.assertEqual(text, original)
        self.assertEqual(summary["inline_editor_notes_separated"], 0)
        self.assertEqual(summary["ambiguous_editor_note_labels_preserved"], 0)

    def test_p221_work_list_stays_in_body_when_author_note_is_removed(self):
        anchor, note = edition._REVIEWED_EDITOR_SPANS[221][-1]
        prose = "《罗得岛之围》、《穆斯塔法》、《印第安女王》和《印第安皇帝》。"
        record = b.PageRecord(221, anchor + " [Ed.]", translated_text=prose + note + "[编者注]贺拉斯说：继续选文。")
        body, notes = edition._apply_reviewed_editor_spans(record)
        self.assertEqual(len(notes), 1)
        self.assertIn(prose, body)
        self.assertIn("贺拉斯说：继续选文。", body)
        self.assertNotIn("奥雷里伯爵所作", body)
        self.assertIn("奥雷里伯爵所作", notes[0]["content"])

    def test_p207_latin_translation_becomes_body_with_citation_only_note(self):
        quote = "起初我们渴望超越那些我们认为是我们领袖的人，但当我们对超越他们甚至与他们平等感到绝望时，我们的热情随着希望而减弱；当它无法赶上时，它就不再跟随；抛开我们无法超越的东西，我们为我们的努力寻找另一个出口。"
        record = b.PageRecord(207, "Sed ut primo Velleius Paterculus", translated_text='前文。Sed ut primo conquirimus。^"' + quote + '"维莱乌斯·帕特库鲁斯，《罗马史》1:17。[编者注] 论戏剧诗 179')
        body, notes = edition._reviewed_language_cleanup(record)
        self.assertIn(quote, body)
        self.assertNotIn("Sed ut primo", body)
        self.assertEqual(notes[0]["content"], "维莱乌斯·帕特库鲁斯，《罗马史》1:17。")
        self.assertNotIn("论戏剧诗 179", body)

    def test_p1150_slogan_translated_once_without_duplicate_note(self):
        record = b.PageRecord(1150, "Fiat ars—pereat mundus", translated_text='前文。“Fiat ars—pereat mundus，”^^法西斯主义说。后文。“‘”让艺术存在，让世界毁灭。[编者注]')
        body, notes = edition._reviewed_language_cleanup(record)
        self.assertEqual(body.count("让艺术存在，让世界毁灭"), 1)
        self.assertNotIn("Fiat", body)
        self.assertNotIn("[编者注]", body)
        self.assertIn("后文。", body)
        self.assertEqual(notes, [])

    def test_p1371_three_note_types_and_anchors(self):
        record = b.PageRecord(1371, "Two Greek words for love Morrison, Beloved, Pt II", translated_text='正文中的自爱，厄洛斯与阿加佩。宠儿是谁？继续正文。宠儿是谁？*\'Ibid., p. 324. [Au.] *^两个表示爱的希腊词——第一个与性欲相关，第二个与灵性相关。[Ed.] \'^Morrison, Beloved, Pt II, pp. 200-17. lAu.] BHABHA LOCATIONS OF CULTURE 1343')
        body, notes = edition._reviewed_language_cleanup(record)
        self.assertEqual([note["kind"] for note in notes], ["author", "editor", "author"])
        self.assertIn("自爱[^reviewed-language-pdf1371-1]", body)
        self.assertIn("厄洛斯与阿加佩[^reviewed-language-pdf1371-2]", body)
        self.assertTrue(body.endswith("宠儿是谁？[^reviewed-language-pdf1371-3]"))
        for fragment in ("Ibid", "Morrison", "BHABHA", "[Ed.]", "[Au.]"):
            self.assertNotIn(fragment, body)
        self.assertIn("继续正文。", body)

    def test_markerless_short_mixed_prose_not_guessed(self):
        source = "作品正文和注释界限未知。[编者注]"
        text, summary, _ = self.convert(202, source, 1)
        self.assertEqual(text, source)
        self.assertEqual(summary["ambiguous_editor_note_labels_preserved"], 1)

if __name__ == "__main__":
    unittest.main()
