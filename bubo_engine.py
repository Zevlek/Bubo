"""
╔══════════════════════════════════════════════════════════╗
║               BUBO — Unified Trading Engine              ║
║          Phase 1 + 2a + 2b + 3b — Score & Backtest      ║
╚══════════════════════════════════════════════════════════╝

Remplace bubo.py avec l'intégration de la Phase 3b (social).

Usage:
    python bubo_engine.py                 # Analyse live
    python bubo_engine.py --backtest      # Backtest combiné 2 ans
    python bubo_engine.py --watch         # Surveillance continue
    python bubo_engine.py --tickers RTX LMT
    python bubo_engine.py --no-finbert    # Sans FinBERT (rapide)
"""

import sys
import os
import time
import argparse
import warnings
import json
import urllib.request
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
from market_hours import format_duration_compact, get_us_market_clock

warnings.filterwarnings("ignore")
os.makedirs("data", exist_ok=True)
os.makedirs("charts", exist_ok=True)
sys.path.insert(0, ".")

# ─────────────────────────────────────────────
# Import des modules (graceful si absent)
# ─────────────────────────────────────────────

MODULES = {}

try:
    from phase1_technical import (TradingConfig, MarketDataFetcher,
                                   TechnicalAnalyzer, Backtester)
    MODULES["phase1"] = True
except ImportError as e:
    MODULES["phase1"] = False
    print(f"  ⚠️  Phase 1 non disponible: {e}")

try:
    from phase2a_events import EventCalendar, EventFilter
    MODULES["phase2a"] = True
except ImportError as e:
    MODULES["phase2a"] = False
    print(f"  ⚠️  Phase 2a non disponible: {e}")

try:
    from phase2b_sentiment import (FinBERTAnalyzer, NewsFetcher,
                                    SentimentEngine)
    MODULES["phase2b"] = True
except ImportError as e:
    MODULES["phase2b"] = False
    print(f"  ⚠️  Phase 2b non disponible: {e}")

try:
    from phase3b_social import (SocialPipeline, SocialConfig,
                                 load_config as load_social_config)
    MODULES["phase3b"] = True
except ImportError as e:
    MODULES["phase3b"] = False
    print(f"  ⚠️  Phase 3b non disponible: {e}")


# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

try:
    from universe_screener import (
        UniverseScreener,
        ScreenerConfig,
        APIBudgetManager,
        APIBudgetConfig,
        load_universe,
    )
    MODULES["screener"] = True
except ImportError as e:
    MODULES["screener"] = False
    print(f"  ⚠️  Screener non disponible: {e}")


WATCHLIST = {
    "AM.PA":  "Dassault Aviation",
    "AIR.PA": "Airbus",
    "LMT":    "Lockheed Martin",
    "RTX":    "Raytheon",
}

@dataclass
class EngineConfig:
    tickers: list = field(default_factory=lambda: list(WATCHLIST.keys()))
    timeframe: str = "1d"

    # Poids — redistribués dynamiquement si un module manque
    weight_technical: float = 0.35
    weight_news: float = 0.25
    weight_social: float = 0.20
    weight_events: float = 0.20

    # Seuils
    buy_threshold: float = 60.0
    sell_threshold: float = 40.0
    strong_buy_threshold: float = 72.0
    strong_sell_threshold: float = 28.0

    # Position sizing
    base_position_pct: float = 0.10
    max_position_pct: float = 0.25
    min_position_pct: float = 0.05
    allow_short: bool = False

    # Risk
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.10
    trade_fee_bps: float = 5.0      # 5 bps commission per side
    slippage_bps: float = 5.0       # 5 bps slippage per side
    initial_capital: float = 10000.0
    risk_gates_enabled: bool = True
    min_confidence_for_entry: float = 30.0
    max_open_positions: int = 3
    max_total_exposure_pct: float = 0.60

    # Backtest
    backtest_period_years: int = 2
    watch_interval_min: int = 30
    us_market_only: bool = True
    use_finbert: bool = True
    decision_engine: str = "llm"  # llm | rules
    paper_broker: str = "local"  # local | ibkr
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 42
    ibkr_account: str = ""
    ibkr_exchange: str = "SMART"
    ibkr_currency: str = "USD"
    ibkr_capital_limit: float = 10000.0
    ibkr_existing_positions_policy: str = "include"  # include | ignore


def get_adaptive_weights(cfg: EngineConfig) -> dict:
    """Poids normalisés selon les modules disponibles."""
    raw = {
        "technical": cfg.weight_technical if MODULES.get("phase1") else 0,
        "news":      cfg.weight_news if MODULES.get("phase2b") and cfg.use_finbert else 0,
        "social":    cfg.weight_social if MODULES.get("phase3b") else 0,
        "events":    cfg.weight_events if MODULES.get("phase2a") else 0,
    }
    total = sum(raw.values())
    if total == 0:
        return {"technical": 1.0, "news": 0, "social": 0, "events": 0}
    return {k: v / total for k, v in raw.items()}


def _normalize_decision_engine(value: str) -> str:
    eng = str(value or "llm").strip().lower()
    return eng if eng in {"llm", "rules"} else "llm"


def _normalize_ibkr_existing_positions_policy(value: str) -> str:
    policy = str(value or "include").strip().lower()
    return policy if policy in {"include", "ignore"} else "include"


# ─────────────────────────────────────────────
# Scoring Engine
# ─────────────────────────────────────────────

