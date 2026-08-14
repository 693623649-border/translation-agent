from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from semantic_ir import (
    DocumentSemantic,
    ReviewDecision,
    SemanticBlock,
    SemanticChapter,
    SemanticContractError,
    SourceLocator,
    append_review_decision,
    sha256_text,
)


class SemanticIrTests(unittest.TestCase):
    def fixture(self) -> DocumentSemantic:
        locator = SourceLocator(adapter="epub", source="part1.xhtml", href="p1")
        block = SemanticBlock(
            id="chapter-1-b1",
            kind="paragraph",
            markdown="Text.",
            locators=(locator,),
        )
        chapter = SemanticChapter(
            id="chapter-1",
            sequence=1,
            title="Chapter 1",
            blocks=(block,),
        )
        return DocumentSemantic(
            schema_version=1,
            id="book",
            source_mode="epub",
            source_sha256="a" * 64,
            source_language="en",
            chapters=(chapter,),
        )

    def test_document_round_trip_preserves_locators(self) -> None:
        document = self.fixture()

        restored = DocumentSemantic.from_dict(document.to_dict())

        self.assertEqual(restored, document)

    def test_document_rejects_duplicate_global_block_ids(self) -> None:
        document = self.fixture()
        chapter = document.chapters[0]
        with self.assertRaisesRegex(SemanticContractError, "duplicate global block"):
            DocumentSemantic(
                schema_version=1,
                id="book",
                source_mode="epub",
                source_sha256="a" * 64,
                source_language="en",
                chapters=(
                    chapter,
                    SemanticChapter(
                        id="chapter-2",
                        sequence=2,
                        title="Chapter 2",
                        blocks=chapter.blocks,
                    ),
                ),
            )

    def test_review_decisions_are_append_only_and_source_bound(self) -> None:
        source = "Original"
        decision = ReviewDecision(
            schema_version=1,
            issue_id="issue-1",
            unit_id="unit-1",
            source_sha256=sha256_text(source),
            reviewer="reviewer@example",
            decision="accepted",
            timestamp="2026-08-14T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-decisions.jsonl"
            append_review_decision(path, decision)
            append_review_decision(path, decision)
            lines = path.read_text(encoding="utf-8").splitlines()

        self.assertEqual(len(lines), 2)
        self.assertTrue(decision.matches_source(source))
        self.assertFalse(decision.matches_source("Changed"))


if __name__ == "__main__":
    unittest.main()

