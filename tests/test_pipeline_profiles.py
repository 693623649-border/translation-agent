import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from pipeline_profiles import ModelProfile, PipelineProfiles, load_pipeline_profiles
from book_pipeline import (
    TRANSLATION_PROMPT_VERSION,
    build_parser,
    build_translation_client,
    resolve_ocr_api_key,
    resolve_translation_api_key,
)


class PipelineProfileTests(unittest.TestCase):
    def test_local_paddle_profile_loads_strict_content_and_runtime_tables(self) -> None:
        config = """
[profiles.paddle_v6_medium]
adapter = "paddleocr-local"
provider = "local"
model = "PP-OCRv6_medium"
credential_env = ""
concurrency = 32
reading_direction = "horizontal"

[profiles.paddle_v6_medium.content]
engine = "paddle_static"
precision = "fp32"
use_doc_orientation_classify = false
thresholds = { det = 0.30, recognition = 0.65 }

[profiles.paddle_v6_medium.runtime]
devices = ["gpu:0", "gpu:1"]
instances_per_device = 2
text_recognition_batch_size = 64
queue_depth = 64

[pipeline]
ocr_profile = "paddle_v6_medium"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(config, encoding="utf-8")
            loaded = load_pipeline_profiles(path)

        profile = loaded.get("paddle_v6_medium")
        self.assertEqual(profile.adapter, "paddleocr-local")
        self.assertEqual(profile.credential_env, "")
        self.assertEqual(profile.content["engine"], "paddle_static")
        self.assertEqual(profile.content["thresholds"]["det"], 0.30)
        self.assertEqual(profile.runtime["devices"], ("gpu:0", "gpu:1"))
        self.assertEqual(profile.resolve_credential().get_secret_value(), "")

    def test_only_local_paddle_profile_may_omit_credentials(self) -> None:
        remote = ModelProfile(
            name="remote",
            adapter="openai-chat",
            provider="custom",
            model="remote-model",
        )
        with self.assertRaisesRegex(ValueError, "credential_env"):
            remote.resolve_credential()

    def test_content_and_runtime_are_deeply_frozen(self) -> None:
        original = {
            "engine": "paddle_static",
            "models": {"det": "PP-OCRv6_medium_det"},
            "features": ["orientation", {"unwarp": False}],
        }
        profile = ModelProfile(
            name="paddle",
            adapter="paddleocr-local",
            provider="local",
            model="PP-OCRv6_medium",
            content=original,
            runtime={"devices": ["gpu:0", "gpu:1"]},
        )
        original["engine"] = "changed-after-construction"
        original["models"]["det"] = "changed-after-construction"
        original["features"].append("changed-after-construction")

        self.assertIsInstance(profile.content, MappingProxyType)
        self.assertIsInstance(profile.content["models"], MappingProxyType)
        self.assertEqual(profile.content["engine"], "paddle_static")
        self.assertEqual(profile.content["models"]["det"], "PP-OCRv6_medium_det")
        self.assertEqual(len(profile.content["features"]), 2)
        with self.assertRaises(TypeError):
            profile.content["engine"] = "onnx"  # type: ignore[index]
        with self.assertRaises(TypeError):
            profile.content["models"]["det"] = "tiny"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            profile.runtime["devices"].append("gpu:2")
        self.assertIsInstance(hash(profile), int)

    def test_settings_fingerprints_are_canonical_and_section_specific(self) -> None:
        first = ModelProfile(
            name="paddle",
            adapter="paddleocr-local",
            provider="local",
            model="PP-OCRv6_medium",
            content={"nested": {"b": 2, "a": ["中", 1]}, "engine": "paddle"},
            runtime={"devices": ["gpu:0", "gpu:1"], "batch": 64},
        )
        reordered = ModelProfile(
            name="another-name",
            adapter="paddleocr-local",
            provider="local",
            model="PP-OCRv6_medium",
            content={"engine": "paddle", "nested": {"a": ["中", 1], "b": 2}},
            runtime={"batch": 64, "devices": ["gpu:0", "gpu:1"]},
        )
        tuned_runtime = replace(first, runtime={"devices": ["gpu:0"], "batch": 32})
        changed_content = replace(first, content={"engine": "onnx"})

        self.assertRegex(first.content_fingerprint, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(first.content_fingerprint, reordered.content_fingerprint)
        self.assertEqual(first.runtime_fingerprint, reordered.runtime_fingerprint)
        self.assertEqual(first.content_fingerprint, tuned_runtime.content_fingerprint)
        self.assertNotEqual(first.runtime_fingerprint, tuned_runtime.runtime_fingerprint)
        self.assertNotEqual(first.content_fingerprint, changed_content.content_fingerprint)

    def test_settings_reject_secret_like_fields_at_any_depth(self) -> None:
        for settings in (
            {"api_key": "raw-key"},
            {"client": {"access-token": "raw-token"}},
            {"worker_password": "raw-password"},
            {"authorization": "Bearer raw-token"},
        ):
            with self.subTest(settings=settings), self.assertRaisesRegex(
                ValueError, "looks like a secret field"
            ):
                ModelProfile(
                    name="unsafe",
                    adapter="paddleocr-local",
                    provider="local",
                    model="PP-OCRv6_medium",
                    content=settings,
                )

        safe = ModelProfile(
            name="safe",
            adapter="openai-chat",
            provider="local",
            model="local-model",
            content={"max_tokens": 4096, "tokenizer": "example"},
        )
        self.assertEqual(safe.content["max_tokens"], 4096)

    def test_settings_reject_non_portable_or_dangerous_values(self) -> None:
        cases = (
            ({"path": Path("model.bin")}, "unsupported PosixPath"),
            ({"devices": {"gpu:0", "gpu:1"}}, "unsupported set"),
            ({"threshold": float("nan")}, "NaN or infinity"),
            ({"too_large": 2**63}, "signed 64-bit"),
        )
        for settings, error in cases:
            with self.subTest(settings=settings), self.assertRaisesRegex(ValueError, error):
                ModelProfile(
                    name="unsafe",
                    adapter="paddleocr-local",
                    provider="local",
                    model="PP-OCRv6_medium",
                    runtime=settings,
                )

        with self.assertRaisesRegex(ValueError, "must be a mapping/TOML table"):
            ModelProfile(
                name="unsafe",
                adapter="paddleocr-local",
                provider="local",
                model="PP-OCRv6_medium",
                content=["not", "a", "mapping"],  # type: ignore[arg-type]
            )

    def test_example_config_defaults_to_deepseek_flash(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "pipeline.example.toml"
        loaded = load_pipeline_profiles(config_path)

        self.assertEqual(loaded.translation_profile, "deepseek_flash")
        self.assertEqual(loaded.proofread_profile, "deepseek_flash")
        self.assertEqual(
            loaded.for_stage("proofread").name,
            "deepseek_flash",
        )
        translation = loaded.for_stage("translation")
        assert translation is not None
        self.assertEqual(translation.model, "deepseek-v4-flash")
        self.assertEqual(loaded.get("deepseek_pro").model, "deepseek-v4-pro")

    def test_raw_secret_is_rejected_as_credential_environment_name(self) -> None:
        with self.assertRaisesRegex(ValueError, "environment variable name"):
            ModelProfile(
                name="unsafe",
                adapter="coding-plan-mcp",
                provider="zhipu",
                model="glm-4.6v",
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
            adapter="glm-ocr",
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
            adapter="glm-chat",
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
        self.assertEqual(identity.adapter, "glm-chat")

    def test_loads_model_profiles_and_pipeline_stage_selection(self) -> None:
        config = """
