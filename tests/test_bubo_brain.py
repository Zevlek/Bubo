import unittest

from bubo_brain import GeminiBrain


class GeminiBrainTests(unittest.TestCase):
    def test_prompt_includes_execution_cost_context(self):
        brain = GeminiBrain(api_key="")
        payload = {
            "ticker": "AAPL",
            "name": "AAPL",
            "collected_at": "2026-04-10T12:00:00",
            "technical": {},
            "events": {},
            "news": {},
            "social": {},
            "constraints": {
                "trade_fee_bps_per_side": 5.0,
                "slippage_bps_per_side": 5.0,
                "roundtrip_cost_bps": 20.0,
                "managed_capital_eur": 10000.0,
                "max_open_positions": 3,
                "max_position_pct": 0.25,
                "max_total_exposure_pct": 0.60,
                "allow_short": False,
            },
        }
        prompt = brain._build_prompt(payload)
        self.assertIn("CONTRAINTES D'EXÉCUTION", prompt)
        self.assertIn("Frais/ordre", prompt)
        self.assertIn("Coût A/R estimé", prompt)
        self.assertIn("edge net", prompt)

    def test_validate_autocompletes_non_critical_missing_fields(self):
        brain = GeminiBrain(api_key="")
        result = brain._validate(
            {"ticker": "AAPL", "decision": "BUY", "score": 72},
            "AAPL",
            status="ok",
            model="test-model",
        )
        self.assertTrue(result.get("_llm_ok"))
        self.assertEqual(result.get("decision"), "BUY")
        self.assertEqual(result.get("confidence"), 0.0)
        self.assertEqual(result.get("position_size_pct"), 0.0)
        self.assertEqual(result.get("_llm_status"), "ok_partial")

    def test_parse_fallback_from_non_json_text(self):
        brain = GeminiBrain(api_key="")
        parsed = brain._parse(
            'decision: "SELL", score: 31, confidence: 77, position_size_pct: 0.12',
            ticker="AAPL",
            model="test-model",
            finish_reason="",
        )
        self.assertTrue(parsed.get("_llm_ok"))
        self.assertEqual(parsed.get("decision"), "SELL")
        self.assertAlmostEqual(float(parsed.get("score", 0.0)), 31.0, places=2)
        self.assertEqual(parsed.get("_llm_status"), "ok_fallback")


if __name__ == "__main__":
    unittest.main()
