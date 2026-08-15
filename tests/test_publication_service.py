from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publication_service import (
    BOOKMARKED_PDF,
    DOCX,
    EPUB,
    KNOWLEDGE_BASE,
    PublicationVerificationRequest,
    run_publication_verification,
)


class PublicationServiceTests(unittest.TestCase):
    def test_default_report_path_is_profile_and_chapter_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            self.assertEqual(
                PublicationVerificationRequest(
                    output_dir=output,
                ).resolved_report_path,
                output / "audit" / "release-report.json",
            )
            self.assertEqual(
                PublicationVerificationRequest(
                    output_dir=output,
                    publication_profile="word",
                ).resolved_report_path,
                output / "audit" / "word-release-report.json",
            )
            self.assertEqual(
                PublicationVerificationRequest(
                    output_dir=output,
                    publication_profile="word",
                    chapter_ids=("chapter-1",),
                ).resolved_report_path,
                output / "audit" / "chapter-report.json",
            )

    def test_service_maps_positive_artifact_contract_to_verifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            source = output / "source.pdf"
            request = PublicationVerificationRequest(
                output_dir=output,
                source_pdf=source,
                book_title="Book",
                expected_language="zh-CN",
                expected_translation_fingerprint="translation-fingerprint",
                require_translation=True,
                required_artifacts=frozenset({DOCX, KNOWLEDGE_BASE}),
                require_docx_render=False,
                require_all_reviewed=True,
                publication_profile="word",
                chapter_ids=("chapter-1", "chapter-2"),
            )
            with patch(
                "publication_service.verify_publication",
                return_value={"ok": True, "release_ready": True},
            ) as verifier:
                result = run_publication_verification(request)

        self.assertTrue(result.ok)
        self.assertEqual(verifier.call_args.args, (output,))
        self.assertEqual(
            verifier.call_args.kwargs,
            {
                "source_pdf": source,
                "book_title": "Book",
                "expected_language": "zh-CN",
                "expected_translation_fingerprint": "translation-fingerprint",
                "require_translation": True,
                "require_epub": False,
                "require_docx": True,
                "require_docx_render": False,
                "require_knowledge_base": True,
                "require_bookmarked_pdf": False,
                "require_all_reviewed": True,
                "publication_profile": "word",
                "chapter_ids": ("chapter-1", "chapter-2"),
                "report_path": output / "audit" / "chapter-report.json",
            },
        )

    def test_service_allows_optional_source_and_ignores_unused_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            request = PublicationVerificationRequest(
                output_dir=output,
                source_pdf=None,
                expected_translation_fingerprint="unused",
                require_translation=False,
                required_artifacts=frozenset({EPUB, BOOKMARKED_PDF}),
            )
            with patch(
                "publication_service.verify_publication",
                return_value={"ok": False},
            ) as verifier:
                result = run_publication_verification(request)

        self.assertFalse(result.ok)
        self.assertIsNone(verifier.call_args.kwargs["source_pdf"])
        self.assertIsNone(
            verifier.call_args.kwargs["expected_translation_fingerprint"]
        )
        self.assertTrue(verifier.call_args.kwargs["require_epub"])
        self.assertTrue(verifier.call_args.kwargs["require_bookmarked_pdf"])
        self.assertFalse(verifier.call_args.kwargs["require_docx"])
        self.assertFalse(verifier.call_args.kwargs["require_docx_render"])

    def test_request_rejects_unknown_contract_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "publication_profile"):
                PublicationVerificationRequest(
                    output_dir=output,
                    publication_profile="epub",  # type: ignore[arg-type]
                )
            with self.assertRaisesRegex(ValueError, "unknown required"):
                PublicationVerificationRequest(
                    output_dir=output,
                    required_artifacts=frozenset(
                        {"unknown"}  # type: ignore[arg-type]
                    ),
                )
            with self.assertRaisesRegex(ValueError, "chapter_ids"):
                PublicationVerificationRequest(
                    output_dir=output,
                    chapter_ids=("",),
                )
            with self.assertRaisesRegex(ValueError, "translation fingerprint"):
                PublicationVerificationRequest(
                    output_dir=output,
                    require_translation=True,
                )


if __name__ == "__main__":
    unittest.main()
