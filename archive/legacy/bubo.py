"""
╔══════════════════════════════════════════════════════════╗
║                    BUBO - Trading Bot                    ║
║              Pipeline Unifié - Phase 3                   ║
╚══════════════════════════════════════════════════════════╝

Architecture:
  Phase 1  → Analyse technique (RSI, MACD, BB, Volume)
  Phase 2a → Calendrier événements (earnings, macro)
  Phase 2b → Sentiment FinBERT (news en temps réel)
  Phase 3  → Agrégation + Score final + Monitoring

Score final [0-100]:
  - Technique  : 40 pts max
  - Sentiment  : 35 pts max
  - Événements : 25 pts max

Usage:
  python bubo.py              → Analyse complète + rapport
  python bubo.py --watch      → Mode surveillance (refresh toutes les Xmin)
  python bubo.py --backtest   → Backtest pipeline complet
"""

import sys
import os
import time
import argparse
import warnings
from datetime import datetime, timedelta, date

import yfinance as yf
import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
os.makedirs("data", exist_ok=True)
os.makedirs("charts", exist_ok=True)

# ── Imports des phases précédentes ──
sys.path.insert(0, ".")
from phase1_technical   import TradingConfig, MarketDataFetcher, TechnicalAnalyzer, Backtester
from phase2a_events     import EventCalendar, EventFilter, backtest_with_events
from phase3b_social import SocialPipeline, get_social_score
from phase2b_sentiment  import FinBERTAnalyzer, NewsFetcher, SentimentEngine, annotate_with_sentiment


# ─────────────────────────────────────────────
# CONFIG GLOBALE
# ─────────────────────────────────────────────

WATCHLIST = {
    # ticker : nom affiché
    "AM.PA":  "Dassault Aviation",
    "AIR.PA": "Airbus",
    "LMT":    "Lockheed Martin",
    "RTX":    "Raytheon",
}

# Poids des composantes dans le score final
SCORE_WEIGHTS = {
    "technical":  0.40,
    "sentiment":  0.35,
    "events":     0.25,
}

# Seuils de décision
BUY_THRESHOLD  = 60   # score > 60 → BUY
SELL_THRESHOLD = 40   # score < 40 → SELL

# Refresh en mode --watch (minutes)
WATCH_INTERVAL_MIN = 15


# ─────────────────────────────────────────────
# SCORE AGRÉGATEUR
# ─────────────────────────────────────────────

