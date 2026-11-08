from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from translation_agent.semantic.semantic_translation_runner import (
    SemanticOutputError,
    SemanticRequestError,
    SemanticTranslationError,
    build_openai_chat_request,
    build_prompt,
    protect_tokens,
    restore_tokens,
    batch_units,
    translate_units,
)


def _unit(unit_id: str, chapter_id: str, sequence: int, source: str) -> dict:
    return {
        "schema_version": 1,
        "id": unit_id,
        "chapter_id": chapter_id,
        "sequence": sequence,
        "kind": "paragraph",
        "source_href": f"{chapter_id}.xhtml",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "source_markdown": source,
    }


class SemanticTranslationRunnerTests(unittest.TestCase):
    def test_prepare_is_network_free_and_contains_glossary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "# Heading\n\nBody[^n]."
            unit = _unit("c-u1", "c", 1, source)
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
            unit = _unit("c-u1", "c", 1, source)
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
            units.append(_unit(f"{chapter}-{sequence}", chapter, sequence, text))

        batches = batch_units(units, max_chars=150)

        self.assertTrue(all(len({unit["chapter_id"] for unit in batch}) == 1 for batch in batches))
        self.assertGreaterEqual(len(batches), 3)

    def test_batches_run_concurrently_and_retry_failed_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = []
            for chapter in ("a", "b"):
                source = f"English sentence for {chapter}."
                units.append(_unit(f"{chapter}-1", chapter, 1, source))
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
            unit = _unit("c-1", "c", 1, source)
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
                units.append(_unit(f"c-{sequence}", "c", sequence, source))
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
        with self.assertRaises(SemanticOutputError):
            restore_tokens("⟦SEMANTIC_TOKEN_0001⟧ ⟦SEMANTIC_TOKEN_0000⟧", tokens)

    def test_source_as_translation_is_rejected_before_cache_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "A complete English sentence must be translated."
            unit = _unit("c-1", "c", 1, source)
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

    def test_markup_heavy_reference_unit_allows_a_translated_term(self) -> None:
        source = (
            '<span id="key-chu1_4-6"></span>'
            "[＊６](007_intro.xhtml#ref-chu1_4-6)\u3000段階論"
        )
        translated = (
            '<span id="key-chu1_4-6"></span>'
            "[＊６](007_intro.xhtml#ref-chu1_4-6)\u3000阶段论"
        )
        protected, _tokens = protect_tokens(source)
        model_output = protected.replace("段階論", "阶段论")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = _unit("c-1", "c", 1, source)
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            report = translate_units(
                units,
                root / "translated.jsonl",
                cache_dir=root / "cache",
                request=lambda _: (
                    "⟦UNIT:c-1:START⟧\n"
                    f"{model_output}\n"
                    "⟦UNIT:c-1:END⟧"
                ),
                retries=1,
            )
            rows = [
                json.loads(line)
                for line in (root / "translated.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(report["unit_count"], 1)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["translated_markdown"], translated)

    def test_markup_does_not_hide_unchanged_latin_prose(self) -> None:
        source = (
            '<span id="key-chu1_4-6"></span>'
            "A complete English sentence that needs translation."
        )
        echoed, _tokens = protect_tokens(source)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = _unit("c-1", "c", 1, source)
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(SemanticTranslationError, "unchanged source"):
                translate_units(
                    units,
                    root / "translated.jsonl",
                    cache_dir=root / "cache",
                    request=lambda _: (
                        "⟦UNIT:c-1:START⟧\n"
                        f"{echoed}\n"
                        "⟦UNIT:c-1:END⟧"
                    ),
                    retries=1,
                )

    def test_identical_cjk_term_inside_markup_is_accepted(self) -> None:
        source = (
            '<span id="key-chu1_4-10"></span>'
            "[＊10](007_intro.xhtml#ref-chu1_4-10)\u3000京都学派"
        )
        echoed, _tokens = protect_tokens(source)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = _unit("c-1", "c", 1, source)
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            report = translate_units(
                units,
                root / "translated.jsonl",
                cache_dir=root / "cache",
                request=lambda _: (
                    "⟦UNIT:c-1:START⟧\n"
                    f"{echoed}\n"
                    "⟦UNIT:c-1:END⟧"
                ),
                retries=1,
            )
            rows = [
                json.loads(line)
                for line in (root / "translated.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

            self.assertEqual(report["unit_count"], 1)
            self.assertEqual(rows[0]["translated_markdown"], source)

    def test_cache_identity_includes_provider_endpoint_and_prompt_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "A complete English sentence."
            unit = _unit("c-1", "c", 1, source)
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

    def test_cache_identity_includes_the_actual_prompt_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unit = _unit("c-1", "c", 1, "A complete English sentence.")
            units = root / "units.jsonl"
            units.write_text(json.dumps(unit) + "\n", encoding="utf-8")

            with patch(
                "translation_agent.semantic.semantic_translation_runner.PROMPT_CONTRACT_SHA256",
                "0" * 64,
            ):
                translate_units(units, root / "one.jsonl")
            with patch(
                "translation_agent.semantic.semantic_translation_runner.PROMPT_CONTRACT_SHA256",
                "1" * 64,
            ):
                translate_units(units, root / "two.jsonl")

            one = json.loads((root / "one.jsonl").read_text(encoding="utf-8"))
            two = json.loads((root / "two.jsonl").read_text(encoding="utf-8"))
            self.assertNotEqual(one["cache_key"], two["cache_key"])


class SemanticRequestRecoveryTests(unittest.TestCase):
    def _units(self, root: Path, chapters: str = "ab") -> Path:
        path = root / "units.jsonl"
        path.write_text("".join(
            json.dumps(_unit(f"{chapter}-{i}", chapter, i, f"English sentence number {i}.")) + "\n"
            for chapter in chapters for i in range(1, 9)
        ), encoding="utf-8")
        return path

    def _request(self):
        return build_openai_chat_request(
            api_key="offline-test", base_url="https://offline.invalid/v1",
            model="offline", timeout=1,
        )

    def _ids(self, request) -> list[str]:
        payload = json.loads(request.data)
        return re.findall(r"⟦UNIT:([^:]+):START⟧", payload["messages"][-1]["content"])

    def _response(self, ids: list[str]) -> io.BytesIO:
        content = "\n".join(f"⟦UNIT:{uid}:START⟧\n完整中文译文。\n⟦UNIT:{uid}:END⟧" for uid in ids)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    def test_permanent_http_errors_stop_without_retry_split_or_next_batch(self) -> None:
        for status in (400, 401, 403, 404):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                units = self._units(root)
                calls = []

                def urlopen(request, **kwargs):
                    calls.append(len(self._ids(request)))
                    raise HTTPError(request.full_url, status, "offline probe", {}, io.BytesIO(b"offline probe"))

                with patch("urllib.request.urlopen", urlopen), patch("time.sleep") as sleep:
                    with self.assertRaises(SemanticRequestError) as caught:
                        translate_units(units, root / "out.jsonl", request=self._request(), concurrency=1)
                self.assertEqual(caught.exception.status_code, status)
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(calls, [8])
                sleep.assert_not_called()
                self.assertFalse((root / "out.jsonl").exists())

    def test_transient_http_and_network_errors_have_bounded_attempts_without_split(self) -> None:
        for status in (408, 429, 500, 503, None):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                units = self._units(root)
                calls = []

                def urlopen(request, **kwargs):
                    calls.append(len(self._ids(request)))
                    if status is None:
                        raise URLError("offline network failure")
                    raise HTTPError(request.full_url, status, "offline", {}, io.BytesIO(b"offline"))

                with patch("urllib.request.urlopen", urlopen), patch("time.sleep") as sleep:
                    with self.assertRaises(SemanticRequestError) as caught:
                        translate_units(units, root / "out.jsonl", request=self._request(), concurrency=1)
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(calls, [8, 8, 8])
                self.assertEqual(sleep.call_count, 2)

    def test_transient_http_error_recovers_in_same_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = self._units(root, "a")
            calls = []

            def urlopen(request, **kwargs):
                ids = self._ids(request)
                calls.append(len(ids))
                if len(calls) < 3:
                    raise HTTPError(request.full_url, 429, "offline", {}, io.BytesIO(b"rate limit"))
                return self._response(ids)

            with patch("urllib.request.urlopen", urlopen), patch("time.sleep"):
                translate_units(units, root / "out.jsonl", request=self._request(), concurrency=1)
            self.assertEqual(calls, [8, 8, 8])
            self.assertEqual(len((root / "out.jsonl").read_text(encoding="utf-8").splitlines()), 8)

    def test_unknown_request_error_is_not_retried_or_split(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = self._units(root)
            with patch("time.sleep") as sleep:
                from unittest.mock import Mock
                request = Mock(side_effect=SemanticTranslationError("unclassified request failure"))
                with self.assertRaisesRegex(SemanticTranslationError, "unclassified"):
                    translate_units(units, root / "out.jsonl", request=request, concurrency=1)
            self.assertEqual(request.call_count, 1)
            sleep.assert_not_called()

    def test_successful_cache_survives_failure_and_bad_cache_fails_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = self._units(root)
            calls = []

            def urlopen(request, **kwargs):
                ids = self._ids(request)
                calls.append(ids[0])
                if ids[0].startswith("b"):
                    raise HTTPError(request.full_url, 401, "offline", {}, io.BytesIO(b"offline"))
                return self._response(ids)

            with patch("urllib.request.urlopen", urlopen):
                with self.assertRaises(SemanticRequestError):
                    translate_units(units, root / "out.jsonl", cache_dir=root / "cache", request=self._request(), concurrency=1)
            self.assertEqual(calls, ["a-1", "b-1"])
            calls.clear()

            def recovered(request, **kwargs):
                ids = self._ids(request)
                calls.append(ids[0])
                return self._response(ids)

            with patch("urllib.request.urlopen", recovered):
                report = translate_units(units, root / "out.jsonl", cache_dir=root / "cache", request=self._request(), concurrency=1)
            self.assertEqual(report["cache_hits"], 1)
            self.assertEqual(calls, ["b-1"])
            for corrupt in ("not JSON", json.dumps({"model_output": "broken markers"})):
                for cache in (root / "cache").glob("*.json"):
                    cache.write_text(corrupt, encoding="utf-8")
                with patch("urllib.request.urlopen") as transport, patch("time.sleep") as sleep:
                    with self.assertRaisesRegex(SemanticTranslationError, "invalid batch cache"):
                        translate_units(units, root / "bad.jsonl", cache_dir=root / "cache", request=self._request(), concurrency=1)
                transport.assert_not_called()
                sleep.assert_not_called()
                self.assertFalse((root / "bad.jsonl").exists())

    def test_inflight_request_finishes_but_no_more_work_or_retry_starts(self) -> None:
        from concurrent.futures import ThreadPoolExecutor as RealExecutor

        for inflight_succeeds in (True, False):
            with self.subTest(success=inflight_succeeds), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                units = self._units(root, "abcd")
                barrier = threading.Barrier(2)
                failed = threading.Event()
                calls = []

                class ObservedExecutor(RealExecutor):
                    def submit(self, fn, *args, **kwargs):
                        future = super().submit(fn, *args, **kwargs)
                        future.add_done_callback(lambda f: failed.set() if f.exception() is not None else None)
                        return future

                def urlopen(request, **kwargs):
                    ids = self._ids(request)
                    calls.append(ids[0])
                    barrier.wait(timeout=3)
                    if ids[0].startswith("a"):
                        raise HTTPError(request.full_url, 403, "offline", {}, io.BytesIO(b"offline"))
                    self.assertTrue(failed.wait(timeout=3))
                    if not inflight_succeeds:
                        raise HTTPError(request.full_url, 503, "offline", {}, io.BytesIO(b"offline"))
                    return self._response(ids)

                with patch("translation_agent.semantic.semantic_translation_runner.ThreadPoolExecutor", ObservedExecutor), patch("urllib.request.urlopen", urlopen), patch("time.sleep"):
                    with self.assertRaises(SemanticRequestError) as caught:
                        translate_units(units, root / "out.jsonl", cache_dir=root / "cache", request=self._request(), concurrency=2)
                self.assertEqual(caught.exception.status_code, 403)
                self.assertCountEqual(calls, ["a-1", "b-1"])
                self.assertEqual(len(list((root / "cache").glob("*.json"))), int(inflight_succeeds))
                self.assertFalse((root / "out.jsonl").exists())

    def test_http_envelope_failure_does_not_trigger_content_split(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = self._units(root)
            with patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"choices": []}')) as transport:
                with self.assertRaisesRegex(SemanticRequestError, "no message content"):
                    translate_units(units, root / "out.jsonl", request=self._request(), concurrency=1)
            self.assertEqual(transport.call_count, 1)


class OpenAICompatibleTransportTests(unittest.TestCase):
    def _capture(self, thinking: str) -> dict:
        captured: dict = {}

        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *exc: object) -> bool:
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": "译文"}}]}
                ).encode("utf-8")

        def _urlopen(request: object, timeout: int | None = None) -> "_Response":
            captured.update(json.loads(request.data.decode("utf-8")))  # type: ignore[attr-defined]
            return _Response()

        with patch("urllib.request.urlopen", _urlopen):
            request = build_openai_chat_request(
                api_key="test-key",
                base_url="https://api.deepseek.com",
                model="deepseek-flash",
                timeout=30,
                thinking=thinking,
            )
            self.assertEqual(request("hi"), "译文")
        return captured

    def test_reasoning_levels_are_sent_as_reasoning_effort(self) -> None:
        for level in ("low", "high", "max"):
            payload = self._capture(level)
            self.assertEqual(payload["reasoning_effort"], level)
            self.assertNotIn("thinking", payload)

    def test_legacy_thinking_dialect_is_unchanged(self) -> None:
        disabled = self._capture("disabled")
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", disabled)
        enabled = self._capture("enabled")
        self.assertEqual(enabled["thinking"], {"type": "enabled"})
        omitted = self._capture("omit")
        self.assertNotIn("thinking", omitted)
        self.assertNotIn("reasoning_effort", omitted)


if __name__ == "__main__":
    unittest.main()
