"""
Trading Bot - Phase 1: Grid Search Optimizer
=============================================
Teste toutes les combinaisons de paramètres et trouve les meilleurs
selon le Sharpe ratio, win rate et drawdown.

Usage:
    python phase1_optimizer.py

Résultats sauvegardés dans:
    charts/optimizer_heatmap.png
    charts/optimizer_results.csv
    charts/best_params_backtest.png
"""

import yfinance as yf
import pandas as pd
import numpy as np
import pandas_ta as ta
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import itertools
import warnings
from dataclasses import dataclass, field
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

warnings.filterwarnings('ignore')
os.makedirs("charts", exist_ok=True)


# ─────────────────────────────────────────────
# GRILLE DE PARAMÈTRES À TESTER
# ─────────────────────────────────────────────

PARAM_GRID = {
    "rsi_period":            [10, 14, 21],
    "rsi_oversold":          [25, 30, 35],
    "rsi_overbought":        [65, 70, 75],
    "bb_period":             [15, 20, 25],
    "bb_std":                [1.5, 2.0, 2.5],
    "volume_spike_threshold":[1.5, 2.0, 2.5],
    "stop_loss_pct":         [0.02, 0.03, 0.05],
    "take_profit_pct":       [0.04, 0.06, 0.10],
    "signal_threshold":      [2, 3, 4],      # score minimum pour déclencher
}

# Tickers utilisés pour l'optimisation (on moyenne les résultats sur tous)
OPTIMIZATION_TICKERS = ["AM.PA", "AIR.PA", "LMT", "RTX"]

# Métrique principale à maximiser
OPTIMIZE_FOR = "sharpe_ratio"   # sharpe_ratio | total_return_pct | win_rate

# Nombre de combinaisons max à tester (random sampling si trop grand)
MAX_COMBINATIONS = 500

# Coûts d'exécution utilisés pendant l'optimisation (réalistes par défaut)
TRADE_FEE_BPS = 5.0
SLIPPAGE_BPS = 5.0


# ─────────────────────────────────────────────
# DATA CACHE (fetch une seule fois)
# ─────────────────────────────────────────────

def fetch_all_data(tickers: list) -> dict:
    """Fetch et cache toutes les données brutes."""
    print("📥 Téléchargement des données...")
    data = {}
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="2y", interval="1d",
                           progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()
            data[ticker] = df
            print(f"  ✅ {ticker}: {len(df)} bougies")
        except Exception as e:
            print(f"  ❌ {ticker}: {e}")
    return data


# ─────────────────────────────────────────────
# MOTEUR D'ÉVALUATION (rapide, sans charts)
# ─────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame, p: dict) -> Optional[pd.DataFrame]:
    """Calcule les indicateurs pour un set de paramètres."""
    try:
        df = df.copy()

        df["rsi"] = ta.rsi(df["Close"], length=p["rsi_period"])

        macd = ta.macd(df["Close"], fast=12, slow=26, signal=9)
        macd_col  = [c for c in macd.columns if c.startswith("MACD_")][0]
        macds_col = [c for c in macd.columns if c.startswith("MACDs_")][0]
        df["macd"]        = macd[macd_col]
        df["macd_signal"] = macd[macds_col]

        bb = ta.bbands(df["Close"], length=p["bb_period"], std=p["bb_std"])
        bb_pct_col   = [c for c in bb.columns if c.startswith("BBP_")][0]
        bb_upper_col = [c for c in bb.columns if c.startswith("BBU_")][0]
        bb_lower_col = [c for c in bb.columns if c.startswith("BBL_")][0]
        df["bb_pct"]   = bb[bb_pct_col]
        df["bb_upper"] = bb[bb_upper_col]
        df["bb_lower"] = bb[bb_lower_col]

        df["volume_ma"]    = ta.sma(df["Volume"], length=20)
        df["volume_ratio"] = df["Volume"] / df["volume_ma"]
        df["volume_spike"] = df["volume_ratio"] > p["volume_spike_threshold"]

        df["sma_200"] = ta.sma(df["Close"], length=200)

        return df.dropna()
    except Exception:
        return None


def generate_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Génère les signaux pour un set de paramètres."""
    df = df.copy()
    df["signal"] = 0

    for i in range(1, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i - 1]
        score = 0

        # BUY signals
        if row["rsi"] < p["rsi_oversold"]:                                    score += 1
        if prev["macd"] < prev["macd_signal"] and row["macd"] > row["macd_signal"]: score += 2
        if row["bb_pct"] < 0.1:                                               score += 1
        if row["volume_spike"] and row["Close"] > row["Open"]:                score += 2
        if not pd.isna(row["sma_200"]) and row["Close"] > row["sma_200"]:     score += 1

        # SELL signals
        if row["rsi"] > p["rsi_overbought"]:                                  score -= 1
        if prev["macd"] > prev["macd_signal"] and row["macd"] < row["macd_signal"]: score -= 2
        if row["bb_pct"] > 0.9:                                               score -= 1
        if row["volume_spike"] and row["Close"] < row["Open"]:                score -= 2

        threshold = p["signal_threshold"]
        if score >= threshold:
            df.iloc[i, df.columns.get_loc("signal")] = 1
        elif score <= -threshold:
            df.iloc[i, df.columns.get_loc("signal")] = -1

    return df


def backtest(df: pd.DataFrame, p: dict, initial_capital: float = 10_000.0) -> dict:
    """Backtest rapide sans look-ahead et avec coûts d'exécution."""
    capital = initial_capital
    trades = []
    current_trade = None
    equity = []

    fee_rate = TRADE_FEE_BPS / 10_000
    slippage_rate = SLIPPAGE_BPS / 10_000
    signal_shift = df["signal"].shift(1).fillna(0)

    for i, (_, row) in enumerate(df.iterrows()):
        open_px = row.get("Open", np.nan)
        high_px = row.get("High", np.nan)
        low_px = row.get("Low", np.nan)
        close_px = row.get("Close", np.nan)
        signal_prev = int(signal_shift.iloc[i])

        # Exit
        if current_trade is not None:
            exit_price = None
            if pd.notna(low_px) and low_px <= current_trade["sl"]:
                exit_price = current_trade["sl"] * (1 - slippage_rate)
            elif pd.notna(high_px) and high_px >= current_trade["tp"]:
                exit_price = current_trade["tp"] * (1 - slippage_rate)
            elif signal_prev == -1 and pd.notna(open_px) and open_px > 0:
                exit_price = open_px * (1 - slippage_rate)

            if exit_price is not None and exit_price > 0:
                gross_exit = exit_price * current_trade["shares"]
                exit_fee = gross_exit * fee_rate
                capital += gross_exit - exit_fee

                gross_pnl = (exit_price - current_trade["entry"]) * current_trade["shares"]
                net_pnl = gross_pnl - current_trade["entry_fee"] - exit_fee
                trades.append(net_pnl)
                current_trade = None

        # Entry (signal de la veille, exécution à l'open du jour)
        if current_trade is None and signal_prev == 1 and pd.notna(open_px) and open_px > 0:
            entry_price = open_px * (1 + slippage_rate)
            pos_value = capital * 0.10
            shares = pos_value / entry_price if entry_price > 0 else 0

            if shares > 0:
                gross_entry = entry_price * shares
                entry_fee = gross_entry * fee_rate
                total_debit = gross_entry + entry_fee
                if total_debit <= capital:
                    capital -= total_debit
                    current_trade = {
                        "entry": entry_price,
                        "shares": shares,
                        "entry_fee": entry_fee,
                        "sl": entry_price * (1 - p["stop_loss_pct"]),
                        "tp": entry_price * (1 + p["take_profit_pct"]),
                    }

        pv = capital
        if current_trade is not None and pd.notna(close_px) and close_px > 0:
            pv += current_trade["shares"] * close_px
        equity.append(pv)

    # Clore trade ouvert
    if current_trade is not None:
        last = df.iloc[-1].get("Close", np.nan)
        if pd.notna(last) and last > 0:
            exit_price = last * (1 - slippage_rate)
            gross_exit = exit_price * current_trade["shares"]
            exit_fee = gross_exit * fee_rate
            capital += gross_exit - exit_fee

            gross_pnl = (exit_price - current_trade["entry"]) * current_trade["shares"]
            net_pnl = gross_pnl - current_trade["entry_fee"] - exit_fee
            trades.append(net_pnl)
            current_trade = None

            if equity:
                equity[-1] = capital

    if not trades:
        return {"sharpe_ratio": -999, "win_rate": 0, "total_return_pct": 0,
                "max_drawdown_pct": 0, "nb_trades": 0}

    equity_arr = np.array(equity) if equity else np.array([initial_capital])
    returns = np.diff(equity_arr) / equity_arr[:-1] if len(equity_arr) > 1 else np.array([])
    sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if len(returns) > 1 and returns.std() > 0 else 0

    rolling_max = np.maximum.accumulate(equity_arr)
    drawdown = (equity_arr - rolling_max) / rolling_max
    max_dd = drawdown.min() if len(drawdown) > 0 else 0

    wins = sum(1 for t in trades if t > 0)
    total_pnl = sum(trades)

    return {
        "sharpe_ratio":      round(sharpe, 4),
        "win_rate":          round(wins / len(trades) * 100, 2),
        "total_return_pct":  round(total_pnl / initial_capital * 100, 3),
        "max_drawdown_pct":  round(max_dd * 100, 3),
        "nb_trades":         len(trades),
    }


def evaluate_params(params: dict, cached_data: dict) -> dict:
    """Évalue un set de paramètres sur tous les tickers, retourne la moyenne."""
    all_metrics = []

    for ticker, raw_df in cached_data.items():
        df = compute_indicators(raw_df, params)
        if df is None or len(df) < 50:
            continue
        df = generate_signals(df, params)
        metrics = backtest(df, params)
        if metrics["nb_trades"] >= 2:  # ignorer si trop peu de trades
            all_metrics.append(metrics)

    if not all_metrics:
        return {**params, "sharpe_ratio": -999, "win_rate": 0,
                "total_return_pct": 0, "max_drawdown_pct": 0, "nb_trades": 0}

    # Moyenne des métriques sur tous les tickers
    result = {**params}
    for key in ["sharpe_ratio", "win_rate", "total_return_pct", "max_drawdown_pct", "nb_trades"]:
        result[key] = round(np.mean([m[key] for m in all_metrics]), 4)

    return result


# ─────────────────────────────────────────────
# GRID SEARCH
# ─────────────────────────────────────────────

def run_grid_search(cached_data: dict) -> pd.DataFrame:
    """Lance le grid search complet avec sampling si trop de combinaisons."""

    # Générer toutes les combinaisons
    keys = list(PARAM_GRID.keys())
    all_combos = list(itertools.product(*[PARAM_GRID[k] for k in keys]))
    total = len(all_combos)

    print(f"\n🔍 Combinaisons totales possibles: {total:,}")

    # Random sampling si trop grand
    if total > MAX_COMBINATIONS:
        np.random.seed(42)
        indices = np.random.choice(total, MAX_COMBINATIONS, replace=False)
        combos = [all_combos[i] for i in indices]
        print(f"🎲 Sampling aléatoire: {MAX_COMBINATIONS} combinaisons testées")
    else:
        combos = all_combos
        print(f"✅ Test exhaustif: {total} combinaisons")

    param_list = [dict(zip(keys, combo)) for combo in combos]

    # Évaluation parallèle
    results = []
    completed = 0

    print(f"\n⚙️  Optimisation en cours...\n")

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(evaluate_params, p, cached_data): p for p in param_list}

        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            completed += 1

            if completed % 50 == 0 or completed == len(param_list):
                pct = completed / len(param_list) * 100
                best_so_far = max(results, key=lambda x: x[OPTIMIZE_FOR])
                print(f"  [{pct:5.1f}%] {completed}/{len(param_list)} | "
                      f"Best {OPTIMIZE_FOR}: {best_so_far[OPTIMIZE_FOR]:.3f}")

    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values(OPTIMIZE_FOR, ascending=False)

    return df_results