class ScoreAggregator:
    """
    Agrège les signaux des 3 phases en un score final [0-100].

    Technical score [0-100]:
      Basé sur le signal_strength de Phase 1 + position des indicateurs.

    Sentiment score [0-100]:
      Basé sur le sentiment_score FinBERT normalisé.

    Event score [0-100]:
      Basé sur le event_modifier et le profil du ticker.
    """

    def compute_technical_score(self, row: pd.Series) -> float:
        """Convertit les indicateurs techniques en score [0-100]."""
        score = 50.0  # neutre par défaut

        if "signal" not in row:
            return score

        signal   = row.get("signal", 0)
        strength = row.get("signal_strength", 0)

        if signal == 1:
            score = 50 + strength * 50
        elif signal == -1:
            score = 50 - strength * 50
        else:
            # Signal neutre — affiner avec les indicateurs bruts
            points = 0

            rsi = row.get("rsi", 50)
            if rsi < 35:   points += 10
            elif rsi > 65: points -= 10

            bb_pct = row.get("bb_pct", 0.5)
            if bb_pct < 0.2:   points += 8
            elif bb_pct > 0.8: points -= 8

            macd      = row.get("macd", 0)
            macd_sig  = row.get("macd_signal", 0)
            if macd > macd_sig:   points += 7
            elif macd < macd_sig: points -= 7

            vol_ratio = row.get("volume_ratio", 1)
            if vol_ratio > 1.5 and row.get("Close", 0) > row.get("Open", 0):
                points += 5

            score = np.clip(50 + points, 0, 100)

        return round(score, 1)

    def compute_sentiment_score(self, sentiment_score: float,
                                  article_count: int,
                                  avg_confidence: float) -> float:
        """Convertit le score FinBERT en score [0-100]."""
        if article_count == 0:
            return 50.0   # neutre si pas de news

        # sentiment_score est dans [-1, +1], on le ramène à [0, 100]
        raw = (sentiment_score + 1) / 2 * 100

        # Pondérer par la confiance et le nombre d'articles
        confidence_factor = min(avg_confidence / 0.8, 1.0)
        coverage_factor   = min(article_count / 5, 1.0)
        weight = 0.6 + 0.4 * confidence_factor * coverage_factor

        # Interpoler vers le neutre (50) si peu de données
        score = 50 + (raw - 50) * weight

        return round(np.clip(score, 0, 100), 1)

    def compute_event_score(self, event_modifier: float,
                              event_blackout: bool,
                              days_to_earnings: float,
                              ticker_profile: str) -> float:
        """Convertit le contexte événementiel en score [0-100]."""
        if event_blackout:
            return 50.0   # neutre en blackout (pas de boost ni pénalité)

        # Convertir le modifier en score
        # modifier=1.0 → 50, modifier=1.4 → ~70, modifier=0.7 → ~35
        score = 50 * event_modifier

        # Bonus si on approche d'un earnings sur reliable_beater
        if ticker_profile == "reliable_beater" and not pd.isna(days_to_earnings):
            if days_to_earnings <= 3:
                score += 15   # très proche d'un earnings fiable
            elif days_to_earnings <= 7:
                score += 8

        return round(np.clip(score, 0, 100), 1)

    def aggregate(self,
                  tech_score: float,
                  sent_score: float,
                  event_score: float,
                  event_blackout: bool) -> dict:
        """Calcule le score final agrégé et la décision."""
        w = SCORE_WEIGHTS

        final = (
            tech_score  * w["technical"] +
            sent_score  * w["sentiment"] +
            event_score * w["events"]
        )
        final = round(final, 1)

        # Décision
        if event_blackout:
            decision = "HOLD"
            reason   = "Zone blackout macro/earnings"
        elif final >= BUY_THRESHOLD:
            decision = "BUY"
            reason   = f"Score {final:.0f}/100"
        elif final <= SELL_THRESHOLD:
            decision = "SELL"
            reason   = f"Score {final:.0f}/100"
        else:
            decision = "HOLD"
            reason   = f"Score {final:.0f}/100 (zone neutre)"

        return {
            "score_technical": tech_score,
            "score_sentiment": sent_score,
            "score_events":    event_score,
            "score_final":     final,
            "decision":        decision,
            "reason":          reason,
        }


# ─────────────────────────────────────────────
# PIPELINE PRINCIPAL
# ─────────────────────────────────────────────

