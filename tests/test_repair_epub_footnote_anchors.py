from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from lxml import etree

from epub_semantic_import import import_epub
from tools.repair_epub_footnote_anchors import (
    RepairError,
    repair_epub_footnote_anchors,
)


def _write_epub(
    path: Path,
    *,
    duplicate_extra_count: int = 1,
    anonymous_note_count: int = 1,
    missing_target: bool = False,
    colliding_id: bool = False,
) -> None:
    hrefs = ['<a epub:type="noteref" href="#note-1">1</a>']
    for _ in range(duplicate_extra_count):
        target = "missing" if missing_target else "note-1"
        hrefs.append(f'<a epub:type="noteref" href="#{target}">1</a>')
    anonymous = "".join(
        f'<aside epub:type="footnote"><p>Anonymous {index}</p></aside>'
        for index in range(1, anonymous_note_count + 1)
    )
    collision = '<p id="note-1__repair_2">collision</p>' if colliding_id else ""
    members = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
        "OEBPS/content.opf": b'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Repair Test</dc:title><dc:language>en</dc:language><dc:identifier>id</dc:identifier></metadata>
 <manifest><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine>
</package>''',
        "OEBPS/chapter1.xhtml": f'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<section><h1>Chapter</h1><p>Refs {" ".join(hrefs)}.</p>
<aside epub:type="footnote" id="note-1"><p>Original note.</p></aside>
{collision}
{anonymous}</section>
</body></html>'''.encode("utf-8"),
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", members.pop("mimetype"), compress_type=zipfile.ZIP_STORED)
        for name, value in members.items():
            archive.writestr(name, value)


class RepairEpubFootnoteAnchorsTests(unittest.TestCase):
    def test_repairs_duplicate_noteref_to_anonymous_footnote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.epub"
            output = root / "repaired.epub"
            report = root / "report.json"
            _write_epub(source)

            result = repair_epub_footnote_anchors(source, output, report)

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["repair_count"], 1)
            self.assertEqual(
                result["repairs"],
                [
                    {
                        "doc": "OEBPS/chapter1.xhtml",
                        "old_id": "note-1",
                        "new_id": "note-1__repair_2",
                        "occurrence": 2,
                    }
                ],
            )
            self.assertEqual(json.loads(report.read_text(encoding="utf-8")), result)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist()[0], "mimetype")
                self.assertEqual(
                    archive.getinfo("mimetype").compress_type,
                    zipfile.ZIP_STORED,
                )
                chapter = archive.read("OEBPS/chapter1.xhtml")
            tree = etree.fromstring(chapter)
            hrefs = tree.xpath("//*[local-name()='a']/@href")
            ids = tree.xpath("//*[@id]/@id")
            self.assertEqual(hrefs, ["#note-1", "#note-1__repair_2"])
            self.assertIn("note-1__repair_2", ids)

            import_result = import_epub(output, root / "semantic")
            self.assertEqual(import_result["status"], "passed")
            audit = json.loads(
                (root / "semantic" / "audit" / "semantic-reconstruction.json")
                .read_text(encoding="utf-8")
            )
            self.assertFalse(audit["release_blocked"])

    def test_count_mismatch_fails_without_output_epub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.epub"
            output = root / "repaired.epub"
            report = root / "report.json"
            _write_epub(source, anonymous_note_count=0)

            with self.assertRaisesRegex(RepairError, "does not match"):
                repair_epub_footnote_anchors(source, output, report)

            self.assertFalse(output.exists())
            self.assertEqual(
                json.loads(report.read_text(encoding="utf-8"))["status"],
                "blocked",
            )

    def test_missing_target_fails_without_output_epub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.epub"
            output = root / "repaired.epub"
            report = root / "report.json"
            _write_epub(source, missing_target=True)

            with self.assertRaisesRegex(RepairError, "target missing"):
                repair_epub_footnote_anchors(source, output, report)

            self.assertFalse(output.exists())

    def test_generated_id_collision_fails_without_output_epub(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.epub"
            output = root / "repaired.epub"
            report = root / "report.json"
            _write_epub(source, colliding_id=True)

            with self.assertRaisesRegex(RepairError, "collision"):
                repair_epub_footnote_anchors(source, output, report)

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
