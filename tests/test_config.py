import os
import tempfile
import unittest

from agit import config as config_mod
from agit.config import PRESETS, Config, ConfigError


class EnvCase(unittest.TestCase):
    """Config is env-driven, so each test gets a clean environment."""

    KEYS = ["AGIT_PROVIDER", "AGIT_BASE_URL", "AGIT_API_KEY", "AGIT_MODEL",
            "AGIT_MAX_STEPS", "AGIT_YES", "AGIT_NO_NATIVE_TOOLS",
            "BHARATCODE_API_KEY", "BHARATCODE_BASE_URL", "BHARATCODE_MODEL",
            "GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY",
            "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            "OLLAMA_HOST", "AGIT_ASSUME_OLLAMA"]

    def setUp(self):
        self.saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        self.tmp = tempfile.mkdtemp(prefix="agit-config-")

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TestResolution(EnvCase):
    def test_no_credentials_falls_back_to_offline_script(self):
        cfg = config_mod.resolve(self.tmp)
        self.assertEqual(cfg.provider, "script")
        self.assertTrue(cfg.offline)
        self.assertEqual(config_mod.check(cfg), [])

    def test_bharatcode_env_is_picked_up(self):
        os.environ["BHARATCODE_API_KEY"] = "k-123"
        os.environ["BHARATCODE_BASE_URL"] = "https://api.example.test/v1"
        os.environ["BHARATCODE_MODEL"] = "qwen-3.8-27b"
        cfg = config_mod.resolve(self.tmp)
        self.assertEqual(cfg.provider, "bharatcode")
        self.assertEqual(cfg.model, "qwen-3.8-27b")
        self.assertEqual(cfg.endpoint(), "https://api.example.test/v1/chat/completions")
        self.assertEqual(config_mod.check(cfg), [])

    def test_missing_base_url_is_reported_not_guessed(self):
        os.environ["BHARATCODE_API_KEY"] = "k-123"
        cfg = config_mod.resolve(self.tmp, {"provider": "bharatcode"})
        problems = config_mod.check(cfg)
        self.assertTrue(any("base URL" in p for p in problems))

    def test_missing_key_is_reported(self):
        cfg = config_mod.resolve(self.tmp, {"provider": "groq"})
        problems = config_mod.check(cfg)
        self.assertTrue(any("API key" in p for p in problems))

    def test_cli_overrides_beat_env(self):
        os.environ["AGIT_MODEL"] = "from-env"
        cfg = config_mod.resolve(self.tmp, {"provider": "groq", "model": "from-flag"})
        self.assertEqual(cfg.model, "from-flag")

    def test_project_config_file_is_honoured(self):
        config_mod.save_project_config(self.tmp, {"provider": "ollama", "max_steps": 3})
        cfg = config_mod.resolve(self.tmp)
        self.assertEqual(cfg.provider, "ollama")
        self.assertEqual(cfg.max_steps, 3)
        self.assertEqual(cfg.base_url, "http://localhost:11434/v1")

    def test_unknown_provider_fails_loudly(self):
        with self.assertRaises(ConfigError):
            config_mod.resolve(self.tmp, {"provider": "definitely-not-real"})

    def test_api_key_is_never_printed(self):
        cfg = Config(api_key="super-secret-token")
        self.assertNotIn("super-secret", str(cfg.redacted()))

    def test_every_preset_documents_a_free_route(self):
        for name, preset in PRESETS.items():
            self.assertTrue(preset["free"], name)
            self.assertTrue(preset["hint"], name)


if __name__ == "__main__":
    unittest.main()
