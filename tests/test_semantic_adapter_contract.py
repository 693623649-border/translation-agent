from __future__ import annotations

import hashlib
import unittest

from born_digital_pdf_import import _translation_units as pdf_translation_units
from epub_semantic_import import _translation_units as epub_translation_units
from semantic_ir import (
    SemanticContractError,
    TranslationUnit,
    normalize_translation_unit_record,
)
from semantic_translation_runner import _batch_key, batch_units


CANONICAL_KEYS = {
    "schema_version",
    "id",
    "chapter_id",
    "sequence",
    "kind",
    "source_markdown",
    "source_sha256",
    "locators",
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SemanticAdapterContractTests(unittest.TestCase):
    def test_epub_and_pdf_writers_emit_the_same_canonical_unit_shape(self) -> None:
        epub_markdown = "# Chapter\n\nA complete paragraph.\n\n- A list item\n"
        epub_units = epub_translation_units(
            "epub-0001",
            epub_markdown,
            "OEBPS/chapter.xhtml",
        )
        pdf_markdown = (
            "# Chapter\n\nA complete paragraph.\n\n"
            "| Name | Value |\n| --- | --- |\n| Power | Freedom |\n"
        )
        pdf_units = pdf_translation_units(
            "pdf-text-0001",
            pdf_markdown,
            "4-5",
            block_pages=(4, 4, 5),
        )

        for adapter, markdown, units in (
            ("epub", epub_markdown, epub_units),
            ("text-pdf", pdf_markdown, pdf_units),
        ):
            with self.subTest(adapter=adapter):
                self.assertTrue(units)
                self.assertTrue(all(set(unit) == CANONICAL_KEYS for unit in units))
                canonical = [TranslationUnit.from_dict(unit) for unit in units]
                self.assertEqual(
                    [unit.sequence for unit in canonical],
                    list(range(1, len(canonical) + 1)),
                )
                self.assertEqual(
                    "\n\n".join(unit.source_markdown for unit in canonical) + "\n",
                    markdown,
                )
                self.assertTrue(
                    all(
                        unit.source_sha256 == _sha256(unit.source_markdown)
                        for unit in canonical
                    )
                )
                self.assertEqual(
                    [unit.locators[0].block_index for unit in canonical],
                    list(range(len(canonical))),
                )
                self.assertTrue(
                    all(unit.locators[0].adapter == adapter for unit in canonical)
                )

        self.assertEqual(epub_units[-1]["kind"], "list_item")
        self.assertEqual(
            epub_units[0]["locators"][0]["href"],
            "OEBPS/chapter.xhtml",
        )
        self.assertEqual(
            [unit["locators"][0]["page"] for unit in pdf_units],
            [4, 4, 5],
        )
        for unit in pdf_units:
            legacy_digest = hashlib.sha256(
                (
                    f"pdf-text-0001\0{unit['sequence']}\0"
                    f"{unit['source_sha256']}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            self.assertEqual(
                unit["id"],
                f"pdf-text-0001-u{unit['sequence']:04d}-{legacy_digest}",
            )

    def test_new_locators_preserve_legacy_ids_and_batch_cache_identity(self) -> None:
        source = "- A legacy list item"
        source_sha256 = _sha256(source)
        digest = hashlib.sha256(
            f"OEBPS/chapter.xhtml\0{1}\0{source_sha256}".encode("utf-8")
        ).hexdigest()[:16]
        legacy_epub = {
            "schema_version": 1,
            "id": f"epub-0001-u0001-{digest}",
            "chapter_id": "epub-0001",
            "sequence": 1,
            "kind": "list",
            "source_href": "OEBPS/chapter.xhtml",
            "source_sha256": source_sha256,
            "source_markdown": source,
        }
        canonical = normalize_translation_unit_record(legacy_epub)
        written = epub_translation_units(
            "epub-0001",
            source + "\n",
            "OEBPS/chapter.xhtml",
        )[0]

        self.assertEqual(written["id"], legacy_epub["id"])
        self.assertEqual(canonical.kind, "list_item")
        self.assertEqual(canonical.locators[0].href, "OEBPS/chapter.xhtml")
        with self.assertRaises(SemanticContractError):
            TranslationUnit.from_dict(legacy_epub)

        key_options = {
            "model": "test-model",
            "target_language": "简体中文",
            "glossary": {},
        }
        self.assertEqual(
            _batch_key([legacy_epub], **key_options),
            _batch_key([canonical.to_dict()], **key_options),
        )
        self.assertEqual(batch_units([legacy_epub]), [[legacy_epub]])

        legacy_pdf = {
            "schema_version": 1,
            "id": "pdf-text-0001-u0001-legacy",
            "chapter_id": "pdf-text-0001",
            "sequence": 1,
            "kind": "paragraph",
            "source_pages": "4-5",
            "source_sha256": _sha256("PDF source paragraph."),
            "source_markdown": "PDF source paragraph.",
        }
        normalized_pdf = normalize_translation_unit_record(legacy_pdf)
        self.assertEqual(normalized_pdf.locators[0].adapter, "text-pdf")
        self.assertEqual(normalized_pdf.locators[0].source, "pages:4-5")
        self.assertIsNone(normalized_pdf.locators[0].page)

        with self.assertRaisesRegex(SemanticContractError, "must not mix"):
            normalize_translation_unit_record(
                {
                    **canonical.to_dict(),
                    "source_href": "conflicting.xhtml",
                }
            )


if __name__ == "__main__":
    unittest.main()
