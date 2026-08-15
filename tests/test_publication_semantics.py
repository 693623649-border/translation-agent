from __future__ import annotations

import unittest

from publication_semantics import (
    append_markdown_footnotes,
    markdown_footnote_contract_sha256,
    markdown_footnotes_to_docx_markers,
    parse_markdown_footnotes,
    reconstruct_adjacent_page_footnotes,
    reconstruct_page_footnotes,
    semantic_audit_summary,
    strip_markdown_footnote_section_headings,
)


class PublicationSemanticsTests(unittest.TestCase):
    def test_standalone_heading_before_markdown_definitions_is_removed(self) -> None:
        source = (
            "正文。[^legacy]\n\n"
            "注释：\n\n"
            "[^legacy]: 已审定定义。\n"
        )

        cleaned = strip_markdown_footnote_section_headings(source)

        self.assertNotIn("\n注释：\n", cleaned)
        self.assertIn("[^legacy]: 已审定定义。", cleaned)

    def test_footnote_heading_cleaner_preserves_prose_and_unproven_heading(self) -> None:
        prose = "这是评论性的注释：不得删除。\n\n[^legacy]: 定义。\n"
        unproven = "注释：\n\n这里仍是正文。\n"

        self.assertEqual(strip_markdown_footnote_section_headings(prose), prose)
        self.assertEqual(
            strip_markdown_footnote_section_headings(unproven),
            unproven,
        )

    def test_page_local_note_is_moved_only_with_one_proven_reference(self) -> None:
        page = (
            "正文在这里引用资料。[1]\n\n"
            "下一段仍属于正文。\n\n"
            "[1] Author, Book Title, p. 3.\n"
            "12"
        )

        result = reconstruct_page_footnotes(page, source_page="pdf-0012-physical-01")

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].stable_id, "pdf-0012-physical-01-n1")
        self.assertEqual(result.footnotes[0].text, "Author, Book Title, p. 3.")
        self.assertIn("[^pdf-0012-physical-01-n1]", result.body)
        self.assertNotIn("[1] Author", result.body)

    def test_missing_reference_is_retained_and_blocks_release(self) -> None:
        page = "没有可见脚注标记的正文。\n\n〔1〕来源说明，第3页。\n12"

        result = reconstruct_page_footnotes(page, source_page="pdf-0012-physical-01")

        self.assertTrue(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertIn("〔1〕来源说明", result.body)
        self.assertEqual(
            {issue.code for issue in result.issues},
            {"semantic_footnote_reference_missing"},
        )

    def test_multiple_notes_on_one_page_keep_distinct_reference_landings(self) -> None:
        page = (
            "第一条正文引用。[1]\n\n"
            "第二条正文引用。[2]\n\n"
            "[1] First source.\n"
            "[2] Second source.\n"
            "12"
        )

        result = reconstruct_page_footnotes(page, source_page="p12")

        self.assertFalse(result.release_blocked)
        self.assertEqual(
            [item.stable_id for item in result.footnotes],
            ["p12-n1", "p12-n2"],
        )
        self.assertEqual(
            [item.text for item in result.footnotes],
            ["First source.", "Second source."],
        )
        self.assertIn("第一条正文引用。[^p12-n1]", result.body)
        self.assertIn("第二条正文引用。[^p12-n2]", result.body)
        self.assertNotIn("[1] First source", result.body)
        self.assertNotIn("[2] Second source", result.body)

    def test_numeric_note_stops_before_existing_markdown_definitions(self) -> None:
        page = (
            "正文引用最后一条页注〔90〕，另有两条既存尾注"
            "[^legacy-part2-1][^legacy-part2-2]。\n\n"
            "〔90〕本页第九十条注释。\n\n"
            "[^legacy-part2-1]: 第一条既存尾注。\n\n"
            "[^legacy-part2-2]: 第二条既存尾注。"
        )

        result = reconstruct_page_footnotes(page, source_page="pdf-0173-physical-01")
        rendered = append_markdown_footnotes(result.body, result.footnotes)
        inventory = parse_markdown_footnotes(rendered)

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].text, "本页第九十条注释。")
        self.assertTrue(inventory.valid)
        self.assertEqual(
            {note_id for note_id, _text in inventory.definitions},
            {
                "pdf-0173-physical-01-n90",
                "legacy-part2-1",
                "legacy-part2-2",
            },
        )

    def test_equivalent_bracket_styles_resolve_the_same_note_label(self) -> None:
        cases = (
            ("正文以圆括号引用资料。(1)\n\n[1] 第一条来源。", "(1)"),
            ("正文以方括号引用资料。[1]\n\n〔1〕第二条来源。", "[1]"),
            ("正文以全角圆括号引用。（1）\n\n[1] 第三条来源。", "（1）"),
            ("正文以全角方括号引用。［1］\n\n〔1〕第四条来源。", "［1］"),
        )

        for index, (page, original_marker) in enumerate(cases, start=1):
            with self.subTest(original_marker=original_marker):
                result = reconstruct_page_footnotes(page, source_page=f"p{index}")
                self.assertFalse(result.release_blocked)
                self.assertEqual(len(result.footnotes), 1)
                self.assertNotIn(original_marker, result.body)
                self.assertIn(f"[^p{index}-n1]", result.body)

    def test_full_width_square_definition_is_reconstructed(self) -> None:
        result = reconstruct_page_footnotes(
            "正文中的来源。[1]\n\n［1］原书页底注释。",
            source_page="pdf-0012-physical-01",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].text, "原书页底注释。")
        self.assertIn("[^pdf-0012-physical-01-n1]", result.body)
        self.assertNotIn("［1］", result.body)

    def test_spaced_square_definition_is_reconstructed(self) -> None:
        result = reconstruct_page_footnotes(
            "正文中的来源。[1]\n\n[ 1 ] 原书页底注释。",
            source_page="pdf-0013-physical-01",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertIn("[^pdf-0013-physical-01-n1]", result.body)
        self.assertNotIn("[ 1 ]", result.body)

    def test_equivalent_bracket_styles_remain_ambiguous_with_two_landings(self) -> None:
        page = "正文先引用[1]，稍后又引用(1)。\n\n〔1〕来源说明。"

        result = reconstruct_page_footnotes(page, source_page="p1")

        self.assertTrue(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertIn("〔1〕来源说明", result.body)
        self.assertEqual(
            {issue.code for issue in result.issues},
            {"semantic_footnote_reference_ambiguous"},
        )
        self.assertEqual(result.issues[0].evidence["reference_count"], 2)

    def test_circled_note_is_reconstructed_with_one_page_local_landing(self) -> None:
        result = reconstruct_page_footnotes(
            "正文引用本雅明的论述①。\n\n①本雅明：《文集》，第416页。\n12",
            source_page="pdf-0012-physical-01",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(
            result.footnotes[0].stable_id,
            "pdf-0012-physical-01-n1",
        )
        self.assertEqual(
            result.footnotes[0].text,
            "本雅明：《文集》，第416页。",
        )
        self.assertIn("[^pdf-0012-physical-01-n1]", result.body)
        self.assertNotIn("①本雅明", result.body)

    def test_circled_twenty_uses_canonical_numeric_stable_id(self) -> None:
        result = reconstruct_page_footnotes(
            "正文引用④以及最后一条⑳。\n\n⑳第二十条注释。",
            source_page="p20",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].label, "20")
        self.assertEqual(result.footnotes[0].stable_id, "p20-n20")
        self.assertIn("[^p20-n20]", result.body)

    def test_circled_twenty_one_is_reconstructed_page_locally(self) -> None:
        result = reconstruct_page_footnotes(
            "正文引用第二十一条资料㉑。\n\n㉑第二十一条注释。",
            source_page="p21",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].label, "21")
        self.assertEqual(result.footnotes[0].stable_id, "p21-n21")
        self.assertIn("[^p21-n21]", result.body)
        self.assertNotIn("㉑第二十一条注释", result.body)

    def test_circled_fifty_is_the_supported_upper_boundary(self) -> None:
        result = reconstruct_page_footnotes(
            "正文引用第五十条资料㊿。\n\n㊿第五十条注释。",
            source_page="p50",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].label, "50")
        self.assertEqual(result.footnotes[0].stable_id, "p50-n50")
        self.assertIn("[^p50-n50]", result.body)

    def test_unicode_codepoint_after_circled_fifty_is_not_a_note_marker(self) -> None:
        original = "正文中的㋀不是圈号数字。\n\n㋀也不是脚注定义。"

        result = reconstruct_page_footnotes(original, source_page="p-outside")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.issues, ())
        self.assertEqual(result.body, original)

    def test_circled_definition_marker_may_occupy_its_own_line(self) -> None:
        result = reconstruct_page_footnotes(
            "正文中的资料落点④。\n\n④\n参看波德莱尔：《恶之花》。\n9",
            source_page="p9",
        )

        self.assertFalse(result.release_blocked)
        self.assertEqual(len(result.footnotes), 1)
        self.assertEqual(result.footnotes[0].text, "参看波德莱尔：《恶之花》。")
        self.assertIn("[^p9-n4]", result.body)

    def test_unmatched_circled_definition_is_retained_with_nonblocking_issue(self) -> None:
        original = "正文没有可见的同圈号引用。\n\n①参看《文集》，第3页。"

        result = reconstruct_page_footnotes(original, source_page="p3")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.body, original)
        self.assertEqual(
            {issue.code for issue in result.issues},
            {"semantic_footnote_reference_missing"},
        )
        self.assertFalse(result.issues[0].blocking)
        self.assertEqual(result.issues[0].evidence["notation"], "circled")

    def test_ambiguous_circled_definition_is_retained_with_audit_issue(self) -> None:
        original = "第一次引用①，稍后又引用①。\n\n①参看《文集》。"

        result = reconstruct_page_footnotes(original, source_page="p4")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.body, original)
        self.assertEqual(
            {issue.code for issue in result.issues},
            {"semantic_footnote_reference_ambiguous"},
        )
        self.assertEqual(result.issues[0].evidence["reference_count"], 2)

    def test_cross_referenced_circled_list_is_never_converted_to_footnotes(self) -> None:
        original = (
            "下文分为①理论和②实践两类。\n"
            "① 理论类：介绍概念。\n"
            "② 实践类：介绍方法。"
        )

        result = reconstruct_page_footnotes(original, source_page="p-list")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.issues, ())
        self.assertEqual(result.body, original)

    def test_circled_list_with_standalone_markers_is_not_a_note_block(self) -> None:
        original = (
            "下文分为①理论和②实践两类。\n"
            "①\n理论类介绍概念。\n"
            "②\n实践类介绍方法。"
        )

        result = reconstruct_page_footnotes(original, source_page="p-list")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.issues, ())
        self.assertEqual(result.body, original)

    def test_high_circled_numbers_in_an_ordinary_list_are_not_footnotes(self) -> None:
        original = (
            "下文包括㉑理论和㉒实践两类。\n"
            "㉑ 理论类：介绍概念。\n"
            "㉒ 实践类：介绍方法。"
        )

        result = reconstruct_page_footnotes(original, source_page="p-high-list")

        self.assertFalse(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(result.issues, ())
        self.assertEqual(result.body, original)

    def test_plain_number_never_becomes_a_footnote_landing(self) -> None:
        page = "正文中的裸数字 1 不是脚注标记。\n\n[1] 来源说明。"

        result = reconstruct_page_footnotes(page, source_page="p1")

        self.assertTrue(result.release_blocked)
        self.assertEqual(result.footnotes, ())
        self.assertEqual(
            {issue.code for issue in result.issues},
            {"semantic_footnote_reference_missing"},
        )

    def test_unique_circled_note_may_resolve_on_immediately_following_page(self) -> None:
        first = reconstruct_page_footnotes(
            "前页正文一直延续到结尾，引用资料②而继续讨论。",
            source_page="pdf-0007-physical-01",
        )
        second = reconstruct_page_footnotes(
            "后页正文。\n\n" + "延续讨论。" * 30 + "\n\n②参看《文集》，第416页。\n3",
            source_page="pdf-0008-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0008-physical-01",
            ),
        )

        self.assertIn("[^pdf-0008-physical-01-n2]", results[0].body)
        self.assertNotIn("②参看", results[1].body)
        self.assertEqual(len(results[1].footnotes), 1)
        self.assertEqual(results[1].footnotes[0].confidence, 0.99)
        self.assertEqual(
            results[1].footnotes[0].reconstruction_scope,
            "adjacent-page",
        )
        self.assertEqual(
            results[1].footnotes[0].reference_source_page,
            "pdf-0007-physical-01",
        )
        self.assertEqual(results[1].issues, ())

    def test_circled_twenty_one_may_resolve_on_adjacent_page(self) -> None:
        first = reconstruct_page_footnotes(
            "前页正文一直延续到结尾，引用第二十一条资料㉑而继续讨论。",
            source_page="pdf-0020-physical-01",
        )
        second = reconstruct_page_footnotes(
            "后页正文。\n\n" + "延续讨论。" * 30 + "\n\n㉑参看《文集》，第421页。\n21",
            source_page="pdf-0021-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0020-physical-01",
                "pdf-0021-physical-01",
            ),
        )

        self.assertIn("[^pdf-0021-physical-01-n21]", results[0].body)
        self.assertNotIn("㉑参看", results[1].body)
        self.assertEqual(len(results[1].footnotes), 1)
        self.assertEqual(results[1].footnotes[0].label, "21")
        self.assertEqual(
            results[1].footnotes[0].reconstruction_scope,
            "adjacent-page",
        )
        self.assertEqual(results[1].issues, ())

    def test_cross_page_reconstruction_requires_canonical_adjacency(self) -> None:
        first = reconstruct_page_footnotes(
            "正文" * 80 + "引用②。",
            source_page="pdf-0007-physical-01",
        )
        second_text = "正文" * 80 + "\n②参看《文集》。"
        second = reconstruct_page_footnotes(
            second_text,
            source_page="pdf-0009-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0009-physical-01",
            ),
        )

        self.assertEqual(results, (first, second))

    def test_cross_page_reconstruction_does_not_skip_a_spread_half(self) -> None:
        first = reconstruct_page_footnotes(
            "正文" * 80 + "引用②。",
            source_page="pdf-0007-physical-01",
        )
        second = reconstruct_page_footnotes(
            "正文" * 80 + "\n②参看《文集》。",
            source_page="pdf-0008-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0008-physical-01",
            ),
            physical_pages_per_pdf_page=2,
        )

        self.assertEqual(results, (first, second))

    def test_cross_page_reconstruction_rejects_previous_page_list_cluster(self) -> None:
        first = reconstruct_page_footnotes(
            "正文" * 80 + "分为①理论和②实践两类。",
            source_page="pdf-0007-physical-01",
        )
        second_text = "正文" * 80 + "\n②参看《文集》。"
        second = reconstruct_page_footnotes(
            second_text,
            source_page="pdf-0008-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0008-physical-01",
            ),
        )

        self.assertEqual(results, (first, second))

    def test_cross_page_reconstruction_rejects_early_unbounded_definition(self) -> None:
        first = reconstruct_page_footnotes(
            "正文" * 80 + "引用②。",
            source_page="pdf-0007-physical-01",
        )
        second_text = "②参看《文集》。\n随后正文没有空行边界。" + "正文" * 80
        second = reconstruct_page_footnotes(
            second_text,
            source_page="pdf-0008-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0008-physical-01",
            ),
        )

        self.assertEqual(results, (first, second))

    def test_cross_page_reconstruction_accepts_bounded_early_citation(self) -> None:
        first = reconstruct_page_footnotes(
            "正文" * 80 + "引用②。",
            source_page="pdf-0007-physical-01",
        )
        second = reconstruct_page_footnotes(
            "②参看《文集》，第3页。\n\n新页正文继续。" + "正文" * 80,
            source_page="pdf-0008-physical-01",
        )

        results = reconstruct_adjacent_page_footnotes(
            (first, second),
            source_pages=(
                "pdf-0007-physical-01",
                "pdf-0008-physical-01",
            ),
        )

        self.assertIn("[^pdf-0008-physical-01-n2]", results[0].body)
        self.assertTrue(results[1].body.startswith("新页正文继续"))
        self.assertEqual(results[1].footnotes[0].text, "参看《文集》，第3页。")

    def test_markdown_inventory_and_docx_markers_are_one_to_one(self) -> None:
        markdown = (
            "# 第一章\n\n正文[^p1-n1]。\n\n"
            "[^p1-n1]: 完整注释。\n"
        )

        inventory = parse_markdown_footnotes(markdown)
        body, notes = markdown_footnotes_to_docx_markers(
            markdown,
            namespace="chapter-1",
        )

        self.assertTrue(inventory.valid)
        self.assertNotIn("[^p1-n1]:", body)
        self.assertIn("[[FN:chapter-1-p1-n1]]", body)
        self.assertEqual(notes, (("chapter-1-p1-n1", "完整注释。"),))

    def test_ascii_exclamation_before_reference_is_a_valid_footnote(self) -> None:
        markdown = "正文结束![^n]\n\n[^n]: 注释。\n"

        inventory = parse_markdown_footnotes(markdown)
        body, notes = markdown_footnotes_to_docx_markers(
            markdown,
            namespace="chapter-1",
        )

        self.assertTrue(inventory.valid)
        self.assertEqual(inventory.references, ("n",))
        self.assertIn("正文结束![[FN:chapter-1-n]]", body)
        self.assertEqual(notes, (("chapter-1-n", "注释。"),))

    def test_docx_marker_escapes_immediate_ascii_parenthetical_prose(self) -> None:
        markdown = "正文。[^n](这里是正文。)\n\n[^n]: 注释。\n"

        body, notes = markdown_footnotes_to_docx_markers(
            markdown,
            namespace="chapter-1",
        )

        self.assertIn("[[FN:chapter-1-n]]\\(这里是正文。)", body)
        self.assertEqual(notes, (("chapter-1-n", "注释。"),))

    def test_duplicate_reference_is_not_a_valid_word_footnote_contract(self) -> None:
        markdown = "正文[^n]，再次引用[^n]。\n\n[^n]: 注释。\n"
        inventory = parse_markdown_footnotes(markdown)

        self.assertFalse(inventory.valid)
        self.assertEqual(inventory.duplicate_references, ("n",))
        with self.assertRaises(ValueError):
            markdown_footnotes_to_docx_markers(markdown, namespace="chapter")

    def test_append_definitions_and_audit_summary(self) -> None:
        page = reconstruct_page_footnotes(
            "正文。[1]\n\n[1] Note.",
            source_page="p1",
        )
        rendered = append_markdown_footnotes(page.body, page.footnotes)
        summary = semantic_audit_summary(
            [
                {
                    "footnote_count": 1,
                    "issues": [],
                    "pages": [
                        {
                            "footnotes": [
                                {"reconstruction_scope": "adjacent-page"}
                            ]
                        }
                    ],
                }
            ]
        )

        self.assertIn("[^p1-n1]: Note.", rendered)
        self.assertEqual(summary["footnote_count"], 1)
        self.assertEqual(summary["adjacent_page_footnote_count"], 1)
        self.assertFalse(summary["release_blocked"])

    def test_footnote_contract_hash_ignores_prose_but_tracks_note_relation(self) -> None:
        original = "正文 A[^n]。\n\n[^n]: 注释。\n"
        prose_edit = "正文 B[^n]。\n\n[^n]: 注释。\n"
        note_edit = "正文 B[^n]。\n\n[^n]: 修改后的注释。\n"

        self.assertEqual(
            markdown_footnote_contract_sha256(original),
            markdown_footnote_contract_sha256(prose_edit),
        )
        self.assertNotEqual(
            markdown_footnote_contract_sha256(prose_edit),
            markdown_footnote_contract_sha256(note_edit),
        )

    def test_footnote_definition_preserves_blank_separated_continuation(self) -> None:
        markdown = (
            "正文[^n]。\n\n"
            "[^n]: 第一段。\n\n"
            "    第二段。\n"
        )

        inventory = parse_markdown_footnotes(markdown)

        self.assertTrue(inventory.valid)
        self.assertEqual(inventory.definitions, (("n", "第一段。第二段。"),))


if __name__ == "__main__":
    unittest.main()
