"""
Trading Bot - Phase 2b: Analyse de Sentiment News (FinBERT)
============================================================
FinBERT est un modèle BERT fine-tuné sur des textes financiers.
Il classifie chaque texte en: positive / negative / neutral
avec un score de confiance.

Sources de news:
  - NewsAPI         (news générales, gratuit 100req/j)
  - Yahoo Finance   (via yfinance news)
  - Finnhub         (news par ticker, gratuit 60req/min)

Pipeline:
  1. Fetch des news par ticker (dernières 24h ou période de backtest)
  2. Scoring FinBERT sur chaque titre/résumé
  3. Agrégation en signal journalier [-1, +1]
  4. Intégration dans le système de score global

Setup (une seule fois):
    pip install transformers torch sentencepiece

Le modèle FinBERT (~400MB) est téléchargé automatiquement
depuis HuggingFace lors du premier run et caché localement.

Usage:
    python phase2b_sentiment.py
"""

import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import time
import hashlib
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field
from typing import Optional
import warnings

warnings.filterwarnings("ignore")
os.makedirs("data/news_cache", exist_ok=True)
os.makedirs("charts", exist_ok=True)


# ─────────────────────────────────────────────
# CONFIG SENTIMENT
# ─────────────────────────────────────────────

# Clés API optionnelles — le système fonctionne sans (via yfinance)
# Pour plus de couverture, obtenez des clés gratuites sur:
#   NewsAPI  : https://newsapi.org  (100 req/jour gratuit)
#   Finnhub  : https://finnhub.io   (60 req/min gratuit)
NEWSAPI_KEY  = ""   # Optionnel
FINNHUB_KEY  = ""   # Optionnel

# Seuils de sentiment
SENTIMENT_POSITIVE_THRESHOLD = 0.15   # score > seuil → signal positif
SENTIMENT_NEGATIVE_THRESHOLD = -0.15  # score < seuil → signal négatif

# Fenêtre de calcul du sentiment (jours)
SENTIMENT_WINDOW_DAYS = 3

# Cache des news (évite de re-fetcher)
CACHE_TTL_HOURS = 6


# ─────────────────────────────────────────────
# STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class NewsArticle:
    ticker: str
    title: str
    summary: str
    source: str
    published_at: datetime
    url: str = ""
    sentiment_label: Optional[str] = None    # positive | negative | neutral
    sentiment_score: Optional[float] = None  # [-1, +1]
    sentiment_confidence: Optional[float] = None  # [0, 1]


@dataclass
class DailySentiment:
    ticker: str
    date: date
    sentiment_score: float      # Score agrégé [-1, +1]
    article_count: int
    positive_count: int
    negative_count: int
    neutral_count: int
    avg_confidence: float
    signal: int                 # -1, 0, +1
    top_headlines: list = field(default_factory=list)


# ─────────────────────────────────────────────
# MODÈLE FINBERT
# ─────────────────────────────────────────────