class BuboPipeline:
    """
    Orchestre les 3 phases et produit une analyse complète par ticker.
    """

    def __init__(self, use_finbert: bool = True):
        self.config     = TradingConfig()
        self.fetcher    = MarketDataFetcher()
        self.analyzer   = TechnicalAnalyzer(self.config)
        self.aggregator = ScoreAggregator()
        self.calendar   = None
        self.event_filter = None
        self.finbert    = None
        self.sent_engine = None
        self.use_finbert = use_finbert
        self._initialized = False

    def initialize(self):
        """Charge tous les modèles et données. À appeler une seule fois."""
        print("🦉 Initialisation de BUBO...")

        # Phase 2a — Calendrier
        print("  📅 Chargement calendrier événements...")
        self.calendar     = EventCalendar(list(WATCHLIST.keys()))
        self.calendar.load_all()
        self.event_filter = EventFilter(self.calendar)

        # Phase 2b — FinBERT
        if self.use_finbert:
            print("  🤖 Chargement FinBERT...")
            self.finbert = FinBERTAnalyzer()
            finbert_ok   = self.finbert.load()
            if finbert_ok:
                news_fetcher     = NewsFetcher()
                self.sent_engine = SentimentEngine(self.finbert, news_fetcher)
            else:
                print("  ⚠️  FinBERT non disponible — sentiment désactivé")
                self.use_finbert = False

        self._initialized = True
        print("  ✅ BUBO prêt\n")

    def analyze_ticker(self, ticker: str) -> dict:
        """Analyse complète d'un ticker. Retourne un dict de résultats."""
        if not self._initialized:
            self.initialize()

        result = {
            "ticker":  ticker,
            "name":    WATCHLIST.get(ticker, ticker),
            "time":    datetime.now(),
            "error":   None,
        }

        try:
            # ── Phase 1: Technique ──
            df = self.fetcher.fetch(ticker, "1d")
            if df is None:
                result["error"] = "Impossible de charger les données"
                return result

            df = self.analyzer.compute_indicators(df)
            df = self.analyzer.generate_signals(df)
            last = df.iloc[-1]

            tech_score = self.aggregator.compute_technical_score(last)

            # ── Phase 2a: Événements ──
            df_annotated = self.event_filter.annotate_dataframe(df, ticker)
            last_ann     = df_annotated.iloc[-1]
            profile      = self.event_filter.get_profile(ticker)

            event_score = self.aggregator.compute_event_score(
                event_modifier   = float(last_ann.get("event_modifier", 1.0)),
                event_blackout   = bool(last_ann.get("event_blackout", False)),
                days_to_earnings = float(last_ann.get("days_to_next_earnings", np.nan)),
                ticker_profile   = profile.profile,
            )

            # ── Phase 2b: Sentiment ──
            sent_score   = 50.0
            sent_daily   = None
            if self.use_finbert and self.sent_engine:
                sent_daily = self.sent_engine.get_current_sentiment(ticker)
                sent_score = self.aggregator.compute_sentiment_score(
                    sentiment_score = sent_daily.sentiment_score,
                    article_count   = sent_daily.article_count,
                    avg_confidence  = sent_daily.avg_confidence,
                )

            # ── Agrégation finale ──
            scores = self.aggregator.aggregate(
                tech_score     = tech_score,
                sent_score     = sent_score,
                event_score    = event_score,
                event_blackout = bool(last_ann.get("event_blackout", False)),
            )

            # ── Données de marché ──
            price        = float(last["Close"])
            prev_close   = float(df.iloc[-2]["Close"])
            price_change = (price - prev_close) / prev_close * 100

            result.update({
                "price":             price,
                "price_change_pct":  price_change,
                "rsi":               float(last.get("rsi", 0)),
                "macd":              float(last.get("macd", 0)),
                "macd_signal_val":   float(last.get("macd_signal", 0)),
                "bb_pct":            float(last.get("bb_pct", 0.5)),
                "volume_ratio":      float(last.get("volume_ratio", 1.0)),
                "tech_signal":       int(last.get("signal", 0)),
                "tech_signal_str":   float(last.get("signal_strength", 0)),
                "tech_reasons":      str(last.get("signal_reasons", "")),
                "event_blackout":    bool(last_ann.get("event_blackout", False)),
                "event_modifier":    float(last_ann.get("event_modifier", 1.0)),
                "event_description": str(last_ann.get("event_description", "")),
                "days_to_earnings":  float(last_ann.get("days_to_next_earnings", np.nan)),
                "ticker_profile":    profile.profile,
                "sent_score_raw":    sent_daily.sentiment_score if sent_daily else 0.0,
                "sent_articles":     sent_daily.article_count if sent_daily else 0,
                "sent_signal":       sent_daily.signal if sent_daily else 0,
                "sent_headlines":    sent_daily.top_headlines if sent_daily else [],
                **scores,
            })

        except Exception as e:
            result["error"] = str(e)
            import traceback
            traceback.print_exc()

        return result

    def analyze_all(self) -> list[dict]:
        """Analyse tous les tickers de la watchlist."""
        results = []
        for ticker in WATCHLIST:
            r = self.analyze_ticker(ticker)
            results.append(r)
        return results


# ─────────────────────────────────────────────
# AFFICHAGE CONSOLE
# ─────────────────────────────────────────────

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def decision_color(decision: str) -> str:
    colors = {"BUY": "\033[92m", "SELL": "\033[91m", "HOLD": "\033[93m"}
    reset  = "\033[0m"
    return f"{colors.get(decision, '')}{decision}{reset}"


def score_bar(score: float, width: int = 20) -> str:
    """Barre de progression colorée pour un score [0-100]."""
    filled = int(score / 100 * width)
    if score >= BUY_THRESHOLD:
        color = "\033[92m"   # vert
    elif score <= SELL_THRESHOLD:
        color = "\033[91m"   # rouge
    else:
        color = "\033[93m"   # jaune
    reset = "\033[0m"
    bar = "█" * filled + "░" * (width - filled)
    return f"{color}{bar}{reset}"


