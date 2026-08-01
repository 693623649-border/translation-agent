import os
import unittest
from unittest.mock import patch

from pdf_text_agent import PROVIDER_PROFILES, resolve_agent_connection


class LegacyDeepSeekEnvironmentTests(unittest.TestCase):
    def resolve_base_url(self, override: str | None = None) -> str:
        _, base_url, _, _ = resolve_agent_connection(
            profile=PROVIDER_PROFILES["deepseek"],
            base_url_override=override,
        )
        return base_url

    def test_prefers_current_deepseek_api_base(self) -> None:
        env = {
            "DEEPSEEK_API_BASE": "https://current.example/v1",
            "DEEPSEEK_BASE_URL": "https://legacy.example/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(self.resolve_base_url(), "https://current.example/v1")

    def test_accepts_legacy_deepseek_base_url(self) -> None:
        with patch.dict(
            os.environ,
            {"DEEPSEEK_BASE_URL": "https://legacy.example/v1"},
            clear=True,
        ):
            self.assertEqual(self.resolve_base_url(), "https://legacy.example/v1")

    def test_cli_override_precedes_environment(self) -> None:
        env = {
            "DEEPSEEK_API_BASE": "https://current.example/v1",
            "DEEPSEEK_BASE_URL": "https://legacy.example/v1",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                self.resolve_base_url("https://override.example/v1"),
                "https://override.example/v1",
            )

    def test_uses_profile_default_without_environment_override(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                self.resolve_base_url(),
                PROVIDER_PROFILES["deepseek"].default_base_url,
            )


if __name__ == "__main__":
    unittest.main()
