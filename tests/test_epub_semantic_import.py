from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock
import zipfile

from book_pipeline import build_docx
from epub_semantic_import import (
    EpubSemanticError,
    apply_translations,
    import_epub,
    prune_long_footnotes,
)
from publication_semantics import parse_markdown_footnotes


def _write_epub(path: Path, *, broken_note: bool = False) -> None:
    target = "missing" if broken_note else "note-1"
    members = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
        "OEBPS/content.opf": b'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title><dc:creator>A. Author</dc:creator><dc:language>en</dc:language><dc:identifier>id</dc:identifier></metadata>
 <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine>
</package>''',
        "OEBPS/nav.xhtml": b'''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav><a href="chapter1.xhtml">One</a></nav></body></html>''',
        "OEBPS/chapter1.xhtml": f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<section><h1>Chapter One</h1><p>Body <em>word</em><a epub:type="noteref" href="#{target}">1</a>.</p>
<aside epub:type="footnote" id="note-1"><p>Complete <i>note</i>.</p></aside>
<aside epub:type="footnote"><p>Continuation paragraph.</p></aside>
<p>Index locator <a href="chapter1.xhtml#page_12">12</a>.</p></section>
</body></html>'''.encode(),
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", members.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name, value in members.items():
            archive.writestr(name, value)


def _write_orphan_note_epub(path: Path) -> None:
    _write_epub(path)
    with zipfile.ZipFile(path, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    chapter = members["OEBPS/chapter1.xhtml"].decode()
    chapter = chapter.replace(
        "<section><h1>Chapter One</h1>",
        '<section><aside epub:type="footnote"><p>Orphan.</p></aside><h1>Chapter One</h1>',
    )
    members["OEBPS/chapter1.xhtml"] = chapter.encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", members.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name, value in members.items():
            archive.writestr(name, value)


def _write_split_bracket_link_epub(path: Path) -> None:
    _write_epub(path)
    with zipfile.ZipFile(path, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    chapter = members["OEBPS/chapter1.xhtml"].decode()
    chapter = chapter.replace(
        "</section>",
        '''<p>Text <sup><a id="note-1-ref" href="#note-1-ref">[</a></sup><sup><a href="#note-1-ref">1</a></sup><sup><a href="#note-1-ref">]</a></sup>.</p>
<p><span id="note-1-back"><a href="#note-1-back">[</a><a href="#note-1-back">1</a><a href="#note-1-back">]</a> Note label.</span></p></section>''',
    )
    members["OEBPS/chapter1.xhtml"] = chapter.encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            members.pop("mimetype"),
            compress_type=zipfile.ZIP_STORED,
        )
        for name, value in members.items():
            archive.writestr(name, value)


def _write_printed_page_marker_epub(path: Path) -> None:
    _write_epub(path)
    with zipfile.ZipFile(path, "r") as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    chapter = members["OEBPS/chapter1.xhtml"].decode()
    chapter = chapter.replace(
        '<section><h1>Chapter One</h1><p>Body <em>word</em>',
        '<section><h1>Chapter One</h1>'
        '<p class="calibre7"><span class="calibre21">17</span></p>'
        '<p>Body <sup class="calibre14">1</sup>'
        '<sup class="calibre14">8</sup><em>word</em> x<sup>2</sup>',
    )
    members["OEBPS/chapter1.xhtml"] = chapter.encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            members.pop("mimetype"),
            compress_type=zipfile.ZIP_STORED,
        )
        for name, value in members.items():
            archive.writestr(name, value)