class FinBERTAnalyzer:
    """
    Wrapper autour de FinBERT (ProsusAI/finbert).
    Charge le modèle une seule fois, analyse en batch pour la rapidité.
    Utilise le GPU (CUDA) automatiquement si disponible — RTX 5090 = très rapide.
    """

    MODEL_NAME = "ProsusAI/finbert"

    def __init__(self):
        self.pipeline = None
        self.device = -1  # CPU par défaut

    def load(self):
        """Charge le modèle FinBERT. Télécharge depuis HuggingFace si nécessaire."""
        try:
            from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
            import torch

            # Détecter GPU
            if torch.cuda.is_available():
                self.device = 0
                gpu_name = torch.cuda.get_device_name(0)
                vram = torch.cuda.get_device_properties(0).total_memory / 1e9
                print(f"  🎮 GPU détecté: {gpu_name} ({vram:.1f}GB VRAM)")
            else:
                print("  💻 GPU non disponible, utilisation CPU")

            print(f"  📥 Chargement FinBERT ({self.MODEL_NAME})...")
            print(f"     Premier lancement: téléchargement ~400MB depuis HuggingFace")

            self.pipeline = pipeline(
                "text-classification",
                model=self.MODEL_NAME,
                tokenizer=self.MODEL_NAME,
                device=self.device,
                top_k=None,            # Retourne tous les scores
                truncation=True,
                max_length=512,
            )
            print(f"  ✅ FinBERT chargé")
            return True

        except ImportError:
            print("  ❌ transformers/torch non installés")
            print("     Installez avec: pip install transformers torch sentencepiece")
            return False
        except Exception as e:
            print(f"  ❌ Erreur chargement FinBERT: {e}")
            return False

    def analyze_batch(self, texts: list[str]) -> list[dict]:
        """
        Analyse un batch de textes.
        Retourne une liste de dicts {label, score, confidence}.
        """
        if not self.pipeline:
            return [{"label": "neutral", "score": 0.0, "confidence": 0.0}] * len(texts)

        results = []
        # Batch size adapté à la VRAM — 32 pour une RTX 5090
        batch_size = 32 if self.device == 0 else 8

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            try:
                outputs = self.pipeline(batch)
                for output in outputs:
                    # output = liste de {label, score} pour chaque classe
                    scores = {item["label"]: item["score"] for item in output}
                    pos = scores.get("positive", 0)
                    neg = scores.get("negative", 0)
                    neu = scores.get("neutral",  0)

                    # Score net [-1, +1]
                    net_score = pos - neg

                    # Label dominant
                    dominant = max(scores, key=scores.get)
                    confidence = scores[dominant]

                    results.append({
                        "label":      dominant,
                        "score":      round(net_score, 4),
                        "confidence": round(confidence, 4),
                        "pos":        round(pos, 4),
                        "neg":        round(neg, 4),
                        "neu":        round(neu, 4),
                    })
            except Exception as e:
                print(f"  ⚠️  Erreur batch FinBERT: {e}")
                results.extend([{"label": "neutral", "score": 0.0, "confidence": 0.0}] * len(batch))

        return results

    def analyze_single(self, text: str) -> dict:
        """Analyse un seul texte."""
        results = self.analyze_batch([text])
        return results[0] if results else {"label": "neutral", "score": 0.0, "confidence": 0.0}


# ─────────────────────────────────────────────
# FETCHERS DE NEWS
# ─────────────────────────────────────────────

