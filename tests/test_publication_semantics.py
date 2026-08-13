from __future__ import annotations

import unittest

from publication_semantics import (
    append_markdown_footnotes,
    markdown_footnote_contract_sha256,
    markdown_footnotes_to_docx_markers,
    parse_markdown_footnotes,
    reconstruct_page_footnotes,
    semantic_audit_summary,
)


class PublicationSemanticsTests(unittest.TestCase):
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
                }
            ]
        )

        self.assertIn("[^p1-n1]: Note.", rendered)
        self.assertEqual(summary["footnote_count"], 1)
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
