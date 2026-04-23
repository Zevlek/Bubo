"""
Universe prescreener + API budget manager.

Goal:
- Scan a broad universe with cheap metrics (price/volume/volatility)
- Keep only the top movers for deep analysis
- Respect API budgets (especially social/news providers)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import re

import numpy as np
import pandas as pd

from phase1_technical import MarketDataFetcher


_VALID_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
_BLOCKED_SYMBOLS = {
    "USD",
    "EUR",
    "JPY",
    "GBP",
    "CHF",
    "CAD",
    "AUD",
    "NZD",
    "CNH",
    # Known synthetic/invalid symbols encountered in generated universes.
    "XTSLA",
    "SGAFT",
    "MOGA",
}
_BLOCKED_SUFFIXES = (
    ".CVR",
    ".WS",
    ".W",
    ".RT",
    ".WT",
    ".U",
    " WI",
)
_SYMBOL_ALIASES = {
    "BRKB": "BRK.B",
    "BRKA": "BRK.A",
    "BFB": "BF.B",
    "BFA": "BF.A",
}


def is_valid_us_equity_ticker(raw: object) -> bool:
    symbol = str(raw or "").replace("\ufeff", "").strip().upper()
    symbol = _SYMBOL_ALIASES.get(symbol, symbol)
    if not symbol:
        return False
    if symbol in _BLOCKED_SYMBOLS:
        return False
    if symbol.endswith("=X"):
        return False
    if any(symbol.endswith(sfx) for sfx in _BLOCKED_SUFFIXES):
        return False
    if not _VALID_TICKER_RE.match(symbol):
        return False
    # Keep BRK.B/BF.B style class shares, reject long dotted derivatives.
    if "." in symbol and len(symbol.rsplit(".", 1)[-1]) > 2:
        return False
    return True


@dataclass
class ScreenerConfig:
    timeframe: str = "1d"
    min_bars: int = 30
    top_n: int = 20

    # Weighting for movement score
    w_abs_ret_1d: float = 1.8
    w_abs_ret_5d: float = 0.8
    w_rvol: float = 12.0
    w_range: float = 1.5


class UniverseScreener:
    """Ranks tickers by expected movement intensity."""

    def __init__(self, cfg: ScreenerConfig | None = None, fetcher: MarketDataFetcher | None = None):
        self.cfg = cfg or ScreenerConfig()
        self.fetcher = fetcher or MarketDataFetcher()
        self.last_successes: set[str] = set()
        self.last_failures: set[str] = set()

    def screen(self, tickers: Iterable[str], top_n: int | None = None) -> pd.DataFrame:
        rows = []
        top_n = top_n or self.cfg.top_n
        self.last_successes = set()
        self.last_failures = set()

        for ticker in tickers:
            tk = str(ticker).strip().upper()
            if not tk:
                continue
            try:
                df = self.fetcher.fetch(tk, self.cfg.timeframe)
            except Exception:
                df = None

            if df is None or len(df) < self.cfg.min_bars:
                self.last_failures.add(tk)
                continue
            self.last_successes.add(tk)

            close = df["Close"]
            high = df["High"]
            low = df["Low"]
            volume = df["Volume"] if "Volume" in df.columns else pd.Series(dtype=float)

            c0 = float(close.iloc[-1])
            c1 = float(close.iloc[-2]) if len(close) >= 2 else np.nan
            c5 = float(close.iloc[-6]) if len(close) >= 6 else np.nan

            abs_ret_1d_pct = abs((c0 / c1 - 1) * 100) if pd.notna(c1) and c1 > 0 else 0.0
            abs_ret_5d_pct = abs((c0 / c5 - 1) * 100) if pd.notna(c5) and c5 > 0 else 0.0

            if len(volume) >= 20 and float(volume.tail(20).mean()) > 0:
                rvol = float(volume.iloc[-1] / volume.tail(20).mean())
            else:
                rvol = 1.0

            range_14 = (high.tail(14) - low.tail(14)).mean() if len(df) >= 14 else (high - low).mean()
            range_pct = float(range_14 / c0 * 100) if c0 > 0 and pd.notna(range_14) else 0.0

            score = (
                abs_ret_1d_pct * self.cfg.w_abs_ret_1d
                + abs_ret_5d_pct * self.cfg.w_abs_ret_5d
                + max(0.0, rvol - 1.0) * self.cfg.w_rvol
                + range_pct * self.cfg.w_range
            )

            rows.append(
                {
                    "ticker": tk,
                    "screen_score": round(score, 3),
                    "abs_ret_1d_pct": round(abs_ret_1d_pct, 3),
                    "abs_ret_5d_pct": round(abs_ret_5d_pct, 3),
                    "rvol": round(rvol, 3),
                    "range_pct": round(range_pct, 3),
                    "close": round(c0, 4),
                }
            )

        if not rows:
            return pd.DataFrame(
                columns=[
                    "ticker",
                    "screen_score",
                    "abs_ret_1d_pct",
                    "abs_ret_5d_pct",
                    "rvol",
                    "range_pct",
                    "close",
                ]
            )

        ranked = pd.DataFrame(rows).sort_values("screen_score", ascending=False).reset_index(drop=True)
        return ranked.head(int(top_n)).copy()


@dataclass
class APIBudgetConfig:
    watch_interval_min: int = 15
    stocktwits_req_per_hour: int = 200
    stocktwits_req_per_ticker: int = 1
    reddit_req_per_ticker: int = 0
    utilization: float = 0.80
    hard_cap_tickers: int = 50


class APIBudgetManager:
    """
    Very simple budget estimator.
    Focuses on social calls because they are usually the first bottleneck.
    """

    def __init__(self, cfg: APIBudgetConfig | None = None):
        self.cfg = cfg or APIBudgetConfig()

    def max_tickers_per_cycle(self) -> int:
        cycle_social_budget = (
            self.cfg.stocktwits_req_per_hour * self.cfg.watch_interval_min / 60.0
        ) * self.cfg.utilization
        req_per_ticker = max(
            1,
            int(self.cfg.stocktwits_req_per_ticker) + int(self.cfg.reddit_req_per_ticker),
        )
        max_by_social = int(cycle_social_budget // req_per_ticker)
        return max(1, min(self.cfg.hard_cap_tickers, max_by_social))

    def apply(self, ranked: pd.DataFrame, requested_max: int) -> tuple[pd.DataFrame, dict]:
        requested_max = max(1, int(requested_max))
        allowed = min(requested_max, self.max_tickers_per_cycle(), len(ranked))
        selected = ranked.head(allowed).copy()
        summary = {
            "requested": requested_max,
            "allowed": allowed,
            "dropped": max(0, len(ranked) - allowed),
            "budget_cap": self.max_tickers_per_cycle(),
        }
        return selected, summary


def load_universe(path: str | Path, strict_us: bool = False) -> list[str]:
    """
    Load tickers from:
    - TXT: one ticker per line
    - CSV: uses first matching column among ticker/symbol/asset (case-insensitive)
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Universe file not found: {p}")

    def clean_symbol(raw: object) -> str:
        symbol = str(raw).replace("\ufeff", "").strip().upper()
        symbol = symbol.lstrip("$")
        symbol = _SYMBOL_ALIASES.get(symbol, symbol)
        return symbol

    if p.suffix.lower() == ".txt":
        tickers = [clean_symbol(line) for line in p.read_text(encoding="utf-8").splitlines() if clean_symbol(line)]
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(p)
        if df.empty:
            return []
        col_map = {str(c).strip().lower(): c for c in df.columns}
        key = None
        for candidate in ("ticker", "symbol", "asset"):
            if candidate in col_map:
                key = col_map[candidate]
                break
        if key is None:
            key = df.columns[0]
        tickers = [clean_symbol(x) for x in df[key].dropna().tolist() if clean_symbol(x)]
    else:
        raise ValueError("Unsupported universe format. Use .txt or .csv")

    # preserve order + unique
    seen = set()
    deduped = []
    for t in tickers:
        if strict_us and not is_valid_us_equity_ticker(t):
            continue
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped
