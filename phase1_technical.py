"""
Trading Bot - Phase 1: Market Data + Technical Analysis
========================================================
Stack: yfinance, pandas, numpy, ta-lib alternative (pandas-ta)
GPU: Calculs vectorisés via numpy/cupy (optionnel)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import pandas_ta as ta
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import warnings
warnings.filterwarnings('ignore')


# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

@dataclass
class TradingConfig:
    # Univers d'assets à surveiller
    watchlist: list = field(default_factory=lambda: [
        # CAC 40 - Defense/Aero (pertinent avec la géopolitique)
        "AM.PA",    # Dassault Aviation
        "AIR.PA",   # Airbus
        "HO.PA",    # Thales
        # US Defense
        "LMT",      # Lockheed Martin
        "RTX",      # Raytheon
        "NOC",      # Northrop Grumman
        # Indices
        "^FCHI",    # CAC 40
        "^GSPC",    # S&P 500
    ])

    # Paramètres techniques — optimisés via grid search (Sharpe 1.772)
    rsi_period: int = 10
    rsi_overbought: float = 65.0
    rsi_oversold: float = 30.0

    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9

    bb_period: int = 25
    bb_std: float = 2.0

    volume_ma_period: int = 20
    volume_spike_threshold: float = 2.5  # x fois la moyenne = anomalie

    # Score minimum pour déclencher un signal
    signal_threshold: int = 2

    # Backtest
    initial_capital: float = 10_000.0
    position_size_pct: float = 0.10    # 10% du capital par trade
    stop_loss_pct: float = 0.02        # Stop loss 2%
    take_profit_pct: float = 0.10      # Take profit 10% (ratio 1:5)
    trade_fee_bps: float = 5.0      # Commission par ordre (bps)
    slippage_bps: float = 5.0       # Slippage par execution (bps)


# ─────────────────────────────────────────────
# DATA FETCHER
# ─────────────────────────────────────────────

class MarketDataFetcher:
    """
    Récupère les données OHLCV depuis yfinance.
    Supporte plusieurs timeframes pour le moyen terme.
    """

    TIMEFRAMES = {
        "1h":  {"period": "60d",  "interval": "1h"},
        "4h":  {"period": "60d",  "interval": "1h"},   # agrégé depuis 1h
        "1d":  {"period": "2y",   "interval": "1d"},
        "1wk": {"period": "5y",   "interval": "1wk"},
    }

    def fetch(self, ticker: str, timeframe: str = "1d") -> Optional[pd.DataFrame]:
        """Télécharge et nettoie les données pour un ticker."""
        try:
            params = self.TIMEFRAMES.get(timeframe, self.TIMEFRAMES["1d"])
            df = yf.download(
                ticker,
                period=params["period"],
                interval=params["interval"],
                progress=False,
                auto_adjust=True,
                threads=False,  # more stable in long-running watch loops
            )

            if df.empty:
                print(f"⚠️  Pas de données pour {ticker}")
                return None

            # Nettoyage
            df = df.dropna()
            df.index = pd.to_datetime(df.index)

            # Aplatir les colonnes multi-index si nécessaire
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Agréger en 4h si demandé
            if timeframe == "4h":
                df = df.resample("4h").agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum"
                }).dropna()

            print(f"✅ {ticker} | {timeframe} | {len(df)} bougies | "
                  f"{df.index[0].date()} → {df.index[-1].date()}")
            return df

        except Exception as e:
            print(f"❌ Erreur fetch {ticker}: {e}")
            return None

    def fetch_multiple(self, tickers: list, timeframe: str = "1d") -> dict:
        """Fetch plusieurs tickers en parallèle."""
        data = {}
        for ticker in tickers:
            df = self.fetch(ticker, timeframe)
            if df is not None:
                data[ticker] = df
        return data


# ─────────────────────────────────────────────
# TECHNICAL ANALYSIS ENGINE
# ─────────────────────────────────────────────

class TechnicalAnalyzer:
    """
    Calcule tous les indicateurs techniques et génère des signaux.
    Utilise pandas-ta (vectorisé, rapide sur CPU/GPU via numpy).
    """

    def __init__(self, config: TradingConfig):
        self.cfg = config

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Ajoute tous les indicateurs au dataframe."""
        df = df.copy()

        # ── RSI ──
        df["rsi"] = ta.rsi(df["Close"], length=self.cfg.rsi_period)

        # ── MACD ──
        macd = ta.macd(
            df["Close"],
            fast=self.cfg.macd_fast,
            slow=self.cfg.macd_slow,
            signal=self.cfg.macd_signal
        )
        # Détection dynamique des colonnes (noms varient selon la version de pandas-ta)
        macd_col  = [c for c in macd.columns if c.startswith("MACD_")][0]
        macds_col = [c for c in macd.columns if c.startswith("MACDs_")][0]
        macdh_col = [c for c in macd.columns if c.startswith("MACDh_")][0]
        df["macd"]        = macd[macd_col]
        df["macd_signal"] = macd[macds_col]
        df["macd_hist"]   = macd[macdh_col]

        # ── Bollinger Bands ──
        bb = ta.bbands(df["Close"], length=self.cfg.bb_period, std=self.cfg.bb_std)
        # pandas-ta génère "2.0" ou "2" selon la version — on détecte dynamiquement
        bb_upper_col = [c for c in bb.columns if c.startswith("BBU_")][0]
        bb_mid_col   = [c for c in bb.columns if c.startswith("BBM_")][0]
        bb_lower_col = [c for c in bb.columns if c.startswith("BBL_")][0]
        bb_pct_col   = [c for c in bb.columns if c.startswith("BBP_")][0]
        df["bb_upper"] = bb[bb_upper_col]
        df["bb_mid"]   = bb[bb_mid_col]
        df["bb_lower"] = bb[bb_lower_col]
        df["bb_pct"]   = bb[bb_pct_col]

        # ── Volume ──
        df["volume_ma"] = ta.sma(df["Volume"], length=self.cfg.volume_ma_period)
        df["volume_ratio"] = df["Volume"] / df["volume_ma"]
        df["volume_spike"] = df["volume_ratio"] > self.cfg.volume_spike_threshold

        # ── Moyennes mobiles ──
        df["sma_20"]  = ta.sma(df["Close"], length=20)
        df["sma_50"]  = ta.sma(df["Close"], length=50)
        df["sma_200"] = ta.sma(df["Close"], length=200)
        df["ema_12"]  = ta.ema(df["Close"], length=12)
        df["ema_26"]  = ta.ema(df["Close"], length=26)

        # ── ATR (volatilité) ──
        df["atr"] = ta.atr(df["High"], df["Low"], df["Close"], length=14)
        df["atr_pct"] = df["atr"] / df["Close"] * 100

        # ── Trend ──
        df["trend_up"] = (df["sma_20"] > df["sma_50"]) & (df["sma_50"] > df["sma_200"])
        df["trend_down"] = (df["sma_20"] < df["sma_50"]) & (df["sma_50"] < df["sma_200"])

        return df.dropna()

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Génère des signaux de trading basés sur les indicateurs.
        signal = 1 (achat), -1 (vente), 0 (neutre)
        Chaque signal a un score de confiance entre 0 et 1.
        """
        df = df.copy()
        df["signal"] = 0
        df["signal_strength"] = 0.0
        df["signal_reasons"] = ""

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev = df.iloc[i - 1]
            score = 0
            reasons = []

            # ── Signaux ACHAT ──

            # RSI oversold
            if row["rsi"] < self.cfg.rsi_oversold:
                score += 1
                reasons.append(f"RSI oversold ({row['rsi']:.1f})")

            # MACD crossover haussier
            if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]:
                score += 2
                reasons.append("MACD bullish crossover")

            # Prix proche bande Bollinger basse
            if row["bb_pct"] < 0.1:
                score += 1
                reasons.append("Prix bande BB basse")

            # Volume spike + bougie verte
            if row["volume_spike"] and row["Close"] > row["Open"]:
                score += 2
                reasons.append(f"Volume spike ({row['volume_ratio']:.1f}x) haussier")

            # Prix au-dessus SMA200 (tendance long terme)
            if row["Close"] > row["sma_200"]:
                score += 1
                reasons.append("Au-dessus SMA200")

            # ── Signaux VENTE ──

            # RSI overbought
            if row["rsi"] > self.cfg.rsi_overbought:
                score -= 1
                reasons.append(f"RSI overbought ({row['rsi']:.1f})")

            # MACD crossover baissier
            if prev["macd"] > prev["macd_signal"] and row["macd"] < row["macd_signal"]:
                score -= 2
                reasons.append("MACD bearish crossover")

            # Prix proche bande Bollinger haute
            if row["bb_pct"] > 0.9:
                score -= 1
                reasons.append("Prix bande BB haute")

            # Volume spike + bougie rouge
            if row["volume_spike"] and row["Close"] < row["Open"]:
                score -= 2
                reasons.append(f"Volume spike ({row['volume_ratio']:.1f}x) baissier")

            # Décision finale
            if score >= self.cfg.signal_threshold:
                df.iloc[i, df.columns.get_loc("signal")] = 1
                df.iloc[i, df.columns.get_loc("signal_strength")] = min(score / 6, 1.0)
                df.iloc[i, df.columns.get_loc("signal_reasons")] = " | ".join(reasons)
            elif score <= -self.cfg.signal_threshold:
                df.iloc[i, df.columns.get_loc("signal")] = -1
                df.iloc[i, df.columns.get_loc("signal_strength")] = min(abs(score) / 6, 1.0)
                df.iloc[i, df.columns.get_loc("signal_reasons")] = " | ".join(reasons)

        return df


# ─────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────

@dataclass
class Trade:
    ticker: str
    entry_date: datetime
    entry_price: float
    direction: int       # 1 = long, -1 = short
    shares: float
    stop_loss: float
    take_profit: float
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    exit_date: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    exit_reason: Optional[str] = None


class Backtester:
    """
    Backtester simple mais rigoureux.
    Simule l'exécution des signaux avec stop-loss et take-profit.
    """

    def __init__(self, config: TradingConfig):
        self.cfg = config

    def run(self, df: pd.DataFrame, ticker: str) -> dict:
        capital = self.cfg.initial_capital
        trades = []
        current_trade = None
        equity_curve = []

        fee_rate = self.cfg.trade_fee_bps / 10_000
        slippage_rate = self.cfg.slippage_bps / 10_000

        # Decisions use previous bar signal; execution happens on current bar open.
        signal_shift = df["signal"].shift(1).fillna(0)

        for i, (date, row) in enumerate(df.iterrows()):
            open_px = row.get("Open", np.nan)
            high_px = row.get("High", np.nan)
            low_px = row.get("Low", np.nan)
            close_px = row.get("Close", np.nan)
            signal_prev = int(signal_shift.iloc[i])

            # Verifier exit trade en cours
            if current_trade is not None:
                exit_price = None
                exit_reason = None

                if current_trade.direction == 1:  # Long
                    if pd.notna(low_px) and low_px <= current_trade.stop_loss:
                        exit_price = current_trade.stop_loss * (1 - slippage_rate)
                        exit_reason = "stop_loss"
                    elif pd.notna(high_px) and high_px >= current_trade.take_profit:
                        exit_price = current_trade.take_profit * (1 - slippage_rate)
                        exit_reason = "take_profit"
                    elif signal_prev == -1 and pd.notna(open_px) and open_px > 0:
                        exit_price = open_px * (1 - slippage_rate)
                        exit_reason = "signal_exit"

                if exit_price is not None:
                    gross_exit = exit_price * current_trade.shares
                    exit_fee = gross_exit * fee_rate
                    capital += gross_exit - exit_fee

                    gross_pnl = (exit_price - current_trade.entry_price) * current_trade.shares
                    pnl = gross_pnl - current_trade.entry_fee - exit_fee

                    current_trade.exit_date = date
                    current_trade.exit_price = exit_price
                    current_trade.pnl = pnl
                    current_trade.exit_fee = exit_fee
                    current_trade.exit_reason = exit_reason
                    trades.append(current_trade)
                    current_trade = None

            # Nouveau signal d'entree
            if current_trade is None and signal_prev == 1 and pd.notna(open_px) and open_px > 0:
                entry_price = open_px * (1 + slippage_rate)
                position_value = capital * self.cfg.position_size_pct
                shares = position_value / entry_price if entry_price > 0 else 0

                if shares > 0:
                    gross_entry = entry_price * shares
                    entry_fee = gross_entry * fee_rate
                    total_debit = gross_entry + entry_fee

                    if total_debit <= capital:
                        stop_loss = entry_price * (1 - self.cfg.stop_loss_pct)
                        take_profit = entry_price * (1 + self.cfg.take_profit_pct)

                        capital -= total_debit
                        current_trade = Trade(
                            ticker=ticker,
                            entry_date=date,
                            entry_price=entry_price,
                            direction=1,
                            shares=shares,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            entry_fee=entry_fee,
                        )

            # Calcul equity (mark-to-market au close)
            portfolio_value = capital
            if current_trade is not None and pd.notna(close_px) and close_px > 0:
                portfolio_value += current_trade.shares * close_px
            equity_curve.append({"date": date, "equity": portfolio_value})

        # Fermer trade ouvert en fin de periode
        if current_trade is not None:
            last_price = df.iloc[-1].get("Close", np.nan)
            if pd.notna(last_price) and last_price > 0:
                exit_price = last_price * (1 - slippage_rate)
                gross_exit = exit_price * current_trade.shares
                exit_fee = gross_exit * fee_rate
                capital += gross_exit - exit_fee

                gross_pnl = (exit_price - current_trade.entry_price) * current_trade.shares
                pnl = gross_pnl - current_trade.entry_fee - exit_fee

                current_trade.exit_date = df.index[-1]
                current_trade.exit_price = exit_price
                current_trade.pnl = pnl
                current_trade.exit_fee = exit_fee
                current_trade.exit_reason = "end_of_period"
                trades.append(current_trade)
                current_trade = None
                if equity_curve:
                    equity_curve[-1]["equity"] = capital

        return self._compute_stats(trades, equity_curve, ticker)

    def _compute_stats(self, trades: list, equity_curve: list, ticker: str) -> dict:
        if not trades:
            return {"ticker": ticker, "error": "Aucun trade généré"}

        equity_df = pd.DataFrame(equity_curve).set_index("date")
        returns = equity_df["equity"].pct_change().dropna()

        winning_trades = [t for t in trades if t.pnl > 0]
        total_pnl = sum(t.pnl for t in trades)

        # Drawdown max
        rolling_max = equity_df["equity"].cummax()
        drawdown = (equity_df["equity"] - rolling_max) / rolling_max
        max_drawdown = drawdown.min()

        # Sharpe ratio (annualisé)
        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0

        stats = {
            "ticker": ticker,
            "nb_trades": len(trades),
            "win_rate": len(winning_trades) / len(trades) * 100,
            "total_pnl": total_pnl,
            "total_return_pct": (total_pnl / self.cfg.initial_capital) * 100,
            "max_drawdown_pct": max_drawdown * 100,
            "sharpe_ratio": sharpe,
            "avg_pnl_per_trade": total_pnl / len(trades),
            "best_trade": max(t.pnl for t in trades),
            "worst_trade": min(t.pnl for t in trades),
            "equity_curve": equity_df,
            "trades": trades
        }
        return stats


# ─────────────────────────────────────────────
# VISUALISATION
# ─────────────────────────────────────────────

class ChartPlotter:
    """Génère des charts complets pour analyser les signaux."""

    def plot_analysis(self, df: pd.DataFrame, stats: dict, ticker: str, save_path: str = None):
        fig = plt.figure(figsize=(16, 12))
        fig.suptitle(f"Analysis: {ticker}", fontsize=14, fontweight='bold')
        gs = gridspec.GridSpec(4, 1, height_ratios=[3, 1, 1, 1], hspace=0.3)

        # ── Panel 1: Prix + BB + Signaux ──
        ax1 = fig.add_subplot(gs[0])
        ax1.plot(df.index, df["Close"], color="#2196F3", linewidth=1.5, label="Close", zorder=3)
        ax1.plot(df.index, df["sma_20"], color="#FF9800", linewidth=1, alpha=0.7, label="SMA20")
        ax1.plot(df.index, df["sma_50"], color="#9C27B0", linewidth=1, alpha=0.7, label="SMA50")
        ax1.plot(df.index, df["sma_200"], color="#F44336", linewidth=1, alpha=0.7, label="SMA200")
        ax1.fill_between(df.index, df["bb_upper"], df["bb_lower"], alpha=0.1, color="#2196F3")
        ax1.plot(df.index, df["bb_upper"], color="#2196F3", linewidth=0.5, alpha=0.5)
        ax1.plot(df.index, df["bb_lower"], color="#2196F3", linewidth=0.5, alpha=0.5)

        # Signaux
        buy_signals = df[df["signal"] == 1]
        sell_signals = df[df["signal"] == -1]
        ax1.scatter(buy_signals.index, buy_signals["Close"], marker="^",
                   color="#4CAF50", s=100, zorder=5, label=f"BUY ({len(buy_signals)})")
        ax1.scatter(sell_signals.index, sell_signals["Close"], marker="v",
                   color="#F44336", s=100, zorder=5, label=f"SELL ({len(sell_signals)})")
        ax1.legend(loc="upper left", fontsize=8)
        ax1.set_ylabel("Prix")
        ax1.grid(True, alpha=0.3)

        # ── Panel 2: Volume ──
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        colors = ["#4CAF50" if c >= o else "#F44336"
                 for c, o in zip(df["Close"], df["Open"])]
        ax2.bar(df.index, df["Volume"], color=colors, alpha=0.7, width=0.8)
        ax2.plot(df.index, df["volume_ma"], color="#FF9800", linewidth=1)
        ax2.set_ylabel("Volume")
        ax2.grid(True, alpha=0.3)

        # ── Panel 3: RSI ──
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        ax3.plot(df.index, df["rsi"], color="#9C27B0", linewidth=1.5)
        ax3.axhline(y=70, color="#F44336", linewidth=0.8, linestyle="--", alpha=0.7)
        ax3.axhline(y=30, color="#4CAF50", linewidth=0.8, linestyle="--", alpha=0.7)
        ax3.axhline(y=50, color="gray", linewidth=0.5, linestyle="--", alpha=0.5)
        ax3.fill_between(df.index, df["rsi"], 70,
                        where=(df["rsi"] > 70), color="#F44336", alpha=0.3)
        ax3.fill_between(df.index, df["rsi"], 30,
                        where=(df["rsi"] < 30), color="#4CAF50", alpha=0.3)
        ax3.set_ylim(0, 100)
        ax3.set_ylabel("RSI")
        ax3.grid(True, alpha=0.3)

        # ── Panel 4: MACD ──
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        ax4.plot(df.index, df["macd"], color="#2196F3", linewidth=1.5, label="MACD")
        ax4.plot(df.index, df["macd_signal"], color="#FF9800", linewidth=1, label="Signal")
        hist_colors = ["#4CAF50" if h >= 0 else "#F44336" for h in df["macd_hist"]]
        ax4.bar(df.index, df["macd_hist"], color=hist_colors, alpha=0.6, width=0.8)
        ax4.axhline(y=0, color="gray", linewidth=0.5)
        ax4.set_ylabel("MACD")
        ax4.legend(loc="upper left", fontsize=8)
        ax4.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"📊 Chart sauvegardé: {save_path}")

        plt.close()

    def plot_equity_curve(self, stats_list: list, save_path: str = None):
        fig, axes = plt.subplots(len(stats_list), 1,
                                figsize=(14, 4 * len(stats_list)))
        if len(stats_list) == 1:
            axes = [axes]

        for ax, stats in zip(axes, stats_list):
            if "error" in stats:
                continue
            ec = stats["equity_curve"]
            ax.plot(ec.index, ec["equity"], color="#2196F3", linewidth=1.5)
            ax.fill_between(ec.index, ec["equity"], ec["equity"].min(),
                           alpha=0.1, color="#2196F3")

            # Titre avec stats clés
            title = (f"{stats['ticker']} | "
                    f"Return: {stats['total_return_pct']:.1f}% | "
                    f"Win rate: {stats['win_rate']:.0f}% | "
                    f"Sharpe: {stats['sharpe_ratio']:.2f} | "
                    f"Max DD: {stats['max_drawdown_pct']:.1f}%")
            ax.set_title(title, fontsize=10)
            ax.set_ylabel("Capital (€)")
            ax.grid(True, alpha=0.3)

        plt.tight_layout()

        if save_path:
            plt.savefig(save_path, dpi=150, bbox_inches="tight")
            print(f"📈 Equity curve sauvegardée: {save_path}")

        plt.close()


# ─────────────────────────────────────────────
# MAIN - PIPELINE COMPLET
# ─────────────────────────────────────────────

def run_phase1(tickers: list = None, timeframe: str = "1d"):
    """
    Lance le pipeline complet Phase 1 sur une liste de tickers.
    """
    config = TradingConfig()
    if tickers:
        config.watchlist = tickers

    fetcher   = MarketDataFetcher()
    analyzer  = TechnicalAnalyzer(config)
    backtester = Backtester(config)
    plotter   = ChartPlotter()

    print("=" * 60)
    print("TRADING BOT - PHASE 1: ANALYSE TECHNIQUE")
    print("=" * 60)

    all_stats = []

    for ticker in config.watchlist:
        print(f"\n{'─'*40}")
        print(f"Processing: {ticker}")

        # 1. Fetch données
        df = fetcher.fetch(ticker, timeframe)
        if df is None:
            continue

        # 2. Calcul indicateurs
        df = analyzer.compute_indicators(df)

        # 3. Génération signaux
        df = analyzer.generate_signals(df)

        # 4. Backtest
        stats = backtester.run(df, ticker)
        all_stats.append(stats)

        # 5. Affichage résultats
        if "error" not in stats:
            print(f"  📊 Trades: {stats['nb_trades']}")
            print(f"  ✅ Win rate: {stats['win_rate']:.1f}%")
            print(f"  💰 PnL total: {stats['total_pnl']:.2f}€ ({stats['total_return_pct']:.1f}%)")
            print(f"  📉 Max drawdown: {stats['max_drawdown_pct']:.1f}%")
            print(f"  📐 Sharpe ratio: {stats['sharpe_ratio']:.2f}")

            # Derniers signaux actifs
            recent_signals = df[df["signal"] != 0].tail(3)
            if not recent_signals.empty:
                print(f"\n  🔔 Derniers signaux:")
                for date, row in recent_signals.iterrows():
                    direction = "BUY 🟢" if row["signal"] == 1 else "SELL 🔴"
                    print(f"     {date.date()} | {direction} | Force: {row['signal_strength']:.0%}")
                    print(f"     Raisons: {row['signal_reasons']}")

        # 6. Charts
        import os; os.makedirs("charts", exist_ok=True)
        chart_path = f"charts/chart_{ticker.replace('.', '_')}.png"
        plotter.plot_analysis(df, stats, ticker, save_path=chart_path)

    # 7. Equity curves
    if all_stats:
        plotter.plot_equity_curve(
            all_stats,
            save_path="charts/equity_curves.png"
        )

    print("\n" + "=" * 60)
    print("Phase 1 terminée ✅")
    print("=" * 60)

    return all_stats


if __name__ == "__main__":
    # Test rapide sur Dassault + un US stock
    results = run_phase1(
        tickers=["AM.PA", "AIR.PA", "LMT", "RTX"],
        timeframe="1d"
    )