class EpubSemanticImportTests(unittest.TestCase):
    def test_prune_long_footnotes_updates_publication_contract_but_not_source_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            import_epub(source, output)
            manifest_before = json.loads(
                (output / "chapters.json").read_text(encoding="utf-8")
            )
            filename = manifest_before[0]["filename"]
            source_chapter = output / "semantic" / "source_chapters" / filename
            source_before = source_chapter.read_bytes()
            current_chapter = output / "chapters" / filename
            current_chapter.write_text(
                current_chapter.read_text(encoding="utf-8")
                + "\n3\n\n6\n\n11\n",
                encoding="utf-8",
            )

            result = prune_long_footnotes(
                output,
                minimum_characters=10,
                remove_standalone_page_markers=True,
            )
            manifest = json.loads(
                (output / "chapters.json").read_text(encoding="utf-8")
            )
            markdown = (output / "chapters" / filename).read_text(encoding="utf-8")
            audit = json.loads(
                (output / "audit" / "semantic-reconstruction.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertTrue(result["changed"])
            self.assertEqual(result["removed_count"], 1)
            self.assertEqual(result["remaining_count"], 0)
            self.assertEqual(result["removed_page_marker_count"], 3)
            self.assertNotRegex(markdown, r"(?m)^\s*(?:3|6|11)\s*$")
            self.assertEqual(manifest[0]["semantic_footnote_count"], 0)
            self.assertEqual(manifest[0]["suppressed_long_footnote_count"], 1)
            self.assertEqual(parse_markdown_footnotes(markdown).definitions, ())
            self.assertEqual(source_chapter.read_bytes(), source_before)
            self.assertEqual(audit["summary"]["footnote_count"], 0)
            self.assertIn(
                "core.publication.prune-long-footnotes",
                audit["generated_by"],
            )
            self.assertTrue((output / "audit" / "reader-edition-pruning.json").is_file())

    def test_printed_page_markers_are_removed_without_losing_real_footnotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_printed_page_marker_epub(source)

            result = import_epub(source, output)
            manifest = json.loads(
                (output / "chapters.json").read_text(encoding="utf-8")
            )
            markdown = (
                output / "chapters" / manifest[0]["filename"]
            ).read_text(encoding="utf-8")
            inventory = parse_markdown_footnotes(markdown)

            self.assertEqual(result["status"], "passed")
            self.assertIsNone(re.search(r"(?m)^\s*17\s*$", markdown))
            self.assertNotIn('<sup>1</sup>', markdown)
            self.assertNotIn('<sup>8</sup>', markdown)
            self.assertIn('x<sup>2</sup>', markdown)
            self.assertTrue(inventory.valid)
            self.assertEqual(len(inventory.references), 1)
            self.assertEqual(len(inventory.definitions), 1)

    def test_split_bracket_links_become_semantic_footnotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_split_bracket_link_epub(source)

            result = import_epub(source, output)
            manifest = json.loads(
                (output / "chapters.json").read_text(encoding="utf-8")
            )
            markdown = (
                output / "chapters" / manifest[0]["filename"]
            ).read_text(encoding="utf-8")
            destination = root / "book.docx"
            inventory = parse_markdown_footnotes(markdown)

            self.assertEqual(result["status"], "passed")
            self.assertTrue(inventory.valid)
            self.assertEqual(len(inventory.definitions), 2)
            self.assertIn("Note label.", inventory.definitions[1][1])
            self.assertNotIn("#note-1-ref", markdown)
            self.assertNotIn("#note-1-back", markdown)
            build_docx(
                destination,
                output / "chapters",
                manifest,
                book_title="Test Book",
            )
            self.assertTrue(destination.is_file())

    def test_spine_and_notes_become_publishable_semantic_chapters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)

            result = import_epub(source, output)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            markdown = (output / "chapters" / manifest[0]["filename"]).read_text(encoding="utf-8")
            audit = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["chapter_count"], 1)
            self.assertEqual(manifest[0]["source_href"], "OEBPS/chapter1.xhtml")
            self.assertIn("# Chapter One", markdown)
            self.assertIn("*word*", markdown)
            inventory = parse_markdown_footnotes(markdown)
            self.assertTrue(inventory.valid)
            self.assertEqual(len(inventory.definitions), 1)
            self.assertIn("Continuation paragraph.", inventory.definitions[0][1])
            self.assertIn("Index locator 12.", markdown)
            self.assertNotIn("chapter1.xhtml#page_12", markdown)
            self.assertEqual(audit["generated_by"], "core.source.epub+core.reconstruct.semantic")
            self.assertEqual(audit["summary"]["continuation_merged_count"], 1)
            self.assertFalse(audit["release_blocked"])
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            note_unit = next(
                unit for unit in units if unit["kind"] == "footnote_definition"
            )
            self.assertIn("\n\n    Continuation paragraph.", note_unit["source_markdown"])

    def test_orphan_anonymous_note_continuation_is_a_visible_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            _write_orphan_note_epub(source)

            result = import_epub(source, root / "output")
            audit = json.loads((root / "output" / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertTrue(result["release_blocked"])
            self.assertIn(
                "epub_orphan_note_continuation",
                {issue["code"] for issue in audit["chapters"][0]["issues"]},
            )

    def test_missing_note_target_blocks_semantic_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            _write_epub(source, broken_note=True)

            result = import_epub(source, root / "output")
            audit = json.loads((root / "output" / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))

            self.assertTrue(result["release_blocked"])
            self.assertEqual(audit["chapters"][0]["issues"][0]["code"], "epub_footnote_target_missing")

    def test_translation_exchange_is_hash_bound_and_preserves_note_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            import_epub(source, output)
            units_path = output / "semantic" / "translation-units.jsonl"
            units = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines()]
            for unit in units:
                unit["translated_markdown"] = (
                    unit["source_markdown"]
                    .replace("Chapter One", "第一章")
                    .replace("Body", "正文")
                    .replace("word", "词语")
                    .replace("Index locator", "索引定位")
                    .replace("Complete", "完整的")
                    .replace("note", "注释")
                    .replace("Continuation paragraph", "续接段落")
                )
            translated = root / "translated.jsonl"
            translated.write_text("".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units), encoding="utf-8")
            reconstruction_path = output / "audit" / "semantic-reconstruction.json"
            reconstruction_before = reconstruction_path.read_bytes()

            result = apply_translations(output, translated)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            markdown = (output / "chapters" / manifest[0]["filename"]).read_text(encoding="utf-8")

            self.assertEqual(result["status"], "passed")
            self.assertEqual(manifest[0]["display_title"], "第一章")
            self.assertTrue(parse_markdown_footnotes(markdown).valid)
            self.assertEqual(reconstruction_path.read_bytes(), reconstruction_before)
            translation_audit = json.loads(
                (output / "audit" / "semantic-translation.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                translation_audit["contract_mode"],
                "epub-spine-translated-markdown-footnotes",
            )
            self.assertEqual(
                translation_audit["upstream_reconstruction"]["sha256"],
                hashlib.sha256(reconstruction_before).hexdigest(),
            )
            self.assertEqual(translation_audit["schema_version"], 1)
            self.assertEqual(
                translation_audit["review_resolution"],
                {"schema_version": 1, "decisions": []},
            )

            units[1]["translated_markdown"] = units[1]["translated_markdown"].replace("[^epub-", "[^changed-")
            translated.write_text("".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units), encoding="utf-8")
            with self.assertRaises(EpubSemanticError):
                apply_translations(output, translated)

    def test_blocked_reconstruction_cannot_be_cleared_by_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_orphan_note_epub(source)
            result = import_epub(source, output)
            self.assertTrue(result["release_blocked"])
            reconstruction = output / "audit" / "semantic-reconstruction.json"
            before = reconstruction.read_bytes()
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for unit in units:
                unit["translated_markdown"] = "中文译文"
            translated = root / "translated.jsonl"
            translated.write_text(
                "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EpubSemanticError, "blocked"):
                apply_translations(output, translated)

            self.assertEqual(reconstruction.read_bytes(), before)
            self.assertFalse((output / "audit" / "semantic-translation.json").exists())

    def test_source_text_cannot_be_submitted_as_simplified_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            import_epub(source, output)
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for unit in units:
                unit["translated_markdown"] = unit["source_markdown"]
            translated = root / "translated.jsonl"
            translated.write_text(
                "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EpubSemanticError, "unchanged source"):
                apply_translations(output, translated)

    def test_commit_failure_rolls_back_chapters_manifest_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            import_epub(source, output)
            units = [
                json.loads(line)
                for line in (output / "semantic" / "translation-units.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            replacements = {
                "Chapter One": "第一章",
                "Body": "正文",
                "word": "词语",
                "Index locator": "索引定位",
                "Complete": "完整的",
                "note": "注释",
                "Continuation paragraph": "续接段落",
            }
            for unit in units:
                value = unit["source_markdown"]
                for old, new in replacements.items():
                    value = value.replace(old, new)
                unit["translated_markdown"] = value
            translated = root / "translated.jsonl"
            translated.write_text(
                "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
                encoding="utf-8",
            )
            manifest_path = output / "chapters.json"
            manifest_before = manifest_path.read_bytes()
            manifest = json.loads(manifest_before)
            chapter_path = output / "chapters" / manifest[0]["filename"]
            chapter_before = chapter_path.read_bytes()

            import semantic_apply

            real_replace = semantic_apply._replace_file
            calls = 0

            def fail_once(source_path: Path, target_path: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected commit failure")
                real_replace(source_path, target_path)

            with mock.patch("semantic_apply._replace_file", side_effect=fail_once):
                with self.assertRaisesRegex(EpubSemanticError, "rolled back"):
                    apply_translations(output, translated)

            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual(chapter_path.read_bytes(), chapter_before)
            self.assertFalse((output / "audit" / "semantic-translation.json").exists())


if __name__ == "__main__":
    unittest.main()
