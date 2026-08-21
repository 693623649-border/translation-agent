from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from semantic_translation_runner import (
    SemanticTranslationError,
    build_prompt,
    protect_tokens,
    restore_tokens,
    batch_units,
    translate_units,
)


class SemanticTranslationRunnerTests(unittest.TestCase):
    def test_prepare_is_network_free_and_contains_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "# Heading\n\nBody[^n]."
            unit = {"id": "c-u1", "chapter_id": "c", "sequence": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_markdown": source}
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")
            output = root / "prepared.jsonl"

            report = translate_units(units, output, glossary={"power": "权力"})
            prepared = json.loads(output.read_text(encoding="utf-8"))

            self.assertEqual(report["mode"], "prepare")
            self.assertIn("power => 权力", prepared["prompt"])
            self.assertIn("⟦SEMANTIC_TOKEN_0000⟧", prepared["prompt"])

    def test_prompt_requires_complete_academic_translation_and_protects_link_target(self) -> None:
        source = "See [morality](part0015.html#id_705)."
        unit = {
            "id": "index-u1",
            "chapter_id": "index",
            "sequence": 1,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "source_markdown": source,
        }

        prompt, protected = build_prompt([unit], target_language="简体中文", glossary={})

        self.assertIn("不得摘要、删节、扩写", prompt)
        self.assertNotIn("part0015.html#id_705", prompt)
        self.assertEqual(protected[0][1], ("part0015.html#id_705",))

    def test_runner_restores_markers_and_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "Text[^n].\n\n[^n]: Note."
            unit = {"id": "c-u1", "chapter_id": "c", "sequence": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_markdown": source}
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")
            calls = []

            def request(prompt: str) -> str:
                calls.append(prompt)
                return (
                    "⟦UNIT:c-u1:START⟧\n"
                    "译文⟦SEMANTIC_TOKEN_0000⟧。\n\n"
                    "⟦SEMANTIC_TOKEN_0001⟧: 注释。\n"
                    "⟦UNIT:c-u1:END⟧"
                )

            first = translate_units(units, root / "one.jsonl", cache_dir=root / "cache", request=request)
            second = translate_units(units, root / "two.jsonl", cache_dir=root / "cache", request=request)
            result = json.loads((root / "two.jsonl").read_text(encoding="utf-8"))

            self.assertEqual(len(calls), 1)
            self.assertEqual(first["cache_hits"], 0)
            self.assertEqual(second["cache_hits"], 1)
            self.assertIn("[^n]", result["translated_markdown"])
            self.assertFalse(result["translated_markdown"].startswith("```"))

    def test_batching_stays_inside_chapters_and_honors_max_chars(self) -> None:
        units = []
        for chapter, sequence, text in (
            ("a", 1, "one"), ("a", 2, "two"), ("a", 3, "x" * 100),
            ("b", 1, "three"),
        ):
            units.append({"id": f"{chapter}-{sequence}", "chapter_id": chapter, "sequence": sequence, "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "source_markdown": text})

        batches = batch_units(units, max_chars=150)

        self.assertTrue(all(len({unit["chapter_id"] for unit in batch}) == 1 for batch in batches))
        self.assertGreaterEqual(len(batches), 3)

    def test_batches_run_concurrently_and_retry_failed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = []
            for chapter in ("a", "b"):
                source = f"English sentence for {chapter}."
                units.append({"id": f"{chapter}-1", "chapter_id": chapter, "sequence": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_markdown": source})
            path = root / "units.jsonl"
            path.write_text("".join(json.dumps(unit) + "\n" for unit in units))
            barrier = threading.Barrier(2)
            counts = {"a-1": 0, "b-1": 0}

            def request(prompt: str) -> str:
                unit_id = "a-1" if "⟦UNIT:a-1:START⟧" in prompt else "b-1"
                counts[unit_id] += 1
                if counts[unit_id] == 1:
                    barrier.wait(timeout=2)
                if unit_id == "a-1" and counts[unit_id] == 1:
                    return "broken"
                return f"⟦UNIT:{unit_id}:START⟧\n中文译文。\n⟦UNIT:{unit_id}:END⟧"

            report = translate_units(path, root / "out.jsonl", request=request, concurrency=2, retries=2)

            self.assertEqual(report["batch_count"], 2)
            self.assertEqual(counts, {"a-1": 2, "b-1": 1})

    def test_persistent_failure_does_not_write_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "Long English source sentence."
            unit = {"id": "c-1", "chapter_id": "c", "sequence": 1, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_markdown": source}
            path = root / "units.jsonl"; path.write_text(json.dumps(unit) + "\n")
            output = root / "out.jsonl"

            with self.assertRaises(SemanticTranslationError):
                translate_units(path, output, request=lambda _: "invalid", retries=2)
            self.assertFalse(output.exists())

    def test_structurally_bad_batch_is_split_without_losing_unit_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = []
            for sequence in (1, 2):
                source = f"Long English source sentence {sequence}."
                units.append({"id": f"c-{sequence}", "chapter_id": "c", "sequence": sequence, "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_markdown": source})
            path = root / "units.jsonl"
            path.write_text("".join(json.dumps(unit) + "\n" for unit in units))

            def request(prompt: str) -> str:
                if "⟦UNIT:c-1:START⟧" in prompt and "⟦UNIT:c-2:START⟧" in prompt:
                    return "malformed combined response"
                unit_id = "c-1" if "⟦UNIT:c-1:START⟧" in prompt else "c-2"
                return f"⟦UNIT:{unit_id}:START⟧\n中文译文。\n⟦UNIT:{unit_id}:END⟧"

            report = translate_units(path, root / "out.jsonl", request=request, retries=1)
            rows = [
                json.loads(line)
                for line in (root / "out.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(report["unit_count"], 2)
            self.assertEqual([row["id"] for row in rows], ["c-1", "c-2"])

    def test_changed_marker_order_is_rejected(self) -> None:
        protected, tokens = protect_tokens("A[^a] B[^b]")
        self.assertIn("SEMANTIC_TOKEN", protected)
        with self.assertRaises(SemanticTranslationError):
            restore_tokens("⟦SEMANTIC_TOKEN_0001⟧ ⟦SEMANTIC_TOKEN_0000⟧", tokens)

    def test_source_as_translation_is_rejected_before_cache_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "A complete English sentence must be translated."
            unit = {
                "id": "c-1",
                "chapter_id": "c",
                "sequence": 1,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "source_markdown": source,
            }
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(SemanticTranslationError, "unchanged source"):
                translate_units(
                    units,
                    root / "translated.jsonl",
                    cache_dir=root / "cache",
                    request=lambda _: (
                        "⟦UNIT:c-1:START⟧\n"
                        f"{source}\n"
                        "⟦UNIT:c-1:END⟧"
                    ),
                    retries=1,
                )

            self.assertFalse((root / "translated.jsonl").exists())
            self.assertFalse((root / "cache").exists())

    def test_cache_identity_includes_provider_endpoint_and_prompt_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "A complete English sentence."
            unit = {
                "id": "c-1",
                "chapter_id": "c",
                "sequence": 1,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "source_markdown": source,
            }
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            translate_units(
                units,
                root / "one.jsonl",
                provider="provider-a",
                base_url="https://one.example/v1",
                prompt_profile="academic-v1",
                thinking="disabled",
                temperature=0.0,
            )
            translate_units(
                units,
                root / "two.jsonl",
                provider="provider-a",
                base_url="https://two.example/v1",
                prompt_profile="academic-v2",
                thinking="enabled",
                temperature=0.2,
            )
            one = json.loads((root / "one.jsonl").read_text(encoding="utf-8"))
            two = json.loads((root / "two.jsonl").read_text(encoding="utf-8"))

            self.assertNotEqual(one["cache_key"], two["cache_key"])


if __name__ == "__main__":
    unittest.main()
