import io
import unittest
import urllib.error

import phase3b_social
from phase3b_social import RedditFetcher, SocialConfig


class SocialPipelineTests(unittest.TestCase):
    def test_public_reddit_rate_limit_disables_subsequent_fetches(self):
        cfg = SocialConfig(
            reddit_client_id="",
            reddit_client_secret="",
            reddit_rate_limit_cooldown_seconds=60,
        )
        cfg.subreddits_general = ["stocks"]
        cfg.subreddits_defense = []
        cfg.subreddits_europe = []

        fetcher = RedditFetcher(cfg)
        calls = {"count": 0}

        def fake_urlopen(req, timeout=0):
            calls["count"] += 1
            url = getattr(req, "full_url", "https://reddit.invalid")
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))

        original_urlopen = phase3b_social.urllib.request.urlopen
        try:
            phase3b_social.urllib.request.urlopen = fake_urlopen
            self.assertEqual(fetcher.fetch("AAPL"), [])
            self.assertEqual(fetcher.fetch("MSFT"), [])
        finally:
            phase3b_social.urllib.request.urlopen = original_urlopen

        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
