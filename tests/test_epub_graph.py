from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

import pipeline_graph.epub as epub_graph_module
from pipeline_graph.core import NodeExecutionError
from pipeline_graph.epub import (
    ART_EPUB,
    ART_EPUB_REPORT,
    ART_READER_CHAPTERS,
    ART_SEMANTIC_CHAPTERS,
    ART_SOURCE,
    ART_TRANSLATIONS,
    ART_TRANSLATION_UNITS,
    EPUB_GRAPH_ADAPTER_VERSION,
    EpubGraphConfigurationError,
    EpubGraphOptions,
    EpubSourceChangedError,
    NODE_APPLY,
    NODE_EPUB,
    NODE_IMPORT,
    NODE_READER,
    NODE_SOURCE,
    NODE_TRANSLATIONS_IMPORT,
    NODE_VERIFY_EPUB,
    prepare_epub_graph,
)
from tests.test_epub_semantic_import import _write_cross_spine_link_epub


def _write_epub(path: Path, *, extra_members: dict[str, bytes] | None = None) -> None:
    members = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>''',
        "OEBPS/content.opf": b'''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1"><dc:title>Test Book</dc:title><dc:creator>A. Author</dc:creator><dc:language>en</dc:language><dc:identifier>id</dc:identifier></metadata>
 <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
 <spine><itemref idref="c1"/></spine>
</package>''',
        "OEBPS/nav.xhtml": b'''<html xmlns="http://www.w3.org/1999/xhtml"><body><nav epub:type="toc" xmlns:epub="http://www.idpf.org/2007/ops"><ol><li><a href="chapter1.xhtml">Chapter One</a></li></ol></nav></body></html>''',
        "OEBPS/chapter1.xhtml": b'''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>