class ScoringEngine:
    """Moteur central de décision — agrège les 4 phases."""

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.cfg.decision_engine = _normalize_decision_engine(self.cfg.decision_engine)
        self.weights = get_adaptive_weights(cfg)
        self._event_tickers_sig = None
        self.llm_collector = None
        self.llm_brain = None
        self.llm_ready = False

        print(f"\n🦉 BUBO Engine — Modules actifs:")
        for name, active in MODULES.items():
            print(f"  {'✅' if active else '⚠️ '} {name}")
        active_w = {k: v for k, v in self.weights.items() if v > 0}
        print(f"  Poids: {' | '.join(f'{k}={v:.0%}' for k, v in active_w.items())}")
        print(f"  Decision Engine: {self.cfg.decision_engine}")

        # Init modules
        self.tech_config = TradingConfig() if MODULES["phase1"] else None
        self.fetcher = MarketDataFetcher() if MODULES["phase1"] else None
        self.analyzer = TechnicalAnalyzer(self.tech_config) if MODULES["phase1"] else None

        self.event_calendar = None
        self.event_filter = None
        if MODULES["phase2a"]:
            self.event_calendar = EventCalendar(cfg.tickers)
            self.event_calendar.load_all()
            self.event_filter = EventFilter(self.event_calendar)
            self._event_tickers_sig = tuple(sorted(cfg.tickers))

        self.finbert = None
        self.news_fetcher = None
        self.sentiment_engine = None
        if MODULES["phase2b"] and cfg.use_finbert:
            try:
                self.finbert = FinBERTAnalyzer()
                if not self.finbert.load():
                    MODULES["phase2b"] = False
                else:
                    self.news_fetcher = NewsFetcher()
                    self.sentiment_engine = SentimentEngine(self.finbert, self.news_fetcher)
            except Exception as e:
                print(f"  ⚠️  FinBERT init: {e}")
                MODULES["phase2b"] = False

        self.social_pipeline = None
        if MODULES["phase3b"]:
            try:
                social_cfg = load_social_config()
                self.social_pipeline = SocialPipeline(social_cfg)
            except Exception as e:
                print(f"  ⚠️  Social init: {e}")
                MODULES["phase3b"] = False

        if self.cfg.decision_engine == "llm":
            self._init_llm_engine()

        # Recalculate weights after potential module failures
        self.weights = get_adaptive_weights(cfg)

    def _init_llm_engine(self):
        try:
            from bubo_brain import DataCollector, GeminiBrain, load_gemini_key
        except Exception as e:
            print(f"  ⚠️  LLM init: bubo_brain indisponible ({e}) — NO_DECISION")
            self.llm_ready = False
            return

        try:
            api_key = load_gemini_key()
        except Exception as e:
            print(f"  ⚠️  LLM init: key Gemini introuvable ({e}) — NO_DECISION")
            self.llm_ready = False
            return

        if not api_key:
            print("  ⚠️  LLM init: GEMINI_API_KEY manquante — NO_DECISION")
            self.llm_ready = False
            return

        try:
            collector = DataCollector(use_finbert=self.cfg.use_finbert)
            collector.init_events(self.cfg.tickers)
            if self.cfg.use_finbert:
                collector.init_sentiment()
            collector.init_social()
            brain = GeminiBrain(api_key)
            if getattr(brain, "client", None) is None:
                print("  ⚠️  LLM init: client Gemini indisponible — NO_DECISION")
                self.llm_ready = False
                return
            self.llm_collector = collector
            self.llm_brain = brain
            self.llm_ready = True
            print("  ✅ LLM decision active (Gemini)")
        except Exception as e:
            print(f"  ⚠️  LLM init: {e} — NO_DECISION")
            self.llm_ready = False

    def set_tickers(self, tickers: list):
        """
        Update active tickers at runtime.
        Event calendar is reloaded only when ticker set actually changes.
        """
        cleaned = []
        seen = set()
        for t in tickers:
            t = str(t).strip().upper()
            if t and t not in seen:
                cleaned.append(t)
                seen.add(t)

        self.cfg.tickers = cleaned

        llm_collector = getattr(self, "llm_collector", None)

        if not MODULES.get("phase2a"):
            if llm_collector is not None and hasattr(llm_collector, "init_events"):
                try:
                    llm_collector.init_events(cleaned)
                except Exception:
                    pass
            return

        new_sig = tuple(sorted(cleaned))
        if new_sig == self._event_tickers_sig:
            return

        self.event_calendar = EventCalendar(cleaned)
        self.event_calendar.load_all()
        self.event_filter = EventFilter(self.event_calendar)
        self._event_tickers_sig = new_sig
        if llm_collector is not None and hasattr(llm_collector, "init_events"):
            try:
                llm_collector.init_events(cleaned)
            except Exception:
                pass

    def _score_llm(self, ticker: str) -> dict:
        if not self.llm_ready or self.llm_collector is None or self.llm_brain is None:
            raise RuntimeError("LLM engine not ready")

        data = self.llm_collector.collect(ticker)
        llm = self.llm_brain.analyze(data, dry_run=False)
        decision = str(llm.get("decision", "HOLD")).strip().upper()
        if decision not in {"BUY", "STRONG BUY", "SELL", "STRONG SELL", "HOLD"}:
            decision = "HOLD"

        score = float(llm.get("score", 50.0) or 50.0)
        score = max(0.0, min(100.0, score))
        confidence = float(llm.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(100.0, confidence))

        position = float(llm.get("position_size_pct", 0.0) or 0.0)
        if decision in {"BUY", "STRONG BUY"}:
            position = max(self.cfg.min_position_pct, min(self.cfg.max_position_pct, position))
        else:
            position = 0.0

        reasons = []
        warnings_list = []

        reasoning = str(llm.get("raisonnement", "") or "").strip()
        if reasoning:
            reasons.append(f"LLM: {reasoning}")

        for sig in (llm.get("signaux_cles", []) or [])[:4]:
            s = str(sig).strip()
            if s:
                reasons.append(f"LLM signal: {s}")

        for rk in (llm.get("risques", []) or [])[:4]:
            s = str(rk).strip()
            if s:
                warnings_list.append(f"LLM risk: {s}")

        events = data.get("events", {}) if isinstance(data, dict) else {}
        if isinstance(events, dict) and events.get("blackout_actif"):
            decision = "HOLD"
            position = 0.0
            blackout_reason = str(events.get("blackout_raison", "")).strip()
            if blackout_reason:
                warnings_list.append(f"Event blackout: {blackout_reason}")

        return {
            "score": round(score, 1),
            "decision": decision,
            "confidence": round(confidence, 1),
            "position_size_pct": round(position, 3),
            "reasons": reasons,
            "warnings": warnings_list,
        }

    def score_ticker(self, ticker: str) -> dict:
        """Score unifiÃ© pour un ticker. Retourne dict complet."""
        result = {
            "ticker": ticker,
            "name": WATCHLIST.get(ticker, ticker),
            "timestamp": datetime.now().isoformat(),
            "scores": {},
            "final_score": 50.0,
            "decision": "HOLD",
            "confidence": 0.0,
            "position_size_pct": 0.0,
            "reasons": [],
            "warnings": [],
        }

        if self.cfg.decision_engine == "llm":
            try:
                llm = self._score_llm(ticker)
                result["scores"]["llm"] = llm["score"]
                result["final_score"] = llm["score"]
                result["decision"] = llm["decision"]
                result["confidence"] = llm["confidence"]
                result["position_size_pct"] = llm["position_size_pct"]
                result["reasons"].extend(llm.get("reasons", []))
                result["warnings"].extend(llm.get("warnings", []))
                return result
            except Exception as e:
                result["scores"]["llm"] = 50.0
                result["final_score"] = 50.0
                result["decision"] = "NO_DECISION"
                result["confidence"] = 0.0
                result["position_size_pct"] = 0.0
                result["warnings"].append(f"LLM unavailable: {e}")
                return result

        # â”€â”€ Phase 1: Technique â”€â”€
        if MODULES.get("phase1"):
            try:
                ts = self._score_technical(ticker)
                result["scores"]["technical"] = ts["score"]
                result["reasons"].extend(ts.get("reasons", []))
            except Exception as e:
                result["warnings"].append(f"Tech: {e}")

        # â”€â”€ Phase 2a: Events â”€â”€
        event_modifier = 1.0
        event_blocked = False
        if MODULES.get("phase2a"):
            try:
                es = self._score_events(ticker)
                result["scores"]["events"] = es["score"]
                event_modifier = es.get("modifier", 1.0)
                event_blocked = es.get("blocked", False)
                result["reasons"].extend(es.get("reasons", []))
                result["warnings"].extend(es.get("warnings", []))
            except Exception as e:
                result["warnings"].append(f"Events: {e}")

        # â”€â”€ Phase 2b: News â”€â”€
        if MODULES.get("phase2b") and self.sentiment_engine:
            try:
                ns = self._score_news(ticker)
                result["scores"]["news"] = ns["score"]
                result["reasons"].extend(ns.get("reasons", []))
            except Exception as e:
                result["warnings"].append(f"News: {e}")

        # â”€â”€ Phase 3b: Social â”€â”€
        if MODULES.get("phase3b") and self.social_pipeline:
            try:
                ss = self._score_social(ticker)
                result["scores"]["social"] = ss["score"]
                result["reasons"].extend(ss.get("reasons", []))
            except Exception as e:
                result["warnings"].append(f"Social: {e}")

        # â”€â”€ Aggregate â”€â”€
        w = self.weights
        final = sum(
            result["scores"].get(k, 50.0) * w.get(k, 0)
            for k in ["technical", "news", "social", "events"]
        )
        result["final_score"] = round(final, 1)

        # Confidence
        active = list(result["scores"].values())
        if len(active) >= 2:
            std = np.std(active)
            concordance = max(0, 1 - std / 25)
            coverage = len(active) / 4
            result["confidence"] = round(concordance * coverage * 100, 1)
        else:
            result["confidence"] = round(len(active) * 15, 1)

        # Decision
        f = result["final_score"]
        if event_blocked:
            result["decision"] = "HOLD"
        elif f >= self.cfg.strong_buy_threshold:
            result["decision"] = "STRONG BUY"
        elif f >= self.cfg.buy_threshold:
            result["decision"] = "BUY"
        elif f <= self.cfg.strong_sell_threshold:
            result["decision"] = "STRONG SELL"
        elif f <= self.cfg.sell_threshold:
            result["decision"] = "SELL"
        else:
            result["decision"] = "HOLD"

        # Position sizing (long-only by default; shorts only if explicitly enabled)
        decision = result["decision"]
        is_long_entry = decision in ("BUY", "STRONG BUY")
        is_short_entry = self.cfg.allow_short and decision in ("SELL", "STRONG SELL")
        if (is_long_entry or is_short_entry) and not event_blocked and event_modifier > 0:
            dist = abs(f - 50) / 50
            conf_f = result["confidence"] / 100
            size = self.cfg.base_position_pct * (1 + dist) * (0.5 + 0.5 * conf_f)
            size *= event_modifier
            result["position_size_pct"] = round(
                max(self.cfg.min_position_pct,
                    min(self.cfg.max_position_pct, size)), 3)

        return result
    def _score_technical(self, ticker: str) -> dict:
        df = self.fetcher.fetch(ticker, self.cfg.timeframe)
        if df is None or df.empty:
            return {"score": 50.0, "reasons": []}

        df = self.analyzer.compute_indicators(df)
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else row
        score = 50.0
        reasons = []

        rsi = row.get("rsi", 50)
        if pd.notna(rsi):
            if rsi < self.tech_config.rsi_oversold:
                score += 15; reasons.append(f"RSI oversold ({rsi:.1f})")
            elif rsi > self.tech_config.rsi_overbought:
                score -= 15; reasons.append(f"RSI overbought ({rsi:.1f})")

        bb_pct = row.get("bb_pct", 0.5)
        if pd.notna(bb_pct):
            if bb_pct < 0.1:
                score += 12; reasons.append("BB basse")
            elif bb_pct > 0.9:
                score -= 12; reasons.append("BB haute")

        macd = row.get("macd", 0)
        macd_sig = row.get("macd_signal", 0)
        if pd.notna(macd) and pd.notna(macd_sig):
            prev_macd = prev.get("macd", 0)
            prev_sig = prev.get("macd_signal", 0)
            if pd.notna(prev_macd) and pd.notna(prev_sig):
                if macd > macd_sig and prev_macd <= prev_sig:
                    score += 10; reasons.append("MACD bullish cross")
                elif macd < macd_sig and prev_macd >= prev_sig:
                    score -= 10; reasons.append("MACD bearish cross")

        sma200 = row.get("sma_200")
        close = row.get("Close", 0)
        if pd.notna(sma200) and pd.notna(close):
            if close > sma200:
                score += 5; reasons.append("Au-dessus SMA200")
            else:
                score -= 5

        vol_ratio = row.get("volume_ratio", 1.0)
        if pd.notna(vol_ratio) and vol_ratio > self.tech_config.volume_spike_threshold:
            if score > 50:
                score += 8; reasons.append(f"Volume spike ({vol_ratio:.1f}x) haussier")
            elif score < 50:
                score -= 8; reasons.append(f"Volume spike ({vol_ratio:.1f}x) baissier")

        return {"score": round(max(0, min(100, score)), 1), "reasons": reasons}

    def _score_events(self, ticker: str) -> dict:
        today = date.today()
        score = 50.0
        reasons = []
        warnings_list = []
        modifier = 1.0

        is_blocked, block_reason = self.event_filter.is_blackout(ticker, today)
        if is_blocked:
            warnings_list.append(f"âš ï¸ {block_reason}")
            modifier = 0.0
            return {
                "score": 50.0,
                "reasons": reasons,
                "warnings": warnings_list,
                "modifier": modifier,
                "blocked": True,
            }

        mod, mod_reason = self.event_filter.get_event_score_modifier(ticker, today)
        if mod != 1.0:
            modifier = mod
            reasons.append(mod_reason)
            if mod > 1.0:
                score += 10
            elif mod < 1.0:
                score -= 10

        return {
            "score": round(max(0, min(100, score)), 1),
            "reasons": reasons,
            "warnings": warnings_list,
            "modifier": modifier,
            "blocked": False,
        }

    def _score_news(self, ticker: str) -> dict:
        daily = self.sentiment_engine.get_current_sentiment(ticker)
        # DailySentiment.sentiment_score is [-1, 1]
        score = 50 + daily.sentiment_score * 50
        reasons = []

        if daily.signal == 1:
            label = "BULLISH"
        elif daily.signal == -1:
            label = "BEARISH"
        else:
            label = "NEUTRAL"

        if daily.article_count > 0:
            reasons.append(f"News {label} ({daily.article_count} articles, "
                          f"score {daily.sentiment_score:+.3f})")
            if daily.top_headlines:
                reasons.append(f"  → {daily.top_headlines[0][:80]}")

        return {"score": round(max(0, min(100, score)), 1), "reasons": reasons}

    def _score_social(self, ticker: str) -> dict:
        data = self.social_pipeline.analyze_ticker(ticker)
        score = data.get("social_score", 50.0)
        reasons = []
        label = data.get("social_label", "NEUTRAL")
        count = data.get("mention_count", 0)
        if count > 0:
            reasons.append(f"Social {label} ({count} posts)")
        return {"score": round(score, 1), "reasons": reasons}


# ─────────────────────────────────────────────
# Combined Backtester
# ─────────────────────────────────────────────

