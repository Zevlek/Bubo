"""
Trading Bot - Phase 2a: Calendrier Événements Financiers
=========================================================
Sources:
  - yfinance          → earnings dates, dividendes par ticker
  - investpy / yahoo  → calendrier économique macro (BCE, Fed, NFP...)
  - pandas_market_calendars → jours de marché, fermetures

Stratégies événementielles implémentées:
  1. PRE-EARNINGS DRIFT  — les actions montent souvent dans les 5j avant earnings
  2. POST-EARNINGS PLAY  — on trade la réaction J+1 après la surprise
  3. EVENT BLACKOUT      — on bloque les entrées dans les 2j avant un event binaire
  4. MACRO FILTER        — on filtre les trades pendant les annonces Fed/BCE

Usage:
    python phase2a_events.py
    
    Ou import dans phase1:
    from phase2a_events import EventCalendar, EventFilter
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from typing import Optional
import warnings
import os

warnings.filterwarnings('ignore')
os.makedirs("charts", exist_ok=True)
os.makedirs("data", exist_ok=True)


# ─────────────────────────────────────────────
# STRUCTURES DE DONNÉES
# ─────────────────────────────────────────────

@dataclass
class FinancialEvent:
    ticker: str
    event_type: str          # earnings | dividend | split | macro
    event_date: date
    description: str
    importance: int          # 1=low, 2=medium, 3=high (high = blackout zone)
    expected_value: Optional[float] = None   # EPS estimé, taux attendu...
    actual_value: Optional[float] = None     # EPS réel (rempli après l'event)
    surprise_pct: Optional[float] = None     # (actual - expected) / |expected| * 100


@dataclass
class EventWindow:
    """Fenêtre temporelle autour d'un événement."""
    event: FinancialEvent
    blackout_start: date     # Date début zone de risque (entrées bloquées)
    blackout_end: date       # Date fin zone de risque
    play_date: Optional[date] = None   # Date optimale pour trader la réaction


# ─────────────────────────────────────────────
# CALENDRIER PRINCIPAL
# ─────────────────────────────────────────────

