from __future__ import annotations

import hashlib
import unittest

from semantic_apply import SemanticApplyError, validate_translation_set
from semantic_ir import ReviewDecision, SemanticContractError, SourceLocator


def _source_unit(markdown: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "chapter-u0001-test",
        "chapter_id": "chapter",
        "sequence": 1,
        "kind": "paragraph",
        "source_href": "chapter.xhtml",
        "source_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "source_markdown": markdown,
    }


def _translated_unit(source: dict[str, object], markdown: str) -> dict[str, object]:
    return {
        "schema_version": source["schema_version"],
        "id": source["id"],
        "chapter_id": source["chapter_id"],
        "sequence": source["sequence"],
        "source_sha256": source["source_sha256"],
        "translated_markdown": markdown,
    }


class SemanticApplyValidationTests(unittest.TestCase):
    def test_structure_contract_rejects_heading_list_table_link_and_note_changes(self) -> None:
        source_markdown = (
            "# Heading\n\n"
            "- One\n  - Two\n\n"
            "| Name | Value |\n| --- | --- |\n| Power | [Freedom](https://example.test/x) |\n\n"
            "Body[^n].\n\n[^n]: Complete note."
        )
        translated_markdown = (
            "# 标题\n\n"
            "- 第一项\n  - 第二项\n\n"
            "| 名称 | 数值 |\n| --- | --- |\n| 权力 | [自由](https://example.test/x) |\n\n"
            "正文[^n]。\n\n[^n]: 完整注释。"
        )
        source = _source_unit(source_markdown)
        mutations = {
            "heading": translated_markdown.replace("# 标题", "## 标题"),
            "list": translated_markdown.replace("  - 第二项\n", "第二项\n"),
            "table": translated_markdown.replace("| 权力 | [自由]", "| [自由]"),
            "link": translated_markdown.replace("https://example.test/x", "https://example.test/y"),
            "footnote": translated_markdown.replace("[^n]: 完整", "[^changed]: 完整"),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                with self.assertRaises(SemanticApplyError):
                    validate_translation_set(
                        [source], [_translated_unit(source, mutation)]
                    )

        result = validate_translation_set(
            [source], [_translated_unit(source, translated_markdown)]
        )
        self.assertEqual(result.chapter_order, ("chapter",))

    def test_glossary_is_checked_when_supplied(self) -> None:
        source = _source_unit("Power shapes freedom in political life.")
        translated = _translated_unit(source, "权势塑造政治生活中的自由。")

        with self.assertRaisesRegex(SemanticApplyError, "glossary"):
            validate_translation_set(
                [source], [translated], glossary={"Power": "权力"}
            )

    def test_full_unit_schema_and_order_are_required(self) -> None:
        first = _source_unit("First complete English paragraph.")
        second = {
            **_source_unit("Second complete English paragraph."),
            "id": "chapter-u0002-test",
            "sequence": 3,
        }
        translated = [
            _translated_unit(first, "第一段完整的中文译文。"),
            _translated_unit(second, "第二段完整的中文译文。"),
        ]

        with self.assertRaisesRegex(SemanticApplyError, "not contiguous"):
            validate_translation_set([first, second], translated)

    def test_review_decision_contract_is_versioned_and_source_bound(self) -> None:
        decision = ReviewDecision(
            schema_version=1,
            issue_id="issue-1",
            unit_id="chapter-u0001-test",
            source_sha256="a" * 64,
            reviewer="reviewer@example.test",
            decision="accepted",
            timestamp="2026-08-14T12:00:00Z",
        )

        self.assertEqual(decision.to_dict()["schema_version"], 1)
        with self.assertRaises(SemanticContractError):
            ReviewDecision(
                schema_version=1,
                issue_id="issue-1",
                unit_id=None,
                source_sha256="a" * 64,
                reviewer="reviewer@example.test",
                decision="waived",
                timestamp="2026-08-14T12:00:00Z",
            )

    def test_canonical_translation_unit_locator_is_accepted(self) -> None:
        source_markdown = "A complete canonical source paragraph."
        source = {
            **_source_unit(source_markdown),
            "locators": [
                SourceLocator(
                    adapter="epub",
                    source="book.epub",
                    href="chapter.xhtml",
                    block_index=0,
                ).to_dict()
            ],
        }
        source.pop("source_href")

        result = validate_translation_set(
            [source], [_translated_unit(source, "一段完整的规范中文译文。")]
        )

        self.assertEqual(result.schema_version, 1)


if __name__ == "__main__":
    unittest.main()
