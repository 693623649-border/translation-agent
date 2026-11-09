from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz

from translation_agent.pipeline.graph import NodeExecutionError, NodeResult, NodeSpec
from translation_agent.pipeline.graph import book
from translation_agent.runtime.digests import sha256_file


class SourceDigestReuseTests(unittest.TestCase):
    def _prepare(self, root: Path):
        source = root / "source.pdf"
        with fitz.open() as document:
            document.new_page()
            document.save(source)
        with source.open("ab") as handle:
            handle.write(b"\n%probe000\n")
        prepared = book.prepare_book_graph(
            [str(source), "--output-dir", str(root / "out"), "--phase", "all"],
            options=book.BookGraphOptions(target_artifacts=(book.ART_SOURCE,), load_dotenv=False),
        )
        return source, prepared

    def test_cold_and_repeated_hot_decisions_hash_source_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, prepared = self._prepare(Path(temporary))
            counts = []
            for index in range(3):
                hashed = []

                def counted(path):
                    if path.resolve() == source.resolve():
                        hashed.append(path)
                    return sha256_file(path)

                with patch.object(book, "sha256_file", side_effect=counted):
                    result = prepared.execute()
                counts.append(len(hashed))
                self.assertEqual(result.executed if index == 0 else result.skipped, (book.NODE_SOURCE,))
            self.assertEqual(counts, [1, 1, 1])

    def test_same_length_source_rewrite_with_restored_mtime_is_rejected_on_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, prepared = self._prepare(Path(temporary))
            prepared.execute()
            before = source.stat()
            source.write_bytes(source.read_bytes().replace(b"%probe000", b"%probe111"))
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(source.stat().st_size, before.st_size)
            with self.assertRaises(NodeExecutionError) as caught:
                prepared.execute()
            self.assertIsInstance(caught.exception.cause, book.SourceBindingError)
            self.assertIn("different source PDF", str(caught.exception))

    def test_damaged_state_forces_rerun_and_binding_tamper_still_fails(self) -> None:
        for target in ("state_digest", "state_fingerprint", "binding"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                source, prepared = self._prepare(Path(temporary))
                result = prepared.execute()
                if target == "binding":
                    path = result.state_path.parent / "source.json"
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload["sha256"] = "0" * 64
                else:
                    path = result.state_path
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    node = payload["nodes"][book.NODE_SOURCE]
                    if target == "state_digest":
                        node["outputs"][book.ART_SOURCE]["sha256"] = "0" * 64
                    else:
                        node["output_fingerprints"][book.ART_SOURCE] = ""
                path.write_text(json.dumps(payload), encoding="utf-8")
                if target == "binding":
                    with self.assertRaises(NodeExecutionError) as caught:
                        prepared.execute()
                    self.assertIsInstance(caught.exception.cause, book.SourceBindingError)
                else:
                    rerun = prepared.execute()
                    self.assertEqual(rerun.executed, (book.NODE_SOURCE,))
                    self.assertEqual(rerun.values[book.ART_SOURCE]["sha256"], sha256_file(source))

    def test_source_write_during_handler_invalidates_memo_before_output_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, prepared = self._prepare(Path(temporary))
            original = next(node for node in prepared.graph.nodes if node.name == book.NODE_SOURCE)

            def handler(context):
                result = original.handler(context)
                before = source.stat()
                source.write_bytes(source.read_bytes().replace(b"%probe000", b"%probe111"))
                os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
                return result

            from dataclasses import replace
            prepared.graph.replace(book.NODE_SOURCE, replace(original, handler=handler))
            with self.assertRaises(NodeExecutionError) as caught:
                prepared.execute()
            self.assertIn("no longer matches its digest", str(caught.exception))

    def test_other_artifact_is_revalidated_after_a_node_writes_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, prepared = self._prepare(root)
            artifact = root / "output.bin"
            artifact.write_bytes(b"original")

            def handler(context):
                value = book._file_artifact(artifact)
                artifact.write_bytes(b"modified")
                return NodeResult(outputs={book.ART_DOCX: value})

            prepared.graph.replace(book.NODE_DOCX, NodeSpec(name=book.NODE_DOCX, handler=handler, provides=(book.ART_DOCX,)))
            from translation_agent.pipeline.graph import GraphExecutor
            with self.assertRaises(NodeExecutionError) as caught:
                GraphExecutor(prepared.graph).execute(prepared.context, targets=(book.ART_DOCX,))
            self.assertIn("no longer matches its digest", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