class EventCalendar:
    """
    Agrège tous les événements financiers pour une liste de tickers.
    Sources: yfinance (earnings, dividendes), OpenBB/Yahoo (macro).
    """

    # Événements macro importants à surveiller (Fed, BCE, NFP, CPI...)
    # Ces dates sont approximatives — en prod on les fetche depuis une API
    MACRO_EVENTS_2025_2026 = [
        # Format: (date, description, importance)
        # Fed FOMC meetings 2025
        ("2025-01-29", "Fed FOMC Decision", 3),
        ("2025-03-19", "Fed FOMC Decision", 3),
        ("2025-05-07", "Fed FOMC Decision", 3),
        ("2025-06-18", "Fed FOMC Decision", 3),
        ("2025-07-30", "Fed FOMC Decision", 3),
        ("2025-09-17", "Fed FOMC Decision", 3),
        ("2025-11-05", "Fed FOMC Decision", 3),
        ("2025-12-17", "Fed FOMC Decision", 3),
        # Fed FOMC 2026
        ("2026-01-28", "Fed FOMC Decision", 3),
        ("2026-03-18", "Fed FOMC Decision", 3),
        # BCE meetings 2025
        ("2025-01-30", "BCE Policy Decision", 3),
        ("2025-03-06", "BCE Policy Decision", 3),
        ("2025-04-17", "BCE Policy Decision", 3),
        ("2025-06-05", "BCE Policy Decision", 3),
        ("2025-07-24", "BCE Policy Decision", 3),
        ("2025-09-11", "BCE Policy Decision", 3),
        ("2025-10-30", "BCE Policy Decision", 3),
        ("2025-12-18", "BCE Policy Decision", 3),
        # BCE 2026
        ("2026-01-30", "BCE Policy Decision", 3),
        ("2026-03-05", "BCE Policy Decision", 3),
        # US NFP (Non-Farm Payrolls) — 1er vendredi du mois
        ("2025-01-10", "US NFP Jobs Report", 2),
        ("2025-02-07", "US NFP Jobs Report", 2),
        ("2025-03-07", "US NFP Jobs Report", 2),
        ("2025-04-04", "US NFP Jobs Report", 2),
        ("2025-05-02", "US NFP Jobs Report", 2),
        ("2025-06-06", "US NFP Jobs Report", 2),
        ("2025-07-03", "US NFP Jobs Report", 2),
        ("2025-08-01", "US NFP Jobs Report", 2),
        ("2025-09-05", "US NFP Jobs Report", 2),
        ("2025-10-03", "US NFP Jobs Report", 2),
        ("2025-11-07", "US NFP Jobs Report", 2),
        ("2025-12-05", "US NFP Jobs Report", 2),
        ("2026-01-09", "US NFP Jobs Report", 2),
        ("2026-02-06", "US NFP Jobs Report", 2),
        ("2026-03-06", "US NFP Jobs Report", 2),
        # US CPI — mi-mois environ
        ("2025-01-15", "US CPI Inflation", 2),
        ("2025-02-12", "US CPI Inflation", 2),
        ("2025-03-12", "US CPI Inflation", 2),
        ("2025-04-10", "US CPI Inflation", 2),
        ("2025-05-13", "US CPI Inflation", 2),
        ("2025-06-11", "US CPI Inflation", 2),
        ("2025-07-15", "US CPI Inflation", 2),
        ("2025-08-12", "US CPI Inflation", 2),
        ("2025-09-10", "US CPI Inflation", 2),
        ("2025-10-15", "US CPI Inflation", 2),
        ("2025-11-12", "US CPI Inflation", 2),
        ("2025-12-10", "US CPI Inflation", 2),
        ("2026-01-14", "US CPI Inflation", 2),
        ("2026-02-11", "US CPI Inflation", 2),
        ("2026-03-11", "US CPI Inflation", 2),
    ]

    def __init__(self, tickers: list):
        self.tickers = tickers
        self.events: list[FinancialEvent] = []
        self._loaded = False

    def load_all(self) -> list[FinancialEvent]:
        """Charge tous les événements depuis toutes les sources."""
        print("📅 Chargement du calendrier événements...")
        self.events = []

        # 1. Earnings & dividendes depuis yfinance
        for ticker in self.tickers:
            self._load_ticker_events(ticker)

        # 2. Événements macro
        self._load_macro_events()

        # Trier par date
        self.events.sort(key=lambda e: e.event_date)
        self._loaded = True

        print(f"  ✅ {len(self.events)} événements chargés")
        return self.events

    def _load_ticker_events(self, ticker: str):
        """Charge earnings et dividendes pour un ticker via yfinance."""
        try:
            stock = yf.Ticker(ticker)

            # ── Earnings ──
            try:
                cal = stock.calendar
                if cal is not None and not cal.empty:
                    # Earnings Date peut être une liste ou une valeur unique
                    earnings_dates = cal.get("Earnings Date", [])
                    if hasattr(earnings_dates, '__iter__') and not isinstance(earnings_dates, str):
                        for ed in earnings_dates:
                            if pd.notna(ed):
                                event_date = pd.Timestamp(ed).date()
                                self.events.append(FinancialEvent(
                                    ticker=ticker,
                                    event_type="earnings",
                                    event_date=event_date,
                                    description=f"{ticker} Earnings Release",
                                    importance=3,
                                    expected_value=cal.get("EPS Estimate", [None])[0] if isinstance(cal.get("EPS Estimate"), list) else cal.get("EPS Estimate"),
                                ))
                    elif pd.notna(earnings_dates):
                        event_date = pd.Timestamp(earnings_dates).date()
                        self.events.append(FinancialEvent(
                            ticker=ticker,
                            event_type="earnings",
                            event_date=event_date,
                            description=f"{ticker} Earnings Release",
                            importance=3,
                        ))
            except Exception:
                pass

            # ── Earnings History (pour calculer les surprises passées) ──
            try:
                eh = stock.earnings_history
                if eh is not None and not eh.empty:
                    for idx, row in eh.iterrows():
                        event_date = pd.Timestamp(idx).date() if hasattr(idx, 'date') else idx
                        surprise = None
                        if pd.notna(row.get("epsEstimate")) and pd.notna(row.get("epsActual")):
                            est = row["epsEstimate"]
                            act = row["epsActual"]
                            surprise = ((act - est) / abs(est) * 100) if est != 0 else 0

                        self.events.append(FinancialEvent(
                            ticker=ticker,
                            event_type="earnings",
                            event_date=event_date if isinstance(event_date, date) else datetime.strptime(str(event_date), "%Y-%m-%d").date(),
                            description=f"{ticker} Earnings (historique)",
                            importance=3,
                            expected_value=row.get("epsEstimate"),
                            actual_value=row.get("epsActual"),
                            surprise_pct=surprise,
                        ))
            except Exception:
                pass

            # ── Dividendes ──
            try:
                divs = stock.dividends
                if divs is not None and not divs.empty:
                    # Garder seulement les 8 derniers + prochains
                    recent_divs = divs.tail(8)
                    for div_date, div_amount in recent_divs.items():
                        self.events.append(FinancialEvent(
                            ticker=ticker,
                            event_type="dividend",
                            event_date=pd.Timestamp(div_date).date(),
                            description=f"{ticker} Dividende {div_amount:.3f}",
                            importance=1,
                            actual_value=float(div_amount),
                        ))
            except Exception:
                pass

            print(f"  📋 {ticker}: événements chargés")

        except Exception as e:
            print(f"  ⚠️  {ticker}: {e}")

    def _load_macro_events(self):
        """Charge les événements macro depuis la liste statique."""
        for date_str, description, importance in self.MACRO_EVENTS_2025_2026:
            self.events.append(FinancialEvent(
                ticker="MACRO",
                event_type="macro",
                event_date=datetime.strptime(date_str, "%Y-%m-%d").date(),
                description=description,
                importance=importance,
            ))

    def get_events_for_ticker(self, ticker: str,
                               start: date = None,
                               end: date = None,
                               event_types: list = None) -> list[FinancialEvent]:
        """Filtre les événements par ticker, période et type."""
        events = [e for e in self.events
                 if e.ticker == ticker or e.ticker == "MACRO"]

        if start:
            events = [e for e in events if e.event_date >= start]
        if end:
            events = [e for e in events if e.event_date <= end]
        if event_types:
            events = [e for e in events if e.event_type in event_types]

        return sorted(events, key=lambda e: e.event_date)

    def get_upcoming_events(self, days_ahead: int = 30) -> list[FinancialEvent]:
        """Retourne les événements dans les N prochains jours."""
        today = date.today()
        end = today + timedelta(days=days_ahead)
        return [e for e in self.events
                if today <= e.event_date <= end]

    def get_earnings_surprises(self, ticker: str) -> pd.DataFrame:
        """Retourne l'historique des surprises EPS pour un ticker."""
        earnings = [e for e in self.events
                   if e.ticker == ticker
                   and e.event_type == "earnings"
                   and e.surprise_pct is not None]

        if not earnings:
            return pd.DataFrame()

        return pd.DataFrame([{
            "date": e.event_date,
            "eps_estimate": e.expected_value,
            "eps_actual": e.actual_value,
            "surprise_pct": e.surprise_pct,
        } for e in earnings]).sort_values("date")

    def export_to_dataframe(self) -> pd.DataFrame:
        """Exporte tous les événements en DataFrame."""
        if not self.events:
            return pd.DataFrame()

        return pd.DataFrame([{
            "ticker": e.ticker,
            "type": e.event_type,
            "date": e.event_date,
            "description": e.description,
            "importance": e.importance,
            "eps_estimate": e.expected_value,
            "eps_actual": e.actual_value,
            "surprise_pct": e.surprise_pct,
        } for e in self.events]).sort_values("date")


