"""Local vLLM translation provider: payload compatibility and profile wiring.

Covers the ``deploy/local-llm`` integration (branch
``deploy/vllm-translation-rtx5090``): the local client must never send
DeepSeek-only payload fields to the strict OpenAI-compatible server, must be
usable without a credential, and must resolve identities that never collide
with the cloud DeepSeek cache.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from book_pipeline import (
    DEFAULT_LOCAL_LLM_API_BASE,
    DEFAULT_LOCAL_LLM_MODEL,
    LocalClient,
    build_parser,
    build_translation_client,
    resolve_translation_identity,
)
from kb_translation import DeepSeekTranslator, KbTranslationError, LocalTranslator
from pipeline_profiles import load_pipeline_profiles


class RecordingLocalClient(LocalClient):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.requests: list[tuple[str, dict]] = []

    def _post(self, endpoint: str, payload: dict) -> dict:
        self.requests.append((endpoint, payload))
        return {"choices": [{"message": {"content": '{"entries": []}'}}]}


class LocalClientTests(unittest.TestCase):
    def test_local_client_omits_cloud_only_fields(self) -> None:
        client = RecordingLocalClient(api_key="local")
        self.assertEqual(client.chat_text("text", system="system"), '{"entries": []}')
        self.assertEqual(client.chat_json("json", system="system"), {"entries": []})
        self.assertEqual(len(client.requests), 2)
        for endpoint, payload in client.requests:
            self.assertEqual(endpoint, "chat/completions")
            self.assertNotIn("thinking", payload)
            self.assertNotIn("reasoning_effort", payload)
            self.assertEqual(payload["model"], DEFAULT_LOCAL_LLM_MODEL)
            self.assertEqual(payload["temperature"], 0.1)

    def test_model_identity_reports_local_provider(self) -> None:
        client = LocalClient(api_key="local")
        identity = client.model_identity(
            target_language="简体中文", prompt_version="p1"
        )
        self.assertEqual(identity.provider, "local")
        self.assertEqual(identity.base_url, DEFAULT_LOCAL_LLM_API_BASE)
        self.assertEqual(identity.model, DEFAULT_LOCAL_LLM_MODEL)

    def test_cli_accepts_local_provider(self) -> None:
        args = build_parser().parse_args(
            ["sample.pdf", "--translation-provider", "local"]
        )
        self.assertEqual(args.translation_provider, "local")

    def test_local_profile_builds_client_without_credential(self) -> None:
        profiles = load_pipeline_profiles(Path("pipeline.example.toml"))
        profile = profiles.for_stage("translation", "local_hymt2")
        self.assertIsNotNone(profile)
        self.assertEqual(profile.provider, "local")
        args = build_parser().parse_args(["sample.pdf"])
        with patch.dict(os.environ, {}, clear=True):
            client = build_translation_client(
                args,
                glm_api_base="https://example.invalid",
                profile=profile,
            )
        self.assertIsInstance(client, LocalClient)
        self.assertEqual(client.api_key, "local")
        self.assertEqual(client.api_base, "http://127.0.0.1:8010/v1")
        self.assertEqual(client.text_model, DEFAULT_LOCAL_LLM_MODEL)

    def test_identity_resolution_matches_client(self) -> None:
        profiles = load_pipeline_profiles(Path("pipeline.example.toml"))
        profile = profiles.for_stage("translation", "local_hymt2")
        args = build_parser().parse_args(["sample.pdf"])
        with patch.dict(os.environ, {}, clear=True):
            identity = resolve_translation_identity(
                args,
                glm_api_base="https://example.invalid",
                profile=profile,
            )
        client_identity = LocalClient(
            api_key="local",
            api_base=identity.base_url,
            text_model=identity.model,
        ).model_identity(
            target_language=identity.target_language,
            prompt_version=identity.prompt_version,
        )
        self.assertEqual(identity, client_identity)

    def test_missing_credential_still_blocks_cloud_providers(self) -> None:
        profiles = load_pipeline_profiles(Path("pipeline.example.toml"))
        profile = profiles.for_stage("translation", "deepseek_flash")
        args = build_parser().parse_args(["sample.pdf"])
        with patch.dict(os.environ, {}, clear=True):
            client = build_translation_client(
                args,
                glm_api_base="https://example.invalid",
                profile=profile,
            )
        self.assertIsNone(client)


class KbTranslatorTests(unittest.TestCase):
    def test_deepseek_translator_requires_credential(self) -> None:
        translator = DeepSeekTranslator()
        self.assertTrue(translator.require_credential)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KbTranslationError):
                translator.translate("<<<SEG 0000>>>")

    def test_local_translator_defaults(self) -> None:
        translator = LocalTranslator()
        self.assertEqual(translator.provider_name, "local")
        self.assertFalse(translator.require_credential)
        self.assertEqual(translator.api_key_env, "LOCAL_LLM_API_KEY")
        self.assertEqual(translator.base_url, "http://127.0.0.1:8010/v1")
        self.assertEqual(translator.model, DEFAULT_LOCAL_LLM_MODEL)
        # "default" is kb_translation's sentinel for omitting the thinking
        # field entirely from the payload.
        self.assertEqual(translator.thinking, "default")

    def test_local_translator_uses_placeholder_key_without_env(self) -> None:
        translator = LocalTranslator()
        sent: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ANN001 - test shim
            sent["url"] = request.full_url
            sent["auth"] = request.get_header("Authorization")
            body = request.data.decode("utf-8")
            sent["payload_thinking"] = '"thinking"' in body

            class Response:
                def read(self) -> bytes:
                    return json.dumps(
                        {"choices": [{"message": {"content": "译文"}}]},
                        ensure_ascii=False,
                    ).encode("utf-8")

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()

        import kb_translation as module

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(module.urllib.request, "urlopen", fake_urlopen):
                value = translator.translate("<<<SEG 0000>>>段落")
        self.assertEqual(value, "译文")
        self.assertTrue(sent["url"].startswith("http://127.0.0.1:8010/v1/"))
        self.assertEqual(sent["auth"], "Bearer local")
        self.assertFalse(sent["payload_thinking"])


if __name__ == "__main__":
    unittest.main()