class NewsFetcher:
    """
    Agrège les news depuis plusieurs sources.
    Système de cache pour éviter les appels répétés.
    """

    # Mapping ticker → mots-clés de recherche
    TICKER_KEYWORDS = {
        "AM.PA":  ["Dassault Aviation", "Rafale", "Falcon jet"],
        "AIR.PA": ["Airbus", "A320", "A350", "A380"],
        "HO.PA":  ["Thales", "Thales Group", "defense electronics"],
        "LMT":    ["Lockheed Martin", "F-35", "LMT defense"],
        "RTX":    ["RTX", "Raytheon", "Pratt Whitney", "Collins Aerospace"],
        "NOC":    ["Northrop Grumman", "B-21", "Northrop defense"],
    }

    def __init__(self):
        self._cache: dict = {}

    def _cache_key(self, ticker: str, start: date, end: date) -> str:
        key = f"{ticker}_{start}_{end}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def _load_cache(self, cache_key: str) -> Optional[list]:
        cache_file = f"data/news_cache/{cache_key}.json"
        if os.path.exists(cache_file):
            mtime = datetime.fromtimestamp(os.path.getmtime(cache_file))
            if datetime.now() - mtime < timedelta(hours=CACHE_TTL_HOURS):
                with open(cache_file) as f:
                    return json.load(f)
        return None

    def _save_cache(self, cache_key: str, articles: list):
        cache_file = f"data/news_cache/{cache_key}.json"
        with open(cache_file, "w") as f:
            json.dump(articles, f, default=str)

    def fetch_yfinance_news(self, ticker: str, limit: int = 20) -> list[NewsArticle]:
        """Fetch les news depuis Yahoo Finance via yfinance."""
        articles = []
        try:
            stock = yf.Ticker(ticker)
            news = stock.news

            if not news:
                return []

            for item in news[:limit]:
                # Structure yfinance news
                content = item.get("content", {})
                if not content:
                    continue

                title   = content.get("title", "")
                summary = content.get("summary", "") or content.get("description", "")
                source  = content.get("provider", {}).get("displayName", "Yahoo Finance") if isinstance(content.get("provider"), dict) else "Yahoo Finance"
                url     = content.get("canonicalUrl", {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else ""

                # Date de publication
                pub_time = content.get("pubDate", "")
                try:
                    if pub_time:
                        pub_dt = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                    else:
                        pub_dt = datetime.now()
                except Exception:
                    pub_dt = datetime.now()

                if title:
                    articles.append(NewsArticle(
                        ticker=ticker,
                        title=title,
                        summary=summary[:500] if summary else "",
                        source=source,
                        published_at=pub_dt,
                        url=url,
                    ))

        except Exception as e:
            print(f"  ⚠️  yfinance news {ticker}: {e}")

        return articles

    def fetch_newsapi(self, ticker: str, start: date, end: date) -> list[NewsArticle]:
        """Fetch depuis NewsAPI (nécessite une clé API)."""
        if not NEWSAPI_KEY:
            return []

        try:
            import requests
            keywords = self.TICKER_KEYWORDS.get(ticker, [ticker])
            query = " OR ".join(f'"{kw}"' for kw in keywords[:2])

            url = "https://newsapi.org/v2/everything"
            params = {
                "q": query,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 20,
                "apiKey": NEWSAPI_KEY,
            }

            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            articles = []
            for item in data.get("articles", []):
                pub_dt = datetime.fromisoformat(
                    item["publishedAt"].replace("Z", "+00:00")
                ) if item.get("publishedAt") else datetime.now()

                articles.append(NewsArticle(
                    ticker=ticker,
                    title=item.get("title", ""),
                    summary=item.get("description", "")[:500],
                    source=item.get("source", {}).get("name", "NewsAPI"),
                    published_at=pub_dt,
                    url=item.get("url", ""),
                ))

            return articles

        except Exception as e:
            print(f"  ⚠️  NewsAPI {ticker}: {e}")
            return []

    def fetch_finnhub(self, ticker: str, start: date, end: date) -> list[NewsArticle]:
        """Fetch depuis Finnhub (nécessite une clé API)."""
        if not FINNHUB_KEY:
            return []

        try:
            import requests
            # Finnhub utilise des symboles US — adapter pour Paris
            fh_ticker = ticker.replace(".PA", "")

            url = "https://finnhub.io/api/v1/company-news"
            params = {
                "symbol": fh_ticker,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "token": FINNHUB_KEY,
            }

            resp = requests.get(url, params=params, timeout=10)
            data = resp.json()

            articles = []
            for item in data[:20] if isinstance(data, list) else []:
                pub_dt = datetime.fromtimestamp(item.get("datetime", 0))
                articles.append(NewsArticle(
                    ticker=ticker,
                    title=item.get("headline", ""),
                    summary=item.get("summary", "")[:500],
                    source=item.get("source", "Finnhub"),
                    published_at=pub_dt,
                    url=item.get("url", ""),
                ))

            return articles

        except Exception as e:
            print(f"  ⚠️  Finnhub {ticker}: {e}")
            return []

    def fetch_all(self, ticker: str,
                  start: date = None,
                  end: date = None) -> list[NewsArticle]:
        """Agrège les news depuis toutes les sources disponibles."""
        if end is None:
            end = date.today()
        if start is None:
            start = end - timedelta(days=7)

        cache_key = self._cache_key(ticker, start, end)
        cached = self._load_cache(cache_key)
        if cached:
            return [NewsArticle(**a) for a in cached]

        articles = []

        # Source 1: yfinance (toujours disponible)
        yf_articles = self.fetch_yfinance_news(ticker)
        articles.extend(yf_articles)

        # Source 2: NewsAPI (si clé disponible)
        if NEWSAPI_KEY:
            na_articles = self.fetch_newsapi(ticker, start, end)
            articles.extend(na_articles)

        # Source 3: Finnhub (si clé disponible)
        if FINNHUB_KEY:
            fh_articles = self.fetch_finnhub(ticker, start, end)
            articles.extend(fh_articles)

        # Déduplication par titre
        seen_titles = set()
        unique = []
        for a in articles:
            title_key = a.title[:50].lower().strip()
            if title_key and title_key not in seen_titles:
                seen_titles.add(title_key)
                unique.append(a)

        # Cache
        self._save_cache(cache_key, [a.__dict__ for a in unique])

        return unique


# ─────────────────────────────────────────────
# MOTEUR DE SENTIMENT
# ─────────────────────────────────────────────

class SentimentEngine:
    """
    Orchestre le fetch des news + scoring FinBERT + agrégation.
    Produit un score de sentiment journalier par ticker.
    """

    def __init__(self, finbert: FinBERTAnalyzer, fetcher: NewsFetcher):
        self.finbert = finbert
        self.fetcher = fetcher

    def _text_for_analysis(self, article: NewsArticle) -> str:
        """Construit le texte à analyser (titre + résumé tronqué)."""
        parts = [article.title]
        if article.summary:
            parts.append(article.summary[:200])
        return " ".join(parts)[:512]

    def score_articles(self, articles: list[NewsArticle]) -> list[NewsArticle]:
        """Applique FinBERT sur une liste d'articles."""
        if not articles:
            return []

        texts = [self._text_for_analysis(a) for a in articles]
        results = self.finbert.analyze_batch(texts)

        for article, result in zip(articles, results):
            article.sentiment_label      = result["label"]
            article.sentiment_score      = result["score"]
            article.sentiment_confidence = result["confidence"]

        return articles

    def compute_daily_sentiment(self,
                                 ticker: str,
                                 articles: list[NewsArticle],
                                 target_date: date) -> DailySentiment:
        """
        Agrège les scores des articles en un score journalier.
        Pondération par:
          - Confiance du modèle
          - Fraîcheur de l'article (articles récents = plus de poids)
        """
        # Filtrer les articles de la fenêtre temporelle
        window_start = datetime.combine(
            target_date - timedelta(days=SENTIMENT_WINDOW_DAYS),
            datetime.min.time()
        )
        window_end = datetime.combine(target_date, datetime.max.time())

        def to_naive_dt(val) -> datetime:
            """Convertit string ou datetime timezone-aware en datetime naive."""
            if isinstance(val, str):
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                except Exception:
                    dt = datetime.now()
            elif isinstance(val, datetime):
                dt = val
            else:
                dt = datetime.now()
            # Retirer le timezone
            return dt.replace(tzinfo=None)

        relevant = [
            a for a in articles
            if a.sentiment_score is not None
            and window_start <= to_naive_dt(a.published_at) <= window_end
        ]

        if not relevant:
            return DailySentiment(
                ticker=ticker, date=target_date,
                sentiment_score=0.0, article_count=0,
                positive_count=0, negative_count=0, neutral_count=0,
                avg_confidence=0.0, signal=0,
            )

        # Pondération par fraîcheur (exponentielle, décroit sur 3 jours)
        weights = []
        for a in relevant:
            age_days = (window_end - to_naive_dt(a.published_at)).total_seconds() / 86400
            freshness_weight = np.exp(-age_days / SENTIMENT_WINDOW_DAYS)
            confidence_weight = a.sentiment_confidence or 0.5
            weights.append(freshness_weight * confidence_weight)

        total_weight = sum(weights) or 1.0
        scores = [a.sentiment_score for a in relevant]

        # Score pondéré
        weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight

        pos = sum(1 for a in relevant if a.sentiment_label == "positive")
        neg = sum(1 for a in relevant if a.sentiment_label == "negative")
        neu = sum(1 for a in relevant if a.sentiment_label == "neutral")
        avg_conf = np.mean([a.sentiment_confidence for a in relevant if a.sentiment_confidence])

        # Signal
        if weighted_score > SENTIMENT_POSITIVE_THRESHOLD:
            signal = 1
        elif weighted_score < SENTIMENT_NEGATIVE_THRESHOLD:
            signal = -1
        else:
            signal = 0

        # Top headlines (pour le rapport)
        sorted_articles = sorted(relevant, key=lambda a: abs(a.sentiment_score), reverse=True)
        top_headlines = [
            f"[{a.sentiment_label.upper()[:3]} {a.sentiment_score:+.2f}] {a.title[:80]}"
            for a in sorted_articles[:3]
        ]

        return DailySentiment(
            ticker=ticker,
            date=target_date,
            sentiment_score=round(weighted_score, 4),
            article_count=len(relevant),
            positive_count=pos,
            negative_count=neg,
            neutral_count=neu,
            avg_confidence=round(avg_conf, 4),
            signal=signal,
            top_headlines=top_headlines,
        )

    def build_sentiment_series(self,
                                ticker: str,
                                start_date: date,
                                end_date: date) -> pd.DataFrame:
        """
        Construit une série temporelle de sentiment pour un ticker.
        Pour le backtest, fetch les news par période.
        """
        print(f"  📰 Fetch news {ticker}...")
        articles = self.fetcher.fetch_all(ticker, start_date, end_date)
        print(f"     {len(articles)} articles récupérés")

        if not articles:
            return pd.DataFrame()

        # Scorer tous les articles
        articles = self.score_articles(articles)

        # Agréger par jour
        sentiments = []
        current = start_date
        while current <= end_date:
            daily = self.compute_daily_sentiment(ticker, articles, current)
            sentiments.append({
                "date":            daily.date,
                "sentiment_score": daily.sentiment_score,
                "article_count":   daily.article_count,
                "positive_count":  daily.positive_count,
                "negative_count":  daily.negative_count,
                "sentiment_signal":daily.signal,
                "avg_confidence":  daily.avg_confidence,
            })
            current += timedelta(days=1)

        df = pd.DataFrame(sentiments).set_index("date")
        return df

    def get_current_sentiment(self, ticker: str) -> DailySentiment:
        """Sentiment actuel (dernières 72h) — pour le trading live."""
        today = date.today()
        articles = self.fetcher.fetch_all(ticker, today - timedelta(days=3), today)
        articles = self.score_articles(articles)
        return self.compute_daily_sentiment(ticker, articles, today)


# ─────────────────────────────────────────────
# INTÉGRATION DANS LE SCORE GLOBAL
# ─────────────────────────────────────────────

def annotate_with_sentiment(df: pd.DataFrame,
                             sentiment_df: pd.DataFrame) -> pd.DataFrame:
    """
    Fusionne le dataframe OHLCV+signaux avec les scores de sentiment.
    Le sentiment modifie le score global de deux façons:
      1. Confirmation: sentiment aligné avec signal technique → boost
      2. Contradiction: sentiment opposé au signal → réduction
    """
    df = df.copy()
    df["sentiment_score"]  = 0.0
    df["sentiment_signal"] = 0
    df["sentiment_boost"]  = 1.0
    df["final_signal"]     = df["signal"].copy()

    if sentiment_df.empty:
        return df

    for idx, row in df.iterrows():
        check_date = idx.date() if hasattr(idx, "date") else idx

        # Chercher le sentiment du jour (ou J-1 si weekend)
        sent_row = None
        for lag in [0, 1, 2]:
            lookup = check_date - timedelta(days=lag)
            if lookup in sentiment_df.index:
                sent_row = sentiment_df.loc[lookup]
                break

        if sent_row is None or sent_row["article_count"] == 0:
            continue

        s_score  = sent_row["sentiment_score"]
        s_signal = sent_row["sentiment_signal"]
        tech_signal = row["signal"]

        df.at[idx, "sentiment_score"]  = s_score
        df.at[idx, "sentiment_signal"] = s_signal

        # Calcul du boost
        if tech_signal == 1 and s_signal == 1:
            # Confirmation haussière → boost fort
            boost = 1.0 + min(abs(s_score) * 2, 0.5)
        elif tech_signal == 1 and s_signal == -1:
            # Contradiction → on annule le signal technique
            boost = 0.0
        elif tech_signal == 1 and s_signal == 0:
            # Sentiment neutre → légère réduction
            boost = 0.85
        elif tech_signal == -1 and s_signal == -1:
            # Confirmation baissière
            boost = 1.0 + min(abs(s_score) * 2, 0.5)
        elif tech_signal == -1 and s_signal == 1:
            # Contradiction baissière → on annule
            boost = 0.0
        else:
            boost = 1.0

        df.at[idx, "sentiment_boost"] = boost

        # Signal final: annuler si boost = 0
        if boost == 0.0:
            df.at[idx, "final_signal"] = 0
        else:
            df.at[idx, "final_signal"] = tech_signal

    return df


# ─────────────────────────────────────────────
# RAPPORT SENTIMENT
# ─────────────────────────────────────────────

def print_sentiment_report(ticker: str, daily: DailySentiment):
    """Affiche le rapport de sentiment pour un ticker."""
    icons = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}
    signal_icons = {1: "📈 BULLISH", -1: "📉 BEARISH", 0: "➡️  NEUTRE"}

    score_bar_len = int(abs(daily.sentiment_score) * 20)
    bar_char = "█" if daily.sentiment_score >= 0 else "░"
    bar = bar_char * score_bar_len

    print(f"\n  {ticker} — {daily.date}")
    print(f"  Score: {daily.sentiment_score:+.3f} {bar}")
    print(f"  Signal: {signal_icons.get(daily.signal, '?')}")
    print(f"  Articles: {daily.article_count} "
          f"(🟢{daily.positive_count} 🔴{daily.negative_count} 🟡{daily.neutral_count})")
    print(f"  Confiance moyenne: {daily.avg_confidence:.0%}")
    if daily.top_headlines:
        print(f"  Top headlines:")
        for h in daily.top_headlines:
            print(f"    · {h}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    TICKERS = ["AM.PA", "AIR.PA", "LMT", "RTX"]

    print("=" * 60)
    print("TRADING BOT - PHASE 2b: SENTIMENT FINBERT")
    print("=" * 60)

    # 1. Charger FinBERT
    print("\n🤖 Initialisation FinBERT...")
    finbert = FinBERTAnalyzer()
    finbert_ok = finbert.load()

    if not finbert_ok:
        print("\n❌ FinBERT non disponible — vérifiez l'installation:")
        print("   pip install transformers torch sentencepiece")
        return

    # 2. Initialiser fetcher + engine
    fetcher = NewsFetcher()
    engine  = SentimentEngine(finbert, fetcher)

    # 3. Sentiment actuel sur tous les tickers
    print("\n" + "=" * 60)
    print("📰 SENTIMENT ACTUEL (dernières 72h)")
    print("=" * 60)

    current_sentiments = {}
    for ticker in TICKERS:
        daily = engine.get_current_sentiment(ticker)
        current_sentiments[ticker] = daily
        print_sentiment_report(ticker, daily)

    # 4. Sauvegarder les sentiments actuels
    sentiment_data = []
    for ticker, daily in current_sentiments.items():
        sentiment_data.append({
            "ticker":          ticker,
            "date":            daily.date,
            "sentiment_score": daily.sentiment_score,
            "signal":          daily.signal,
            "article_count":   daily.article_count,
            "positive":        daily.positive_count,
            "negative":        daily.negative_count,
            "neutral":         daily.neutral_count,
            "confidence":      daily.avg_confidence,
            "top_headline":    daily.top_headlines[0] if daily.top_headlines else "",
        })

    pd.DataFrame(sentiment_data).to_csv("data/current_sentiment.csv", index=False)
    print(f"\n💾 Sentiment sauvegardé: data/current_sentiment.csv")

    # 5. Test d'intégration avec Phase 1
    print("\n" + "=" * 60)
    print("🔗 TEST INTÉGRATION PHASE 1 + SENTIMENT")
    print("=" * 60)

    try:
        import sys
        sys.path.insert(0, ".")
        from phase1_technical import TradingConfig, MarketDataFetcher, TechnicalAnalyzer

        config   = TradingConfig()
        mfetcher = MarketDataFetcher()
        analyzer = TechnicalAnalyzer(config)

        # Test sur le ticker avec le plus de news
        test_ticker = max(current_sentiments,
                         key=lambda t: current_sentiments[t].article_count)
        print(f"\n  Test sur {test_ticker} "
              f"({current_sentiments[test_ticker].article_count} articles récents)")

        df = mfetcher.fetch(test_ticker, "1d")
        if df is not None:
            df = analyzer.compute_indicators(df)
            df = analyzer.generate_signals(df)

            # Construire série de sentiment (30 derniers jours pour le test)
            end_d   = date.today()
            start_d = end_d - timedelta(days=30)
            sent_series = engine.build_sentiment_series(test_ticker, start_d, end_d)

            # Intégrer
            df_enriched = annotate_with_sentiment(df.tail(30), sent_series)

            # Afficher les signaux modifiés
            modified = df_enriched[
                df_enriched["signal"] != df_enriched["final_signal"]
            ]
            if not modified.empty:
                print(f"\n  ⚡ Signaux modifiés par le sentiment ({len(modified)}):")
                for idx, row in modified.iterrows():
                    change = f"{int(row['signal'])} → {int(row['final_signal'])}"
                    print(f"    {idx.date()} | {change} | "
                          f"Sentiment: {row['sentiment_score']:+.3f} | "
                          f"Boost: {row['sentiment_boost']:.2f}x")
            else:
                print(f"\n  ℹ️  Aucun signal technique modifié sur les 30 derniers jours")
                print(f"     (normal si peu de signaux techniques récents)")

    except Exception as e:
        print(f"  ⚠️  Test intégration: {e}")

    print("\n" + "=" * 60)
    print("Phase 2b terminée ✅")
    print("\nPour utiliser dans le pipeline global:")
    print("  from phase2b_sentiment import FinBERTAnalyzer, NewsFetcher, SentimentEngine")
    print("  from phase2b_sentiment import annotate_with_sentiment")
    print("=" * 60)


if __name__ == "__main__":
    main()
