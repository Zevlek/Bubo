import tempfile
import unittest
from pathlib import Path

import bubo_engine
import pandas as pd
from bubo_engine import (
    EngineConfig,
    ScoringEngine,
    load_paper_state,
    notify_paper_webhook,
    run_paper_cycle,
)


class PaperTradingTests(unittest.TestCase):
    def setUp(self):
        cfg = EngineConfig()
        cfg.initial_capital = 10_000.0
        cfg.trade_fee_bps = 0.0
        cfg.slippage_bps = 0.0
        cfg.max_open_positions = 10
        cfg.max_total_exposure_pct = 1.0
        cfg.min_position_pct = 0.01
        self.cfg = cfg

        engine = ScoringEngine.__new__(ScoringEngine)
        engine.cfg = cfg
        engine.fetcher = None
        self.engine = engine

    def _run_cycle(self,
                   results: dict,
                   prices: dict,
                   state_path: Path,
                   trade_enabled: bool = True,
                   trade_pause_reason: str = "") -> dict:
        original_latest_price = bubo_engine._latest_price
        try:
            def fake_latest_price(_engine, ticker: str, _cache: dict):
                return prices.get(ticker)

            bubo_engine._latest_price = fake_latest_price
            return run_paper_cycle(
                self.engine,
                results,
                str(state_path),
                trade_enabled=trade_enabled,
                trade_pause_reason=trade_pause_reason,
            )
        finally:
            bubo_engine._latest_price = original_latest_price

    def test_buy_then_signal_sell_updates_equity(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"

            buy_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 80.0,
                    "confidence": 80.0,
                }
            }
            summary_buy = self._run_cycle(buy_signal, {"AAA": 100.0}, state_path)
            self.assertEqual(summary_buy["positions"], 1)
            self.assertEqual(summary_buy["paper_broker"], "local")
            self.assertTrue(any(a.startswith("BUY AAA") for a in summary_buy["actions"]))

            persisted = load_paper_state(str(state_path), self.cfg)
            self.assertIn("AAA", persisted["positions"])
            self.assertEqual(persisted["cycles"], 1)

            sell_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "SELL",
                    "position_size_pct": 0.0,
                    "final_score": 20.0,
                    "confidence": 70.0,
                }
            }
            summary_sell = self._run_cycle(sell_signal, {"AAA": 110.0}, state_path)
            self.assertEqual(summary_sell["positions"], 0)
            self.assertTrue(any("SELL AAA" in a for a in summary_sell["actions"]))
            self.assertAlmostEqual(summary_sell["equity"], 10_100.0, places=2)
            self.assertGreater(summary_sell["realized_pnl"], 0.0)
            self.assertEqual(summary_sell["num_closed_trades"], 1)
            self.assertEqual(summary_sell["wins"], 1)
            self.assertEqual(summary_sell["losses"], 0)
            self.assertAlmostEqual(summary_sell["win_rate"], 1.0, places=3)
            self.assertEqual(summary_sell["profit_factor"], float("inf"))

            trades_csv = Path(summary_sell["trades_path"])
            equity_csv = Path(summary_sell["equity_curve_path"])
            daily_csv = Path(summary_sell["daily_stats_path"])
            daily_md = Path(summary_sell["daily_report_path"])
            self.assertTrue(trades_csv.exists())
            self.assertTrue(equity_csv.exists())
            self.assertTrue(daily_csv.exists())
            self.assertTrue(daily_md.exists())

            trades_df = pd.read_csv(trades_csv)
            curve_df = pd.read_csv(equity_csv)
            daily_df = pd.read_csv(daily_csv)
            self.assertEqual(len(trades_df), 1)
            self.assertGreaterEqual(len(curve_df), 2)
            self.assertGreaterEqual(len(daily_df), 1)
            self.assertIn("daily_return_pct", daily_df.columns)
            self.assertIn("closed_trades", daily_df.columns)
            self.assertEqual(summary_sell["daily_closed_trades"], 1)
            self.assertEqual(summary_sell["daily_wins"], 1)
            self.assertEqual(summary_sell["daily_losses"], 0)
            self.assertAlmostEqual(summary_sell["daily_win_rate"], 1.0, places=3)
            self.assertGreaterEqual(summary_sell["daily_actions"], 2)

    def test_stop_loss_exit_without_sell_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"

            buy_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 75.0,
                    "confidence": 75.0,
                }
            }
            self._run_cycle(buy_signal, {"AAA": 100.0}, state_path)

            summary = self._run_cycle({}, {"AAA": 97.0}, state_path)
            self.assertEqual(summary["positions"], 0)
            self.assertTrue(any("stop_loss" in a for a in summary["actions"]))

    def test_trading_gate_pauses_orders_but_keeps_mark_to_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"

            buy_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 80.0,
                    "confidence": 80.0,
                }
            }
            summary_buy = self._run_cycle(buy_signal, {"AAA": 100.0}, state_path)
            self.assertEqual(summary_buy["positions"], 1)

            sell_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "SELL",
                    "position_size_pct": 0.0,
                    "final_score": 10.0,
                    "confidence": 90.0,
                }
            }
            summary_paused = self._run_cycle(
                sell_signal,
                {"AAA": 110.0},
                state_path,
                trade_enabled=False,
                trade_pause_reason="market closed",
            )
            self.assertFalse(summary_paused["trading_enabled"])
            self.assertEqual(summary_paused["positions"], 1)
            self.assertEqual(summary_paused["actions"], [])
            self.assertTrue(any("Trading gate:" in w for w in summary_paused.get("warnings", [])))
            self.assertGreater(summary_paused["unrealized_pnl"], 0.0)

    def test_rotation_skips_same_day_position_when_min_hold_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"
            self.cfg.max_open_positions = 1
            self.cfg.rotation_enabled = True
            self.cfg.rotation_max_per_cycle = 1
            self.cfg.rotation_min_edge = 0.0
            self.cfg.rotation_min_hold_days = 1

            # Open one long position.
            open_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.50,
                    "final_score": 60.0,
                    "confidence": 60.0,
                }
            }
            summary_open = self._run_cycle(open_signal, {"AAA": 100.0}, state_path)
            self.assertEqual(summary_open["positions"], 1)

            # Try to rotate on the same day to BBB.
            rotate_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 55.0,
                    "confidence": 55.0,
                },
                "BBB": {
                    "ticker": "BBB",
                    "decision": "BUY",
                    "position_size_pct": 0.50,
                    "final_score": 95.0,
                    "confidence": 90.0,
                },
            }
            summary_rotate = self._run_cycle(rotate_signal, {"AAA": 101.0, "BBB": 50.0}, state_path)
            self.assertEqual(summary_rotate["positions"], 1)
            self.assertFalse(any("rotation" in a for a in summary_rotate["actions"]))
            persisted = load_paper_state(str(state_path), self.cfg)
            self.assertIn("AAA", persisted["positions"])
            self.assertNotIn("BBB", persisted["positions"])

    def test_short_entry_and_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"
            self.cfg.allow_short = True

            short_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "SELL",
                    "position_size_pct": 0.20,
                    "final_score": 20.0,
                    "confidence": 85.0,
                }
            }
            summary_short = self._run_cycle(short_signal, {"AAA": 100.0}, state_path)
            self.assertEqual(summary_short["positions"], 1)
            self.assertTrue(any(a.startswith("SHORT SELL AAA") for a in summary_short["actions"]))

            persisted = load_paper_state(str(state_path), self.cfg)
            self.assertIn("AAA", persisted["positions"])
            self.assertLess(int(persisted["positions"]["AAA"]["shares"]), 0)

            cover_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.0,  # cover only, no same-cycle long flip
                    "final_score": 80.0,
                    "confidence": 80.0,
                }
            }
            summary_cover = self._run_cycle(cover_signal, {"AAA": 90.0}, state_path)
            self.assertEqual(summary_cover["positions"], 0)
            self.assertTrue(any("BUY_TO_COVER AAA" in a for a in summary_cover["actions"]))
            self.assertGreater(summary_cover["realized_pnl"], 0.0)

    def test_invalid_state_file_is_reinitialized(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"
            state_path.write_text("not valid json", encoding="utf-8")

            state = load_paper_state(str(state_path), self.cfg)
            self.assertEqual(state["cash"], self.cfg.initial_capital)
            self.assertEqual(state["positions"], {})
            self.assertEqual(state["trades"], [])
            self.assertEqual(state["equity_curve"], [])
            self.assertEqual(state["action_log"], [])

    def test_missing_price_blocks_new_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"
            buy_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 82.0,
                    "confidence": 82.0,
                }
            }
            summary = self._run_cycle(buy_signal, {"AAA": None}, state_path)
            self.assertEqual(summary["positions"], 0)
            self.assertEqual(summary["actions"], [])

    def test_ibkr_unavailable_does_not_fallback_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "paper_state.json"
            self.cfg.paper_broker = "ibkr"
            buy_signal = {
                "AAA": {
                    "ticker": "AAA",
                    "decision": "BUY",
                    "position_size_pct": 0.10,
                    "final_score": 82.0,
                    "confidence": 82.0,
                }
            }
            original_connect = bubo_engine.IBKRPaperAdapter.connect
            try:
                def _fail_connect(_self):
                    raise RuntimeError("unreachable")

                bubo_engine.IBKRPaperAdapter.connect = _fail_connect
                summary = self._run_cycle(buy_signal, {"AAA": 100.0}, state_path)
            finally:
                bubo_engine.IBKRPaperAdapter.connect = original_connect
                self.cfg.paper_broker = "local"

            self.assertEqual(summary["paper_broker"], "ibkr")
            self.assertEqual(summary["positions"], 0)
            self.assertEqual(summary["actions"], [])
            self.assertTrue(any("IBKR unavailable" in w for w in summary.get("warnings", [])))

    def test_notify_webhook_skips_when_no_actions(self):
        ok, reason = notify_paper_webhook(
            "https://example.invalid/webhook",
            {"actions": [], "equity": 10000.0, "cash": 10000.0, "positions": 0, "win_rate": None},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "no actions")

    def test_notify_webhook_posts_payload(self):
        sent = {"called": False}

        class _Resp:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout=0):
            sent["called"] = True
            self.assertEqual(req.method, "POST")
            self.assertEqual(timeout, 8)
            body = req.data.decode("utf-8")
            self.assertIn("BUBO Paper", body)
            self.assertIn("BUY AAA", body)
            return _Resp()

        original_urlopen = bubo_engine.urllib.request.urlopen
        try:
            bubo_engine.urllib.request.urlopen = fake_urlopen
            ok, reason = notify_paper_webhook(
                "https://example.invalid/webhook",
                {
                    "actions": ["BUY AAA x10"],
                    "equity": 10100.0,
                    "cash": 9000.0,
                    "positions": 1,
                    "win_rate": 1.0,
                },
                watch_mode=True,
            )
        finally:
            bubo_engine.urllib.request.urlopen = original_urlopen

        self.assertTrue(sent["called"])
        self.assertTrue(ok)
        self.assertEqual(reason, "http 204")


if __name__ == "__main__":
    unittest.main()