# ─────────────────────────────────────────────
# FILTRE D'ÉVÉNEMENTS (s'intègre au backtester)
# ─────────────────────────────────────────────

class TickerEarningsProfile:
    """
    Profil adaptatif calculé depuis l'historique des surprises EPS.
    Détermine le comportement du filtre pour chaque ticker.
    """
    def __init__(self, ticker: str, surprises: list[float]):
        self.ticker = ticker
        self.surprises = surprises

        n = len(surprises)
        if n == 0:
            # Pas d'historique → comportement neutre/prudent
            self.beat_rate       = 0.5
            self.avg_surprise    = 0.0
            self.volatility      = 0.0
            self.profile         = "unknown"
        else:
            self.beat_rate    = sum(1 for s in surprises if s > 0) / n
            self.avg_surprise = np.mean(surprises)
            self.volatility   = np.std(surprises)  # dispersion des surprises

            # Classification du profil
            if self.beat_rate >= 0.75 and self.avg_surprise > 5:
                self.profile = "reliable_beater"   # RTX, AIR.PA → booster
            elif self.beat_rate >= 0.5 and self.volatility < 15:
                self.profile = "consistent"         # neutre prudent
            elif self.volatility > 30:
                self.profile = "volatile"           # LMT Q2 -77% → blackout strict
            else:
                self.profile = "neutral"

    @property
    def earnings_blackout_active(self) -> bool:
        """Faut-il bloquer les entrées avant earnings ?"""
        # On bloque seulement les profils incertains ou volatils
        return self.profile in ("volatile", "unknown", "neutral")

    @property
    def pre_earnings_boost(self) -> float:
        """Multiplicateur max de position en fenêtre pre-earnings."""
        boosts = {
            "reliable_beater": 1.40,   # +40% position avant earnings fiables
            "consistent":      1.10,   # +10% léger boost
            "volatile":        0.70,   # -30% on réduit l'exposition
            "unknown":         1.00,   # neutre
            "neutral":         1.00,
        }
        return boosts.get(self.profile, 1.0)

    @property
    def post_earnings_beat_boost(self) -> float:
        """Multiplicateur si surprise positive J+1."""
        if self.profile == "reliable_beater":
            return 1.50   # on entre fort après une bonne surprise
        elif self.profile == "consistent":
            return 1.20
        else:
            return 1.10

    @property
    def post_earnings_miss_penalty(self) -> float:
        """Multiplicateur si surprise négative J+1."""
        if self.profile == "volatile":
            return 0.30   # on coupe drastiquement sur un volatile qui déçoit
        else:
            return 0.50

    def __repr__(self):
        return (f"TickerEarningsProfile({self.ticker} | {self.profile} | "
                f"beat={self.beat_rate:.0%} | avg_surprise={self.avg_surprise:+.1f}% | "
                f"vol={self.volatility:.1f}%)")


