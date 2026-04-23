"""
╔══════════════════════════════════════════════════════════╗
║            BUBO BRAIN — LLM Decision Engine              ║
║        Gemini 2.5 Pro remplace le scoring hardcodé       ║
╚══════════════════════════════════════════════════════════╝

Au lieu de règles if/else (RSI > 65 → -15 pts), on envoie TOUTES
les données brutes à Gemini 2.5 Pro et il raisonne lui-même.

Setup:
    1. pip install google-genai yfinance pandas-ta transformers torch praw
    2. Obtenir une clé API gratuite: https://aistudio.google.com/apikey
    3. Set: GEMINI_API_KEY=ta_clé (variable d'environnement)
       Ou: remplir gemini_config.json

Usage:
    python bubo_brain.py                 # Analyse live
    python bubo_brain.py --watch         # Surveillance continue
    python bubo_brain.py --tickers RTX LMT
    python bubo_brain.py --no-finbert    # Sans FinBERT (plus rapide)
    python bubo_brain.py --dry-run       # Voir le prompt sans appeler l'API
"""

import sys
import os
import json
import re
import time
import argparse
import warnings
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field

import pandas as pd
import numpy as np

warnings.filterwarnings("ignore")
os.makedirs("data", exist_ok=True)
sys.path.insert(0, ".")


# ─────────────────────────────────────────────
# Imports des phases (graceful)
# ─────────────────────────────────────────────

MODULES = {}

try:
    from phase1_technical import TradingConfig, MarketDataFetcher, TechnicalAnalyzer
    MODULES["phase1"] = True
except ImportError as e:
    MODULES["phase1"] = False
    print(f"  ⚠️  Phase 1: {e}")

try:
    from phase2a_events import EventCalendar, EventFilter
    MODULES["phase2a"] = True
except ImportError as e:
    MODULES["phase2a"] = False
    print(f"  ⚠️  Phase 2a: {e}")

try:
    from phase2b_sentiment import FinBERTAnalyzer, NewsFetcher, SentimentEngine
    MODULES["phase2b"] = True
except ImportError as e:
    MODULES["phase2b"] = False
    print(f"  ⚠️  Phase 2b: {e}")

try:
    from phase3b_social import SocialPipeline, load_config as load_social_config
    MODULES["phase3b"] = True
except ImportError as e:
    MODULES["phase3b"] = False
    print(f"  ⚠️  Phase 3b: {e}")


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

WATCHLIST = {
    "AM.PA":  "Dassault Aviation",
    "AIR.PA": "Airbus",
    "LMT":    "Lockheed Martin",
    "RTX":    "Raytheon",
}

def _parse_model_chain(raw: str) -> list[str]:
    items = [m.strip() for m in str(raw or "").split(",") if m.strip()]
    return items or ["gemini-2.5-flash"]


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


GEMINI_MODELS = _parse_model_chain(os.environ.get("BUBO_GEMINI_MODEL_CHAIN", "gemini-2.5-flash"))
MIN_SAFE_OUTPUT_TOKENS = 256
MAX_RETRY_OUTPUT_TOKENS = 2048
GEMINI_MAX_OUTPUT_TOKENS = _env_int(
    "BUBO_GEMINI_MAX_OUTPUT_TOKENS",
    700,
    MIN_SAFE_OUTPUT_TOKENS,
    2048,
)
GEMINI_THINKING_BUDGET = _env_int("BUBO_GEMINI_THINKING_BUDGET", 0, 0, 2048)
PROMPT_MAX_EVENTS = _env_int("BUBO_GEMINI_PROMPT_MAX_EVENTS", 4, 0, 10)
PROMPT_MAX_HEADLINES = _env_int("BUBO_GEMINI_PROMPT_MAX_HEADLINES", 3, 0, 10)
PROMPT_MAX_POSTS = _env_int("BUBO_GEMINI_PROMPT_MAX_POSTS", 2, 0, 10)
PROMPT_MAX_POST_CHARS = _env_int("BUBO_GEMINI_PROMPT_MAX_POST_CHARS", 80, 20, 300)

SYSTEM_PROMPT = """Tu es BUBO, un analyste financier expert spécialisé dans le secteur de la défense (Dassault Aviation, Airbus, Lockheed Martin, Raytheon).

Tu reçois des données brutes provenant de 4 sources pour un ticker:
1. TECHNIQUE: indicateurs RSI, MACD, Bollinger Bands, SMA, volume, ATR
2. NEWS: articles récents analysés par FinBERT avec sentiment positif/négatif/neutre
3. SOCIAL: posts Reddit et Stocktwits avec sentiment agrégé
4. ÉVÉNEMENTS: calendrier earnings, décisions Fed/BCE, NFP, CPI

Ta mission: analyser TOUTES ces données ensemble, trouver les corrélations et divergences, et produire une décision de trading.

RÈGLES DE DÉCISION:
- Tu cherches la CONVERGENCE des signaux (technique + sentiment + social pointent dans la même direction)
- Les divergences (ex: technique bullish mais news bearish) = prudence, tu restes HOLD
- En période de blackout (earnings/Fed/BCE dans <2 jours) = TOUJOURS HOLD, quelle que soit la force des signaux
- Tu dois intégrer les coûts d'exécution (frais + slippage). Si l'edge attendu est inférieur aux coûts aller-retour + marge de sécurité, tu restes HOLD
- Position sizing: plus la convergence est forte et la confiance haute, plus la position est grosse
- Stop loss: 2%, Take profit: 10% (ratio risque/reward 1:5)
- Capital: 10 000€, max 25% par position, max 3 positions simultanées

RÉPONSE OBLIGATOIRE en JSON strict:
- pas de texte avant/après, pas de markdown, pas de bloc ```json
- réponse compacte pour limiter les coûts/tokens
- "raisonnement": <= 160 caractères
- "signaux_cles": max 2 éléments courts
- "risques": max 2 éléments courts
{
    "ticker": "XXX",
    "decision": "BUY ou SELL ou HOLD",
    "score": 0-100,
    "confidence": 0-100,
    "position_size_pct": 0.0-0.25,
    "stop_loss": prix_exact_ou_null,
    "take_profit": prix_exact_ou_null,
    "raisonnement": "Explication synthétique.",
    "signaux_cles": ["signal1", "signal2"],
    "risques": ["risque1", "risque2"]
}"""