[profiles.glm_vision]
adapter = "coding-plan-mcp"
provider = "zhipu"
base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
model = "glm-4.6v-vision-mcp"
credential_env = "GLM_TEST_API_KEY"
timeout = 180
concurrency = 4
thinking = "omit"
reading_direction = "vertical"

[profiles.glm_toc]
adapter = "openai-chat"
provider = "zhipu"
base_url = "https://open.bigmodel.cn/api/coding/paas/v4"
model = "glm-5.2"
credential_env = "GLM_TEST_API_KEY"
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
ocr_profile = "glm_vision"
toc_profile = "glm_toc"
translation_profile = "deepseek_pro"
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pipeline.toml"
            path.write_text(config, encoding="utf-8")
            loaded = load_pipeline_profiles(path)

        self.assertIsInstance(loaded, PipelineProfiles)
        self.assertEqual(loaded.ocr_profile, "glm_vision")
        self.assertEqual(loaded.toc_profile, "glm_toc")
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
        self.assertEqual(loaded.for_stage("ocr"), loaded.get("glm_vision"))
        self.assertEqual(loaded.get("glm_vision").reading_direction, "vertical")
        self.assertEqual(loaded.for_stage("toc"), loaded.get("glm_toc"))
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
        ):
            self.assertNotEqual(changed.fingerprint, fingerprint)


if __name__ == "__main__":
    unittest.main()
