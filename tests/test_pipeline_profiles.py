import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from translation_agent.runtime.pipeline_profiles import ModelProfile, PipelineProfiles, load_pipeline_profiles
from translation_agent.pipeline.book_pipeline import (
    TRANSLATION_PROMPT_VERSION,
    build_parser,
    build_translation_client,
    resolve_ocr_api_key,
    resolve_translation_api_key,
)


class PipelineProfileTests(unittest.TestCase):
    def test_example_config_defaults_to_magpie_flash(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "pipeline.example.toml"
        loaded = load_pipeline_profiles(config_path)

        # The OCR, proofread and translation nodes are pinned to one routed
        # Magpie model; deepseek_* and glm_vision stay available for rollback.
        self.assertEqual(loaded.ocr_profile, "magpie_vision")
        self.assertEqual(loaded.toc_profile, "magpie_flash")
        self.assertEqual(loaded.translation_profile, "magpie_flash")
        self.assertEqual(loaded.proofread_profile, "magpie_flash")
        self.assertEqual(
            loaded.for_stage("proofread").name,
            "magpie_flash",
        )
        ocr = loaded.for_stage("ocr")
        assert ocr is not None
        self.assertEqual(ocr.adapter, "openai-vision")
        self.assertEqual(ocr.model, "mistral/deepseek-v4-1-flash-1017")
        translation = loaded.for_stage("translation")
        assert translation is not None
        self.assertEqual(translation.adapter, "openai-chat")
        self.assertEqual(translation.model, "mistral/deepseek-v4-1-flash-1017")
        toc = loaded.for_stage("toc")
        assert toc is not None
        # The TOC stage rejects anything but an OpenAI-compatible chat adapter.
        self.assertEqual(toc.adapter, "openai-chat")
        self.assertEqual(toc.model, "mistral/deepseek-v4-1-flash-1017")
        self.assertEqual(loaded.get("deepseek_pro").model, "deepseek-v4-pro")
        self.assertEqual(loaded.schema_version, 1)

    def test_example_config_pins_every_stage_to_a_live_adapter(self) -> None:
        """GLM survives only as the RAG embedding provider.

        The example config may still carry rollback profiles, but whichever
        profiles it actually selects must use adapters this release can run:
        ``openai-vision`` for OCR and ``openai-chat`` for the text stages.
        """

        loaded = load_pipeline_profiles(
            Path(__file__).resolve().parents[1] / "pipeline.example.toml"
        )
        expected_adapters = {
            "ocr": {"openai-vision"},
            "toc": {"openai-chat"},
            "translation": {"openai-chat"},
            "proofread": {"openai-chat"},
        }
        for stage, adapters in expected_adapters.items():
            with self.subTest(stage=stage):
                profile = loaded.for_stage(stage)
                assert profile is not None
                self.assertIn(profile.adapter, adapters)
        # No selected stage may route to a removed GLM chat/vision backend.
        removed = {"coding-plan-mcp", "glm-ocr", "glm-chat"}
        for stage in expected_adapters:
            self.assertNotIn(loaded.for_stage(stage).adapter, removed)

    def test_production_config_stages_match_the_example(self) -> None:
        root = Path(__file__).resolve().parents[1]
        example = load_pipeline_profiles(root / "pipeline.example.toml")
        production = load_pipeline_profiles(root / "pipeline.toml")
        for stage in ("ocr", "toc", "translation", "proofread"):
            self.assertEqual(
                production.for_stage(stage).model,
                example.for_stage(stage).model,
                f"{stage} model drifted between pipeline.toml and the example",
            )

    def test_profile_schema_rejects_unknown_fields_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(
                "schema_version=1\n"
                "[profiles.model]\n"
                "adapter='openai-chat'\nprovider='test'\nmodel='x'\n"
                "tempertaure=0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown fields.*tempertaure"):
                load_pipeline_profiles(path)

            path.write_text("schema_version=2\n[profiles]\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema_version=2"):
                load_pipeline_profiles(path)

    def test_raw_secret_is_rejected_as_credential_environment_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "environment variable name"):
            ModelProfile(
                name="unsafe",
                adapter="openai-vision",
                provider="custom",
                model="vision-model",
                credential_env="raw.secret-value",
            )

    def test_profile_credential_does_not_fall_back_to_another_provider_key(self) -> None:
        profile = ModelProfile(
            name="custom",
            adapter="openai-chat",
            provider="custom",
            base_url="https://gateway.example.invalid",
            model="custom-model",
            credential_env="CUSTOM_TRANSLATION_KEY",
        )
        args = build_parser().parse_args(["book.pdf"])
        with patch.dict(
            "os.environ",
            {
                "GLM_CODING_API_KEY": "must-not-be-reused",
                "DEEPSEEK_API_KEY": "must-not-be-reused-either",
            },
            clear=True,
        ):
            self.assertEqual(resolve_translation_api_key(args, profile), "")

    def test_ocr_profile_does_not_fall_back_to_global_ocr_key(self) -> None:
        profile = ModelProfile(
            name="custom-ocr",
            adapter="openai-vision",
            provider="custom",
            base_url="https://gateway.example.invalid",
            model="custom-ocr",
            credential_env="CUSTOM_OCR_KEY",
        )
        args = build_parser().parse_args(["book.pdf", "--phase", "ocr"])
        with patch.dict(
            "os.environ",
            {"GLM_OCR_API_KEY": "must-not-be-reused"},
            clear=True,
        ):
            self.assertEqual(resolve_ocr_api_key(args, profile), "")

    def test_deepseek_client_preserves_profile_adapter_identity(self) -> None:
        profile = ModelProfile(
            name="deepseek-compatible",
            adapter="openai-chat",
            provider="deepseek",
            base_url="https://gateway.example.invalid",
            model="deepseek-v4-pro",
            credential_env="CUSTOM_DEEPSEEK_KEY",
        )
        args = build_parser().parse_args(["book.pdf"])
        with patch.dict(
            "os.environ",
            {"CUSTOM_DEEPSEEK_KEY": "unit-test-key"},
            clear=True,
        ):
            client = build_translation_client(
                args,
                glm_api_base="https://unused.example.invalid",
                profile=profile,
            )
        assert client is not None
        identity = client.model_identity(
            target_language="简体中文",
            prompt_version=TRANSLATION_PROMPT_VERSION,
        )
        self.assertEqual(identity.adapter, "openai-chat")

    def test_loads_model_profiles_and_pipeline_stage_selection(self) -> None:
        config = """
[profiles.vision]
adapter = "openai-vision"
provider = "magpie"
base_url = "https://vision.example.invalid/v1"
model = "vision-model"
credential_env = "VISION_TEST_API_KEY"
timeout = 180
concurrency = 4
thinking = "omit"
reading_direction = "vertical"

[profiles.toc]
adapter = "openai-chat"
provider = "magpie"
base_url = "https://text.example.invalid/v1"
model = "toc-model"
credential_env = "TOC_TEST_API_KEY"
timeout = 120
concurrency = 2
thinking = "disabled"

[profiles.deepseek_pro]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
credential_env = "DEEPSEEK_TEST_API_KEY"
timeout = 240
concurrency = 16
thinking = "disabled"

[pipeline]
ocr_profile = "vision"
toc_profile = "toc"
translation_profile = "deepseek_pro"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(config, encoding="utf-8")
            loaded = load_pipeline_profiles(path)

        self.assertIsInstance(loaded, PipelineProfiles)
        self.assertEqual(loaded.ocr_profile, "vision")
        self.assertEqual(loaded.toc_profile, "toc")
        self.assertEqual(loaded.translation_profile, "deepseek_pro")

        translation = loaded.for_stage("translation")
        self.assertIsInstance(translation, ModelProfile)
        assert translation is not None
        self.assertEqual(translation.name, "deepseek_pro")
        self.assertEqual(translation.adapter, "openai-chat")
        self.assertEqual(translation.provider, "deepseek")
        self.assertEqual(translation.base_url, "https://api.deepseek.com")
        self.assertEqual(translation.model, "deepseek-v4-pro")
        self.assertEqual(translation.credential_env, "DEEPSEEK_TEST_API_KEY")
        self.assertEqual(translation.timeout, 240)
        self.assertEqual(translation.concurrency, 16)
        self.assertEqual(translation.thinking, "disabled")
        vision = loaded.for_stage("ocr")
        assert vision is not None
        self.assertEqual(vision, loaded.get("vision"))
        self.assertEqual(vision.adapter, "openai-vision")
        self.assertEqual(vision.reading_direction, "vertical")
        self.assertEqual(loaded.for_stage("toc"), loaded.get("toc"))
        self.assertEqual(
            loaded.for_stage("proofread"),
            loaded.get("deepseek_pro"),
        )

    def test_credential_is_resolved_from_environment_and_redacted(self) -> None:
        profile = ModelProfile(
            name="translation",
            adapter="openai-chat",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            credential_env="PIPELINE_PROFILE_TEST_TOKEN",
        )
        fake_secret = "unit-test-secret-value"

        with patch.dict(
            "os.environ",
            {"PIPELINE_PROFILE_TEST_TOKEN": fake_secret},
            clear=True,
        ):
            credential = profile.resolve_credential()

        self.assertEqual(credential.get_secret_value(), fake_secret)
        self.assertNotIn(fake_secret, repr(credential))
        self.assertNotIn(fake_secret, str(credential))
        self.assertNotIn(fake_secret, repr(profile))

    def test_model_identity_fingerprint_tracks_cache_relevant_inputs_only(self) -> None:
        fake_secret = "another-unit-test-secret"
        profile = ModelProfile(
            name="translation",
            adapter="openai-chat",
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            credential_env="PIPELINE_PROFILE_TEST_TOKEN",
        )
        identity = profile.identity(
            target_language="简体中文",
            prompt_version="translation-v2",
        )
        fingerprint = identity.fingerprint

        self.assertRegex(fingerprint, re.compile(r"^[0-9a-f]{64}$"))
        self.assertNotIn("PIPELINE_PROFILE_TEST_TOKEN", fingerprint)
        with patch.dict(
            "os.environ",
            {"PIPELINE_PROFILE_TEST_TOKEN": fake_secret},
            clear=True,
        ):
            profile.resolve_credential()
        self.assertNotIn(fake_secret, fingerprint)
        self.assertNotIn(fake_secret, repr(identity))
        for changed in (
            replace(identity, model="deepseek-v4-flash"),
            replace(identity, base_url="https://gateway.example.invalid/deepseek"),
            replace(identity, target_language="繁体中文"),
            replace(identity, prompt_version="translation-v3"),
            replace(identity, thinking="enabled"),
        ):
            self.assertNotEqual(changed.fingerprint, fingerprint)

    def test_reasoning_level_profile_is_accepted(self) -> None:
        config = """
