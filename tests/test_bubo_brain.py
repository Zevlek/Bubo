import unittest

from bubo_brain import GeminiBrain


class GeminiBrainTests(unittest.TestCase):
    def test_prompt_includes_execution_cost_context(self):
        brain = GeminiBrain(api_key="")
        payload = {
            "ticker": "AAPL",
            "name": "AAPL",
            "collected_at": "2026-04-10T12:00:00",
            "technical": {
                "prix_actuel": 123.45,
                "rendement_5j_pct": 2.1,
                "rendement_20j_pct": 6.7,
                "rsi": 58.0,
                "macd": 1.2,
                "macd_signal": 0.9,
                "macd_histogram": 0.3,
                "macd_cross": "bullish",
                "bb_pct": 0.72,
                "bb_lower": 111.0,
                "bb_upper": 130.0,
                "sma_20": 120.0,
                "sma_50": 117.0,
                "sma_200": 102.0,
                "au_dessus_sma200": True,
                "volume_ratio": 2.4,
                "volume_spike": True,
                "volume_anomaly_score": 78.0,
                "volume_anomaly_label": "VERY_HIGH",
                "volume_rvol_mean20": 2.1,
                "volume_rvol_median20": 2.4,
                "volume_zscore_20": 3.2,
                "volume_robust_zscore_60": 4.8,
                "volume_percentile_60": 99.0,
                "atr_pct": 3.4,
                "trend_up": True,
                "trend_down": False,
            },
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
        self.assertIn("CONTRAINTES", prompt)
        self.assertIn("Frais/ordre", prompt)
        self.assertIn("edge net", prompt)
        self.assertIn("Volume anormal:", prompt)
        self.assertIn("VERY_HIGH", prompt)

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
