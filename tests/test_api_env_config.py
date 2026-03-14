import os
import tempfile
import unittest
from pathlib import Path

from phase2b_sentiment import get_news_api_keys
from phase3b_social import load_config


class ApiEnvConfigTests(unittest.TestCase):
    def _swap_env(self, updates: dict[str, str]) -> dict[str, str | None]:
        old = {}
        for k, v in updates.items():
            old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return old

    def _restore_env(self, old: dict[str, str | None]):
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_news_keys_from_prefixed_env(self):
        old = self._swap_env(
            {
                "NEWSAPI_KEY": "",
                "FINNHUB_KEY": "",
                "BUBO_NEWSAPI_KEY": "news_prefixed",
                "BUBO_FINNHUB_KEY": "finn_prefixed",
            }
        )
        try:
            newsapi, finnhub = get_news_api_keys()
            self.assertEqual(newsapi, "news_prefixed")
            self.assertEqual(finnhub, "finn_prefixed")
        finally:
            self._restore_env(old)

    def test_news_keys_prefer_generic_env(self):
        old = self._swap_env(
            {
                "NEWSAPI_KEY": "news_generic",
                "FINNHUB_KEY": "finn_generic",
                "BUBO_NEWSAPI_KEY": "news_prefixed",
                "BUBO_FINNHUB_KEY": "finn_prefixed",
            }
        )
        try:
            newsapi, finnhub = get_news_api_keys()
            self.assertEqual(newsapi, "news_generic")
            self.assertEqual(finnhub, "finn_generic")
        finally:
            self._restore_env(old)

    def test_social_config_env_override(self):
        old = self._swap_env(
            {
                "BUBO_REDDIT_CLIENT_ID": "env_id",
                "BUBO_REDDIT_CLIENT_SECRET": "env_secret",
                "BUBO_REDDIT_USER_AGENT": "env_agent",
                "BUBO_STOCKTWITS_BASE_URL": "https://stocktwits-proxy.example/api/2",
            }
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "social_config.json"
                cfg_path.write_text("{}", encoding="utf-8")
                cfg = load_config(str(cfg_path))
                self.assertEqual(cfg.reddit_client_id, "env_id")
                self.assertEqual(cfg.reddit_client_secret, "env_secret")
                self.assertEqual(cfg.reddit_user_agent, "env_agent")
                self.assertEqual(cfg.stocktwits_base_url, "https://stocktwits-proxy.example/api/2")
        finally:
            self._restore_env(old)


if __name__ == "__main__":
    unittest.main()