class CombinedBacktester:
    """
    Backtest sur données historiques.
    Technique = complet sur 2 ans. Sentiment/social = proxy momentum.
    Events = dates réelles.
    """

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self.tech_config = TradingConfig()
        self.fetcher = MarketDataFetcher()
        self.analyzer = TechnicalAnalyzer(self.tech_config)

        self.event_calendar = None
        self.event_filter = None
        if MODULES.get("phase2a"):
            self.event_calendar = EventCalendar(cfg.tickers)
            self.event_calendar.load_all()
            self.event_filter = EventFilter(self.event_calendar)

    def run(self, tickers: list = None) -> dict:
        tickers = tickers or self.cfg.tickers
        results = {}

        print("\n" + "=" * 70)
        print("  📊 BUBO — BACKTEST COMBINÉ")
        print("=" * 70)

        for ticker in tickers:
            print(f"\n{'─' * 70}")
            print(f"  Backtesting: {ticker}")

            df = self.fetcher.fetch(ticker, self.cfg.timeframe)
            if df is None or len(df) < 50:
                print(f"  ⚠️  Données insuffisantes")
                continue

            df = self.analyzer.compute_indicators(df)
            df["combined_score"] = self._compute_scores(df, ticker)
            # Decisions are made from t-1 signal and executed at t open (no look-ahead)
            df["combined_score_signal"] = df["combined_score"].shift(1)
            result = self._simulate(df, ticker)
            results[ticker] = result
            self._print_result(ticker, result)

        if results:
            self._print_summary(results)
            self._save(results)

        return results

    def _compute_scores(self, df: pd.DataFrame, ticker: str) -> pd.Series:
        """Score combiné jour par jour."""
        scores = pd.Series(50.0, index=df.index)

        for i in range(30, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            score = 50.0

            # RSI
            rsi = row.get("rsi", 50)
            if pd.notna(rsi):
                if rsi < self.tech_config.rsi_oversold:
                    score += 15
                elif rsi > self.tech_config.rsi_overbought:
                    score -= 15
                elif rsi < 40:
                    score += 5
                elif rsi > 60:
                    score -= 5

            # BB
            bb_pct = row.get("bb_pct", 0.5)
            if pd.notna(bb_pct):
                if bb_pct < 0.1:
                    score += 12
                elif bb_pct > 0.9:
                    score -= 12
                elif bb_pct < 0.25:
                    score += 5
                elif bb_pct > 0.75:
                    score -= 5

            # MACD cross
            macd, sig = row.get("macd", 0), row.get("macd_signal", 0)
            p_macd, p_sig = prev.get("macd", 0), prev.get("macd_signal", 0)
            if all(pd.notna(x) for x in [macd, sig, p_macd, p_sig]):
                if macd > sig and p_macd <= p_sig:
                    score += 10
                elif macd < sig and p_macd >= p_sig:
                    score -= 10

            # SMA200
            sma200 = row.get("sma_200")
            close = row.get("Close", 0)
            if pd.notna(sma200) and pd.notna(close):
                score += 5 if close > sma200 else -5

            # Volume
            vol_r = row.get("volume_ratio", 1.0)
            if pd.notna(vol_r) and vol_r > self.tech_config.volume_spike_threshold:
                score += 8 if score > 50 else -8

            # Momentum proxy (simule sentiment)
            if i >= 5:
                prev5_close = df.iloc[i - 5].get("Close", close)
                if pd.notna(prev5_close) and prev5_close > 0:
                    ret5 = (close / prev5_close) - 1
                    if ret5 > 0.05:
                        score += 5
                    elif ret5 < -0.05:
                        score -= 5

            # Event blackout
            if self.event_filter:
                check_date = df.index[i].date() if hasattr(df.index[i], 'date') else df.index[i]
                blocked, _ = self.event_filter.is_blackout(ticker, check_date)
                if blocked:
                    score = 50.0  # Force neutre

            scores.iloc[i] = max(0, min(100, score))

        return scores

    def _simulate(self, df: pd.DataFrame, ticker: str) -> dict:
        """Simule les trades sans look-ahead et avec couts d'execution."""
        capital = self.cfg.initial_capital
        position = None
        trades = []
        equity = []

        fee_rate = self.cfg.trade_fee_bps / 10_000
        slippage_rate = self.cfg.slippage_bps / 10_000

        for i in range(30, len(df)):
            row = df.iloc[i]
            dt = df.index[i]

            open_px = row.get("Open", np.nan)
            high_px = row.get("High", np.nan)
            low_px = row.get("Low", np.nan)
            close_px = row.get("Close", np.nan)
            signal_score = row.get("combined_score_signal", np.nan)

            # Check exit: decisions known at session open, execution on current bar.
            if position:
                exit_price = None
                exit_reason = None

                if pd.notna(low_px) and low_px <= position["stop_loss"]:
                    exit_price = position["stop_loss"] * (1 - slippage_rate)
                    exit_reason = "stop_loss"
                elif pd.notna(high_px) and high_px >= position["take_profit"]:
                    exit_price = position["take_profit"] * (1 - slippage_rate)
                    exit_reason = "take_profit"
                elif pd.notna(signal_score) and signal_score <= self.cfg.sell_threshold:
                    if pd.notna(open_px) and open_px > 0:
                        exit_price = open_px * (1 - slippage_rate)
                        exit_reason = "signal_sell"

                if exit_price is not None and exit_price > 0:
                    gross_exit = position["shares"] * exit_price
                    exit_fee = gross_exit * fee_rate
                    capital += gross_exit - exit_fee

                    gross_pnl = position["shares"] * (exit_price - position["entry"])
                    net_pnl = gross_pnl - position["entry_fee"] - exit_fee
                    invested = position["shares"] * position["entry"] + position["entry_fee"]
                    pnl_pct = (net_pnl / invested * 100) if invested > 0 else 0.0

                    trades.append({
                        "entry_date": position["date"],
                        "exit_date": str(dt)[:10],
                        "entry_price": round(position["entry"], 2),
                        "exit_price": round(exit_price, 2),
                        "pnl": round(net_pnl, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "exit_reason": exit_reason,
                        "entry_score": position["score"],
                        "hold_days": (dt - pd.Timestamp(position["date"])).days
                    })
                    position = None

            # Check entry: signal from t-1, execution at t open.
            if position is None and pd.notna(signal_score) and signal_score >= self.cfg.buy_threshold:
                signal_dt = df.index[i - 1] if i > 0 else dt

                blocked = False
                if self.event_filter:
                    check_date = signal_dt.date() if hasattr(signal_dt, "date") else signal_dt
                    blocked, _ = self.event_filter.is_blackout(ticker, check_date)

                if (not blocked) and pd.notna(open_px) and open_px > 0:
                    dist = (signal_score - 50) / 50
                    pct = self.cfg.base_position_pct * (1 + dist)
                    pct = max(self.cfg.min_position_pct,
                              min(self.cfg.max_position_pct, pct))

                    entry_price = open_px * (1 + slippage_rate)
                    size = capital * pct
                    shares = int(size / entry_price) if entry_price > 0 else 0

                    if shares > 0:
                        gross_entry = shares * entry_price
                        entry_fee = gross_entry * fee_rate
                        total_debit = gross_entry + entry_fee

                        if total_debit <= capital:
                            capital -= total_debit
                            position = {
                                "entry": entry_price,
                                "shares": shares,
                                "date": str(dt)[:10],
                                "score": signal_score,
                                "entry_fee": entry_fee,
                                "stop_loss": entry_price * (1 - self.cfg.stop_loss_pct),
                                "take_profit": entry_price * (1 + self.cfg.take_profit_pct),
                            }

            if pd.isna(close_px) or close_px <= 0:
                eq = capital
            else:
                eq = capital + (position["shares"] * close_px if position else 0)
            equity.append(eq)

        # Close open position at end of period.
        if position:
            last_close = df.iloc[-1].get("Close", np.nan)
            if pd.notna(last_close) and last_close > 0:
                exit_price = last_close * (1 - slippage_rate)
                gross_exit = position["shares"] * exit_price
                exit_fee = gross_exit * fee_rate
                capital += gross_exit - exit_fee

                gross_pnl = position["shares"] * (exit_price - position["entry"])
                net_pnl = gross_pnl - position["entry_fee"] - exit_fee
                invested = position["shares"] * position["entry"] + position["entry_fee"]
                pnl_pct = (net_pnl / invested * 100) if invested > 0 else 0.0

                trades.append({
                    "entry_date": position["date"],
                    "exit_date": str(df.index[-1])[:10],
                    "entry_price": round(position["entry"], 2),
                    "exit_price": round(exit_price, 2),
                    "pnl": round(net_pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "exit_reason": "end_of_period",
                    "entry_score": position["score"],
                    "hold_days": (df.index[-1] - pd.Timestamp(position["date"])).days
                })
                position = None
                if equity:
                    equity[-1] = capital

        eq_s = pd.Series(equity) if equity else pd.Series([self.cfg.initial_capital])
        final_equity = float(capital if position is None else eq_s.iloc[-1])
        total_ret = (final_equity / self.cfg.initial_capital - 1) * 100

        # Drawdown
        peak = eq_s.expanding().max()
        dd = ((eq_s - peak) / peak).min() * 100

        # Sharpe
        daily_ret = eq_s.pct_change().dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)) if daily_ret.std() > 0 else 0

        wins = [t for t in trades if t["pnl"] > 0]
        wr = len(wins) / len(trades) * 100 if trades else 0

        gp = sum(t["pnl"] for t in trades if t["pnl"] > 0)
        gl = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
        pf = gp / gl if gl > 0 else float('inf')

        avg_w = np.mean([t["pnl_pct"] for t in wins]) if wins else 0
        losers = [t for t in trades if t["pnl"] <= 0]
        avg_l = np.mean([t["pnl_pct"] for t in losers]) if losers else 0
        avg_hold = np.mean([t["hold_days"] for t in trades]) if trades else 0

        return {
            "trades": trades, "num_trades": len(trades),
            "win_rate": round(wr, 1),
            "total_pnl": round(sum(t["pnl"] for t in trades), 2),
            "total_return_pct": round(total_ret, 2),
            "max_drawdown_pct": round(dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "profit_factor": round(pf, 2),
            "avg_win_pct": round(avg_w, 2),
            "avg_loss_pct": round(avg_l, 2),
            "avg_hold_days": round(avg_hold, 1),
            "final_equity": round(final_equity, 2),
        }

    def _print_result(self, ticker: str, r: dict):
        print(f"\n  {ticker}")
        print(f"  {'─' * 50}")
        print(f"  📊 Trades:         {r['num_trades']}")
        print(f"  ✅ Win rate:        {r['win_rate']:.1f}%")
        print(f"  💰 PnL:            {r['total_pnl']:+.2f}€ ({r['total_return_pct']:+.2f}%)")
        print(f"  📉 Max drawdown:   {r['max_drawdown_pct']:.2f}%")
        print(f"  📐 Sharpe:         {r['sharpe_ratio']:.2f}")
        print(f"  📈 Profit factor:  {r['profit_factor']:.2f}")
        print(f"  🎯 Avg win/loss:   {r['avg_win_pct']:+.2f}% / {r['avg_loss_pct']:.2f}%")
        print(f"  ⏱️  Avg hold:       {r['avg_hold_days']:.0f}j")
        print(f"  💼 Capital final:  {r['final_equity']:.2f}€")

        if r["trades"]:
            print(f"  Derniers trades:")
            for t in r["trades"][-3:]:
                icon = "🟢" if t["pnl"] > 0 else "🔴"
                print(f"    {icon} {t['entry_date']} → {t['exit_date']} | "
                      f"{t['pnl']:+.2f}€ ({t['pnl_pct']:+.1f}%) | {t['exit_reason']}")

    def _print_summary(self, results: dict):
        print(f"\n{'═' * 70}")
        print(f"  📊 RÉSUMÉ GLOBAL")
        print(f"{'═' * 70}")

        total_pnl = sum(r["total_pnl"] for r in results.values())
        total_trades = sum(r["num_trades"] for r in results.values())
        avg_sharpe = np.mean([r["sharpe_ratio"] for r in results.values()])
        worst_dd = min(r["max_drawdown_pct"] for r in results.values())
        total_wins = sum(len([t for t in r["trades"] if t["pnl"] > 0])
                         for r in results.values())
        wr = total_wins / total_trades * 100 if total_trades > 0 else 0

        print(f"\n  {'Ticker':<10} {'#':>4} {'Win%':>6} {'PnL':>10} "
              f"{'Sharpe':>7} {'MaxDD':>7} {'Final€':>10}")
        print(f"  {'─' * 58}")
        for tk, r in results.items():
            print(f"  {tk:<10} {r['num_trades']:>4} {r['win_rate']:>5.1f}% "
                  f"{r['total_pnl']:>+9.2f}€ {r['sharpe_ratio']:>6.2f} "
                  f"{r['max_drawdown_pct']:>6.2f}% {r['final_equity']:>9.2f}€")

        print(f"  {'─' * 58}")
        total_eq = sum(r["final_equity"] for r in results.values())
        init = self.cfg.initial_capital * len(results)
        ret = (total_eq / init - 1) * 100

        print(f"  {'TOTAL':<10} {total_trades:>4} {wr:>5.1f}% "
              f"{total_pnl:>+9.2f}€ {avg_sharpe:>6.2f} "
              f"{worst_dd:>6.2f}% {total_eq:>9.2f}€")

        print(f"\n  Capital: {init:.0f}€ → {total_eq:.2f}€ ({ret:+.2f}%)")
        print(f"  Sharpe moyen: {avg_sharpe:.2f} | Pire DD: {worst_dd:.2f}%")

        print(f"\n  {'─' * 50}")
        if avg_sharpe > 1.5 and wr > 40 and worst_dd > -5:
            print(f"  ✅ VERDICT: Stratégie viable")
        elif avg_sharpe > 1.0 and worst_dd > -10:
            print(f"  🟡 VERDICT: Prometteur — à affiner")
        elif avg_sharpe > 0.5:
            print(f"  🟠 VERDICT: Modeste — le sentiment live devrait améliorer")
        else:
            print(f"  🔴 VERDICT: Insuffisant — revoir les paramètres")

    def _save(self, results: dict):
        all_trades = []
        for tk, r in results.items():
            for t in r["trades"]:
                t["ticker"] = tk
                all_trades.append(t)

        if all_trades:
            pd.DataFrame(all_trades).to_csv(
                "data/backtest_combined_trades.csv", index=False)
            print(f"\n  📁 Trades: data/backtest_combined_trades.csv")

        summary = [{
            "ticker": tk, **{k: v for k, v in r.items() if k != "trades"}
        } for tk, r in results.items()]
        pd.DataFrame(summary).to_csv(
            "data/backtest_combined_summary.csv", index=False)
        print(f"  📁 Résumé: data/backtest_combined_summary.csv")


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

def apply_portfolio_risk_gates(cfg: EngineConfig, results: dict) -> dict:
    """
    Apply live portfolio limits to per-ticker decisions.
    This mutates `results` in place and returns a short summary.
    """
    summary = {"kept": 0, "blocked": 0, "clipped": 0, "exposure": 0.0}
    if not results or not cfg.risk_gates_enabled:
        return summary

    def force_hold(item: dict, reason: str):
        item["decision"] = "HOLD"
        item["position_size_pct"] = 0.0
        warnings = item.setdefault("warnings", [])
        msg = f"Risk gate: {reason}"
        if msg not in warnings:
            warnings.append(msg)

    candidates = []

    # Gate 1: minimum confidence for long entries.
    for r in results.values():
        if r.get("decision") in ("BUY", "STRONG BUY"):
            conf = float(r.get("confidence", 0.0))
            if conf < cfg.min_confidence_for_entry:
                force_hold(r, f"confidence {conf:.1f}% < {cfg.min_confidence_for_entry:.1f}%")
                summary["blocked"] += 1
                continue
            pos = float(r.get("position_size_pct", 0.0) or 0.0)
            if pos <= 0:
                force_hold(r, "non-positive position size")
                summary["blocked"] += 1
                continue
            candidates.append(r)

    # Highest-quality candidates are allocated first.
    candidates.sort(
        key=lambda x: (float(x.get("final_score", 50.0)),
                       float(x.get("confidence", 0.0))),
        reverse=True,
    )

    exposure = 0.0
    for r in candidates:
        proposed = float(r.get("position_size_pct", 0.0) or 0.0)

        if summary["kept"] >= int(cfg.max_open_positions):
            force_hold(r, f"max positions reached ({cfg.max_open_positions})")
            summary["blocked"] += 1
            continue

        remaining = float(cfg.max_total_exposure_pct) - exposure
        if remaining <= 0:
            force_hold(r, f"max exposure reached ({cfg.max_total_exposure_pct:.0%})")
            summary["blocked"] += 1
            continue

        if proposed > remaining:
            if remaining >= cfg.min_position_pct:
                r["position_size_pct"] = round(remaining, 3)
                r.setdefault("warnings", []).append(
                    f"Risk gate: clipped to {r['position_size_pct']:.1%} (exposure cap)"
                )
                proposed = float(r["position_size_pct"])
                summary["clipped"] += 1
            else:
                force_hold(r, "insufficient exposure budget")
                summary["blocked"] += 1
                continue

        exposure += proposed
        summary["kept"] += 1

    summary["exposure"] = round(exposure, 4)
    return summary


def display_dashboard(engine: ScoringEngine, tickers: list) -> dict:
    print("\n" + "=" * 70)
    print(f"  🦉 BUBO — UNIFIED DASHBOARD")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    results = {}
    for ticker in tickers:
        results[ticker] = engine.score_ticker(ticker)

    risk_summary = apply_portfolio_risk_gates(engine.cfg, results)

    for ticker in tickers:
        r = results[ticker]

        score = r["final_score"]
        decision = r["decision"]
        name = r["name"]

        bar_len = 30
        filled = int((score / 100) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        icons = {"STRONG BUY": "🟢🟢", "BUY": "🟢", "HOLD": "⚪",
                 "SELL": "🔴", "STRONG SELL": "🔴🔴"}

        print(f"\n{'─' * 70}")
        print(f"  {ticker} — {name}  {icons.get(decision, '⚪')} {decision}")
        print(f"  Score:  [{bar}] {score:.1f}/100  (conf: {r['confidence']:.0f}%)")

        # Component scores
        if r["scores"]:
            parts = []
            for k, v in r["scores"].items():
                e = "🟢" if v > 55 else ("🔴" if v < 45 else "⚪")
                parts.append(f"{k}: {e}{v:.0f}")
            print(f"  {' | '.join(parts)}")

        if r["position_size_pct"] > 0:
            eur = r["position_size_pct"] * engine.cfg.initial_capital
            print(f"  💰 Position: {r['position_size_pct']:.1%} = {eur:.0f}€")

        for reason in r.get("reasons", [])[:4]:
            print(f"    → {reason}")
        for w in r.get("warnings", []):
            print(f"    {w}")

    # Summary table
    print(f"\n{'═' * 70}")
    print(f"  {'Ticker':<10} {'Score':>6} {'Décision':<14} {'Conf':>5} {'Pos':>7}")
    print(f"  {'─' * 46}")
    for tk, r in results.items():
        pos = f"{r['position_size_pct']:.0%}" if r['position_size_pct'] > 0 else "—"
        print(f"  {tk:<10} {r['final_score']:>5.1f} {r['decision']:<14} "
              f"{r['confidence']:>4.0f}% {pos:>7}")
    print("═" * 70)
    if risk_summary["blocked"] > 0 or risk_summary["clipped"] > 0:
        print("  Risk gates:"
              f" kept={risk_summary['kept']} blocked={risk_summary['blocked']}"
              f" clipped={risk_summary['clipped']}"
              f" exposure={risk_summary['exposure']:.1%}")

    # Export
    rows = [{
        "ticker": tk, "score": r["final_score"], "decision": r["decision"],
        "confidence": r["confidence"], "position_pct": r["position_size_pct"],
        "timestamp": r["timestamp"],
        **{f"score_{k}": v for k, v in r["scores"].items()}
    } for tk, r in results.items()]
    pd.DataFrame(rows).to_csv("data/signals_latest.csv", index=False)

    return results


# ─────────────────────────────────────────────
# Paper Trading
# ─────────────────────────────────────────────

def _paper_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_paper_state(cfg: EngineConfig) -> dict:
    ts = _paper_now()
    capital = float(cfg.initial_capital)
    return {
        "created_at": ts,
        "updated_at": ts,
        "cycles": 0,
        "cash": capital,
        "equity": capital,
        "realized_pnl": 0.0,
        "positions": {},
        "trades": [],
        "equity_curve": [],
        "action_log": [],
    }


def load_paper_state(state_path: str, cfg: EngineConfig) -> dict:
    p = Path(state_path)
    if not p.exists():
        return _new_paper_state(cfg)

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return _new_paper_state(cfg)

    base = _new_paper_state(cfg)
    base.update(data if isinstance(data, dict) else {})
    if not isinstance(base.get("positions"), dict):
        base["positions"] = {}
    if not isinstance(base.get("trades"), list):
        base["trades"] = []
    if not isinstance(base.get("equity_curve"), list):
        base["equity_curve"] = []
    if not isinstance(base.get("action_log"), list):
        base["action_log"] = []
    base["cash"] = float(base.get("cash", cfg.initial_capital))
    base["equity"] = float(base.get("equity", base["cash"]))
    base["realized_pnl"] = float(base.get("realized_pnl", 0.0))
    base["cycles"] = int(base.get("cycles", 0))
    return base


def save_paper_state(state_path: str, state: dict):
    p = Path(state_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def paper_report_paths(state_path: str) -> dict:
    p = Path(state_path)
    out_dir = p.parent if p.parent else Path(".")
    return {
        "trades_csv": str(out_dir / "paper_trades_latest.csv"),
        "equity_csv": str(out_dir / "paper_equity_curve_latest.csv"),
        "daily_csv": str(out_dir / "paper_daily_stats_latest.csv"),
        "daily_md": str(out_dir / "paper_daily_report_latest.md"),
    }


def _empty_daily_stats_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "day",
            "start_equity",
            "end_equity",
            "daily_return_pct",
            "high_equity",
            "low_equity",
            "max_drawdown_intraday",
            "cash_end",
            "market_value_end",
            "open_positions_end",
            "cycles",
            "actions",
            "closed_trades",
            "wins",
            "losses",
            "daily_win_rate",
            "closed_pnl",
            "realized_pnl_today",
            "realized_pnl_cum_end",
            "unrealized_pnl_end",
        ]
    )


def build_daily_paper_stats(state: dict) -> pd.DataFrame:
    curve_df = pd.DataFrame(state.get("equity_curve", []))
    if curve_df.empty:
        return _empty_daily_stats_df()

    curve_df["timestamp"] = pd.to_datetime(curve_df.get("timestamp"), errors="coerce")
    curve_df = curve_df.dropna(subset=["timestamp"]).sort_values("timestamp")
    if curve_df.empty:
        return _empty_daily_stats_df()

    for col in ["equity", "cash", "market_value", "realized_pnl", "unrealized_pnl", "open_positions"]:
        if col in curve_df.columns:
            curve_df[col] = pd.to_numeric(curve_df[col], errors="coerce")
        else:
            curve_df[col] = np.nan
    curve_df = curve_df.dropna(subset=["equity"])
    if curve_df.empty:
        return _empty_daily_stats_df()

    curve_df["day"] = curve_df["timestamp"].dt.date.astype(str)
    grouped = curve_df.groupby("day", sort=True)

    daily = grouped.agg(
        start_equity=("equity", "first"),
        end_equity=("equity", "last"),
        high_equity=("equity", "max"),
        low_equity=("equity", "min"),
        cash_end=("cash", "last"),
        market_value_end=("market_value", "last"),
        open_positions_end=("open_positions", "last"),
        cycles=("equity", "size"),
        realized_pnl_cum_end=("realized_pnl", "last"),
        unrealized_pnl_end=("unrealized_pnl", "last"),
    ).reset_index()

    daily["daily_return_pct"] = np.where(
        daily["start_equity"] > 0,
        (daily["end_equity"] / daily["start_equity"]) - 1,
        0.0,
    )
    daily["realized_pnl_today"] = daily["realized_pnl_cum_end"].diff()
    if not daily.empty:
        daily.loc[daily.index[0], "realized_pnl_today"] = daily.loc[daily.index[0], "realized_pnl_cum_end"]

    drawdowns = {}
    for day, grp in grouped:
        eq = pd.to_numeric(grp["equity"], errors="coerce").dropna()
        if eq.empty:
            drawdowns[str(day)] = 0.0
            continue
        peak = eq.cummax()
        dd = ((peak - eq) / peak).max()
        drawdowns[str(day)] = float(dd if pd.notna(dd) else 0.0)
    daily["max_drawdown_intraday"] = daily["day"].map(drawdowns).fillna(0.0)

    trades_df = pd.DataFrame(state.get("trades", []))
    if not trades_df.empty and "exit_date" in trades_df.columns:
        trades_df["day"] = trades_df["exit_date"].astype(str)
        trades_df["pnl"] = pd.to_numeric(trades_df.get("pnl"), errors="coerce").fillna(0.0)
        trades_daily = trades_df.groupby("day", sort=True)["pnl"].agg(["size", "sum"]).reset_index()
        trades_daily = trades_daily.rename(columns={"size": "closed_trades", "sum": "closed_pnl"})
        wins = trades_df.groupby("day")["pnl"].apply(lambda s: int((s > 0).sum())).reset_index(name="wins")
        losses = trades_df.groupby("day")["pnl"].apply(lambda s: int((s < 0).sum())).reset_index(name="losses")
        trades_daily = trades_daily.merge(wins, on="day", how="left").merge(losses, on="day", how="left")
    else:
        trades_daily = pd.DataFrame(columns=["day", "closed_trades", "closed_pnl", "wins", "losses"])

    actions_df = pd.DataFrame(state.get("action_log", []))
    if not actions_df.empty and "timestamp" in actions_df.columns:
        actions_df["timestamp"] = pd.to_datetime(actions_df["timestamp"], errors="coerce")
        actions_df = actions_df.dropna(subset=["timestamp"])
        if not actions_df.empty:
            actions_df["day"] = actions_df["timestamp"].dt.date.astype(str)
            actions_daily = actions_df.groupby("day", sort=True).size().reset_index(name="actions")
        else:
            actions_daily = pd.DataFrame(columns=["day", "actions"])
    else:
        actions_daily = pd.DataFrame(columns=["day", "actions"])

    daily = daily.merge(trades_daily, on="day", how="left")
    daily = daily.merge(actions_daily, on="day", how="left")

    for col in ["closed_trades", "wins", "losses", "actions"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce").fillna(0).astype(int)
    daily["closed_pnl"] = pd.to_numeric(daily["closed_pnl"], errors="coerce").fillna(0.0)
    daily["daily_win_rate"] = np.where(
        daily["closed_trades"] > 0,
        daily["wins"] / daily["closed_trades"],
        np.nan,
    )
    daily["open_positions_end"] = pd.to_numeric(daily["open_positions_end"], errors="coerce").fillna(0).astype(int)

    ordered = [
        "day",
        "start_equity",
        "end_equity",
        "daily_return_pct",
        "high_equity",
        "low_equity",
        "max_drawdown_intraday",
        "cash_end",
        "market_value_end",
        "open_positions_end",
        "cycles",
        "actions",
        "closed_trades",
        "wins",
        "losses",
        "daily_win_rate",
        "closed_pnl",
        "realized_pnl_today",
        "realized_pnl_cum_end",
        "unrealized_pnl_end",
    ]
    return daily[ordered].sort_values("day").reset_index(drop=True)


def render_daily_paper_markdown(state: dict, daily_df: pd.DataFrame) -> str:
    if daily_df.empty:
        return "# BUBO Paper Daily Report\n\nNo paper data available yet.\n"

    latest = daily_df.iloc[-1].to_dict()

    def _num(v, default=0.0):
        try:
            if pd.isna(v):
                return default
            return float(v)
        except Exception:
            return default

    wr = latest.get("daily_win_rate")
    if pd.notna(wr):
        wr_txt = f"{float(wr) * 100:.1f}%"
    else:
        wr_txt = "n/a"

    lines = [
        "# BUBO Paper Daily Report",
        "",
        f"Updated: {state.get('updated_at', 'n/a')}",
        f"Date: {latest.get('day', 'n/a')}",
        "",
        "## Daily Snapshot",
        f"- Equity end: {_num(latest.get('end_equity')):.2f}",
        f"- Return: {_num(latest.get('daily_return_pct')):.2%}",
        f"- Max intraday drawdown: {_num(latest.get('max_drawdown_intraday')):.2%}",
        f"- Open positions: {int(_num(latest.get('open_positions_end'), 0))}",
        f"- Actions: {int(_num(latest.get('actions'), 0))}",
        f"- Closed trades: {int(_num(latest.get('closed_trades'), 0))}",
        f"- Wins/Losses: {int(_num(latest.get('wins'), 0))}/{int(_num(latest.get('losses'), 0))} (win rate {wr_txt})",
        f"- Closed PnL: {_num(latest.get('closed_pnl')):+.2f}",
        f"- Realized PnL today: {_num(latest.get('realized_pnl_today')):+.2f}",
        "",
        "## Recent Actions (last 8)",
    ]

    actions = state.get("action_log", []) or []
    if actions:
        for item in actions[-8:]:
            ts = str(item.get("timestamp", "n/a"))
            act = str(item.get("action", ""))
            lines.append(f"- {ts} | {act}")
    else:
        lines.append("- none")

    lines.extend(["", "## Recent Closed Trades (last 8)"])
    trades = state.get("trades", []) or []
    if trades:
        for t in trades[-8:]:
            ticker = str(t.get("ticker", ""))
            exit_date = str(t.get("exit_date", ""))
            pnl = _num(t.get("pnl"))
            reason = str(t.get("exit_reason", ""))
            lines.append(f"- {exit_date} | {ticker} | pnl {pnl:+.2f} | {reason}")
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def export_paper_reports(state_path: str, state: dict) -> tuple[dict, dict]:
    paths = paper_report_paths(state_path)

    trades = state.get("trades", [])
    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        trades_df = pd.DataFrame(
            columns=[
                "ticker",
                "entry_date",
                "exit_date",
                "entry_price",
                "exit_price",
                "shares",
                "entry_fee",
                "exit_fee",
                "pnl",
                "exit_reason",
                "hold_days",
            ]
        )
    trades_df.to_csv(paths["trades_csv"], index=False)

    curve = state.get("equity_curve", [])
    curve_df = pd.DataFrame(curve)
    if curve_df.empty:
        curve_df = pd.DataFrame(
            columns=[
                "timestamp",
                "equity",
                "cash",
                "market_value",
                "realized_pnl",
                "unrealized_pnl",
                "open_positions",
            ]
        )
    curve_df.to_csv(paths["equity_csv"], index=False)

    daily_df = build_daily_paper_stats(state)
    daily_df.to_csv(paths["daily_csv"], index=False)

    md_text = render_daily_paper_markdown(state, daily_df)
    Path(paths["daily_md"]).write_text(md_text, encoding="utf-8")

    latest_daily = daily_df.iloc[-1].to_dict() if not daily_df.empty else {}
    return paths, latest_daily


def compute_paper_metrics(state: dict, market_value: float) -> dict:
    trades = state.get("trades", [])
    pnl_values = [float(t.get("pnl", 0.0)) for t in trades if isinstance(t, dict)]
    num_closed = len(pnl_values)
    wins = sum(1 for p in pnl_values if p > 0)
    losses = sum(1 for p in pnl_values if p < 0)
    win_rate = (wins / num_closed) if num_closed else None

    gross_win = sum(p for p in pnl_values if p > 0)
    gross_loss = abs(sum(p for p in pnl_values if p < 0))
    if gross_loss > 0:
        profit_factor = gross_win / gross_loss
    elif gross_win > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    max_drawdown = 0.0
    peak = None
    for row in state.get("equity_curve", []):
        try:
            eq = float(row.get("equity", np.nan))
        except Exception:
            eq = np.nan
        if not np.isfinite(eq) or eq <= 0:
            continue
        if peak is None or eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > max_drawdown:
            max_drawdown = dd

    equity = float(state.get("equity", 0.0))
    exposure = (market_value / equity) if equity > 0 else 0.0

    return {
        "num_closed_trades": int(num_closed),
        "wins": int(wins),
        "losses": int(losses),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": float(max_drawdown),
        "exposure": float(exposure),
    }


def notify_paper_webhook(webhook_url: str, summary: dict, watch_mode: bool = False) -> tuple[bool, str]:
    if not webhook_url:
        return False, "webhook not set"
    actions = summary.get("actions", []) or []
    if not actions:
        return False, "no actions"

    mode = "WATCH" if watch_mode else "LIVE"
    lines = [
        f"BUBO Paper [{mode}]",
        f"Equity={summary.get('equity', 0.0):.2f} Cash={summary.get('cash', 0.0):.2f}",
        f"Pos={summary.get('positions', 0)} WinRate="
        f"{(summary.get('win_rate') * 100):.1f}%"
        if summary.get("win_rate") is not None
        else f"Pos={summary.get('positions', 0)} WinRate=n/a",
        f"Actions: {', '.join(actions[:5])}",
    ]
    message = "\n".join(lines)
    payload = {"content": message, "text": message}

    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            status = int(getattr(resp, "status", 200))
        if 200 <= status < 300:
            return True, f"http {status}"
        return False, f"http {status}"
    except Exception as e:
        return False, str(e)


def _latest_price(engine: ScoringEngine, ticker: str, cache: dict) -> float | None:
    if ticker in cache:
        return cache[ticker]

    px = None
    try:
        if engine.fetcher:
            df = engine.fetcher.fetch(ticker, engine.cfg.timeframe)
            if df is not None and not df.empty:
                close = df.iloc[-1].get("Close", np.nan)
                if pd.notna(close) and float(close) > 0:
                    px = float(close)
    except Exception:
        px = None

    cache[ticker] = px
    return px


def _normalize_paper_broker(value: str) -> str:
    broker = str(value or "local").strip().lower()
    return broker if broker in {"local", "ibkr"} else "local"


class IBKRPaperAdapter:
    """
    Minimal IBKR paper adapter via ib_insync.
    Keeps BUBO paper-state semantics while sending simulated orders to IBKR.
    """

    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self._ib = None

    def connect(self):
        try:
            from ib_insync import IB  # type: ignore
        except Exception as e:
            raise RuntimeError("ib_insync is required for --paper-broker ibkr") from e

        self._ib = IB()
        self._ib.connect(
            host=self.cfg.ibkr_host,
            port=int(self.cfg.ibkr_port),
            clientId=int(self.cfg.ibkr_client_id),
            timeout=8,
            readonly=False,
            account=str(self.cfg.ibkr_account or "") or None,
        )
        if not self._ib.isConnected():
            raise RuntimeError("IBKR connection failed")
        return self._ib

    def disconnect(self):
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None

    def place_market_order(self, ticker: str, side: str, quantity: int) -> dict:
        if quantity <= 0:
            return {"ok": False, "reason": "invalid quantity"}

        try:
            from ib_insync import Stock, MarketOrder  # type: ignore
        except Exception as e:
            return {"ok": False, "reason": f"ib_insync missing: {e}"}

        ib = self._ib
        if ib is None or not ib.isConnected():
            return {"ok": False, "reason": "not connected"}

        try:
            contract = Stock(
                symbol=ticker,
                exchange=self.cfg.ibkr_exchange,
                currency=self.cfg.ibkr_currency,
            )
            ib.qualifyContracts(contract)
            order = MarketOrder("BUY" if side.upper() == "BUY" else "SELL", int(quantity))
            if self.cfg.ibkr_account:
                order.account = self.cfg.ibkr_account
            trade = ib.placeOrder(contract, order)
            # Wait a bit for fill updates in paper account.
            for _ in range(20):
                ib.sleep(0.25)
                if trade.isDone():
                    break

            filled = int(getattr(trade.orderStatus, "filled", 0) or 0)
            avg_fill = float(getattr(trade.orderStatus, "avgFillPrice", 0.0) or 0.0)
            status = str(getattr(trade.orderStatus, "status", "") or "")

            commission = 0.0
            for fill in getattr(trade, "fills", []) or []:
                rep = getattr(fill, "commissionReport", None)
                if rep is not None:
                    try:
                        commission += float(getattr(rep, "commission", 0.0) or 0.0)
                    except Exception:
                        pass

            if filled <= 0 or avg_fill <= 0:
                return {
                    "ok": False,
                    "reason": f"not filled (status={status})",
                    "status": status,
                }

            return {
                "ok": True,
                "status": status or "Filled",
                "filled": filled,
                "avg_fill_price": avg_fill,
                "commission": float(max(0.0, commission)),
            }
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    def fetch_open_positions(self) -> dict:
        ib = self._ib
        if ib is None or not ib.isConnected():
            return {"ok": False, "reason": "not connected", "positions": []}

        account = str(self.cfg.ibkr_account or "").strip()
        try:
            try:
                pos_rows = ib.positions(account=account) if account else ib.positions()
            except TypeError:
                pos_rows = ib.positions()
        except Exception as e:
            return {"ok": False, "reason": str(e), "positions": []}

        out = []
        for row in pos_rows or []:
            contract = getattr(row, "contract", None)
            symbol = str(getattr(contract, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            try:
                qty = int(getattr(row, "position", 0) or 0)
            except Exception:
                qty = 0
            if qty <= 0:
                continue
            try:
                avg_cost = float(getattr(row, "avgCost", 0.0) or 0.0)
            except Exception:
                avg_cost = 0.0
            try:
                market_price = float(getattr(row, "marketPrice", None))
                if market_price != market_price:  # NaN
                    market_price = None
            except Exception:
                market_price = None
            if market_price is None or market_price <= 0:
                market_price = avg_cost if avg_cost > 0 else None
            out.append(
                {
                    "ticker": symbol,
                    "shares": int(qty),
                    "avg_cost": float(avg_cost),
                    "market_price": float(market_price) if market_price is not None else None,
                }
            )
        return {"ok": True, "positions": out}


def run_paper_cycle(engine: ScoringEngine, results: dict, state_path: str) -> dict:
    """
    Execute one paper-trading cycle from current signals.
    Long-only, with slippage/fees and persistent state.
    """
    cfg = engine.cfg
    state = load_paper_state(state_path, cfg)
    broker = _normalize_paper_broker(getattr(cfg, "paper_broker", "local"))
    state["paper_broker"] = broker
    ibkr_positions_policy = _normalize_ibkr_existing_positions_policy(
        getattr(cfg, "ibkr_existing_positions_policy", "include")
    )
    managed_capital = float(
        getattr(cfg, "ibkr_capital_limit", cfg.initial_capital)
        if broker == "ibkr"
        else cfg.initial_capital
    )
    if managed_capital <= 0:
        managed_capital = float(cfg.initial_capital)

    fee_rate = cfg.trade_fee_bps / 10_000
    slippage_rate = cfg.slippage_bps / 10_000

    positions = state["positions"]
    actions = []
    warnings_list = []
    price_cache = {}
    ibkr = None
    ibkr_trading_disabled = False
    if broker == "ibkr":
        ibkr = IBKRPaperAdapter(cfg)
        try:
            ibkr.connect()
        except Exception as e:
            warnings_list.append(f"IBKR unavailable: {e}")
            ibkr = None
            ibkr_trading_disabled = True
        if ibkr is not None and ibkr_positions_policy == "include":
            sync = ibkr.fetch_open_positions()
            if sync.get("ok"):
                synced: dict[str, dict[str, Any]] = {}
                for p in sync.get("positions", []) or []:
                    if not isinstance(p, dict):
                        continue
                    ticker = str(p.get("ticker", "")).strip().upper()
                    shares = int(p.get("shares", 0) or 0)
                    if not ticker or shares <= 0:
                        continue
                    prev = positions.get(ticker, {}) if isinstance(positions, dict) else {}
                    entry_fee = float(prev.get("entry_fee", 0.0) or 0.0)
                    entry_date = str(prev.get("entry_date", date.today().isoformat()) or date.today().isoformat())
                    avg_cost = float(p.get("avg_cost", 0.0) or 0.0)
                    last_price = float(p.get("market_price", 0.0) or 0.0)
                    if last_price <= 0:
                        last_price = avg_cost if avg_cost > 0 else 0.0
                    mv = float(shares * last_price)
                    invested = float(shares * avg_cost + entry_fee)
                    synced[ticker] = {
                        "ticker": ticker,
                        "shares": shares,
                        "entry_price": float(avg_cost),
                        "entry_fee": float(entry_fee),
                        "entry_date": entry_date,
                        "last_price": float(last_price),
                        "market_value": float(mv),
                        "unrealized_pnl": float(mv - invested),
                    }
                positions.clear()
                positions.update(synced)
            else:
                warnings_list.append(
                    f"IBKR positions sync failed: {sync.get('reason', 'unknown')}"
                )

    # Build superset for pricing: active signals + currently held positions.
    signal_tickers = set(results.keys()) if results else set()
    held_tickers = set(positions.keys())
    all_tickers = sorted(signal_tickers | held_tickers)
    prices = {tk: _latest_price(engine, tk, price_cache) for tk in all_tickers}

    # Exit logic first.
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        px = prices.get(ticker)
        if px is None or px <= 0:
            continue

        decision = (results.get(ticker, {}) or {}).get("decision", "HOLD")
        exit_reason = None
        if decision in ("SELL", "STRONG SELL"):
            exit_reason = "signal_sell"
        elif px <= pos["entry_price"] * (1 - cfg.stop_loss_pct):
            exit_reason = "stop_loss"
        elif px >= pos["entry_price"] * (1 + cfg.take_profit_pct):
            exit_reason = "take_profit"

        if exit_reason:
            exec_px = px * (1 - slippage_rate)
            exit_fee = 0.0
            sold_shares = int(pos["shares"])
            if broker == "ibkr":
                if ibkr is None or ibkr_trading_disabled:
                    continue
                order_res = ibkr.place_market_order(ticker, "SELL", int(pos["shares"]))
                if not order_res.get("ok"):
                    warnings_list.append(
                        f"IBKR SELL {ticker} skipped: {order_res.get('reason', 'unknown')}"
                    )
                    continue
                sold_shares = int(order_res.get("filled", pos["shares"]))
                exec_px = float(order_res.get("avg_fill_price", exec_px) or exec_px)
                exit_fee = float(order_res.get("commission", 0.0) or 0.0)
            else:
                gross_for_fee = pos["shares"] * exec_px
                exit_fee = gross_for_fee * fee_rate

            gross = sold_shares * exec_px
            net_credit = gross - exit_fee
            state["cash"] += net_credit

            entry_shares = int(pos.get("shares", sold_shares))
            entry_cost = entry_shares * pos["entry_price"] + pos.get("entry_fee", 0.0)
            pnl = net_credit - entry_cost
            state["realized_pnl"] += pnl

            hold_days = 0
            try:
                hold_days = (datetime.now().date() - date.fromisoformat(pos["entry_date"])).days
            except Exception:
                hold_days = 0

            state["trades"].append(
                {
                    "ticker": ticker,
                    "entry_date": pos.get("entry_date"),
                    "exit_date": date.today().isoformat(),
                    "entry_price": round(pos["entry_price"], 4),
                    "exit_price": round(exec_px, 4),
                    "shares": int(sold_shares),
                    "entry_fee": round(pos.get("entry_fee", 0.0), 4),
                    "exit_fee": round(exit_fee, 4),
                    "pnl": round(pnl, 4),
                    "exit_reason": exit_reason,
                    "hold_days": int(hold_days),
                }
            )

            actions.append(f"SELL {ticker} x{int(sold_shares)} ({exit_reason})")
            del positions[ticker]

    def positions_market_value() -> float:
        total = 0.0
        for tk, pos in positions.items():
            px = prices.get(tk)
            if px is None or px <= 0:
                px = float(pos.get("last_price", pos["entry_price"]))
            total += pos["shares"] * px
        return total

    # Entry logic after exits.
    ranked_signals = sorted(
        (results or {}).values(),
        key=lambda r: (float(r.get("final_score", 50.0)), float(r.get("confidence", 0.0))),
        reverse=True,
    )
    for r in ranked_signals:
        ticker = str(r.get("ticker", "")).strip().upper()
        if not ticker or ticker in positions:
            continue
        if r.get("decision") not in ("BUY", "STRONG BUY"):
            continue

        target_pct = float(r.get("position_size_pct", 0.0) or 0.0)
        if target_pct <= 0:
            continue
        if len(positions) >= int(cfg.max_open_positions):
            continue

        px = prices.get(ticker)
        if px is None or px <= 0:
            continue

        eq_before = float(managed_capital) if broker == "ibkr" else (float(state["cash"]) + positions_market_value())
        current_expo = positions_market_value() / eq_before if eq_before > 0 else 0.0
        remaining_expo = cfg.max_total_exposure_pct - current_expo
        if remaining_expo < cfg.min_position_pct:
            continue
        target_pct = min(target_pct, remaining_expo)

        exec_px = px * (1 + slippage_rate)
        if broker == "ibkr":
            remaining_alloc = max(0.0, managed_capital - positions_market_value())
            desired_value = min(float(managed_capital) * target_pct, remaining_alloc)
        else:
            desired_value = float(state["cash"]) * target_pct
        shares = int(desired_value / exec_px) if exec_px > 0 else 0
        if shares <= 0:
            continue

        gross = shares * exec_px
        entry_fee = gross * fee_rate
        total_debit = gross + entry_fee
        if broker != "ibkr" and total_debit > state["cash"]:
            affordable = int(state["cash"] / (exec_px * (1 + fee_rate))) if exec_px > 0 else 0
            shares = max(0, affordable)
            gross = shares * exec_px
            entry_fee = gross * fee_rate
            total_debit = gross + entry_fee
        if broker != "ibkr" and (shares <= 0 or total_debit > state["cash"]):
            continue

        if broker == "ibkr":
            if ibkr is None or ibkr_trading_disabled:
                continue
            order_res = ibkr.place_market_order(ticker, "BUY", int(shares))
            if not order_res.get("ok"):
                warnings_list.append(
                    f"IBKR BUY {ticker} skipped: {order_res.get('reason', 'unknown')}"
                )
                continue
            shares = int(order_res.get("filled", shares))
            exec_px = float(order_res.get("avg_fill_price", exec_px) or exec_px)
            gross = shares * exec_px
            entry_fee = float(order_res.get("commission", 0.0) or 0.0)
            total_debit = gross + entry_fee
            if shares <= 0:
                continue

        if broker != "ibkr":
            state["cash"] -= total_debit
        positions[ticker] = {
            "ticker": ticker,
            "shares": int(shares),
            "entry_price": float(exec_px),
            "entry_fee": float(entry_fee),
            "entry_date": date.today().isoformat(),
            "last_price": float(px),
            "market_value": float(shares * px),
            "unrealized_pnl": float((shares * px) - (shares * exec_px + entry_fee)),
        }
        actions.append(f"BUY {ticker} x{shares}")

    # Mark-to-market and equity update.
    unrealized = 0.0
    market_value = 0.0
    for tk, pos in positions.items():
        px = prices.get(tk)
        if px is None or px <= 0:
            px = float(pos.get("last_price", pos["entry_price"]))
        pos["last_price"] = round(float(px), 4)
        mv = float(pos["shares"] * px)
        invested = float(pos["shares"] * pos["entry_price"] + pos.get("entry_fee", 0.0))
        upnl = mv - invested
        pos["market_value"] = round(mv, 4)
        pos["unrealized_pnl"] = round(upnl, 4)
        unrealized += upnl
        market_value += mv

    if broker == "ibkr":
        state["cash"] = round(max(0.0, float(managed_capital) - market_value), 4)
    else:
        state["cash"] = round(float(state["cash"]), 4)
    state["realized_pnl"] = round(float(state["realized_pnl"]), 4)
    if broker == "ibkr":
        state["equity"] = round(float(managed_capital), 4)
    else:
        state["equity"] = round(state["cash"] + market_value, 4)
    state["updated_at"] = _paper_now()
    state["cycles"] = int(state.get("cycles", 0)) + 1
    for action in actions:
        state["action_log"].append(
            {
                "timestamp": state["updated_at"],
                "action": action,
            }
        )
    state["equity_curve"].append(
        {
            "timestamp": state["updated_at"],
            "equity": round(state["equity"], 4),
            "cash": round(state["cash"], 4),
            "market_value": round(float(market_value), 4),
            "realized_pnl": round(float(state["realized_pnl"]), 4),
            "unrealized_pnl": round(float(unrealized), 4),
            "open_positions": int(len(positions)),
        }
    )
    if ibkr is not None:
        ibkr.disconnect()

    save_paper_state(state_path, state)
    report_paths, daily_latest = export_paper_reports(state_path, state)
    metrics = compute_paper_metrics(state, market_value)

    daily_win_rate = daily_latest.get("daily_win_rate")
    if pd.isna(daily_win_rate):
        daily_win_rate = None
    else:
        daily_win_rate = float(daily_win_rate)

    return {
        "paper_broker": state.get("paper_broker", broker),
        "managed_capital": round(float(managed_capital), 4),
        "ibkr_existing_positions_policy": ibkr_positions_policy if broker == "ibkr" else "",
        "state_path": state_path,
        "trades_path": report_paths["trades_csv"],
        "equity_curve_path": report_paths["equity_csv"],
        "daily_stats_path": report_paths["daily_csv"],
        "daily_report_path": report_paths["daily_md"],
        "equity": state["equity"],
        "cash": state["cash"],
        "realized_pnl": state["realized_pnl"],
        "unrealized_pnl": round(unrealized, 4),
        "positions": len(positions),
        "actions": actions,
        "num_closed_trades": metrics["num_closed_trades"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "win_rate": metrics["win_rate"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "exposure": metrics["exposure"],
        "daily_date": daily_latest.get("day"),
        "daily_return_pct": float(daily_latest.get("daily_return_pct", 0.0) or 0.0),
        "daily_closed_trades": int(daily_latest.get("closed_trades", 0) or 0),
        "daily_wins": int(daily_latest.get("wins", 0) or 0),
        "daily_losses": int(daily_latest.get("losses", 0) or 0),
        "daily_win_rate": daily_win_rate,
        "daily_actions": int(daily_latest.get("actions", 0) or 0),
        "daily_closed_pnl": float(daily_latest.get("closed_pnl", 0.0) or 0.0),
        "daily_realized_pnl": float(daily_latest.get("realized_pnl_today", 0.0) or 0.0),
        "warnings": warnings_list,
    }


def print_paper_summary(summary: dict):
    win_rate = summary.get("win_rate")
    win_rate_txt = f"{win_rate * 100:.1f}%" if win_rate is not None else "n/a"
    daily_win_rate = summary.get("daily_win_rate")
    daily_wr_txt = f"{daily_win_rate * 100:.1f}%" if daily_win_rate is not None else "n/a"
    profit_factor = summary.get("profit_factor")
    if profit_factor is None:
        pf_txt = "n/a"
    elif np.isinf(float(profit_factor)):
        pf_txt = "inf"
    else:
        pf_txt = f"{float(profit_factor):.2f}"

    print("\n  Paper:")
    print(f"   Broker={summary.get('paper_broker', 'local')}")
    if summary.get("paper_broker") == "ibkr":
        print(f"   ManagedCapital={summary.get('managed_capital', 0.0):.2f} "
              f"| ExistingPositions={summary.get('ibkr_existing_positions_policy', 'include')}")
    print(f"   Equity={summary['equity']:.2f} | Cash={summary['cash']:.2f} "
          f"| Realized={summary['realized_pnl']:+.2f} | Unrealized={summary['unrealized_pnl']:+.2f} "
          f"| Positions={summary['positions']}")
    print(f"   ClosedTrades={summary.get('num_closed_trades', 0)} "
          f"| WinRate={win_rate_txt} | ProfitFactor={pf_txt} "
          f"| MaxDD={summary.get('max_drawdown', 0.0):.2%} "
          f"| Exposure={summary.get('exposure', 0.0):.1%}")
    if summary.get("daily_date"):
        print(f"   Daily[{summary['daily_date']}]: Return={summary.get('daily_return_pct', 0.0):+.2%} "
              f"| Closed={summary.get('daily_closed_trades', 0)} "
              f"| Wins/Losses={summary.get('daily_wins', 0)}/{summary.get('daily_losses', 0)} "
              f"| DayWR={daily_wr_txt} "
              f"| DayPnL={summary.get('daily_closed_pnl', 0.0):+.2f} "
              f"| RealizedToday={summary.get('daily_realized_pnl', 0.0):+.2f} "
              f"| Actions={summary.get('daily_actions', 0)}")
    if summary.get("actions"):
        print(f"   Actions: {', '.join(summary['actions'][:5])}")
    if summary.get("warnings"):
        print(f"   Warnings: {', '.join(summary['warnings'][:3])}")
    print(f"   State: {summary['state_path']}")
    if summary.get("trades_path"):
        print(f"   Trades CSV: {summary['trades_path']}")
    if summary.get("equity_curve_path"):
        print(f"   Equity CSV: {summary['equity_curve_path']}")
    if summary.get("daily_stats_path"):
        print(f"   Daily CSV: {summary['daily_stats_path']}")
    if summary.get("daily_report_path"):
        print(f"   Daily MD: {summary['daily_report_path']}")


# ─────────────────────────────────────────────
# Universe Prescreen
# ─────────────────────────────────────────────

def build_deep_analysis_list(cfg: EngineConfig,
                             universe: list,
                             preselect_top: int,
                             max_deep: int,
                             use_budget_gate: bool = True) -> tuple[list, pd.DataFrame, dict]:
    """
    Prescreen broad universe and keep a shortlist for deep analysis.
    """
    if not MODULES.get("screener"):
        shortlist = list(universe)[:max(1, max_deep)]
        return shortlist, pd.DataFrame({"ticker": shortlist}), {
            "warning": "screener unavailable",
            "requested": max_deep,
            "allowed": len(shortlist),
        }

    screener = UniverseScreener(
        ScreenerConfig(
            timeframe=cfg.timeframe,
            top_n=max(1, preselect_top),
        )
    )
    ranked = screener.screen(universe, top_n=max(1, preselect_top))
    if ranked.empty:
        return [], ranked, {"requested": max_deep, "allowed": 0}

    if use_budget_gate:
        budget = APIBudgetManager(
            APIBudgetConfig(watch_interval_min=cfg.watch_interval_min)
        )
        selected, meta = budget.apply(ranked, requested_max=max_deep)
    else:
        selected = ranked.head(max(1, max_deep)).copy()
        meta = {
            "requested": max_deep,
            "allowed": len(selected),
            "dropped": max(0, len(ranked) - len(selected)),
            "budget_cap": len(selected),
        }

    tickers = selected["ticker"].astype(str).tolist() if not selected.empty else []
    return tickers, selected, meta


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BUBO — Unified Trading Engine")
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--universe-file", type=str,
                        help="Fichier .txt/.csv de tickers pour prescreen large univers")
    parser.add_argument("--preselect-top", type=int, default=40,
                        help="Nombre de tickers gardes apres prescreen")
    parser.add_argument("--max-deep", type=int, default=20,
                        help="Nombre max de tickers pour analyse detaillee")
    parser.add_argument(
        "--watch-interval-min",
        type=int,
        default=int(os.getenv("BUBO_WATCH_INTERVAL_MIN", "30")),
        help="Intervalle de refresh en mode watch (minutes)",
    )
    parser.add_argument(
        "--us-market-only",
        action=argparse.BooleanOptionalAction,
        default=str(os.getenv("BUBO_US_MARKET_ONLY", "1")).strip().lower() in {"1", "true", "yes", "on"},
        help="En mode watch, execute les cycles seulement pendant la session reguliere US (09:30-16:00 ET).",
    )
    parser.add_argument("--screen-only", action="store_true",
                        help="Ne fait que la preslection et exporte la shortlist")
    parser.add_argument("--no-budget-gate", action="store_true",
                        help="Desactive la limite automatique liee aux budgets API")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--no-finbert", action="store_true")
    parser.add_argument("--paper", action="store_true",
                        help="Active le paper trading avec etat persistant")
    parser.add_argument("--paper-state", type=str, default="data/paper_portfolio_state.json",
                        help="Chemin du fichier etat paper trading")
    parser.add_argument("--paper-reset", action="store_true",
                        help="Reinitialise l'etat paper trading avant execution")
    parser.add_argument("--paper-webhook", type=str, default="",
                        help="Webhook URL pour alertes paper (Discord/Slack)")
    parser.add_argument("--decision-engine", type=str, default=os.getenv("BUBO_DECISION_ENGINE", "llm"),
                        help="Decision engine: llm|rules")
    parser.add_argument("--paper-broker", type=str, default=os.getenv("BUBO_PAPER_BROKER", "local"),
                        help="Broker paper: local|ibkr")
    parser.add_argument("--ibkr-host", type=str, default=os.getenv("BUBO_IBKR_HOST", "127.0.0.1"),
                        help="IBKR host (TWS/Gateway)")
    parser.add_argument("--ibkr-port", type=int, default=int(os.getenv("BUBO_IBKR_PORT", "7497")),
                        help="IBKR port (paper often 7497)")
    parser.add_argument("--ibkr-client-id", type=int, default=int(os.getenv("BUBO_IBKR_CLIENT_ID", "42")),
                        help="IBKR client id")
    parser.add_argument("--ibkr-account", type=str, default=os.getenv("BUBO_IBKR_ACCOUNT", ""),
                        help="IBKR account id (optionnel)")
    parser.add_argument("--ibkr-exchange", type=str, default=os.getenv("BUBO_IBKR_EXCHANGE", "SMART"),
                        help="IBKR stock exchange route")
    parser.add_argument("--ibkr-currency", type=str, default=os.getenv("BUBO_IBKR_CURRENCY", "USD"),
                        help="IBKR contract currency")
    parser.add_argument(
        "--ibkr-capital-limit",
        type=float,
        default=float(os.getenv("BUBO_IBKR_CAPITAL_LIMIT", os.getenv("BUBO_CAPITAL", "10000"))),
        help="Capital max alloue a BUBO sur IBKR",
    )
    parser.add_argument(
        "--ibkr-existing-positions-policy",
        type=str,
        default=os.getenv("BUBO_IBKR_EXISTING_POSITIONS_POLICY", "include"),
        help="Traitement des positions deja ouvertes sur IBKR: include|ignore",
    )
    args = parser.parse_args()
    paper_state_path = args.paper_state

    if args.paper_reset:
        p = Path(paper_state_path)
        try:
            if p.exists():
                p.unlink()
                print(f"  Paper state reset: {paper_state_path}")
            else:
                print(f"  Paper state absent, rien a reset: {paper_state_path}")
        except Exception as e:
            print(f"  Impossible de reset paper state ({paper_state_path}): {e}")
            sys.exit(1)

    print("=" * 70)
    print("  🦉 BUBO — Unified Trading Engine")
    print("=" * 70)

    cfg = EngineConfig()
    cfg.initial_capital = args.capital
    cfg.watch_interval_min = max(1, int(args.watch_interval_min))
    cfg.us_market_only = bool(args.us_market_only)
    cfg.use_finbert = not args.no_finbert
    cfg.decision_engine = _normalize_decision_engine(args.decision_engine)
    cfg.paper_broker = _normalize_paper_broker(args.paper_broker)
    cfg.ibkr_host = str(args.ibkr_host).strip() or "127.0.0.1"
    cfg.ibkr_port = int(args.ibkr_port)
    cfg.ibkr_client_id = int(args.ibkr_client_id)
    cfg.ibkr_account = str(args.ibkr_account).strip()
    cfg.ibkr_exchange = str(args.ibkr_exchange).strip() or "SMART"
    cfg.ibkr_currency = str(args.ibkr_currency).strip().upper() or "USD"
    cfg.ibkr_capital_limit = max(1.0, float(args.ibkr_capital_limit))
    cfg.ibkr_existing_positions_policy = _normalize_ibkr_existing_positions_policy(
        args.ibkr_existing_positions_policy
    )

    if args.tickers:
        cfg.tickers = args.tickers
    elif args.universe_file:
        if not MODULES.get("screener"):
            print("âŒ Module screener indisponible, impossible de faire --universe-file")
            sys.exit(1)

        try:
            universe = load_universe(args.universe_file)
        except Exception as e:
            print(f"âŒ Chargement univers impossible: {e}")
            sys.exit(1)

        if not universe:
            print("âŒ Univers vide")
            sys.exit(1)

        print(f"\nðŸ§­ Prescreen univers: {len(universe)} tickers")
        selected, shortlist_df, meta = build_deep_analysis_list(
            cfg=cfg,
            universe=universe,
            preselect_top=args.preselect_top,
            max_deep=args.max_deep,
            use_budget_gate=not args.no_budget_gate,
        )

        if not shortlist_df.empty:
            shortlist_df.to_csv("data/universe_shortlist_latest.csv", index=False)
            print("ðŸ’¾ Export shortlist: data/universe_shortlist_latest.csv")

        print(f"   Requested={meta.get('requested', args.max_deep)} "
              f"| Allowed={meta.get('allowed', len(selected))} "
              f"| BudgetCap={meta.get('budget_cap', 'n/a')}")

        if selected:
            cfg.tickers = selected
            print(f"   Deep analysis sur: {', '.join(cfg.tickers)}")
        else:
            print("âš ï¸  Aucune action retenue")
            if args.screen_only:
                return
            sys.exit(0)

        if args.screen_only:
            print("âœ… Prescreen termine (--screen-only)")
            return

    if args.backtest:
        if not MODULES.get("phase1"):
            print("❌ Phase 1 requise pour le backtest")
            sys.exit(1)
        bt = CombinedBacktester(cfg)
        bt.run(cfg.tickers)

    elif args.watch:
        engine = ScoringEngine(cfg)
        dynamic_universe = bool(args.universe_file and MODULES.get("screener"))
        print(f"\n  Mode surveillance — refresh {cfg.watch_interval_min}min")
        if cfg.us_market_only:
            print("  Session US only: actif (09:30-16:00 ET, feries US standards inclus)")
        if dynamic_universe:
            print("  Universe dynamique actif: prescreen relance a chaque cycle")
        print(f"  Ctrl+C pour arrêter\n")
        while True:
            try:
                market_clock = get_us_market_clock()
                if cfg.us_market_only and not market_clock.get("is_open"):
                    wait_s = max(30, int(market_clock.get("seconds_to_open") or (cfg.watch_interval_min * 60)))
                    os.system("cls" if os.name == "nt" else "clear")
                    print("\n" + "=" * 70)
                    print("  BUBO — UNIFIED DASHBOARD")
                    print(f"  NY time: {market_clock.get('time_et')}")
                    print("=" * 70)
                    print("  Marche US ferme: cycles en pause")
                    holiday_hint = market_clock.get("holiday_name", "")
                    if holiday_hint:
                        print(f"  Motif fermeture: {holiday_hint}")
                    print(f"  Reouverture: {market_clock.get('next_open_et')} (dans {format_duration_compact(wait_s)})")
                    print("  Heures regulieres: 09:30-16:00 ET (feries US standards inclus)")
                    time.sleep(wait_s)
                    continue

                dynamic_info = ""
                if dynamic_universe:
                    universe = load_universe(args.universe_file)
                    selected, shortlist_df, meta = build_deep_analysis_list(
                        cfg=cfg,
                        universe=universe,
                        preselect_top=args.preselect_top,
                        max_deep=args.max_deep,
                        use_budget_gate=not args.no_budget_gate,
                    )
                    if not shortlist_df.empty:
                        shortlist_df.to_csv("data/universe_shortlist_latest.csv", index=False)

                    if selected:
                        engine.set_tickers(selected)
                    else:
                        engine.set_tickers([])

                    dynamic_info = (
                        f"Universe={len(universe)} | Deep={len(engine.cfg.tickers)} "
                        f"| BudgetCap={meta.get('budget_cap', 'n/a')}"
                    )

                os.system("cls" if os.name == "nt" else "clear")
                cycle_results = {}
                if engine.cfg.tickers:
                    cycle_results = display_dashboard(engine, engine.cfg.tickers)
                else:
                    print("\n" + "=" * 70)
                    print("  BUBO — UNIFIED DASHBOARD")
                    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
                    print("=" * 70)
                    print("  Aucune action retenue pour ce cycle")

                if args.paper:
                    summary = run_paper_cycle(engine, cycle_results, paper_state_path)
                    print_paper_summary(summary)
                    if args.paper_webhook:
                        sent, reason = notify_paper_webhook(args.paper_webhook, summary, watch_mode=True)
                        if sent:
                            print("   Webhook: alerte envoyee")
                        elif reason not in ("no actions", "webhook not set"):
                            print(f"   Webhook: echec ({reason})")

                if dynamic_info:
                    print(f"\n  {dynamic_info}")
                wait_s = cfg.watch_interval_min * 60
                if cfg.us_market_only:
                    clock_after = get_us_market_clock()
                    to_close = clock_after.get("seconds_to_close")
                    if isinstance(to_close, int):
                        wait_s = max(5, min(wait_s, to_close))
                print(f"\n  Prochain refresh: {format_duration_compact(wait_s)}")
                time.sleep(wait_s)
            except KeyboardInterrupt:
                print("\n  👋 Arrêté.")
                break
            except Exception as e:
                print(f"\n  ⚠️ Erreur cycle watch: {e}")
                time.sleep(min(60, cfg.watch_interval_min * 60))

    else:
        engine = ScoringEngine(cfg)
        results = display_dashboard(engine, cfg.tickers)
        if args.paper:
            summary = run_paper_cycle(engine, results, paper_state_path)
            print_paper_summary(summary)
            if args.paper_webhook:
                sent, reason = notify_paper_webhook(args.paper_webhook, summary, watch_mode=False)
                if sent:
                    print("   Webhook: alerte envoyee")
                elif reason not in ("no actions", "webhook not set"):
                    print(f"   Webhook: echec ({reason})")


if __name__ == "__main__":
    main()