def load_gemini_key() -> str:
    """Charge la clé API Gemini depuis env ou fichier config."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key

    config_path = "gemini_config.json"
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("api_key", "")
        if key:
            return key

    template = {
        "api_key": "",
        "_instructions": [
            "1. Aller sur https://aistudio.google.com/apikey",
            "2. Créer une clé API (gratuit)",
            "3. Coller la clé dans le champ api_key ci-dessus",
            "Alternative: set GEMINI_API_KEY=ta_clé (variable d'environnement)"
        ]
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    print(f"📝 Créé: {config_path} — remplissez votre clé API Gemini")
    return ""


# ─────────────────────────────────────────────
# Data Collector
# ─────────────────────────────────────────────

class DataCollector:
    """Collecte toutes les données brutes des 4 phases."""

    def __init__(self, use_finbert: bool = True, runtime_context: dict | None = None):
        self.use_finbert = use_finbert
        self.tech_config = None
        self.fetcher = None
        self.analyzer = None
        self.event_calendar = None
        self.event_filter = None
        self.finbert = None
        self.news_fetcher = None
        self.sentiment_engine = None
        self.social_pipeline = None
        self.runtime_context = dict(runtime_context or {})

        if MODULES["phase1"]:
            self.tech_config = TradingConfig()
            self.fetcher = MarketDataFetcher()
            self.analyzer = TechnicalAnalyzer(self.tech_config)

    def init_events(self, tickers: list):
        if MODULES["phase2a"]:
            self.event_calendar = EventCalendar(tickers)
            self.event_calendar.load_all()
            self.event_filter = EventFilter(self.event_calendar)

    def init_sentiment(self):
        if MODULES["phase2b"] and self.use_finbert:
            try:
                self.finbert = FinBERTAnalyzer()
                if self.finbert.load():
                    self.news_fetcher = NewsFetcher()
                    self.sentiment_engine = SentimentEngine(self.finbert, self.news_fetcher)
                else:
                    MODULES["phase2b"] = False
            except Exception as e:
                print(f"  ⚠️  FinBERT: {e}")
                MODULES["phase2b"] = False

    def init_social(self):
        if MODULES["phase3b"]:
            try:
                social_cfg = load_social_config()
                self.social_pipeline = SocialPipeline(social_cfg)
            except Exception as e:
                print(f"  ⚠️  Social: {e}")
                MODULES["phase3b"] = False

    def set_runtime_context(self, context: dict | None):
        self.runtime_context = dict(context or {})

    def collect(self, ticker: str) -> dict:
        data = {"ticker": ticker, "name": WATCHLIST.get(ticker, ticker),
                "collected_at": datetime.now().isoformat(),
                "technical": {}, "news": {}, "social": {}, "events": {},
                "constraints": dict(self.runtime_context)}

        if MODULES["phase1"]:
            try:
                data["technical"] = self._collect_technical(ticker)
            except Exception as e:
                data["technical"] = {"error": str(e)}

        if MODULES["phase2a"] and self.event_filter:
            try:
                data["events"] = self._collect_events(ticker)
            except Exception as e:
                data["events"] = {"error": str(e)}

        if MODULES["phase2b"] and self.sentiment_engine:
            try:
                data["news"] = self._collect_news(ticker)
            except Exception as e:
                data["news"] = {"error": str(e)}

        if MODULES["phase3b"] and self.social_pipeline:
            try:
                data["social"] = self._collect_social(ticker)
            except Exception as e:
                data["social"] = {"error": str(e)}

        return data

    def _collect_technical(self, ticker: str) -> dict:
        df = self.fetcher.fetch(ticker, "1d")
        if df is None or df.empty:
            return {"error": "Pas de données"}

        df = self.analyzer.compute_indicators(df)
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else row

        returns_5d = ((row["Close"] / df.iloc[-6]["Close"]) - 1) * 100 if len(df) > 6 else 0
        returns_20d = ((row["Close"] / df.iloc[-21]["Close"]) - 1) * 100 if len(df) > 21 else 0

        # Detect MACD cross
        macd_cross = "none"
        if row.get("macd", 0) > row.get("macd_signal", 0) and prev.get("macd", 0) <= prev.get("macd_signal", 0):
            macd_cross = "bullish"
        elif row.get("macd", 0) < row.get("macd_signal", 0) and prev.get("macd", 0) >= prev.get("macd_signal", 0):
            macd_cross = "bearish"

        def safe(val):
            if isinstance(val, (float, np.floating)):
                if pd.isna(val) or np.isinf(val):
                    return None
                return round(float(val), 4)
            return val

        volume_features = self._compute_volume_anomaly_features(df)

        return {
            "prix_actuel": safe(row["Close"]),
            "rendement_5j_pct": safe(returns_5d),
            "rendement_20j_pct": safe(returns_20d),
            "rsi": safe(row.get("rsi", 50)),
            "macd": safe(row.get("macd", 0)),
            "macd_signal": safe(row.get("macd_signal", 0)),
            "macd_histogram": safe(row.get("macd_hist", 0)),
            "macd_cross": macd_cross,
            "bb_pct": safe(row.get("bb_pct", 0.5)),
            "bb_upper": safe(row.get("bb_upper", 0)),
            "bb_lower": safe(row.get("bb_lower", 0)),
            "sma_20": safe(row.get("sma_20", 0)),
            "sma_50": safe(row.get("sma_50", 0)),
            "sma_200": safe(row.get("sma_200", 0)),
            "au_dessus_sma200": bool(row["Close"] > row.get("sma_200", 0)) if pd.notna(row.get("sma_200")) else None,
            "volume_ratio": safe(row.get("volume_ratio", 1.0)),
            "volume_spike": bool(row.get("volume_spike", False)),
            "volume_rvol_mean20": safe(volume_features.get("volume_rvol_mean20")),
            "volume_rvol_median20": safe(volume_features.get("volume_rvol_median20")),
            "volume_zscore_20": safe(volume_features.get("volume_zscore_20")),
            "volume_robust_zscore_60": safe(volume_features.get("volume_robust_zscore_60")),
            "volume_percentile_60": safe(volume_features.get("volume_percentile_60")),
            "volume_anomaly_score": safe(volume_features.get("volume_anomaly_score")),
            "volume_anomaly_label": str(volume_features.get("volume_anomaly_label", "NORMAL")),
            "atr_pct": safe(row.get("atr_pct", 0)),
            "trend_up": bool(row.get("trend_up", False)),
            "trend_down": bool(row.get("trend_down", False)),
        }

    @staticmethod
    def _compute_volume_anomaly_features(df: pd.DataFrame) -> dict:
        volume = pd.to_numeric(df.get("Volume", pd.Series(dtype=float)), errors="coerce")
        volume = volume.replace([np.inf, -np.inf], np.nan).dropna()
        if volume.empty:
            return {}

        now_vol = float(volume.iloc[-1])
        win20 = volume.tail(20)
        win60 = volume.tail(60)

        mean20 = float(win20.mean()) if not win20.empty else np.nan
        median20 = float(win20.median()) if not win20.empty else np.nan
        std20 = float(win20.std(ddof=0)) if len(win20) >= 2 else np.nan

        rvol_mean20 = (now_vol / mean20) if pd.notna(mean20) and mean20 > 0 else np.nan
        rvol_median20 = (now_vol / median20) if pd.notna(median20) and median20 > 0 else np.nan
        z20 = ((now_vol - mean20) / std20) if pd.notna(std20) and std20 > 0 else np.nan

        robust_z60 = np.nan
        percentile60 = np.nan
        if not win60.empty:
            median60 = float(win60.median())
            mad60 = float((win60 - median60).abs().median())
            if mad60 > 0:
                robust_z60 = 0.6745 * (now_vol - median60) / mad60
            percentile60 = float((win60 <= now_vol).mean() * 100.0)

        anomaly_score = 0.0
        if pd.notna(rvol_median20):
            anomaly_score += max(0.0, min(45.0, (float(rvol_median20) - 1.0) * 25.0))
        if pd.notna(robust_z60):
            anomaly_score += max(0.0, min(35.0, float(robust_z60) * 6.0))
        if pd.notna(percentile60):
            anomaly_score += max(0.0, min(20.0, float(percentile60) - 80.0))
        anomaly_score = max(0.0, min(100.0, anomaly_score))

        if anomaly_score >= 80:
            label = "EXTREME"
        elif anomaly_score >= 60:
            label = "VERY_HIGH"
        elif anomaly_score >= 40:
            label = "HIGH"
        elif anomaly_score >= 25:
            label = "ELEVATED"
        else:
            label = "NORMAL"

        return {
            "volume_now": now_vol,
            "volume_rvol_mean20": rvol_mean20,
            "volume_rvol_median20": rvol_median20,
            "volume_zscore_20": z20,
            "volume_robust_zscore_60": robust_z60,
            "volume_percentile_60": percentile60,
            "volume_anomaly_score": anomaly_score,
            "volume_anomaly_label": label,
        }

    def _collect_events(self, ticker: str) -> dict:
        today = date.today()
        blocked, block_reason = self.event_filter.is_blackout(ticker, today)
        modifier, mod_reason = self.event_filter.get_event_score_modifier(ticker, today)

        upcoming = self.event_calendar.get_events_for_ticker(
            ticker, start=today, end=today + timedelta(days=14))
        macro = [e for e in self.event_calendar.get_upcoming_events(days_ahead=14)
                 if e.ticker == "MACRO"]

        events_list = []
        for e in (upcoming + macro)[:10]:
            events_list.append({
                "date": str(e.event_date),
                "type": e.event_type,
                "description": e.description,
                "importance": e.importance,
                "jours_restants": (e.event_date - today).days,
            })

        profile = self.event_filter.get_profile(ticker)
        return {
            "blackout_actif": blocked,
            "blackout_raison": block_reason if blocked else "",
            "modifier_position": round(modifier, 2),
            "profil_earnings": profile.profile,
            "beat_rate_pct": round(profile.beat_rate * 100, 0),
            "evenements_a_venir": events_list,
        }

    def _collect_news(self, ticker: str) -> dict:
        daily = self.sentiment_engine.get_current_sentiment(ticker)
        return {
            "score_sentiment": round(daily.sentiment_score, 4),
            "signal": daily.signal,
            "label": "BULLISH" if daily.signal == 1 else ("BEARISH" if daily.signal == -1 else "NEUTRAL"),
            "nb_articles": daily.article_count,
            "positifs": daily.positive_count,
            "negatifs": daily.negative_count,
            "neutres": daily.neutral_count,
            "confiance_moyenne": round(daily.avg_confidence, 3),
            "top_headlines": daily.top_headlines[:5],
        }

    def _collect_social(self, ticker: str) -> dict:
        result = self.social_pipeline.analyze_ticker(ticker)
        top_posts = [{
            "texte": tp.get("text", "")[:120],
            "source": tp.get("source", ""),
            "sentiment": tp.get("sentiment", ""),
            "score": tp.get("net_score", 0),
            "engagement": tp.get("engagement", 0),
        } for tp in result.get("top_posts", [])[:5]]

        return {
            "score_social": result.get("social_score", 50),
            "label": result.get("social_label", "NEUTRAL"),
            "nb_mentions": result.get("mention_count", 0),
            "sentiment_pondere": result.get("weighted_sentiment", 0),
            "volume_spike": result.get("volume_spike", False),
            "confiance": result.get("confidence", 0),
            "sources": result.get("source_breakdown", {}),
            "top_posts": top_posts,
        }


# ─────────────────────────────────────────────
# Gemini Brain
# ─────────────────────────────────────────────

class GeminiBrain:
    """Envoie les données à Gemini et parse la décision."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = None
        if api_key:
            self._init_client()

    def _attach_meta(self, payload: dict, ok: bool, status: str,
                     model: str = "", error: str = "") -> dict:
        out = dict(payload or {})
        out["_llm_ok"] = bool(ok)
        out["_llm_status"] = str(status or ("ok" if ok else "error"))
        out["_llm_model"] = str(model or "")
        out["_llm_error"] = str(error or "")
        return out

    def _init_client(self):
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            raw_tokens = str(os.environ.get("BUBO_GEMINI_MAX_OUTPUT_TOKENS", "")).strip()
            if raw_tokens:
                try:
                    raw_value = int(raw_tokens)
                    if raw_value < MIN_SAFE_OUTPUT_TOKENS:
                        print(
                            f"  ⚠️  BUBO_GEMINI_MAX_OUTPUT_TOKENS={raw_value} "
                            f"trop bas, plancher de sécurité={MIN_SAFE_OUTPUT_TOKENS}"
                        )
                except Exception:
                    pass
            print(
                "  ✅ Gemini connecté "
                f"(modèles: {', '.join(GEMINI_MODELS)} | max_output_tokens={GEMINI_MAX_OUTPUT_TOKENS} | "
                f"thinking_budget={GEMINI_THINKING_BUDGET})"
            )
        except ImportError:
            print("  ❌ google-genai non installé: pip install google-genai")

    @staticmethod
    def _retry_output_tokens(base_tokens: int) -> list[int]:
        base = max(MIN_SAFE_OUTPUT_TOKENS, min(MAX_RETRY_OUTPUT_TOKENS, int(base_tokens)))
        budgets = [base]
        if base < 512:
            budgets.append(512)
        if budgets[-1] < 1024:
            budgets.append(1024)
        if budgets[-1] < MAX_RETRY_OUTPUT_TOKENS:
            budgets.append(MAX_RETRY_OUTPUT_TOKENS)
        # Deduplicate while preserving order.
        seen = set()
        out = []
        for b in budgets:
            if b in seen:
                continue
            out.append(b)
            seen.add(b)
        return out

    @staticmethod
    def _needs_token_retry(parsed: dict) -> bool:
        status = str(parsed.get("_llm_status", "") or "")
        if status not in {"truncated", "incomplete_payload", "parse_failed"}:
            return False
        err = str(parsed.get("_llm_error", "") or "").upper()
        return "MAX_TOKENS" in err

    @staticmethod
    def _build_generate_config(types_module, output_tokens: int):
        kwargs = {
            "system_instruction": SYSTEM_PROMPT,
            "temperature": 0.3,
            "max_output_tokens": int(output_tokens),
            "response_mime_type": "application/json",
        }
        # Keep budget for visible JSON output; avoids hidden reasoning eating token cap.
        try:
            kwargs["thinking_config"] = types_module.ThinkingConfig(
                thinking_budget=int(GEMINI_THINKING_BUDGET)
            )
        except Exception:
            pass
        return types_module.GenerateContentConfig(**kwargs)

    def analyze(self, ticker_data: dict, dry_run: bool = False) -> dict:
        prompt = self._build_prompt(ticker_data)
        ticker = ticker_data["ticker"]

        if dry_run:
            print(f"\n{'─' * 70}")
            print(f"  DRY RUN — {ticker} — ~{len(prompt) // 4} tokens")
            print(f"{'─' * 70}")
            print(prompt[:3000])
            if len(prompt) > 3000:
                print(f"\n  [...{len(prompt) - 3000} chars tronqués...]")
            return self._default(ticker, status="dry_run", error="dry_run")

        if not self.client:
            return self._default(
                ticker,
                status="client_unavailable",
                error="Gemini client indisponible",
            )

        # Essayer chaque modèle avec retry
        from google.genai import types
        last_error = ""
        for model in GEMINI_MODELS:
            token_budgets = self._retry_output_tokens(GEMINI_MAX_OUTPUT_TOKENS)
            model_failed = False
            for token_budget in token_budgets:
                retry_with_more_tokens = False
                for attempt in range(3):
                    try:
                        response = self.client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config=self._build_generate_config(types, token_budget),
                        )
                        if attempt == 0 and token_budget == token_budgets[0] and model == GEMINI_MODELS[0]:
                            pass  # Premier essai, pas besoin de log
                        else:
                            print(
                                f"    ✅ {model} (tentative {attempt + 1}, max_output_tokens={token_budget})"
                            )
                        finish_reason = self._extract_finish_reason(response)
                        response_text = self._extract_response_text(response)
                        parsed = self._parse(
                            response_text,
                            ticker,
                            model=model,
                            finish_reason=finish_reason,
                        )
                        if parsed.get("_llm_ok", False):
                            return parsed
                        last_error = f"{model}: {parsed.get('_llm_status', 'error')}: {parsed.get('_llm_error', '')}"
                        if self._needs_token_retry(parsed) and token_budget < token_budgets[-1]:
                            retry_with_more_tokens = True
                            print(
                                f"    ↗️  {model}: sortie tronquée (max_output_tokens={token_budget}), retry avec budget plus élevé"
                            )
                            break
                        return parsed

                    except Exception as e:
                        err_str = str(e)
                        last_error = f"{model}: {err_str}"
                        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                            if attempt < 2:
                                wait = 10 * (attempt + 1)
                                print(f"    ⏳ {model}: rate limit, retry dans {wait}s...")
                                time.sleep(wait)
                                continue
                            print(f"    ⚠️  {model}: quota épuisé, essai modèle suivant")
                            model_failed = True
                            break
                        print(f"    ⚠️  {model}: {e}")
                        model_failed = True
                        break
                if model_failed:
                    break
                if retry_with_more_tokens:
                    continue

        print(f"    ❌ Tous les modèles ont échoué pour {ticker}")
        return self._default(
            ticker,
            status="api_error",
            error=(last_error or "all_models_failed"),
        )

    def _build_prompt(self, data: dict) -> str:
        ticker = data["ticker"]
        name = data["name"]
        parts = [f"Analyse {ticker} ({name}) — {data['collected_at'][:16]}"]

        constraints = data.get("constraints", {}) if isinstance(data, dict) else {}
        if constraints and isinstance(constraints, dict):
            def _to_float(value: object, default: float = 0.0) -> float:
                try:
                    return float(value)
                except Exception:
                    return default

            fee_bps = _to_float(constraints.get("trade_fee_bps_per_side"), 0.0)
            slippage_bps = _to_float(constraints.get("slippage_bps_per_side"), 0.0)
            roundtrip_bps = _to_float(constraints.get("roundtrip_cost_bps"), 0.0)
            max_pos = int(_to_float(constraints.get("max_open_positions"), 0.0))
            max_position_pct = _to_float(constraints.get("max_position_pct"), 0.0)
            max_expo = _to_float(constraints.get("max_total_exposure_pct"), 0.0)
            capital = _to_float(constraints.get("managed_capital_eur", constraints.get("capital_eur")), 0.0)
            allow_short = bool(constraints.get("allow_short"))
            parts.append("\n═══ CONTRAINTES D'EXÉCUTION ═══")
            parts.append(
                "Frais/ordre: "
                f"{fee_bps} bps | Slippage/ordre: {slippage_bps} bps | Coût A/R estimé: {roundtrip_bps} bps"
            )
            parts.append(
                f"Capital géré: {capital}€ | Max positions: {max_pos} | "
                f"Max position: {max_position_pct:.0%} | Exposition max: {max_expo:.0%}"
            )
            parts.append(f"Short autorisé: {bool(allow_short)}")
            parts.append("Règle coût: BUY/SELL uniquement si edge net > coûts A/R + marge de sécurité.")

        # Technique
        tech = data.get("technical", {})
        if tech and "error" not in tech:
            parts.append("\n═══ TECHNIQUE ═══")
            parts.append(f"Prix: {tech.get('prix_actuel')}€ | Rend 5j: {tech.get('rendement_5j_pct')}% | 20j: {tech.get('rendement_20j_pct')}%")
            parts.append(f"RSI: {tech.get('rsi')} | MACD: {tech.get('macd')} (signal: {tech.get('macd_signal')}, histo: {tech.get('macd_histogram')}, cross: {tech.get('macd_cross')})")
            parts.append(f"BB %B: {tech.get('bb_pct')} [{tech.get('bb_lower')} - {tech.get('bb_upper')}]")
            parts.append(f"SMA20: {tech.get('sma_20')} | SMA50: {tech.get('sma_50')} | SMA200: {tech.get('sma_200')} | >SMA200: {tech.get('au_dessus_sma200')}")
            parts.append(f"Volume: {tech.get('volume_ratio')}x (spike: {tech.get('volume_spike')}) | ATR%: {tech.get('atr_pct')} | Trend: {'UP' if tech.get('trend_up') else ('DOWN' if tech.get('trend_down') else 'FLAT')}")
            parts.append(
                "Volume anormal: "
                f"score={tech.get('volume_anomaly_score')}/100 ({tech.get('volume_anomaly_label')}) | "
                f"RVOL m20={tech.get('volume_rvol_mean20')}x | RVOL med20={tech.get('volume_rvol_median20')}x | "
                f"z20={tech.get('volume_zscore_20')} | rz60={tech.get('volume_robust_zscore_60')} | "
                f"p60={tech.get('volume_percentile_60')}%"
            )

        # Events
        events = data.get("events", {})
        if events and "error" not in events:
            parts.append("\n═══ ÉVÉNEMENTS ═══")
            if events.get("blackout_actif"):
                parts.append(f"⚠️ BLACKOUT: {events.get('blackout_raison')}")
            parts.append(f"Profil: {events.get('profil_earnings')} | Beat rate: {events.get('beat_rate_pct')}% | Modifier: {events.get('modifier_position')}x")
            for ev in events.get("evenements_a_venir", [])[:PROMPT_MAX_EVENTS]:
                parts.append(f"  J+{ev['jours_restants']:2d} {ev['date']} {ev['description']} (imp: {ev['importance']}/3)")

        # News
        news = data.get("news", {})
        if news and "error" not in news:
            parts.append(f"\n═══ NEWS SENTIMENT ═══")
            parts.append(f"Score: {news.get('score_sentiment'):+.4f} | {news.get('label')} | {news.get('nb_articles')} articles ({news.get('positifs')}+/{news.get('negatifs')}-/{news.get('neutres')}~) | Conf: {news.get('confiance_moyenne')}")
            for h in news.get("top_headlines", [])[:PROMPT_MAX_HEADLINES]:
                parts.append(f"  {h}")

        # Social
        social = data.get("social", {})
        if social and "error" not in social:
            parts.append(f"\n═══ SOCIAL ═══")
            parts.append(f"Score: {social.get('score_social')}/100 | {social.get('label')} | {social.get('nb_mentions')} mentions | Sent: {social.get('sentiment_pondere'):+.3f} | Spike: {social.get('volume_spike')}")
            for p in social.get("top_posts", [])[:PROMPT_MAX_POSTS]:
                parts.append(f"  [{p['source']}] {p['sentiment']} ({p['score']:+.3f}, eng:{p['engagement']}) {p['texte'][:PROMPT_MAX_POST_CHARS]}")

        parts.append(f"\nDécision JSON pour {ticker}:")
        return "\n".join(parts)

    def _extract_response_text(self, response) -> str:
        text = str(getattr(response, "text", "") or "").strip()
        if text:
            return text

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""

        parts = []
        first = candidates[0]
        content = getattr(first, "content", None)
        chunk_list = getattr(content, "parts", None) or []
        for part in chunk_list:
            segment = str(getattr(part, "text", "") or "")
            if segment:
                parts.append(segment)
        return "".join(parts).strip()

    def _extract_finish_reason(self, response) -> str:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        reason = getattr(candidates[0], "finish_reason", "")
        return str(reason or "")

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        clean = str(text or "").strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        return clean.strip()

    @staticmethod
    def _extract_json_block(text: str) -> str:
        clean = GeminiBrain._strip_markdown_fence(text)
        if not clean:
            return ""

        start = clean.find("{")
        if start < 0:
            return clean
        end = clean.rfind("}")
        if end >= start:
            return clean[start:end + 1].strip()
        return clean[start:].strip()

    @staticmethod
    def _repair_json_candidate(candidate: str) -> str:
        text = str(candidate or "").strip()
        if not text:
            return text
        text = re.sub(r",\s*([}\]])", r"\1", text)
        text = re.sub(r",\s*$", "", text)
        missing_brackets = text.count("[") - text.count("]")
        missing_braces = text.count("{") - text.count("}")
        if missing_brackets > 0:
            text += "]" * missing_brackets
        if missing_braces > 0:
            text += "}" * missing_braces
        return text

    @staticmethod
    def _looks_truncated(candidate: str, json_error: Exception, finish_reason: str = "") -> bool:
        reason = str(finish_reason or "").upper()
        if "MAX_TOKENS" in reason:
            return True
        msg = str(json_error or "")
        text = str(candidate or "").rstrip()
        if "Unterminated string" in msg:
            return True
        if text.endswith((",", ":", "\"", "[", "{")):
            return True
        if text.count("{") > text.count("}") or text.count("[") > text.count("]"):
            return True
        return False

    @staticmethod
    def _extract_float(text: str, pattern: str) -> float | None:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if not m:
            return None
        try:
            return float(m.group(1))
        except Exception:
            return None

    @staticmethod
    def _fallback_parse_from_text(text: str, ticker: str) -> dict | None:
        raw = str(text or "")
        if not raw:
            return None

        decision = None
        m_dec = re.search(r'"?decision"?\s*[:=]\s*"?(STRONG BUY|STRONG SELL|BUY|SELL|HOLD)"?', raw, flags=re.IGNORECASE)
        if m_dec:
            decision = m_dec.group(1).upper()
        else:
            m_dec_free = re.search(r"\b(STRONG BUY|STRONG SELL|BUY|SELL|HOLD)\b", raw, flags=re.IGNORECASE)
            if m_dec_free:
                decision = m_dec_free.group(1).upper()

        score = GeminiBrain._extract_float(raw, r'"?score"?\s*[:=]\s*(-?\d+(?:\.\d+)?)')
        confidence = GeminiBrain._extract_float(raw, r'"?(?:confidence|confiance)"?\s*[:=]\s*(-?\d+(?:\.\d+)?)')
        position_pct = GeminiBrain._extract_float(raw, r'"?position_size_pct"?\s*[:=]\s*(-?\d+(?:\.\d+)?)')

        if decision is None and score is None:
            return None

        payload = {
            "ticker": ticker,
            "decision": decision or "HOLD",
            "score": 50.0 if score is None else score,
            "confidence": 0.0 if confidence is None else confidence,
            "position_size_pct": 0.0 if position_pct is None else position_pct,
            "stop_loss": None,
            "take_profit": None,
            "raisonnement": "Réponse partielle récupérée",
            "signaux_cles": [],
            "risques": [],
        }
        return payload

    def _parse(self, text: str, ticker: str, model: str = "", finish_reason: str = "") -> dict:
        try:
            candidate = self._extract_json_block(text)
            if not candidate:
                return self._default(
                    ticker,
                    status="empty_response",
                    model=model,
                    error="Réponse LLM vide",
                )

            try:
                result = json.loads(candidate)
            except json.JSONDecodeError:
                repaired = self._repair_json_candidate(candidate)
                result = json.loads(repaired)
            return self._validate(result, ticker, status="ok", model=model)

        except json.JSONDecodeError as e:
            print(f"    [WARN] Parse echoue: {text[:200]}")
            fallback = self._fallback_parse_from_text(text, ticker)
            if fallback is not None:
                return self._validate(
                    fallback,
                    ticker,
                    status="ok_fallback",
                    model=model,
                    error="fallback_text_parse",
                )
            snippet = str(text or "").replace("\n", " ").replace("\r", " ")
            status = "truncated" if self._looks_truncated(
                candidate=snippet,
                json_error=e,
                finish_reason=finish_reason,
            ) else "parse_failed"
            detail = f"{e.msg} (col={getattr(e, 'colno', '?')})"
            if finish_reason:
                detail += f" | finish_reason={finish_reason}"
            return self._default(
                ticker,
                status=status,
                model=model,
                error=f"JSON invalide: {detail} | {snippet[:180]}",
            )

    def _validate(self, result: dict, ticker: str, status: str = "ok",
                  model: str = "", error: str = "") -> dict:
        """Normalise et valide un résultat."""
        required_keys = ("decision", "score", "confidence", "position_size_pct")
        missing = [k for k in required_keys if k not in result]
        critical_missing = [k for k in ("decision", "score") if k not in result]
        if critical_missing:
            return self._default(
                ticker,
                status="incomplete_payload",
                model=model,
                error=f"Champs manquants: {', '.join(critical_missing)}",
            )

        result.setdefault("ticker", ticker)
        result.setdefault("score", 50)
        result.setdefault("confidence", 0)
        result.setdefault("position_size_pct", 0.0)
        result.setdefault("raisonnement", "")
        result.setdefault("signaux_cles", [])
        result.setdefault("risques", [])

        if missing and not error:
            error = f"Champs auto-completés: {', '.join(missing)}"
            status = "ok_partial"

        raw_decision = str(result.get("decision", "HOLD") or "HOLD").strip().upper().replace("_", " ")
        if raw_decision not in ("BUY", "SELL", "HOLD", "STRONG BUY", "STRONG SELL"):
            raw_decision = "HOLD"
            if not error:
                error = "decision invalide -> HOLD"
            status = "ok_sanitized"
        result["decision"] = raw_decision

        try:
            score = float(result["score"])
        except Exception:
            score = 50.0
        try:
            confidence = float(result["confidence"])
        except Exception:
            confidence = 0.0
        try:
            position_pct = float(result["position_size_pct"])
        except Exception:
            position_pct = 0.0

        result["score"] = max(0.0, min(100.0, score))
        result["confidence"] = max(0.0, min(100.0, confidence))
        result["position_size_pct"] = max(0.0, min(0.25, position_pct))

        return self._attach_meta(result, ok=True, status=status, model=model, error=error)

    def _default(self, ticker: str, status: str = "default",
                 error: str = "", model: str = "") -> dict:
        payload = {
            "ticker": ticker,
            "decision": "HOLD",
            "score": 50,
            "confidence": 0,
            "position_size_pct": 0.0,
            "stop_loss": None,
            "take_profit": None,
            "raisonnement": "Analyse indisponible",
            "signaux_cles": [],
            "risques": [],
        }
        return self._attach_meta(payload, ok=False, status=status, model=model, error=error)


