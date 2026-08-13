from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from epub_semantic_import import EpubSemanticError, apply_translations, import_epub
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


class EpubSemanticImportTests(unittest.TestCase):
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
                unit["translated_markdown"] = unit["source_markdown"].replace("Chapter One", "第一章").replace("Body", "正文").replace("Complete", "完整")
            translated = root / "translated.jsonl"
            translated.write_text("".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units), encoding="utf-8")

            result = apply_translations(output, translated)
            manifest = json.loads((output / "chapters.json").read_text(encoding="utf-8"))
            markdown = (output / "chapters" / manifest[0]["filename"]).read_text(encoding="utf-8")

            self.assertEqual(result["status"], "passed")
            self.assertEqual(manifest[0]["display_title"], "第一章")
            self.assertTrue(parse_markdown_footnotes(markdown).valid)
            reconstruction = json.loads((output / "audit" / "semantic-reconstruction.json").read_text(encoding="utf-8"))
            self.assertEqual(reconstruction["status"], "passed")
            self.assertFalse(reconstruction["release_blocked"])
            self.assertEqual(
                reconstruction["contract_mode"],
                "epub-spine-translated-markdown-footnotes",
            )

            units[1]["translated_markdown"] = units[1]["translated_markdown"].replace("[^epub-", "[^changed-")
            translated.write_text("".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units), encoding="utf-8")
            with self.assertRaises(EpubSemanticError):
                apply_translations(output, translated)


if __name__ == "__main__":
    unittest.main()