def print_ticker_card(r: dict):
    """Affiche une carte complète pour un ticker."""
    if r.get("error"):
        print(f"  ❌ {r['ticker']}: {r['error']}")
        return

    # Header
    price_icon = "▲" if r["price_change_pct"] >= 0 else "▼"
    price_color = "\033[92m" if r["price_change_pct"] >= 0 else "\033[91m"
    reset = "\033[0m"

    print(f"\n  ┌─ {r['name']} ({r['ticker']}) {'─'*30}")
    print(f"  │  Prix: {r['price']:.2f}  "
          f"{price_color}{price_icon} {r['price_change_pct']:+.2f}%{reset}  "
          f"│  Profil: [{r['ticker_profile']}]")

    # Score final
    print(f"  │")
    print(f"  │  SCORE FINAL:  {r['score_final']:5.1f}/100  "
          f"{score_bar(r['score_final'])}  "
          f"→ {decision_color(r['decision'])}")
    print(f"  │")

    # Décomposition des scores
    print(f"  │  Technique:  {r['score_technical']:5.1f}/100  {score_bar(r['score_technical'], 12)}"
          f"  RSI:{r['rsi']:.0f}  BB:{r['bb_pct']:.0%}  Vol:{r['volume_ratio']:.1f}x")
    print(f"  │  Sentiment:  {r['score_sentiment']:5.1f}/100  {score_bar(r['score_sentiment'], 12)}"
          f"  Score:{r['sent_score_raw']:+.3f}  Articles:{r['sent_articles']}")
    print(f"  │  Événements: {r['score_events']:5.1f}/100  {score_bar(r['score_events'], 12)}"
          f"  Modifier:{r['event_modifier']:.2f}x")

    # Détails technique
    if r["tech_reasons"]:
        print(f"  │")
        print(f"  │  📊 Signaux tech: {r['tech_reasons']}")

    # Détails événements
    if r["event_description"]:
        icon = "🚫" if r["event_blackout"] else "📅"
        print(f"  │  {icon} Événements: {r['event_description']}")
    if not pd.isna(r.get("days_to_earnings", np.nan)):
        print(f"  │  📆 Prochain earnings: J+{int(r['days_to_earnings'])}")

    # Headlines sentiment
    if r["sent_headlines"]:
        print(f"  │")
        print(f"  │  📰 News:")
        for h in r["sent_headlines"][:2]:
            print(f"  │     · {h[:85]}")

    print(f"  └{'─'*55}")


