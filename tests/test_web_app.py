import unittest
from unittest.mock import patch

try:
    from web_app import build_engine_command, get_connectivity_report
except ModuleNotFoundError as exc:
    if exc.name == "flask":
        build_engine_command = None
        get_connectivity_report = None
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
                "watch_interval_min": 30,
                "capital": 15000,
                "decision_engine": "llm",
                "paper_enabled": True,
                "paper_state": "data/custom_state.json",
                "paper_webhook": "https://example.invalid/webhook",
                "ibkr_capital_limit": 12000,
                "ibkr_existing_positions_policy": "ignore",
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
        self.assertIn("--watch-interval-min", cmd)
        self.assertIn("30", cmd)
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
        self.assertIn("ibkr", cmd)
        self.assertIn("--ibkr-host", cmd)
        self.assertIn("--ibkr-port", cmd)
        self.assertIn("--ibkr-client-id", cmd)
        self.assertIn("--ibkr-exchange", cmd)
        self.assertIn("--ibkr-currency", cmd)
        self.assertIn("--ibkr-capital-limit", cmd)
        self.assertIn("12000.0", cmd)
        self.assertIn("--ibkr-existing-positions-policy", cmd)
        self.assertIn("ignore", cmd)
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
                "watch_interval_min": 30,
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
                "ibkr_capital_limit": 9000,
                "ibkr_existing_positions_policy": "include",
            },
        )
        self.assertIn("--screen-only", cmd)
        self.assertNotIn("--watch", cmd)
        self.assertNotIn("--paper", cmd)
        self.assertNotIn("--no-finbert", cmd)
        self.assertNotIn("--paper-webhook", cmd)
        self.assertIn("--decision-engine", cmd)
        self.assertIn("rules", cmd)
        self.assertIn("--watch-interval-min", cmd)
        self.assertIn("30", cmd)
        self.assertIn("--paper-broker", cmd)
        self.assertIn("ibkr", cmd)
        self.assertIn("--ibkr-account", cmd)
        self.assertIn("DU123", cmd)
        self.assertIn("--ibkr-capital-limit", cmd)
        self.assertIn("9000.0", cmd)
        self.assertIn("--ibkr-existing-positions-policy", cmd)
        self.assertIn("include", cmd)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            build_engine_command("invalid-mode", {})

    @patch("web_app._compute_connectivity_report")
    def test_connectivity_report_uses_cache(self, compute_mock):
        compute_mock.return_value = {
            "generated_at": "2026-03-14 10:00:00",
            "ttl_s": 120,
            "services": [],
            "summary": {"ok": 0, "warning": 0, "error": 0, "disabled": 0},
        }

        cfg = {
            "decision_engine": "llm",
            "paper_enabled": True,
            "paper_broker": "ibkr",
            "ibkr_host": "ib-gateway",
            "ibkr_port": 4004,
            "ibkr_client_id": 42,
        }

        first = get_connectivity_report(cfg, force=True)
        second = get_connectivity_report(cfg, force=False)

        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(compute_mock.call_count, 1)

    @patch("web_app._compute_connectivity_report")
    def test_connectivity_report_force_bypasses_cache(self, compute_mock):
        compute_mock.side_effect = [
            {
                "generated_at": "2026-03-14 10:00:00",
                "ttl_s": 120,
                "services": [],
                "summary": {"ok": 0, "warning": 0, "error": 0, "disabled": 0},
            },
            {
                "generated_at": "2026-03-14 10:00:01",
                "ttl_s": 120,
                "services": [],
                "summary": {"ok": 0, "warning": 0, "error": 0, "disabled": 0},
            },
        ]

        cfg = {
            "decision_engine": "llm",
            "paper_enabled": True,
            "paper_broker": "ibkr",
            "ibkr_host": "ib-gateway",
            "ibkr_port": 4004,
            "ibkr_client_id": 42,
        }

        first = get_connectivity_report(cfg, force=True)
        second = get_connectivity_report(cfg, force=True)

        self.assertFalse(first["cached"])
        self.assertFalse(second["cached"])
        self.assertEqual(compute_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