<section><h1>Chapter One</h1><p>Body <em>word</em><a epub:type="noteref" href="#note-1">1</a>.</p>
<aside epub:type="footnote" id="note-1"><p>Complete <i>note</i>.</p></aside>
<p>Index locator 12.</p></section></body></html>''',
    }
    members.update(extra_members or {})
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype",
            members.pop("mimetype"),
            compress_type=zipfile.ZIP_STORED,
        )
        for name, value in members.items():
            archive.writestr(name, value)


def _translated_units(units_path: Path, target: Path) -> None:
    units = [
        json.loads(line)
        for line in units_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    replacements = (
        ("Chapter One", "第一章"),
        ("Body", "正文"),
        ("word", "词语"),
        ("Index locator", "索引定位"),
        ("Complete", "完整的"),
        ("note", "注释"),
    )
    for unit in units:
        translated = str(unit["source_markdown"])
        for source, replacement in replacements:
            translated = translated.replace(source, replacement)
        unit["translated_markdown"] = translated
    target.write_text(
        "".join(json.dumps(unit, ensure_ascii=False) + "\n" for unit in units),
        encoding="utf-8",
    )


class EpubGraphTests(unittest.TestCase):
    def test_translation_cache_symlink_cannot_write_outside_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            outside = root / "outside"
            _write_epub(source)
            output.mkdir()
            outside.mkdir()
            (output / ".translation-cache").symlink_to(
                outside,
                target_is_directory=True,
            )
            called = False

            def request(_prompt: str) -> str:
                nonlocal called
                called = True
                return ""

            with self.assertRaisesRegex(
                EpubGraphConfigurationError, "must not contain a symlink"
            ):
                prepare_epub_graph(
                    source,
                    output,
                    options=EpubGraphOptions(
                        translation_mode="run",
                        translation_request=request,
                        translation_request_fingerprint="request-v1",
                        target_artifacts=frozenset({ART_TRANSLATIONS}),
                    ),
                )
            self.assertFalse(called)
            self.assertEqual(list(outside.iterdir()), [])

    def test_source_mutation_inside_import_handler_fails_before_node_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            )
            real_import = epub_graph_module.import_epub

            def mutate_after_import(source_path: Path, output_dir: Path):
                result = real_import(source_path, output_dir)
                source_path.write_bytes(source_path.read_bytes() + b"mutated")
                return result

            with mock.patch(
                "pipeline_graph.epub.import_epub",
                side_effect=mutate_after_import,
            ):
                with self.assertRaises(NodeExecutionError) as raised:
                    prepared.execute()
            self.assertEqual(raised.exception.node_name, NODE_IMPORT)
            self.assertIsInstance(raised.exception.cause, EpubSourceChangedError)

    def test_apply_input_mutation_inside_handler_blocks_release_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            translations = root / "translations.jsonl"
            _write_epub(source)
            imported = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            ).execute()
            _translated_units(
                Path(imported.values[ART_TRANSLATION_UNITS]["path"]),
                translations,
            )
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    translation_mode="apply",
                    translations_path=translations,
                    target_artifacts=frozenset({ART_EPUB_REPORT}),
                ),
            )
            real_apply = epub_graph_module.apply_translations

            def mutate_after_apply(output_dir: Path, translations_path: Path, **kwargs):
                result = real_apply(output_dir, translations_path, **kwargs)
                translations_path.write_bytes(
                    translations_path.read_bytes() + b"\n"
                )
                return result

            with mock.patch(
                "pipeline_graph.epub.apply_translations",
                side_effect=mutate_after_apply,
            ):
                with self.assertRaises(NodeExecutionError) as raised:
                    prepared.execute()
            self.assertEqual(raised.exception.node_name, NODE_APPLY)
            self.assertIsInstance(raised.exception.cause, EpubSourceChangedError)
            self.assertFalse(
                (output / "audit" / "epub-release-report.json").exists()
            )

    def test_native_release_preserves_cross_spine_anchor_links(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_cross_spine_link_epub(source)
            result = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_EPUB_REPORT})
                ),
            ).execute()
            report = json.loads(
                Path(result.values[ART_EPUB_REPORT]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(report["release_ready"], report["errors"])

    def test_translation_units_target_has_exact_minimal_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)

            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            )

            self.assertFalse(output.exists())
            self.assertEqual(
                tuple(node.name for node in prepared.plan()),
                (NODE_SOURCE, NODE_IMPORT),
            )
            result = prepared.execute()
            self.assertEqual(result.plan, (NODE_SOURCE, NODE_IMPORT))
            self.assertIn(ART_TRANSLATION_UNITS, result.values)
            self.assertNotIn(ART_READER_CHAPTERS, result.values)
            source_artifact = result.values[ART_SOURCE]
            self.assertEqual(
                source_artifact["adapter_identity"], EPUB_GRAPH_ADAPTER_VERSION
            )
            self.assertGreater(source_artifact["entry_count"], 0)
            self.assertGreater(source_artifact["uncompressed_size"], 0)

    def test_one_publication_target_closes_without_docx_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(target_artifacts=frozenset({ART_EPUB})),
            )

            expected = (NODE_SOURCE, NODE_IMPORT, NODE_READER, NODE_EPUB)
            self.assertEqual(tuple(node.name for node in prepared.plan()), expected)
            first = prepared.execute()
            self.assertEqual(first.executed, expected)
            self.assertTrue(Path(first.values[ART_EPUB]["path"]).is_file())
            second = prepared.execute()
            self.assertEqual(second.executed, ())
            self.assertEqual(second.skipped, expected)

    def test_none_mode_replaces_a_prior_translated_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            translations = root / "translations.jsonl"
            _write_epub(source)
            import_result = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            ).execute()
            _translated_units(
                Path(import_result.values[ART_TRANSLATION_UNITS]["path"]),
                translations,
            )
            translated = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    translation_mode="apply",
                    translations_path=translations,
                    target_artifacts=frozenset({ART_EPUB}),
                ),
            ).execute()
            translated_reader = translated.values[ART_READER_CHAPTERS]
            chapter_name = json.loads(
                Path(translated_reader["manifest"]).read_text(encoding="utf-8")
            )[0]["filename"]
            self.assertIn(
                "第一章",
                (Path(translated_reader["chapter_dir"]) / chapter_name).read_text(
                    encoding="utf-8"
                ),
            )

            source_run = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    translation_mode="none",
                    target_artifacts=frozenset({ART_EPUB}),
                ),
            ).execute()
            reader = source_run.values[ART_READER_CHAPTERS]
            markdown = (Path(reader["chapter_dir"]) / chapter_name).read_text(
                encoding="utf-8"
            )
            self.assertIn("Chapter One", markdown)
            self.assertNotIn("第一章", markdown)
            self.assertFalse(reader["translation_applied"])
            self.assertFalse(
                (output / "audit" / "semantic-translation.json").exists()
            )

    def test_apply_translation_plan_binds_external_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            translations = root / "translations.jsonl"
            _write_epub(source)
            imported = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            ).execute()
            _translated_units(
                Path(imported.values[ART_TRANSLATION_UNITS]["path"]),
                translations,
            )
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    translation_mode="apply",
                    translations_path=translations,
                    target_artifacts=frozenset({ART_READER_CHAPTERS}),
                ),
            )
            self.assertEqual(
                tuple(node.name for node in prepared.plan()),
                (
                    NODE_SOURCE,
                    NODE_IMPORT,
                    NODE_TRANSLATIONS_IMPORT,
                    NODE_APPLY,
                ),
            )
            result = prepared.execute()
            self.assertIn(ART_TRANSLATIONS, result.values)
            self.assertTrue(result.values[ART_READER_CHAPTERS]["translation_applied"])

    def test_native_epub_report_is_a_full_target_closure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_EPUB_REPORT})
                ),
            )
            self.assertEqual(
                tuple(node.name for node in prepared.plan()),
                (NODE_SOURCE, NODE_IMPORT, NODE_READER, NODE_EPUB, NODE_VERIFY_EPUB),
            )
            result = prepared.execute()
            report_path = Path(result.values[ART_EPUB_REPORT]["path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["release_ready"])
            self.assertEqual(report["publication_profile"], "epub")
            self.assertEqual(report["verifier_node"], NODE_VERIFY_EPUB)

    def test_native_epub_report_failure_blocks_graph_after_writing_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)

            def corrupt_epub(
                output_path: Path,
                _chapter_dir: Path,
                _manifest: list[dict[str, object]],
                *,
                book_title: str,
                language: str,
            ) -> None:
                del book_title, language
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(b"not-an-epub")

            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_EPUB_REPORT})
                ),
            )
            with mock.patch(
                "pipeline_graph.epub.legacy.build_epub",
                side_effect=corrupt_epub,
            ):
                with self.assertRaises(NodeExecutionError) as raised:
                    prepared.execute()
            self.assertEqual(raised.exception.node_name, NODE_VERIFY_EPUB)
            report = json.loads(
                (output / "audit" / "epub-release-report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(report["release_ready"])

    def test_source_inside_managed_output_is_rejected_before_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            source = output / "Test_Book.epub"
            _write_epub(source)
            with self.assertRaisesRegex(
                EpubGraphConfigurationError, "outside the managed output"
            ):
                prepare_epub_graph(
                    source,
                    output,
                    options=EpubGraphOptions(
                        target_artifacts=frozenset({ART_EPUB})
                    ),
                )
            self.assertTrue(zipfile.is_zipfile(source))

    def test_apply_input_inside_managed_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            translations = output / "semantic" / "translations.jsonl"
            _write_epub(source)
            translations.parent.mkdir(parents=True)
            translations.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                EpubGraphConfigurationError, "outside the managed output"
            ):
                prepare_epub_graph(
                    source,
                    output,
                    options=EpubGraphOptions(
                        translation_mode="apply",
                        translations_path=translations,
                        target_artifacts=frozenset({ART_READER_CHAPTERS}),
                    ),
                )
            self.assertTrue(translations.is_file())

    def test_none_reader_cache_clears_stale_translation_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_EPUB_REPORT})
                ),
            )
            prepared.execute()
            stale_audit = output / "audit" / "semantic-translation.json"
            stale_audit.write_text("{}\n", encoding="utf-8")

            rerun = prepared.execute()

            self.assertIn(NODE_READER, rerun.executed)
            self.assertIn(NODE_VERIFY_EPUB, rerun.executed)
            self.assertFalse(stale_audit.exists())

    def test_translation_unit_validator_rejects_hash_current_noncanonical_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            )
            result = prepared.execute()
            artifact = dict(result.values[ART_TRANSLATION_UNITS])
            units_path = Path(artifact["path"])
            payload = b'{"not":"canonical"}\n'
            units_path.write_bytes(payload)
            artifact.update(
                {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                    "unit_count": 1,
                }
            )
            validator = prepared.context.value_validators[ART_TRANSLATION_UNITS]
            self.assertFalse(validator(prepared.context, artifact))

    def test_import_cache_binds_canonical_units_to_source_chapter_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            output = root / "output"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                output,
                options=EpubGraphOptions(
                    target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                ),
            )
            result = prepared.execute()
            artifact = dict(result.values[ART_TRANSLATION_UNITS])
            units_path = Path(artifact["path"])
            rows = [
                json.loads(line)
                for line in units_path.read_text(encoding="utf-8").splitlines()
            ]
            rows[0]["source_markdown"] += " altered"
            rows[0]["source_sha256"] = hashlib.sha256(
                rows[0]["source_markdown"].encode("utf-8")
            ).hexdigest()
            payload = "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in rows
            ).encode("utf-8")
            units_path.write_bytes(payload)
            artifact.update(
                {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            )
            unit_validator = prepared.context.value_validators[
                ART_TRANSLATION_UNITS
            ]
            self.assertTrue(unit_validator(prepared.context, artifact))
            import_node = next(
                node for node in prepared.graph.nodes if node.name == NODE_IMPORT
            )
            assert import_node.cache_validator is not None
            self.assertFalse(
                import_node.cache_validator(
                    prepared.context,
                    {
                        ART_SEMANTIC_CHAPTERS: result.values[
                            ART_SEMANTIC_CHAPTERS
                        ],
                        ART_TRANSLATION_UNITS: artifact,
                    },
                )
            )

    def test_prepared_graph_rejects_source_or_translation_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            _write_epub(source)
            prepared = prepare_epub_graph(
                source,
                root / "output",
                options=EpubGraphOptions(target_artifacts=frozenset({ART_SOURCE})),
            )
            source.write_bytes(source.read_bytes() + b"changed")
            with self.assertRaises(EpubSourceChangedError):
                prepared.execute()

    def test_archive_inspection_rejects_unsafe_duplicate_encrypted_and_bombs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.epub"
            _write_epub(unsafe, extra_members={"../escape": b"bad"})
            with self.assertRaisesRegex(EpubGraphConfigurationError, "unsafe member"):
                prepare_epub_graph(unsafe, root / "out-unsafe")

            duplicate = root / "duplicate.epub"
            _write_epub(duplicate)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "a") as archive:
                    archive.writestr("OEBPS/chapter1.xhtml", b"duplicate")
            with self.assertRaisesRegex(EpubGraphConfigurationError, "duplicate normalized"):
                prepare_epub_graph(duplicate, root / "out-duplicate")

            encrypted = root / "encrypted.epub"
            _write_epub(
                encrypted,
                extra_members={
                    "META-INF/encryption.xml": b"<EncryptedData/>",
                },
            )
            with self.assertRaisesRegex(EpubGraphConfigurationError, "encrypted/DRM"):
                prepare_epub_graph(encrypted, root / "out-encrypted")

            over_budget = root / "budget.epub"
            _write_epub(over_budget, extra_members={"OEBPS/extra.bin": b"12345"})
            with mock.patch(
                "pipeline_graph.epub.MAX_EPUB_TOTAL_UNCOMPRESSED_BYTES", 10
            ):
                with self.assertRaisesRegex(EpubGraphConfigurationError, "aggregate"):
                    prepare_epub_graph(over_budget, root / "out-budget")

            ratio = root / "ratio.epub"
            _write_epub(ratio)
            with zipfile.ZipFile(ratio, "a", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("OEBPS/repetitive.bin", b"0" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(EpubGraphConfigurationError, "compression ratio"):
                prepare_epub_graph(ratio, root / "out-ratio")

    def test_graph_never_loads_dotenv_or_infers_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.epub"
            _write_epub(source)
            with mock.patch(
                "semantic_translation_runner.load_env_file",
                side_effect=AssertionError("dotenv must not be loaded"),
            ):
                result = prepare_epub_graph(
                    source,
                    root / "output",
                    options=EpubGraphOptions(
                        target_artifacts=frozenset({ART_TRANSLATION_UNITS})
                    ),
                ).execute()
            self.assertIn(ART_TRANSLATION_UNITS, result.values)
            with self.assertRaisesRegex(
                EpubGraphConfigurationError, "requires translation_request"
            ):
                EpubGraphOptions(
                    translation_mode="run",
                    target_artifacts=frozenset({ART_TRANSLATIONS}),
                )


if __name__ == "__main__":
    unittest.main()