# ─────────────────────────────────────────────
# VISUALISATION DES RÉSULTATS
# ─────────────────────────────────────────────

def plot_heatmaps(df: pd.DataFrame):
    """Heatmaps des paramètres les plus impactants."""
    fig, axes = plt.subplots(2, 3, figsize=(18, 10))
    fig.suptitle("Grid Search - Impact des paramètres sur le Sharpe Ratio",
                fontsize=14, fontweight='bold')

    param_pairs = [
        ("rsi_oversold",   "rsi_period"),
        ("bb_period",      "bb_std"),
        ("stop_loss_pct",  "take_profit_pct"),
        ("volume_spike_threshold", "signal_threshold"),
        ("rsi_oversold",   "signal_threshold"),
        ("stop_loss_pct",  "signal_threshold"),
    ]

    for ax, (p1, p2) in zip(axes.flat, param_pairs):
        pivot = df.groupby([p1, p2])[OPTIMIZE_FOR].mean().unstack()
        if pivot.empty:
            ax.set_visible(False)
            continue

        im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto",
                      vmin=df[OPTIMIZE_FOR].quantile(0.1),
                      vmax=df[OPTIMIZE_FOR].quantile(0.9))

        ax.set_xticks(range(len(pivot.columns)))
        ax.set_yticks(range(len(pivot.index)))
        ax.set_xticklabels([f"{v:.2f}" if isinstance(v, float) else str(v)
                           for v in pivot.columns], fontsize=8)
        ax.set_yticklabels([f"{v:.2f}" if isinstance(v, float) else str(v)
                           for v in pivot.index], fontsize=8)
        ax.set_xlabel(p2, fontsize=9)
        ax.set_ylabel(p1, fontsize=9)
        ax.set_title(f"{p1} vs {p2}", fontsize=10)

        plt.colorbar(im, ax=ax, fraction=0.046, label="Sharpe")

        # Annoter les valeurs
        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.values[i, j]
                if not np.isnan(val):
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                           fontsize=7, color="black")

    plt.tight_layout()
    plt.savefig("charts/optimizer_heatmap.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("📊 Heatmap sauvegardée: charts/optimizer_heatmap.png")


def plot_param_importance(df: pd.DataFrame):
    """Bar chart de l'importance de chaque paramètre (variance du Sharpe)."""
    fig, ax = plt.subplots(figsize=(12, 5))

    params = list(PARAM_GRID.keys())
    variances = []

    for p in params:
        var = df.groupby(p)[OPTIMIZE_FOR].mean().var()
        variances.append(var if not np.isnan(var) else 0)

    # Normaliser
    total = sum(variances) or 1
    importances = [v / total * 100 for v in variances]

    colors = plt.cm.RdYlGn(np.linspace(0.2, 0.9, len(params)))
    bars = ax.barh(params, importances, color=colors)

    for bar, val in zip(bars, importances):
        ax.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
               f"{val:.1f}%", va="center", fontsize=9)

    ax.set_xlabel("Importance relative (%)")
    ax.set_title("Impact de chaque paramètre sur le Sharpe Ratio")
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()
    plt.savefig("charts/optimizer_importance.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("📊 Importance sauvegardée: charts/optimizer_importance.png")


def plot_top_distributions(df: pd.DataFrame, top_n: int = 50):
    """Distribution des métriques pour le Top N vs le reste."""
    top = df.head(top_n)
    rest = df.iloc[top_n:]

    fig, axes = plt.subplots(1, 4, figsize=(16, 4))
    metrics = ["sharpe_ratio", "win_rate", "total_return_pct", "max_drawdown_pct"]
    titles  = ["Sharpe Ratio", "Win Rate (%)", "Return (%)", "Max Drawdown (%)"]

    for ax, metric, title in zip(axes, metrics, titles):
        ax.hist(rest[metric], bins=20, alpha=0.6, color="#F44336", label="Reste")
        ax.hist(top[metric],  bins=20, alpha=0.8, color="#4CAF50", label=f"Top {top_n}")
        ax.set_title(title, fontsize=10)
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.suptitle(f"Distribution Top {top_n} vs Reste", fontsize=12, fontweight="bold")
    plt.tight_layout()
    plt.savefig("charts/optimizer_distributions.png", dpi=150, bbox_inches="tight")
    plt.close()
    print("📊 Distributions sauvegardées: charts/optimizer_distributions.png")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    print("=" * 60)
    print("TRADING BOT - OPTIMISEUR PHASE 1")
    print(f"Optimisation: {OPTIMIZE_FOR}")
    print("=" * 60)

    # 1. Fetch données (une seule fois)
    cached_data = fetch_all_data(OPTIMIZATION_TICKERS)
    if not cached_data:
        print("❌ Impossible de charger les données")
        return

    # 2. Grid search
    df_results = run_grid_search(cached_data)

    # 3. Sauvegarde CSV
    df_results.to_csv("charts/optimizer_results.csv", index=False)
    print(f"\n💾 {len(df_results)} résultats sauvegardés: charts/optimizer_results.csv")

    # 4. Affichage top 10
    print("\n" + "=" * 60)
    print("🏆 TOP 10 CONFIGURATIONS")
    print("=" * 60)

    top10 = df_results.head(10)
    for i, (_, row) in enumerate(top10.iterrows(), 1):
        print(f"\n#{i} — Sharpe: {row['sharpe_ratio']:.3f} | "
              f"Win: {row['win_rate']:.1f}% | "
              f"Return: {row['total_return_pct']:.2f}% | "
              f"DD: {row['max_drawdown_pct']:.2f}% | "
              f"Trades: {int(row['nb_trades'])}")
        print(f"     RSI period={int(row['rsi_period'])} oversold={row['rsi_oversold']} "
              f"overbought={row['rsi_overbought']}")
        print(f"     BB period={int(row['bb_period'])} std={row['bb_std']} | "
              f"Vol spike={row['volume_spike_threshold']}")
        print(f"     SL={row['stop_loss_pct']:.0%} TP={row['take_profit_pct']:.0%} | "
              f"Signal threshold={int(row['signal_threshold'])}")

    # 5. Meilleure config
    best = df_results.iloc[0]
    print("\n" + "=" * 60)
    print("✅ MEILLEURS PARAMÈTRES À UTILISER DANS phase1_technical.py")
    print("=" * 60)
    print(f"""
TradingConfig(
    rsi_period             = {int(best['rsi_period'])},
    rsi_oversold           = {best['rsi_oversold']},
    rsi_overbought         = {best['rsi_overbought']},
    bb_period              = {int(best['bb_period'])},
    bb_std                 = {best['bb_std']},
    volume_spike_threshold = {best['volume_spike_threshold']},
    stop_loss_pct          = {best['stop_loss_pct']},
    take_profit_pct        = {best['take_profit_pct']},
    signal_threshold       = {int(best['signal_threshold'])},
)
""")

    # 6. Visualisations
    print("📊 Génération des charts...")
    plot_heatmaps(df_results)
    plot_param_importance(df_results)
    plot_top_distributions(df_results)

    print("\n✅ Optimisation terminée !")
    print("📁 Fichiers générés dans charts/:")
    print("   - optimizer_results.csv     (tous les résultats)")
    print("   - optimizer_heatmap.png     (corrélations entre paramètres)")
    print("   - optimizer_importance.png  (impact de chaque paramètre)")
    print("   - optimizer_distributions.png (Top 50 vs reste)")


if __name__ == "__main__":
    main()