class EventFilter:
    """
    Filtre événementiel adaptatif par ticker.

    Comportement différencié selon le profil earnings historique:
      - reliable_beater (RTX, AIR.PA) : blackout désactivé, boost pre/post earnings
      - volatile (LMT)                : blackout strict, réduction d'exposition
      - unknown (AM.PA)               : blackout standard, pas de boost
      - consistent / neutral          : comportement modéré

    Règles macro (Fed, BCE) : toujours appliquées, tous tickers.
    """

    BLACKOUT_DAYS_BEFORE = 2
    DRIFT_DAYS_BEFORE    = 5

    def __init__(self, calendar: EventCalendar):
        self.calendar = calendar
        self._date_index: dict = {}
        self._build_index()
        # Calcul des profils adaptatifs
        self._profiles: dict[str, TickerEarningsProfile] = {}
        self._build_profiles()

    def _build_index(self):
        for event in self.calendar.events:
            key = event.event_date
            if key not in self._date_index:
                self._date_index[key] = []
            self._date_index[key].append(event)

    def _build_profiles(self):
        """Calcule le profil adaptatif pour chaque ticker depuis l'historique."""
        tickers = set(e.ticker for e in self.calendar.events if e.ticker != "MACRO")
        for ticker in tickers:
            surprises = [
                e.surprise_pct for e in self.calendar.events
                if e.ticker == ticker
                and e.event_type == "earnings"
                and e.surprise_pct is not None
            ]
            self._profiles[ticker] = TickerEarningsProfile(ticker, surprises)

    def get_profile(self, ticker: str) -> TickerEarningsProfile:
        """Retourne le profil d'un ticker (crée un profil vide si inconnu)."""
        return self._profiles.get(ticker, TickerEarningsProfile(ticker, []))

    def print_profiles(self):
        """Affiche les profils de tous les tickers."""
        print("\n📊 PROFILS ADAPTATIFS PAR TICKER")
        print("-" * 60)
        profile_labels = {
            "reliable_beater": "🟢 Reliable Beater  (blackout OFF, boost ON)",
            "consistent":      "🔵 Consistent       (blackout OFF, boost léger)",
            "volatile":        "🔴 Volatile         (blackout STRICT, réduction)",
            "unknown":         "⚪ Unknown          (blackout standard)",
            "neutral":         "🟡 Neutral          (comportement modéré)",
        }
        for ticker, profile in sorted(self._profiles.items()):
            label = profile_labels.get(profile.profile, profile.profile)
            print(f"  {ticker:8s} | {label}")
            if profile.surprises:
                print(f"           | Beat rate: {profile.beat_rate:.0%} | "
                      f"Surprise moy: {profile.avg_surprise:+.1f}% | "
                      f"Volatilité: {profile.volatility:.1f}%")
            else:
                print(f"           | Pas d'historique EPS disponible")
            print(f"           | Pre-earnings boost: {profile.pre_earnings_boost:.2f}x | "
                  f"Post-beat: {profile.post_earnings_beat_boost:.2f}x | "
                  f"Post-miss: {profile.post_earnings_miss_penalty:.2f}x")

    def is_blackout(self, ticker: str, check_date: date) -> tuple[bool, str]:
        """
        Blackout adaptatif:
          - reliable_beater : blackout earnings désactivé (on veut trader ces events)
          - autres          : blackout standard avant earnings importance 3
          - macro imp. 3    : blackout pour tous
        """
        profile = self.get_profile(ticker)

        for days_ahead in range(1, self.BLACKOUT_DAYS_BEFORE + 1):
            future_date = check_date + timedelta(days=days_ahead)
            events_on_date = self._date_index.get(future_date, [])
            for event in events_on_date:
                # Blackout macro (Fed, BCE) → toujours actif
                if event.ticker == "MACRO" and event.importance == 3:
                    return True, f"Blackout macro: {event.description} dans {days_ahead}j"
                # Blackout earnings → dépend du profil
                if event.ticker == ticker and event.event_type == "earnings":
                    if profile.earnings_blackout_active:
                        return True, (f"Blackout earnings ({profile.profile}): "
                                     f"{event.description} dans {days_ahead}j")
        return False, ""

    def get_event_score_modifier(self, ticker: str, check_date: date) -> tuple[float, str]:
        """
        Modificateur adaptatif basé sur le profil du ticker.
        """
        profile = self.get_profile(ticker)
        modifier = 1.0
        reasons = []

        # ── Fenêtre pre-earnings ──
        for days_ahead in range(1, self.DRIFT_DAYS_BEFORE + 1):
            future_date = check_date + timedelta(days=days_ahead)
            events_on_date = self._date_index.get(future_date, [])
            for event in events_on_date:
                if event.ticker == ticker and event.event_type == "earnings":
                    # Boost progressif : max au J-1, minimum au J-5
                    proximity = (self.DRIFT_DAYS_BEFORE - days_ahead + 1) / self.DRIFT_DAYS_BEFORE
                    boost = 1.0 + (profile.pre_earnings_boost - 1.0) * proximity
                    modifier = max(modifier, boost)
                    reasons.append(
                        f"Pre-earnings [{profile.profile}] J-{days_ahead} ({boost:.2f}x)"
                    )

        # ── Réaction post-earnings J+1 ──
        yesterday = check_date - timedelta(days=1)
        for event in self._date_index.get(yesterday, []):
            if event.ticker == ticker and event.event_type == "earnings":
                if event.surprise_pct is not None:
                    if event.surprise_pct > 5:
                        boost = profile.post_earnings_beat_boost
                        modifier *= boost
                        reasons.append(
                            f"Post-earnings beat +{event.surprise_pct:.1f}% ({boost:.2f}x)"
                        )
                    elif event.surprise_pct < -5:
                        penalty = profile.post_earnings_miss_penalty
                        modifier *= penalty
                        reasons.append(
                            f"Post-earnings miss {event.surprise_pct:.1f}% ({penalty:.2f}x)"
                        )
                else:
                    # Earnings sans données → boost neutre si reliable_beater
                    if profile.profile == "reliable_beater":
                        modifier *= 1.15
                        reasons.append("Post-earnings [reliable_beater, no data] (1.15x)")

        # ── Macro importance 2 (NFP, CPI) ──
        for event in self._date_index.get(check_date, []):
            if event.ticker == "MACRO" and event.importance == 2:
                modifier *= 0.85
                reasons.append(f"Macro: {event.description} (0.85x)")

        return modifier, " | ".join(reasons)

    def annotate_dataframe(self, df: pd.DataFrame, ticker: str) -> pd.DataFrame:
        """Annote le dataframe avec les colonnes événementielles."""
        df = df.copy()
        df["event_blackout"]        = False
        df["event_modifier"]        = 1.0
        df["event_description"]     = ""
        df["days_to_next_earnings"] = np.nan
        df["ticker_profile"]        = self.get_profile(ticker).profile

        earnings_dates = sorted([
            e.event_date for e in self.calendar.events
            if e.ticker == ticker and e.event_type == "earnings"
        ])

        for idx, row in df.iterrows():
            check_date = idx.date() if hasattr(idx, "date") else idx

            is_bo, bo_reason = self.is_blackout(ticker, check_date)
            df.at[idx, "event_blackout"] = is_bo
            if is_bo:
                df.at[idx, "event_description"] = bo_reason

            modifier, mod_reason = self.get_event_score_modifier(ticker, check_date)
            df.at[idx, "event_modifier"] = modifier
            if mod_reason and not is_bo:
                df.at[idx, "event_description"] = mod_reason

            future_earnings = [d for d in earnings_dates if d > check_date]
            if future_earnings:
                df.at[idx, "days_to_next_earnings"] = (future_earnings[0] - check_date).days

        return df


