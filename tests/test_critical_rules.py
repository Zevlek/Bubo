import unittest
from types import SimpleNamespace

import numpy as np
import pandas as pd

import bubo_engine
from bubo_engine import (
    CombinedBacktester,
    EngineConfig,
    ScoringEngine,
    apply_portfolio_risk_gates,
)
from phase1_optimizer import backtest as optimizer_backtest
from phase1_technical import Backtester, TradingConfig
from phase2a_events import backtest_with_events


class CriticalRulesTests(unittest.TestCase):
    def _make_ohlc(self, n: int, start: str = "2025-01-01") -> pd.DataFrame:
        idx = pd.date_range(start, periods=n, freq="D")
        return pd.DataFrame(
            {
                "Open": np.full(n, 100.0),
                "High": np.full(n, 101.0),
                "Low": np.full(n, 99.0),
                "Close": np.full(n, 100.0),
            },
            index=idx,
        )

    def test_blackout_forces_hold_and_zero_position(self):
        saved_modules = bubo_engine.MODULES.copy()
        try:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update(
                {"phase1": True, "phase2a": True, "phase2b": False, "phase3b": False}
            )

            engine = ScoringEngine.__new__(ScoringEngine)
            engine.cfg = EngineConfig()
            engine.weights = {"technical": 1.0, "news": 0.0, "social": 0.0, "events": 0.0}
            engine.sentiment_engine = None
            engine.social_pipeline = None
            engine._score_technical = lambda _: {"score": 95.0, "reasons": ["tech"]}  # type: ignore[attr-defined]
            engine._score_events = lambda _: {  # type: ignore[attr-defined]
                "score": 50.0,
                "modifier": 0.0,
                "blocked": True,
                "reasons": [],
                "warnings": ["blackout"],
            }

            result = ScoringEngine.score_ticker(engine, "RTX")
            self.assertEqual(result["decision"], "HOLD")
            self.assertEqual(result["position_size_pct"], 0.0)
        finally:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update(saved_modules)

    def test_long_only_sell_keeps_zero_position_size(self):
        saved_modules = bubo_engine.MODULES.copy()
        try:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update(
                {"phase1": True, "phase2a": True, "phase2b": False, "phase3b": False}
            )

            engine = ScoringEngine.__new__(ScoringEngine)
            engine.cfg = EngineConfig()
            engine.weights = {"technical": 1.0, "news": 0.0, "social": 0.0, "events": 0.0}
            engine.sentiment_engine = None
            engine.social_pipeline = None
            engine._score_technical = lambda _: {"score": 0.0, "reasons": ["weak tech"]}  # type: ignore[attr-defined]
            engine._score_events = lambda _: {  # type: ignore[attr-defined]
                "score": 50.0,
                "modifier": 1.0,
                "blocked": False,
                "reasons": [],
                "warnings": [],
            }

            result = ScoringEngine.score_ticker(engine, "RTX")
            self.assertIn(result["decision"], ("SELL", "STRONG SELL"))
            self.assertEqual(result["position_size_pct"], 0.0)
        finally:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update(saved_modules)

    def test_set_tickers_deduplicates_and_normalizes(self):
        saved_modules = bubo_engine.MODULES.copy()
        try:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update({"phase2a": False})

            engine = ScoringEngine.__new__(ScoringEngine)
            engine.cfg = EngineConfig()
            engine._event_tickers_sig = None

            ScoringEngine.set_tickers(engine, ["rtx", "RTX", " lmt ", "", "air.pa"])
            self.assertEqual(engine.cfg.tickers, ["RTX", "LMT", "AIR.PA"])
        finally:
            bubo_engine.MODULES.clear()
            bubo_engine.MODULES.update(saved_modules)

    def test_combined_backtester_does_not_trade_without_shifted_signal(self):
        cfg = EngineConfig()
        cfg.buy_threshold = 60.0
        cfg.trade_fee_bps = 0.0
        cfg.slippage_bps = 0.0

        bt = CombinedBacktester.__new__(CombinedBacktester)
        bt.cfg = cfg
        bt.event_filter = None

        df = self._make_ohlc(31)
        df["combined_score"] = 90.0
        df["combined_score_signal"] = np.nan

        result = CombinedBacktester._simulate(bt, df, "TEST")
        self.assertEqual(result["num_trades"], 0)

    def test_combined_backtester_trades_on_shifted_signal(self):
        cfg = EngineConfig()
        cfg.buy_threshold = 60.0
        cfg.trade_fee_bps = 0.0
        cfg.slippage_bps = 0.0

        bt = CombinedBacktester.__new__(CombinedBacktester)
        bt.cfg = cfg
        bt.event_filter = None

        df = self._make_ohlc(32)
        df["combined_score"] = 50.0
        df["combined_score_signal"] = np.nan
        df.iloc[31, df.columns.get_loc("combined_score_signal")] = 70.0
        df.iloc[31, df.columns.get_loc("Close")] = 101.0

        result = CombinedBacktester._simulate(bt, df, "TEST")
        self.assertEqual(result["num_trades"], 1)
        self.assertEqual(result["trades"][0]["exit_reason"], "end_of_period")

    def test_combined_backtester_final_equity_matches_net_pnl(self):
        cfg = EngineConfig()
        cfg.buy_threshold = 60.0
        cfg.trade_fee_bps = 100.0
        cfg.slippage_bps = 0.0

        bt = CombinedBacktester.__new__(CombinedBacktester)
        bt.cfg = cfg
        bt.event_filter = None

        df = self._make_ohlc(32)
        df["combined_score"] = 50.0
        df["combined_score_signal"] = np.nan
        df.iloc[31, df.columns.get_loc("combined_score_signal")] = 70.0
        df.iloc[31, df.columns.get_loc("Close")] = 100.0

        result = CombinedBacktester._simulate(bt, df, "TEST")
        self.assertEqual(result["num_trades"], 1)
        self.assertAlmostEqual(
            result["final_equity"],
            cfg.initial_capital + result["total_pnl"],
            places=2,
        )

    def test_phase1_backtester_fees_reduce_pnl(self):
        idx = pd.date_range("2025-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 100.0, 110.0],
                "High": [101.0, 101.0, 120.0],
                "Low": [99.0, 99.0, 109.0],
                "Close": [100.0, 100.0, 110.0],
                "signal": [1, 0, 0],
            },
            index=idx,
        )

        cfg_no_fee = TradingConfig()
        cfg_no_fee.trade_fee_bps = 0.0
        cfg_no_fee.slippage_bps = 0.0

        cfg_with_fee = TradingConfig()
        cfg_with_fee.trade_fee_bps = 100.0
        cfg_with_fee.slippage_bps = 0.0

        stats_no_fee = Backtester(cfg_no_fee).run(df, "TEST")
        stats_with_fee = Backtester(cfg_with_fee).run(df, "TEST")

        self.assertNotIn("error", stats_no_fee)
        self.assertNotIn("error", stats_with_fee)
        self.assertLess(stats_with_fee["total_pnl"], stats_no_fee["total_pnl"])
        self.assertAlmostEqual(
            float(stats_with_fee["equity_curve"]["equity"].iloc[-1]),
            cfg_with_fee.initial_capital + stats_with_fee["total_pnl"],
            places=2,
        )

    def test_event_backtest_blocks_shifted_blackout_entries(self):
        idx = pd.date_range("2025-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 100.0, 100.0],
                "High": [101.0, 101.0, 101.0],
                "Low": [99.0, 99.0, 99.0],
                "Close": [100.0, 100.0, 100.0],
                "signal": [1, 0, 0],
                "event_blackout": [True, False, False],
                "event_modifier": [1.0, 1.0, 1.0],
                "event_description": ["blackout", "", ""],
            },
            index=idx,
        )

        cfg = SimpleNamespace(
            initial_capital=10_000.0,
            position_size_pct=0.10,
            stop_loss_pct=0.02,
            take_profit_pct=0.10,
            trade_fee_bps=0.0,
            slippage_bps=0.0,
        )

        result = backtest_with_events(df, object(), "TEST", cfg)
        self.assertIn("error", result)
        self.assertEqual(result["blocked"], 1)

    def test_event_backtest_equity_matches_net_pnl(self):
        idx = pd.date_range("2025-01-01", periods=3, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0, 100.0, 100.0],
                "High": [101.0, 101.0, 101.0],
                "Low": [99.0, 99.0, 99.0],
                "Close": [100.0, 100.0, 100.0],
                "signal": [1, 0, 0],
                "event_blackout": [False, False, False],
                "event_modifier": [1.0, 1.0, 1.0],
                "event_description": ["", "", ""],
            },
            index=idx,
        )

        cfg = SimpleNamespace(
            initial_capital=10_000.0,
            position_size_pct=0.10,
            stop_loss_pct=0.02,
            take_profit_pct=0.10,
            trade_fee_bps=100.0,
            slippage_bps=0.0,
        )

        result = backtest_with_events(df, object(), "TEST", cfg)
        self.assertNotIn("error", result)
        self.assertAlmostEqual(
            float(result["equity_curve"].iloc[-1]),
            cfg.initial_capital + result["total_pnl"],
            places=2,
        )

    def test_optimizer_backtest_uses_shifted_signal(self):
        idx = pd.date_range("2025-01-01", periods=1, freq="D")
        df = pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.0],
                "signal": [1],
            },
            index=idx,
        )
        params = {"stop_loss_pct": 0.02, "take_profit_pct": 0.10}
        result = optimizer_backtest(df, params)
        self.assertEqual(result["nb_trades"], 0)

    def test_risk_gate_blocks_low_confidence_buy(self):
        cfg = EngineConfig()
        cfg.min_confidence_for_entry = 40.0
        cfg.max_open_positions = 3
        cfg.max_total_exposure_pct = 0.60

        results = {
            "AAA": {
                "decision": "BUY",
                "confidence": 25.0,
                "position_size_pct": 0.10,
                "final_score": 70.0,
                "warnings": [],
            }
        }

        summary = apply_portfolio_risk_gates(cfg, results)
        self.assertEqual(results["AAA"]["decision"], "HOLD")
        self.assertEqual(results["AAA"]["position_size_pct"], 0.0)
        self.assertEqual(summary["blocked"], 1)

    def test_risk_gate_clips_exposure_budget(self):
        cfg = EngineConfig()
        cfg.min_confidence_for_entry = 10.0
        cfg.max_open_positions = 3
        cfg.max_total_exposure_pct = 0.20
        cfg.min_position_pct = 0.05

        results = {
            "AAA": {
                "decision": "BUY",
                "confidence": 80.0,
                "position_size_pct": 0.15,
                "final_score": 80.0,
                "warnings": [],
            },
            "BBB": {
                "decision": "BUY",
                "confidence": 75.0,
                "position_size_pct": 0.10,
                "final_score": 70.0,
                "warnings": [],
            },
        }

        summary = apply_portfolio_risk_gates(cfg, results)
        self.assertEqual(results["AAA"]["decision"], "BUY")
        self.assertEqual(results["AAA"]["position_size_pct"], 0.15)
        self.assertEqual(results["BBB"]["decision"], "BUY")
        self.assertEqual(results["BBB"]["position_size_pct"], 0.05)
        self.assertEqual(summary["clipped"], 1)
        self.assertAlmostEqual(summary["exposure"], 0.20, places=3)


if __name__ == "__main__":
    unittest.main()