def print_dashboard(results: list[dict]):
    """Affiche le dashboard complet."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "═" * 60)
    print(f"  🦉 BUBO — Trading Dashboard  │  {now}")
    print("═" * 60)

    # Résumé rapide
    decisions = {r["ticker"]: r.get("decision", "?") for r in results if not r.get("error")}
    buys  = [t for t, d in decisions.items() if d == "BUY"]
    sells = [t for t, d in decisions.items() if d == "SELL"]
    holds = [t for t, d in decisions.items() if d == "HOLD"]

    print(f"\n  📈 BUY  ({len(buys)}):  {', '.join(buys) if buys else '—'}")
    print(f"  📉 SELL ({len(sells)}):  {', '.join(sells) if sells else '—'}")
    print(f"  ➡️   HOLD ({len(holds)}):  {', '.join(holds) if holds else '—'}")

    # Cartes détaillées
    print()
    # Trier par score final décroissant
    sorted_results = sorted(
        [r for r in results if not r.get("error")],
        key=lambda r: r.get("score_final", 0),
        reverse=True
    )
    for r in sorted_results:
        print_ticker_card(r)

    # Prochains événements macro
    if results and not results[0].get("error"):
        print("\n  ─── Prochains événements macro ───")
        upcoming = [
            e for e in results[0].get("sent_headlines", [])
        ]
        # Charger depuis le calendrier directement
        print(f"  (voir phase2a_events.py pour le calendrier complet)")

    print("\n" + "═" * 60)


def print_summary_table(results: list[dict]):
    """Tableau résumé compact."""
    print(f"\n{'─'*80}")
    print(f"  {'TICKER':<10} {'PRIX':>8} {'CHG%':>7} {'TECH':>6} {'SENT':>6} "
          f"{'EVENT':>6} {'FINAL':>6} {'DÉCISION':<10}")
    print(f"{'─'*80}")

    for r in sorted(results, key=lambda x: x.get("score_final", 0), reverse=True):
        if r.get("error"):
            continue
        chg_str = f"{r['price_change_pct']:+.2f}%"
        print(f"  {r['ticker']:<10} {r['price']:>8.2f} {chg_str:>7} "
              f"{r['score_technical']:>6.1f} {r['score_sentiment']:>6.1f} "
              f"{r['score_events']:>6.1f} {r['score_final']:>6.1f} "
              f"{r['decision']:<10}")

    print(f"{'─'*80}")


# ─────────────────────────────────────────────
# BACKTEST PIPELINE COMPLET
# ─────────────────────────────────────────────

def run_full_backtest(pipeline: BuboPipeline):
    """
    Backtest du pipeline complet sur données historiques.
    Compare: Phase1 seule vs Phase1+Events vs Phase1+Events+Sentiment
    """
    print("\n" + "═" * 60)
    print("  📊 BACKTEST PIPELINE COMPLET")
    print("═" * 60)

    config    = pipeline.config
    fetcher   = pipeline.fetcher
    analyzer  = pipeline.analyzer
    backtester = Backtester(config)

    results_p1        = []
    results_p1_events = []

    for ticker in WATCHLIST:
        df = fetcher.fetch(ticker, "1d")
        if df is None:
            continue

        df = analyzer.compute_indicators(df)
        df = analyzer.generate_signals(df)

        # Phase 1 seule
        stats_p1 = backtester.run(df, ticker)
        results_p1.append(stats_p1)

        # Phase 1 + Events
        df_ann = pipeline.event_filter.annotate_dataframe(df, ticker)
        stats_events = backtest_with_events(df_ann, pipeline.event_filter, ticker, config)
        results_p1_events.append(stats_events)

    # Affichage comparatif
    print(f"\n  {'TICKER':<10} {'P1 Sharpe':>10} {'P1 PnL':>10} "
          f"{'P1+E Sharpe':>12} {'P1+E PnL':>10}")
    print(f"  {'─'*58}")

    total_p1     = 0
    total_events = 0

    for s1, se in zip(results_p1, results_p1_events):
        if "error" in s1 or "error" in se:
            continue
        ticker = s1["ticker"]
        print(f"  {ticker:<10} "
              f"{s1['sharpe_ratio']:>10.2f} "
              f"{s1['total_pnl']:>9.2f}€ "
              f"{se['sharpe_ratio']:>12.2f} "
              f"{se['total_pnl']:>9.2f}€")
        total_p1     += s1["total_pnl"]
        total_events += se["total_pnl"]

    print(f"  {'─'*58}")
    print(f"  {'TOTAL':<10} {'':>10} {total_p1:>9.2f}€ {'':>12} {total_events:>9.2f}€")
    print(f"\n  Gain du filtre événements: {total_events - total_p1:+.2f}€")

    # Sauvegarder
    df_p1 = pd.DataFrame([
        {"ticker": r["ticker"], "sharpe": r["sharpe_ratio"],
         "pnl": r["total_pnl"], "win_rate": r["win_rate"],
         "phase": "Phase1"}
        for r in results_p1 if "error" not in r
    ])
    df_ev = pd.DataFrame([
        {"ticker": r["ticker"], "sharpe": r["sharpe_ratio"],
         "pnl": r["total_pnl"], "win_rate": r["win_rate"],
         "phase": "Phase1+Events"}
        for r in results_p1_events if "error" not in r
    ])

    pd.concat([df_p1, df_ev]).to_csv("data/backtest_comparison.csv", index=False)
    print(f"\n  💾 Résultats sauvegardés: data/backtest_comparison.csv")


# ─────────────────────────────────────────────
# EXPORT SIGNAL JOURNALIER
# ─────────────────────────────────────────────

def export_signals(results: list[dict]):
    """Sauvegarde les signaux du jour en CSV."""
    rows = []
    for r in results:
        if r.get("error"):
            continue
        rows.append({
            "date":             datetime.now().date(),
            "time":             datetime.now().strftime("%H:%M"),
            "ticker":           r["ticker"],
            "price":            r["price"],
            "price_change_pct": r["price_change_pct"],
            "decision":         r["decision"],
            "score_final":      r["score_final"],
            "score_technical":  r["score_technical"],
            "score_sentiment":  r["score_sentiment"],
            "score_events":     r["score_events"],
            "tech_signal":      r["tech_signal"],
            "tech_reasons":     r["tech_reasons"],
            "sentiment_score":  r["sent_score_raw"],
            "sentiment_signal": r["sent_signal"],
            "event_modifier":   r["event_modifier"],
            "event_blackout":   r["event_blackout"],
            "ticker_profile":   r["ticker_profile"],
        })

    if not rows:
        return

    df = pd.DataFrame(rows)
    path = f"data/signals_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    df.to_csv(path, index=False)

    # Aussi mettre à jour le fichier "latest"
    df.to_csv("data/signals_latest.csv", index=False)
    print(f"\n  💾 Signaux exportés: {path}")
    return df


# ─────────────────────────────────────────────
# MODE WATCH (surveillance continue)
# ─────────────────────────────────────────────

def run_watch_mode(pipeline: BuboPipeline):
    """Refresh automatique toutes les N minutes."""
    print(f"  👁  Mode surveillance actif (refresh toutes les {WATCH_INTERVAL_MIN} min)")
    print(f"  Ctrl+C pour quitter\n")

    while True:
        try:
            results = pipeline.analyze_all()
            clear_screen()
            print_dashboard(results)
            print_summary_table(results)
            export_signals(results)

            print(f"\n  ⏱  Prochain refresh dans {WATCH_INTERVAL_MIN} min "
                  f"({(datetime.now() + timedelta(minutes=WATCH_INTERVAL_MIN)).strftime('%H:%M')})")
            time.sleep(WATCH_INTERVAL_MIN * 60)

        except KeyboardInterrupt:
            print("\n\n  👋 BUBO arrêté.")
            break


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="🦉 BUBO — Trading Bot Pipeline Unifié"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=f"Mode surveillance (refresh toutes les {WATCH_INTERVAL_MIN} min)"
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="Lance le backtest comparatif Phase1 vs Phase1+Events"
    )
    parser.add_argument(
        "--no-finbert",
        action="store_true",
        help="Désactive FinBERT (plus rapide, sans sentiment)"
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Restreindre l'analyse à certains tickers (ex: --tickers RTX LMT)"
    )
    args = parser.parse_args()

    # Filtrer la watchlist si demandé
    if args.tickers:
        for t in args.tickers:
            if t not in WATCHLIST:
                print(f"⚠️  Ticker inconnu: {t}")
        filtered = {t: v for t, v in WATCHLIST.items() if t in args.tickers}
        if filtered:
            WATCHLIST.clear()
            WATCHLIST.update(filtered)

    # Initialiser le pipeline
    use_finbert = not args.no_finbert
    pipeline = BuboPipeline(use_finbert=use_finbert)
    pipeline.initialize()

    if args.backtest:
        run_full_backtest(pipeline)
        return

    if args.watch:
        run_watch_mode(pipeline)
        return

    # Mode par défaut : analyse unique + rapport complet
    print("🔍 Analyse en cours...\n")
    results = pipeline.analyze_all()

    print_dashboard(results)
    print_summary_table(results)
    df_signals = export_signals(results)

    print("\n  📋 RÉCAPITULATIF")
    print(f"  Tickers analysés: {len([r for r in results if not r.get('error')])}")
    print(f"  BUY:  {sum(1 for r in results if r.get('decision') == 'BUY')}")
    print(f"  SELL: {sum(1 for r in results if r.get('decision') == 'SELL')}")
    print(f"  HOLD: {sum(1 for r in results if r.get('decision') == 'HOLD')}")
    print(f"\n  Poids du score: "
          f"Technique {SCORE_WEIGHTS['technical']:.0%} | "
          f"Sentiment {SCORE_WEIGHTS['sentiment']:.0%} | "
          f"Événements {SCORE_WEIGHTS['events']:.0%}")
    print(f"  Seuils: BUY>{BUY_THRESHOLD} | SELL<{SELL_THRESHOLD}")


if __name__ == "__main__":
    main()
