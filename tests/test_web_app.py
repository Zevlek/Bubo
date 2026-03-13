import unittest

try:
    from web_app import build_engine_command
except ModuleNotFoundError as exc:
    if exc.name == "flask":
        build_engine_command = None
    else:
        raise


@unittest.skipIf(build_engine_command is None, "flask is not installed in this environment")
class WebAppTests(unittest.TestCase):
    def test_build_watch_command_with_overrides(self):
        cmd, cfg = build_engine_command(
            "watch",
            {
                "universe_file": "data/universe_core_v1.txt",
                "preselect_top": 70,
                "max_deep": 25,
                "capital": 15000,
                "decision_engine": "llm",
                "paper_enabled": True,
                "paper_state": "data/custom_state.json",
                "paper_webhook": "https://example.invalid/webhook",
                "no_finbert": True,
                "no_budget_gate": True,
            },
        )

        self.assertIn("bubo_engine.py", cmd)
        self.assertIn("--watch", cmd)
        self.assertIn("--universe-file", cmd)
        self.assertIn("data/universe_core_v1.txt", cmd)
        self.assertIn("--preselect-top", cmd)
        self.assertIn("70", cmd)
        self.assertIn("--max-deep", cmd)
        self.assertIn("25", cmd)
        self.assertIn("--capital", cmd)
        self.assertIn("15000.0", cmd)
        self.assertIn("--decision-engine", cmd)
        self.assertIn("llm", cmd)
        self.assertIn("--paper", cmd)
        self.assertIn("--paper-state", cmd)
        self.assertIn("data/custom_state.json", cmd)
        self.assertIn("--paper-webhook", cmd)
        self.assertIn("https://example.invalid/webhook", cmd)
        self.assertIn("--paper-broker", cmd)
        self.assertIn("local", cmd)
        self.assertIn("--ibkr-host", cmd)
        self.assertIn("--ibkr-port", cmd)
        self.assertIn("--ibkr-client-id", cmd)
        self.assertIn("--ibkr-exchange", cmd)
        self.assertIn("--ibkr-currency", cmd)
        self.assertIn("--no-finbert", cmd)
        self.assertIn("--no-budget-gate", cmd)
        self.assertTrue(cfg["paper_enabled"])

    def test_build_screen_command(self):
        cmd, _ = build_engine_command(
            "screen",
            {
                "universe_file": "data/universe_global_v1.txt",
                "preselect_top": 10,
                "max_deep": 5,
                "decision_engine": "rules",
                "paper_enabled": False,
                "no_finbert": False,
                "paper_webhook": "",
                "paper_broker": "ibkr",
                "ibkr_host": "127.0.0.1",
                "ibkr_port": 7497,
                "ibkr_client_id": 99,
                "ibkr_account": "DU123",
                "ibkr_exchange": "SMART",
                "ibkr_currency": "USD",
            },
        )
        self.assertIn("--screen-only", cmd)
        self.assertNotIn("--watch", cmd)
        self.assertNotIn("--paper", cmd)
        self.assertNotIn("--no-finbert", cmd)
        self.assertNotIn("--paper-webhook", cmd)
        self.assertIn("--decision-engine", cmd)
        self.assertIn("rules", cmd)
        self.assertIn("--paper-broker", cmd)
        self.assertIn("ibkr", cmd)
        self.assertIn("--ibkr-account", cmd)
        self.assertIn("DU123", cmd)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            build_engine_command("invalid-mode", {})


if __name__ == "__main__":
    unittest.main()
