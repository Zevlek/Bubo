import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from universe_screener import (
    APIBudgetConfig,
    APIBudgetManager,
    ScreenerConfig,
    UniverseScreener,
    load_universe,
)


class DummyFetcher:
    def __init__(self, data_map):
        self.data_map = data_map

    def fetch(self, ticker: str, timeframe: str = "1d"):
        return self.data_map.get(ticker)


def make_df(last_close: float, prev_close: float, prev5_close: float,
            last_vol: float, base_vol: float, n: int = 40) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    close = np.full(n, 100.0)
    close[-6] = prev5_close
    close[-2] = prev_close
    close[-1] = last_close

    high = close * 1.01
    low = close * 0.99
    open_ = close.copy()
    volume = np.full(n, base_vol)
    volume[-1] = last_vol

    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
        },
        index=idx,
    )


class UniverseScreenerTests(unittest.TestCase):
    def test_screen_ranks_high_movers_first(self):
        df_stable = make_df(last_close=100.2, prev_close=100.0, prev5_close=100.1,
                            last_vol=1_100, base_vol=1_000)
        df_mover = make_df(last_close=110.0, prev_close=100.0, prev5_close=95.0,
                           last_vol=8_000, base_vol=1_000)

        screener = UniverseScreener(
            ScreenerConfig(top_n=2),
            fetcher=DummyFetcher({"AAA": df_stable, "BBB": df_mover}),
        )
        ranked = screener.screen(["AAA", "BBB"], top_n=2)

        self.assertEqual(len(ranked), 2)
        self.assertEqual(ranked.iloc[0]["ticker"], "BBB")
        self.assertGreater(ranked.iloc[0]["screen_score"], ranked.iloc[1]["screen_score"])

    def test_budget_manager_caps_selected_tickers(self):
        ranked = pd.DataFrame(
            {
                "ticker": [f"T{i:03d}" for i in range(30)],
                "screen_score": np.linspace(100, 10, 30),
            }
        )
        cfg = APIBudgetConfig(
            watch_interval_min=15,
            stocktwits_req_per_hour=200,
            stocktwits_req_per_ticker=1,
            reddit_req_per_ticker=1,
            utilization=0.80,
            hard_cap_tickers=50,
        )
        mgr = APIBudgetManager(cfg)
        selected, meta = mgr.apply(ranked, requested_max=40)

        self.assertEqual(meta["budget_cap"], 20)
        self.assertEqual(meta["allowed"], 20)
        self.assertEqual(len(selected), 20)

    def test_load_universe_from_txt_and_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            txt = base / "u.txt"
            txt.write_text("rtx\nLMT\nrtx\n", encoding="utf-8")
            self.assertEqual(load_universe(txt), ["RTX", "LMT"])

            csv = base / "u.csv"
            pd.DataFrame({"symbol": ["air.pa", "AM.PA", "AIR.PA"]}).to_csv(csv, index=False)
            self.assertEqual(load_universe(csv), ["AIR.PA", "AM.PA"])

    def test_load_universe_strict_us_filters_non_equity_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            txt = base / "u.txt"
            txt.write_text("AAPL\nUSD\nHOLX.CVR\nBRKB\nXTSLA\nMSFT\n", encoding="utf-8")
            self.assertEqual(load_universe(txt, strict_us=True), ["AAPL", "BRK.B", "MSFT"])


if __name__ == "__main__":
    unittest.main()