# ─────────────────────────────────────────────
# BACKTESTER AMÉLIORÉ AVEC ÉVÉNEMENTS
# ─────────────────────────────────────────────

def backtest_with_events(df: pd.DataFrame,
                          event_filter: EventFilter,
                          ticker: str,
                          config) -> dict:
    """
    Backtest qui intÃ¨gre le filtre Ã©vÃ©nementiel.
    ExÃ©cution sans look-ahead: signal(t-1) -> ordre Ã  l'open(t).
    """
    capital = config.initial_capital
    trades = []
    current_trade = None
    equity_curve = []
    blocked_trades = 0

    fee_rate = getattr(config, "trade_fee_bps", 0.0) / 10_000
    slippage_rate = getattr(config, "slippage_bps", 0.0) / 10_000

    signal_shift = df["signal"].shift(1).fillna(0)
    blackout_shift = df.get("event_blackout", pd.Series(False, index=df.index)).shift(1).fillna(False)
    modifier_shift = df.get("event_modifier", pd.Series(1.0, index=df.index)).shift(1).fillna(1.0)
    context_shift = df.get("event_description", pd.Series("", index=df.index)).shift(1).fillna("")

    for i, (date_idx, row) in enumerate(df.iterrows()):
        open_px = row.get("Open", np.nan)
        high_px = row.get("High", np.nan)
        low_px = row.get("Low", np.nan)
        close_px = row.get("Close", np.nan)

        signal_prev = int(signal_shift.iloc[i])
        was_blackout = bool(blackout_shift.iloc[i])
        modifier_prev = modifier_shift.iloc[i]
        modifier_prev = float(modifier_prev) if pd.notna(modifier_prev) else 1.0
        context_prev = context_shift.iloc[i]
        context_prev = str(context_prev) if pd.notna(context_prev) else ""

        # Exit
        if current_trade is not None:
            exit_price = None
            exit_reason = None

            if pd.notna(low_px) and low_px <= current_trade["stop_loss"]:
                exit_price = current_trade["stop_loss"] * (1 - slippage_rate)
                exit_reason = "stop_loss"
            elif pd.notna(high_px) and high_px >= current_trade["take_profit"]:
                exit_price = current_trade["take_profit"] * (1 - slippage_rate)
                exit_reason = "take_profit"
            elif signal_prev == -1 and pd.notna(open_px) and open_px > 0:
                exit_price = open_px * (1 - slippage_rate)
                exit_reason = "signal_exit"

            if exit_price is not None:
                gross_exit = current_trade["shares"] * exit_price
                exit_fee = gross_exit * fee_rate
                capital += gross_exit - exit_fee

                gross_pnl = (exit_price - current_trade["entry_price"]) * current_trade["shares"]
                pnl = gross_pnl - current_trade.get("entry_fee", 0.0) - exit_fee

                trades.append({
                    **current_trade,
                    "exit_price": exit_price,
                    "exit_reason": exit_reason,
                    "exit_fee": exit_fee,
                    "pnl": pnl,
                    "exit_date": date_idx,
                })
                current_trade = None

        # Entry avec filtre Ã©vÃ©nementiel
        if current_trade is None and signal_prev == 1:
            if was_blackout:
                blocked_trades += 1
            else:
                modifier_prev = max(0.0, modifier_prev)
                adjusted_size = min(config.position_size_pct * modifier_prev, 0.20)  # cap 20%

                if adjusted_size > 0 and pd.notna(open_px) and open_px > 0:
                    entry_price = open_px * (1 + slippage_rate)
                    position_value = capital * adjusted_size
                    shares = position_value / entry_price if entry_price > 0 else 0

                    if shares > 0:
                        gross_entry = shares * entry_price
                        entry_fee = gross_entry * fee_rate
                        total_debit = gross_entry + entry_fee

                        if total_debit <= capital:
                            capital -= total_debit
                            current_trade = {
                                "ticker": ticker,
                                "entry_date": date_idx,
                                "entry_price": entry_price,
                                "shares": shares,
                                "stop_loss": entry_price * (1 - config.stop_loss_pct),
                                "take_profit": entry_price * (1 + config.take_profit_pct),
                                "event_context": context_prev,
                                "event_modifier": modifier_prev,
                                "entry_fee": entry_fee,
                            }

        pv = capital
        if current_trade is not None and pd.notna(close_px) and close_px > 0:
            pv += current_trade["shares"] * close_px
        equity_curve.append({"date": date_idx, "equity": pv})

    # Clore trade ouvert
    if current_trade is not None:
        last = df.iloc[-1].get("Close", np.nan)
        if pd.notna(last) and last > 0:
            exit_price = last * (1 - slippage_rate)
            gross_exit = current_trade["shares"] * exit_price
            exit_fee = gross_exit * fee_rate
            capital += gross_exit - exit_fee

            gross_pnl = (exit_price - current_trade["entry_price"]) * current_trade["shares"]
            pnl = gross_pnl - current_trade.get("entry_fee", 0.0) - exit_fee

            trades.append({
                **current_trade,
                "exit_price": exit_price,
                "exit_reason": "end_of_period",
                "exit_fee": exit_fee,
                "pnl": pnl,
                "exit_date": df.index[-1],
            })
            current_trade = None
            if equity_curve:
                equity_curve[-1]["equity"] = capital

    if not trades:
        return {"ticker": ticker, "error": "Aucun trade", "blocked": blocked_trades}

    equity_arr = pd.DataFrame(equity_curve).set_index("date")["equity"]
    returns = equity_arr.pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

    rolling_max = equity_arr.cummax()
    max_dd = ((equity_arr - rolling_max) / rolling_max).min() * 100

    wins = [t for t in trades if t["pnl"] > 0]
    total_pnl = sum(t["pnl"] for t in trades)

    return {
        "ticker": ticker,
        "nb_trades": len(trades),
        "blocked_trades": blocked_trades,
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total_pnl,
        "total_return_pct": total_pnl / config.initial_capital * 100,
        "max_drawdown_pct": max_dd,
        "sharpe_ratio": sharpe,
        "equity_curve": equity_arr,
        "trades": trades,
    }

