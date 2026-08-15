from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
import zipfile

from book_pipeline import build_epub
from epub_publication_verifier import (
    CHECK_IDS,
    EpubPublicationVerificationError,
    normalize_epub_language,
    verify_epub_publication,
)
from epub_semantic_import import apply_translations, import_epub


def _write_source_epub(
    path: Path,
    *,
    duplicate_title: bool = False,
    two_footnotes: bool = False,
) -> None:
    repeated_title = b"<p>Chapter One</p>" if duplicate_title else b""
    second_reference = (
        b'<a epub:type="noteref" href="#note-2">2</a>.'
        if two_footnotes
        else b""
    )
    second_definition = (
        b'<aside epub:type="footnote" id="note-2"><p>Second note.</p></aside>'
        if two_footnotes
        else b""
    )
    members = {
        "META-INF/container.xml": b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
        "OEBPS/content.opf": b'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title><dc:creator>A. Author</dc:creator><dc:language>en</dc:language><dc:identifier>source-id</dc:identifier></metadata>
 <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine>
</package>''',
        "OEBPS/nav.xhtml": b'''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="chapter1.xhtml">One</a></nav></body></html>''',
        "OEBPS/chapter1.xhtml": b'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
    <section><h1>Chapter One</h1>''' + repeated_title + b'''<p>Body <em>word</em><a epub:type="noteref" href="#note-1">1</a>.''' + second_reference + b'''</p>
    <aside epub:type="footnote" id="note-1"><p>Complete note.</p></aside>
    ''' + second_definition + b'''
    <aside epub:type="footnote"><p>Continuation paragraph.</p></aside>
<p>Index locator.</p></section>
</body></html>''',
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        for name, value in members.items():
            archive.writestr(name, value)


def _translate(output: Path, temporary: Path) -> None:
    units_path = output / "semantic" / "translation-units.jsonl"
    units = [
        json.loads(line)
        for line in units_path.read_text(encoding="utf-8").splitlines()
    ]
    replacements = {
        "Chapter One": "第一章",
        "Body": "正文",
        "word": "词语",
        "Complete": "完整",
        "note": "注释",
        "Continuation paragraph": "续段",
        "Index locator": "索引定位",
    }
    for unit in units:
        translated = unit["source_markdown"]
        for source, target in replacements.items():
            translated = translated.replace(source, target)
        unit["translated_markdown"] = translated
    path = temporary / "translations.jsonl"
    path.write_text(
        "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
        encoding="utf-8",
    )
    result = apply_translations(output, path)
    if result["status"] != "passed":  # pragma: no cover - fixture invariant
        raise AssertionError(result)


def _rewrite_epub(path: Path, transform) -> None:
    with zipfile.ZipFile(path, "r") as archive:
        records = [
            (info.filename, archive.read(info), info.compress_type)
            for info in archive.infolist()
            if not info.is_dir()
        ]
    rewritten = transform(records)
    with zipfile.ZipFile(path, "w") as archive:
        for name, value, compression in rewritten:
            archive.writestr(name, value, compress_type=compression)


class EpubPublicationVerifierTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        translated: bool = True,
        duplicate_title: bool = False,
        two_footnotes: bool = False,
    ) -> tuple[Path, Path, Path]:
        source = root / "source.epub"
        output = root / "output"
        artifact = output / "translated.epub"
        _write_source_epub(
            source,
            duplicate_title=duplicate_title,
            two_footnotes=two_footnotes,
        )
        imported = import_epub(source, output)
        self.assertEqual(imported["status"], "passed")
        if translated:
            _translate(output, root)
        manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
        build_epub(
            artifact,
            output / "chapters",
            manifest,
            book_title="测试书",
            language="zh-CN" if translated else "en",
        )
        return source, output, artifact

    @staticmethod
    def _issue_codes(report: dict) -> set[str]:
        return {str(error["code"]) for error in report["errors"]}

    def test_valid_translated_epub_is_release_ready_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output, artifact = self._fixture(Path(temporary))

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )
            persisted = json.loads(
                (output / "audit" / "epub-release-report.json").read_text(
                    encoding="utf-8"
                )
            )
            artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
            canonical_manifest_sha256 = hashlib.sha256(
                (output / "chapters.json").read_bytes()
            ).hexdigest()

        self.assertTrue(report["release_ready"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["mode"], "full")
        self.assertEqual(report["profile"], "epub")
        self.assertEqual(report["publication_profile"], "epub")
        self.assertEqual(
            report["verifier_node"], "core.publication.verify.epub"
        )
        self.assertEqual(tuple(check["id"] for check in report["checks"]), CHECK_IDS)
        self.assertEqual(
            report["artifact"]["sha256"], artifact_sha256
        )
        self.assertEqual(
            report["semantic"]["canonical_manifest_sha256"],
            canonical_manifest_sha256,
        )
        self.assertIsNotNone(
            report["semantic"]["canonical_chapters_sha256"]
        )
        self.assertEqual(persisted, report)

    def test_epub_language_labels_are_normalized_and_invalid_tags_rejected(self) -> None:
        self.assertEqual(normalize_epub_language("简体中文"), "zh-CN")
        self.assertEqual(normalize_epub_language("繁體中文"), "zh-Hant")
        self.assertEqual(normalize_epub_language("English"), "en")
        self.assertEqual(normalize_epub_language("Japanese"), "ja")
        self.assertEqual(normalize_epub_language("eng"), "eng")
        self.assertEqual(normalize_epub_language("zh_cn"), "zh-CN")
        self.assertEqual(normalize_epub_language("und"), "und")
        with self.assertRaisesRegex(
            EpubPublicationVerificationError,
            "BCP 47",
        ):
            normalize_epub_language("中文？")

    def test_publisher_title_cleanup_matches_verifier_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, output, artifact = self._fixture(
                Path(temporary),
                translated=False,
                duplicate_title=True,
            )

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
                target_language="en",
                require_translation=False,
            )

        self.assertTrue(report["release_ready"], report["errors"])

    def test_source_identity_tamper_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)
            source.write_bytes(source.read_bytes() + b"tampered")

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )

        self.assertFalse(report["release_ready"])
        self.assertIn("source_hash_mismatch", self._issue_codes(report))

    def test_translation_audit_must_bind_exact_reconstruction_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)
            path = output / "audit" / "semantic-translation.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["upstream_reconstruction"]["sha256"] = "0" * 64
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )

        self.assertFalse(report["release_ready"])
        self.assertIn("translation_upstream_mismatch", self._issue_codes(report))

    def test_translation_audit_must_bind_exact_translation_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)
            translations = root / "translations.jsonl"
            expected_sha256 = hashlib.sha256(translations.read_bytes()).hexdigest()

            valid = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
                expected_translation_sha256=expected_sha256,
            )
            self.assertTrue(valid["release_ready"], valid["errors"])
            self.assertEqual(
                valid["semantic"]["translation_input_sha256"],
                expected_sha256,
            )

            path = output / "audit" / "semantic-translation.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload.pop("translation_input")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            missing = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
                expected_translation_sha256=expected_sha256,
            )

        self.assertFalse(missing["release_ready"])
        self.assertIn("translation_input_missing", self._issue_codes(missing))

    def test_graph_reader_audit_is_bound_to_translation_and_canonical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)
            graph = output / ".pipeline_graph"
            graph.mkdir()
            reconstruction = output / "audit" / "semantic-reconstruction.json"
            (graph / "draft-semantic-audit.json").write_bytes(
                reconstruction.read_bytes()
            )
            reader = json.loads(
                (output / "audit" / "semantic-translation.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest_path = output / "chapters.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            chapter_paths = [output / "chapters" / item["filename"] for item in manifest]
            digest = hashlib.sha256()
            for path in sorted(chapter_paths, key=lambda item: item.as_posix()):
                digest.update(path.name.encode())
                digest.update(b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode())
                digest.update(b"\0")
            for chapter in reader["chapters"]:
                chapter["source_markdown_sha256"] = chapter["markdown_sha256"]
            reader["reader_manifest_sha256"] = hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest()
            reader["reader_chapters_sha256"] = digest.hexdigest()
            (graph / "reader-semantic-audit.json").write_text(
                json.dumps(reader, ensure_ascii=False), encoding="utf-8"
            )

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )

        self.assertTrue(report["release_ready"], report["errors"])

    def test_canonical_markdown_requires_closed_footnotes_and_current_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)
            chapter = next((output / "chapters").glob("*.md"))
            markdown = chapter.read_text(encoding="utf-8")
            chapter.write_text(
                markdown.replace("]:", "-changed]:", 1),
                encoding="utf-8",
            )

            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )

        self.assertFalse(report["release_ready"])
        self.assertIn("footnote_closure_failed", self._issue_codes(report))

    def test_package_language_and_resource_inventory_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root)

            def corrupt(records):
                rewritten = []
                for name, value, compression in records:
                    if name.endswith("package.opf"):
                        value = value.replace(b"<dc:language>zh-CN</dc:language>", b"<dc:language>en</dc:language>")
                    if name.endswith("style.css"):
                        continue
                    rewritten.append((name, value, compression))
                return rewritten

            _rewrite_epub(artifact, corrupt)
            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
            )

        codes = self._issue_codes(report)
        self.assertFalse(report["release_ready"])
        self.assertTrue(
            {"epub_language_mismatch", "epub_resource_missing"} & codes,
            codes,
        )

    def test_required_package_metadata_and_nav_language_are_verified(self) -> None:
        cases = ("identifier", "modified", "nav-language")
        expected = {
            "identifier": "epub_unique_identifier_invalid",
            "modified": "epub_modified_invalid",
            "nav-language": "epub_nav_language_mismatch",
        }
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output, artifact = self._fixture(root, translated=False)

                def corrupt(records):
                    rewritten = []
                    for name, value, compression in records:
                        if name.endswith("package.opf"):
                            if case == "identifier":
                                value = value.replace(
                                    b' unique-identifier="book-id"', b""
                                )
                            elif case == "modified":
                                value = value.replace(
                                    b'property="dcterms:modified"',
                                    b'property="other"',
                                )
                        elif case == "nav-language" and name.endswith("nav.xhtml"):
                            value = value.replace(b'lang="en"', b'lang="ja"', 1)
                        rewritten.append((name, value, compression))
                    return rewritten

                _rewrite_epub(artifact, corrupt)
                report = verify_epub_publication(
                    output,
                    source_epub=source,
                    artifact_path=artifact,
                    target_language="en",
                    require_translation=False,
                )

            self.assertFalse(report["release_ready"])
            self.assertIn(expected[case], self._issue_codes(report))

    def test_content_bytes_and_internal_resources_must_match_canonical_book(self) -> None:
        cases = ("text", "resource")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, output, artifact = self._fixture(root)

                def corrupt(records):
                    rewritten = []
                    for name, value, compression in records:
                        if name.endswith(".xhtml") and not name.endswith("nav.xhtml"):
                            if case == "text":
                                value = value.replace("正文".encode(), "篡改".encode(), 1)
                            else:
                                value = value.replace(
                                    b"</body>", b'<img src="missing.png" alt="" /></body>'
                                )
                        rewritten.append((name, value, compression))
                    return rewritten

                _rewrite_epub(artifact, corrupt)
                report = verify_epub_publication(
                    output,
                    source_epub=source,
                    artifact_path=artifact,
                )

            self.assertFalse(report["release_ready"])
            self.assertIn(
                "epub_content_text_mismatch"
                if case == "text"
                else "epub_internal_link_missing",
                self._issue_codes(report),
            )

    def test_footnote_references_must_bijectively_close_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(
                root,
                translated=False,
                two_footnotes=True,
            )

            def duplicate_first_reference_target(records):
                rewritten = []
                for name, value, compression in records:
                    if name.endswith(".xhtml") and not name.endswith("nav.xhtml"):
                        targets = re.findall(rb'href="(#fn:[^"]+)"', value)
                        self.assertEqual(len(targets), 2)
                        value = value.replace(
                            b'href="' + targets[1] + b'"',
                            b'href="' + targets[0] + b'"',
                            1,
                        )
                    rewritten.append((name, value, compression))
                return rewritten

            _rewrite_epub(artifact, duplicate_first_reference_target)
            report = verify_epub_publication(
                output,
                source_epub=source,
                artifact_path=artifact,
                target_language="en",
                require_translation=False,
            )

        self.assertFalse(report["release_ready"])
        self.assertIn(
            "epub_footnote_bijection_mismatch",
            self._issue_codes(report),
        )

    def test_source_only_mode_is_explicit_and_artifact_autodiscovery_is_unambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output, artifact = self._fixture(root, translated=False)

            report = verify_epub_publication(
                output,
                source_epub=source,
                target_language="en",
                require_translation=False,
            )
            (output / "second.epub").write_bytes(artifact.read_bytes())
            with self.assertRaisesRegex(
                EpubPublicationVerificationError,
                "exactly one",
            ):
                verify_epub_publication(
                    output,
                    source_epub=source,
                    target_language="en",
                    require_translation=False,
                )

        self.assertTrue(report["release_ready"])
        self.assertFalse(report["semantic"]["translation_applied"])


if __name__ == "__main__":
    unittest.main()