[profiles.deepseek_flash_high]
adapter = "openai-chat"
provider = "deepseek"
base_url = "https://api.deepseek.com"
model = "deepseek-flash"
credential_env = "DEEPSEEK_TEST_API_KEY"
timeout = 900
concurrency = 8
thinking = "high"

[pipeline]
translation_profile = "deepseek_flash_high"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(config, encoding="utf-8")
            loaded = load_pipeline_profiles(path)

        profile = loaded.for_stage("translation")
        assert profile is not None
        self.assertEqual(profile.model, "deepseek-flash")
        self.assertEqual(profile.thinking, "high")
        self.assertEqual(loaded.for_stage("proofread").name, "deepseek_flash_high")

    def test_unknown_thinking_value_is_rejected(self) -> None:
        config = """
[profiles.deepseek_flash_medium]
adapter = "openai-chat"
provider = "deepseek"
model = "deepseek-flash"
thinking = "medium"

[pipeline]
translation_profile = "deepseek_flash_medium"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(config, encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_pipeline_profiles(path)

        self.assertIn("thinking must be one of", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class ThinkingDialectTests(unittest.TestCase):
    """`thinking` speaks two provider dialects; the client must pick correctly.

    A reasoning level wrapped in ``thinking.type`` is accepted by an
    OpenAI-compatible gateway but silently runs without reasoning, so the
    level has to travel as ``reasoning_effort``.
    """

    def _client(self, thinking: str):
        from translation_agent.pipeline.book_pipeline import ChatCompletionsClient

        return ChatCompletionsClient(
            api_key="test-key",
            api_base="https://example.invalid/v1",
            text_model="test-model",
            thinking=thinking,
        )

    def _payload_for(self, thinking: str) -> dict:
        client = self._client(thinking)
        captured: dict = {}

        def fake_post(endpoint, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        client._post = fake_post
        client.chat_text("prompt", system="system")
        return captured

    def test_reasoning_levels_use_reasoning_effort(self) -> None:
        for level in ("low", "high", "max"):
            with self.subTest(level=level):
                payload = self._payload_for(level)
                self.assertEqual(payload["reasoning_effort"], level)
                self.assertNotIn("thinking", payload)

    def test_explicit_thinking_states_keep_their_dialect(self) -> None:
        for state in ("enabled", "disabled"):
            with self.subTest(state=state):
                payload = self._payload_for(state)
                self.assertEqual(payload["thinking"], {"type": state})
                self.assertNotIn("reasoning_effort", payload)

    def test_omit_sends_neither_dialect(self) -> None:
        payload = self._payload_for("omit")
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_chat_json_honours_the_same_dialect(self) -> None:
        client = self._client("low")
        captured: dict = {}

        def fake_post(endpoint, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": '{"ok": true}'}}]}

        client._post = fake_post
        self.assertEqual(client.chat_json("prompt", system="system"), {"ok": True})
        self.assertEqual(captured["reasoning_effort"], "low")

    def test_deepseek_client_shares_the_dialect(self) -> None:
        from translation_agent.pipeline.book_pipeline import DeepSeekClient

        client = DeepSeekClient(
            api_key="test-key",
            api_base="https://example.invalid/v1",
            text_model="deepseek-v4-flash",
            thinking="low",
        )
        captured: dict = {}

        def fake_post(endpoint, payload):
            captured.update(payload)
            return {"choices": [{"message": {"content": "ok"}}]}

        client._post = fake_post
        client.chat_text("prompt", system="system")
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertNotIn("thinking", captured)