def print_upcoming_events(calendar: EventCalendar, days: int = 30):
    """Affiche les événements à venir."""
    events = calendar.get_upcoming_events(days_ahead=days)
    today = date.today()

    print(f"\n{'='*60}")
    print(f"📅 ÉVÉNEMENTS À VENIR (30 jours)")
    print(f"{'='*60}")

    if not events:
        print("  Aucun événement trouvé")
        return

    importance_icons = {1: "🔵", 2: "🟡", 3: "🔴"}

    for event in events:
        days_until = (event.event_date - today).days
        icon = importance_icons.get(event.importance, "⚪")
        ticker_str = f"[{event.ticker}]" if event.ticker != "MACRO" else "[MACRO]"
        print(f"  {icon} {event.event_date} (J+{days_until:2d}) {ticker_str:10s} {event.description}")
        if event.expected_value is not None:
            print(f"             EPS estimé: {event.expected_value:.3f}")


def print_earnings_surprises(calendar: EventCalendar, tickers: list):
    """Affiche l'historique des surprises earnings."""
    print(f"\n{'='*60}")
    print(f"📊 HISTORIQUE SURPRISES EARNINGS")
    print(f"{'='*60}")

    for ticker in tickers:
        df = calendar.get_earnings_surprises(ticker)
        if df.empty:
            print(f"\n  {ticker}: pas d'historique disponible")
            continue

        print(f"\n  {ticker}:")
        avg_surprise = df["surprise_pct"].mean()
        beat_rate = (df["surprise_pct"] > 0).mean() * 100
        print(f"  Surprise moyenne: {avg_surprise:+.1f}% | Beat rate: {beat_rate:.0f}%")

        for _, row in df.tail(6).iterrows():
            icon = "✅" if row["surprise_pct"] > 0 else "❌"
            print(f"    {icon} {row['date']} | EPS estimé: {row['eps_estimate']:.3f} | "
                  f"Réel: {row['eps_actual']:.3f} | Surprise: {row['surprise_pct']:+.1f}%")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    # Import de la Phase 1
    import sys
    sys.path.insert(0, ".")
    from phase1_technical import TradingConfig, MarketDataFetcher, TechnicalAnalyzer

    TICKERS = ["AM.PA", "AIR.PA", "LMT", "RTX"]
    config = TradingConfig()

    print("=" * 60)
    print("TRADING BOT - PHASE 2a: CALENDRIER ÉVÉNEMENTS")
    print("=" * 60)

    # 1. Charger le calendrier
    calendar = EventCalendar(TICKERS)
    calendar.load_all()

    # 2. Afficher événements à venir
    print_upcoming_events(calendar, days=30)

    # 3. Afficher historique surprises
    print_earnings_surprises(calendar, TICKERS)

    # 4. Sauvegarder le calendrier complet en CSV
    df_events = calendar.export_to_dataframe()
    df_events.to_csv("data/events_calendar.csv", index=False)
    print(f"\n💾 Calendrier sauvegardé: data/events_calendar.csv ({len(df_events)} events)")

    # 5. Backtest comparatif Phase1 vs Phase1+Events
    print(f"\n{'='*60}")
    print("📊 BACKTEST COMPARATIF: Sans vs Avec filtre événements")
    print(f"{'='*60}")

    fetcher  = MarketDataFetcher()
    analyzer = TechnicalAnalyzer(config)
    event_filter = EventFilter(calendar)

    # Afficher les profils adaptatifs calculés
    event_filter.print_profiles()

    results_comparison = []

    for ticker in TICKERS:
        df = fetcher.fetch(ticker, "1d")
        if df is None:
            continue

        df = analyzer.compute_indicators(df)
        df = analyzer.generate_signals(df)

        # Annoter avec les événements
        df_annotated = event_filter.annotate_dataframe(df, ticker)

        # Backtest avec événements
        stats = backtest_with_events(df_annotated, event_filter, ticker, config)

        if "error" not in stats:
            profile = event_filter.get_profile(ticker)
            print(f"\n  {ticker} [{profile.profile}]:")
            print(f"  Trades exécutés: {stats['nb_trades']} | "
                  f"Bloqués (blackout): {stats['blocked_trades']}")
            print(f"  Win rate: {stats['win_rate']:.1f}% | "
                  f"PnL: {stats['total_pnl']:.2f}€ ({stats['total_return_pct']:.1f}%)")
            print(f"  Sharpe: {stats['sharpe_ratio']:.2f} | "
                  f"Max DD: {stats['max_drawdown_pct']:.2f}%")
            results_comparison.append(stats)

            # Trades avec contexte événementiel
            event_trades = [t for t in stats["trades"] if t.get("event_context")]
            if event_trades:
                print(f"\n  🎯 Trades avec contexte événementiel ({len(event_trades)}):")
                for t in event_trades[:4]:
                    pnl_icon = "✅" if t["pnl"] > 0 else "❌"
                    mod = t.get("event_modifier", 1.0)
                    print(f"    {pnl_icon} {t['entry_date'].date()} | "
                          f"PnL: {t['pnl']:+.2f}€ | modifier: {mod:.2f}x | {t['event_context']}")

    # Résumé global
    if results_comparison:
        total_pnl = sum(r["total_pnl"] for r in results_comparison)
        avg_sharpe = np.mean([r["sharpe_ratio"] for r in results_comparison])
        avg_winrate = np.mean([r["win_rate"] for r in results_comparison])
        print(f"\n{'─'*60}")
        print(f"  TOTAL PnL (4 tickers): {total_pnl:.2f}€")
        print(f"  Sharpe moyen:          {avg_sharpe:.2f}")
        print(f"  Win rate moyen:        {avg_winrate:.1f}%")

    print(f"\n{'='*60}")
    print("Phase 2a terminée ✅")
    print("Pour utiliser dans Phase 1, importez:")
    print("  from phase2a_events import EventCalendar, EventFilter")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
