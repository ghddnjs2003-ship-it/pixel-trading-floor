import os
import unittest
from unittest.mock import patch

from backend.config import get_provider_config


class ProviderConfigTests(unittest.TestCase):
    def test_selects_openai_from_environment(self):
        env = {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-key",
            "OPENAI_MODEL": "test-openai-model",
        }
        with patch.dict(os.environ, env), patch("backend.config.load_env_file"):
            config = get_provider_config()
        self.assertEqual(config["base_url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(config["model"], "test-openai-model")
        self.assertEqual(config["api_key"], "test-key")

    def test_selects_xai_provider_from_environment(self):
        env = {
            "LLM_PROVIDER": "xai",
            "XAI_API_KEY": "test-key",
            "XAI_MODEL": "test-grok-model",
        }
        with patch.dict(os.environ, env), patch("backend.config.load_env_file"):
            config = get_provider_config()
        self.assertEqual(config["provider"], "xai")
        self.assertEqual(config["base_url"], "https://api.x.ai/v1/chat/completions")
        self.assertEqual(config["model"], "test-grok-model")


if __name__ == "__main__":
    unittest.main()