# ─────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────

def display_dashboard(results: dict):
    print(f"\n{'═' * 70}")
    print(f"  🦉 BUBO BRAIN — Gemini 2.5")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 70}")

    for ticker, r in results.items():
        score = r.get("score", 50)
        decision = r.get("decision", "HOLD")
        confidence = r.get("confidence", 0)
        name = WATCHLIST.get(ticker, ticker)

        bar_len = 30
        filled = int((score / 100) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        icons = {"STRONG BUY": "🟢🟢", "BUY": "🟢", "HOLD": "⚪",
                 "SELL": "🔴", "STRONG SELL": "🔴🔴"}

        print(f"\n{'─' * 70}")
        print(f"  {ticker} — {name}")
        print(f"  {icons.get(decision, '⚪')} {decision}  |  [{bar}] {score}/100  |  Conf: {confidence}%")

        pos = r.get("position_size_pct", 0)
        if pos > 0:
            eur = pos * 10000
            sl = r.get("stop_loss")
            tp = r.get("take_profit")
            print(f"  💰 {pos:.1%} = {eur:.0f}€" +
                  (f"  SL: {sl}€" if sl else "") +
                  (f"  TP: {tp}€" if tp else ""))

        raison = r.get("raisonnement", "")
        if raison:
            words = raison.split()
            lines, cur = [], "  💭 "
            for w in words:
                if len(cur) + len(w) > 68:
                    lines.append(cur)
                    cur = "     " + w
                else:
                    cur += (" " if cur.strip() else "") + w
            lines.append(cur)
            for l in lines:
                print(l)

        signaux = r.get("signaux_cles", [])
        if signaux:
            print(f"  📊 {' | '.join(str(s) for s in signaux[:5])}")

        risques = r.get("risques", [])
        if risques:
            print(f"  ⚠️  {' | '.join(str(ri) for ri in risques[:3])}")

    print(f"\n{'═' * 70}")
    print(f"  {'Ticker':<10} {'Score':>5} {'Décision':<12} {'Conf':>5} {'Position':>9}")
    print(f"  {'─' * 46}")
    for ticker, r in results.items():
        pos = f"{r.get('position_size_pct', 0):.0%}" if r.get('position_size_pct', 0) > 0 else "—"
        print(f"  {ticker:<10} {r.get('score', 50):>5} {r.get('decision', 'HOLD'):<12} "
              f"{r.get('confidence', 0):>4}% {pos:>9}")
    print(f"{'═' * 70}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="BUBO BRAIN — Gemini 2.5")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--tickers", nargs="+")
    parser.add_argument("--no-finbert", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()

    print("=" * 70)
    print("  🦉 BUBO BRAIN — Gemini 2.5 Decision Engine")
    print("=" * 70)

    tickers = args.tickers or list(WATCHLIST.keys())

    api_key = load_gemini_key()
    if not api_key and not args.dry_run:
        print("\n❌ Clé API Gemini manquante.")
        print("   Remplissez gemini_config.json ou: set GEMINI_API_KEY=...")
        print("   Clé gratuite: https://aistudio.google.com/apikey")
        print("   Ou: python bubo_brain.py --dry-run")
        sys.exit(1)

    print("\n📡 Initialisation...")
    collector = DataCollector(use_finbert=not args.no_finbert)
    collector.init_events(tickers)
    if not args.no_finbert:
        collector.init_sentiment()
    collector.init_social()

    brain = GeminiBrain(api_key) if not args.dry_run else GeminiBrain("")

    def run():
        print(f"\n🔍 Analyse de {len(tickers)} tickers...")
        results = {}
        for i, ticker in enumerate(tickers):
            print(f"\n  📊 {ticker} ({WATCHLIST.get(ticker, '')})...")
            data = collector.collect(ticker)
            decision = brain.analyze(data, dry_run=args.dry_run)
            results[ticker] = decision
            # Pause entre les tickers pour respecter les rate limits
            if i < len(tickers) - 1 and not args.dry_run:
                time.sleep(3)

        if not args.dry_run:
            display_dashboard(results)
            rows = [{"ticker": t, "decision": r.get("decision"), "score": r.get("score"),
                     "confidence": r.get("confidence"), "position_pct": r.get("position_size_pct"),
                     "raisonnement": r.get("raisonnement", "")[:200],
                     "timestamp": datetime.now().isoformat()} for t, r in results.items()]
            df = pd.DataFrame(rows)
            csv_path = "data/brain_decisions.csv"
            if os.path.exists(csv_path):
                existing = pd.read_csv(csv_path)
                df = pd.concat([existing, df], ignore_index=True)
            df.to_csv(csv_path, index=False)
            print(f"\n📊 Export: {csv_path}")
        return results

    if args.watch:
        print(f"\n  Mode surveillance — refresh {args.interval}min | Ctrl+C pour arrêter")
        while True:
            try:
                run()
                print(f"\n  ⏳ Prochain refresh dans {args.interval}min...")
                time.sleep(args.interval * 60)
            except KeyboardInterrupt:
                print("\n  👋 Arrêté.")
                break
    else:
        run()


if __name__ == "__main__":
    main()
