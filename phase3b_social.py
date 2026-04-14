"""
BUBO - Phase 3b: Social Sentiment Analysis (Reddit + Stocktwits)
=================================================================
Analyse le sentiment retail sur Reddit et Stocktwits pour détecter
les mouvements de foule avant qu'ils n'impactent le prix.

100% GRATUIT — aucune API payante requise.

Dépendances:
    pip install praw transformers torch

Configuration Reddit (GRATUIT, 2 minutes):
    1. Aller sur https://www.reddit.com/prefs/apps
    2. Cliquer "create another app..." en bas
    3. Nom: Bubo | Type: script | Redirect URI: http://localhost:8080
    4. Copier le client_id (texte sous le nom de l'app) et le secret
    5. Remplir social_config.json

Stocktwits: aucune clé nécessaire (API publique).

Intégration bubo.py:
    from phase3b_social import SocialPipeline
    social = SocialPipeline()
    result = social.analyze_ticker("RTX")
"""

import json
import os
import re
import time
import html
import hashlib
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path
import urllib.request
import urllib.parse

import pandas as pd

# ─────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────

@dataclass
class SocialConfig:
    """Configuration pour la collecte sociale."""
    reddit_enabled: bool = False
    # Reddit API (GRATUIT — https://www.reddit.com/prefs/apps)
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "Bubo/1.0 by u/YOUR_USERNAME"
    stocktwits_base_url: str = "https://api.stocktwits.com/api/2"

    # Subreddits à surveiller par catégorie
    subreddits_general: list = field(default_factory=lambda: [
        "wallstreetbets", "stocks", "investing", "stockmarket",
        "options", "SecurityAnalysis", "ValueInvesting"
    ])
    subreddits_defense: list = field(default_factory=lambda: [
        "defense", "MilitaryProcurement", "LessCredibleDefence",
        "CredibleDefense", "WarCollege"
    ])
    subreddits_europe: list = field(default_factory=lambda: [
        "vosfinances", "EuropeanStocks", "eupersonalfinance",
        "europe", "geopolitics"
    ])

    # Paramètres d'analyse
    lookback_hours: int = 96          # 4 jours — défense = niche, besoin de plus de données
    min_score_reddit: int = 2         # Abaissé pour niche stocks
    min_engagement_stocktwits: int = 1
    max_posts_per_source: int = 100
    bot_filter_enabled: bool = True
    cache_ttl_minutes: int = 30

    # Poids dans le score final
    reddit_weight: float = 0.60
    stocktwits_weight: float = 0.40

    # Seuils
    bullish_threshold: float = 0.12
    bearish_threshold: float = -0.12
    min_posts_for_signal: int = 2     # Abaissé — défense = niche, peu de posts


# Mapping ticker → termes de recherche
TICKER_SEARCH_MAP = {
    "AM.PA": {
        "keywords_precise": ["Dassault Aviation", "Dassault", "AM.PA"],
        "keywords_broad": ["Rafale", "French defense", "défense française",
                           "FCAS", "Neuron drone", "nEUROn"],
        # $AM sur Stocktwits = Antero Midstream (US), PAS Dassault !
        # DUAVF = Dassault Aviation OTC mais quasi inactif
        "stocktwits_symbols": ["DUAVF"],
        "relevance_keywords": ["dassault", "rafale", "aviation", "defense", "defence"],
        "sector": "defense_eu"
    },
    "AIR.PA": {
        "keywords_precise": ["Airbus", "AIR.PA"],
        "keywords_broad": ["A350", "A320neo", "A400M", "Eurofighter",
                           "European defense", "SCAF"],
        "stocktwits_symbols": ["EADSY"],
        "relevance_keywords": ["airbus", "eadsy", "a350", "a320", "a380", "a400m",
                               "defense", "defence", "aviation"],
        "sector": "defense_eu"
    },
    "LMT": {
        "keywords_precise": ["Lockheed Martin", "Lockheed", "LMT"],
        "keywords_broad": ["F-35", "F35", "THAAD", "Sikorsky",
                           "defense contract", "Pentagon budget"],
        "stocktwits_symbols": ["LMT"],
        "relevance_keywords": ["lockheed", "lmt", "f-35", "f35", "thaad",
                               "sikorsky", "defense", "defence", "pentagon",
                               "military", "contract"],
        "sector": "defense_us"
    },
    "RTX": {
        "keywords_precise": ["Raytheon", "RTX"],
        "keywords_broad": ["Pratt Whitney", "Patriot missile", "Collins Aerospace",
                           "defense stocks", "military spending"],
        "stocktwits_symbols": ["RTX"],
        "relevance_keywords": ["raytheon", "rtx", "pratt", "whitney", "patriot",
                               "collins", "defense", "defence", "military",
                               "missile"],
        "sector": "defense_us"
    },
}


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    hints = ("Ã", "â€™", "â€œ", "â€", "Â", "ðŸ")
    return any(h in text for h in hints)


def _clean_text(value: str, max_len: int = 0) -> str:
    text = html.unescape(str(value or ""))
    if _looks_mojibake(text):
        try:
            repaired = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
            if repaired:
                text = repaired
        except Exception:
            pass
    text = re.sub(r"\s+", " ", text).strip()
    if max_len and max_len > 0:
        text = text[:max_len]
    return text


def generate_config_template(path: str = "social_config.json"):
    """Génère un fichier template pour les clés API."""
    template = {
        "reddit_client_id": "",
        "reddit_client_secret": "",
        "reddit_user_agent": "Bubo/1.0 by u/YOUR_REDDIT_USERNAME",
        "_setup_reddit": [
            "GRATUIT — 2 minutes:",
            "1. Aller sur https://www.reddit.com/prefs/apps",
            "2. Cliquer 'create another app...' en bas de la page",
            "3. Nom: Bubo | Type: script | Redirect: http://localhost:8080",
            "4. Cliquer 'create app'",
            "5. client_id = le texte sous le nom de l'app (genre 'aB3cD4eFgH...')",
            "6. client_secret = le champ 'secret'",
            "7. Remplacer YOUR_REDDIT_USERNAME par votre pseudo Reddit"
        ],
        "_note": "Stocktwits ne nécessite aucune clé — il fonctionne directement."
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(template, f, indent=2, ensure_ascii=False)
    print(f"📝 Template créé: {path}")
    print("   Reddit = GRATUIT. Suivez les instructions dans le fichier.")


def load_config(path: str = "social_config.json") -> SocialConfig:
    """Charge la config depuis un fichier JSON."""
    cfg = SocialConfig()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if k.startswith("_"):
                continue
            if hasattr(cfg, k):
                setattr(cfg, k, v)

    # Env overrides so Docker Compose can configure API credentials directly.
    env_map = {
        "reddit_client_id": ["BUBO_REDDIT_CLIENT_ID", "REDDIT_CLIENT_ID"],
        "reddit_client_secret": ["BUBO_REDDIT_CLIENT_SECRET", "REDDIT_CLIENT_SECRET"],
        "reddit_user_agent": ["BUBO_REDDIT_USER_AGENT", "REDDIT_USER_AGENT"],
        "stocktwits_base_url": ["BUBO_STOCKTWITS_BASE_URL", "STOCKTWITS_BASE_URL"],
    }
    for attr, names in env_map.items():
        for name in names:
            val = os.environ.get(name, "")
            if str(val).strip():
                setattr(cfg, attr, str(val).strip())
                break

    return cfg


# ─────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────

class SocialCache:
    def __init__(self, cache_dir: str = "data/social_cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, source: str, ticker: str) -> Path:
        key = f"{source}_{ticker}"
        h = hashlib.md5(key.encode()).hexdigest()[:12]
        return self.cache_dir / f"{key}_{h}.json"

    def get(self, source: str, ticker: str, ttl_minutes: int = 30) -> Optional[list]:
        p = self._path(source, ticker)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(data.get("cached_at", "2000-01-01"))
            if datetime.now() - cached_at > timedelta(minutes=ttl_minutes):
                return None
            return data.get("posts", [])
        except Exception:
            return None

    def set(self, source: str, ticker: str, posts: list):
        p = self._path(source, ticker)
        data = {"cached_at": datetime.now().isoformat(), "posts": posts}
        p.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")


# ─────────────────────────────────────────────
# Reddit Fetcher
# ─────────────────────────────────────────────

class RedditFetcher:
    def __init__(self, cfg: SocialConfig):
        self.cfg = cfg
        self.reddit = None
        self._init_reddit()

    def _init_reddit(self):
        if not self.cfg.reddit_client_id or not self.cfg.reddit_client_secret:
            print("  ⚠️  Reddit API non configurée — mode JSON public (limité)")
            print("      → Configurez social_config.json (c'est GRATUIT)")
            return
        try:
            import praw
            self.reddit = praw.Reddit(
                client_id=self.cfg.reddit_client_id,
                client_secret=self.cfg.reddit_client_secret,
                user_agent=self.cfg.reddit_user_agent
            )
            _ = self.reddit.subreddit("stocks").display_name
            print("  ✅ Reddit API connectée (PRAW)")
        except ImportError:
            print("  ⚠️  praw non installé — pip install praw")
            self.reddit = None
        except Exception as e:
            print(f"  ⚠️  Reddit API erreur: {e}")
            self.reddit = None

    def fetch(self, ticker: str) -> list:
        search_info = TICKER_SEARCH_MAP.get(ticker, {})
        kw_precise = search_info.get("keywords_precise", [ticker])
        kw_broad = search_info.get("keywords_broad", [])
        sector = search_info.get("sector", "general")

        subs = list(self.cfg.subreddits_general)
        if "eu" in sector:
            subs.extend(self.cfg.subreddits_europe)
        subs.extend(self.cfg.subreddits_defense)
        subs = list(dict.fromkeys(subs))

        if self.reddit:
            return self._fetch_praw(kw_precise, kw_broad, subs)
        else:
            return self._fetch_public_json(kw_precise, kw_broad, subs)

    def _fetch_praw(self, kw_precise: list, kw_broad: list, subreddits: list) -> list:
        posts = []
        cutoff = datetime.now() - timedelta(hours=self.cfg.lookback_hours)
        query_precise = " OR ".join(f'"{kw}"' for kw in kw_precise)
        query_broad = " OR ".join(f'"{kw}"' for kw in kw_broad[:4])

        search_tasks = [(sub, query_precise) for sub in subreddits]
        for sub in self.cfg.subreddits_defense:
            if query_broad:
                search_tasks.append((sub, query_broad))

        seen_ids = set()
        auth_failed = False
        for sub_name, query in search_tasks:
            if auth_failed:
                break
            try:
                subreddit = self.reddit.subreddit(sub_name)
                for submission in subreddit.search(query, sort="new",
                                                   time_filter="week", limit=30):
                    if submission.id in seen_ids:
                        continue
                    seen_ids.add(submission.id)

                    created = datetime.fromtimestamp(submission.created_utc)
                    if created < cutoff:
                        continue
                    if submission.score < self.cfg.min_score_reddit:
                        continue

                    post = {
                        "source": "reddit",
                        "subreddit": sub_name,
                        "title": _clean_text(submission.title, 300),
                        "text": _clean_text(submission.selftext or "", 500),
                        "score": submission.score,
                        "num_comments": submission.num_comments,
                        "created_at": created.isoformat(),
                        "url": f"https://reddit.com{submission.permalink}",
                        "author": str(submission.author),
                        "engagement": submission.score + submission.num_comments * 2
                    }
                    if self.cfg.bot_filter_enabled and self._is_bot(post):
                        continue
                    posts.append(post)

                # Commentaires dans les posts hot
                for submission in subreddit.hot(limit=5):
                    if submission.id in seen_ids:
                        continue
                    created_s = datetime.fromtimestamp(submission.created_utc)
                    if created_s < cutoff:
                        continue
                    try:
                        submission.comments.replace_more(limit=0)
                        all_kws = kw_precise + kw_broad
                        for comment in submission.comments[:30]:
                            ctext = comment.body or ""
                            if any(kw.lower() in ctext.lower() for kw in all_kws):
                                if comment.score >= self.cfg.min_score_reddit:
                                    posts.append({
                                        "source": "reddit_comment",
                                        "subreddit": sub_name,
                                        "title": _clean_text(f"Re: {submission.title[:80]}", 120),
                                        "text": _clean_text(ctext, 500),
                                        "score": comment.score,
                                        "num_comments": 0,
                                        "created_at": datetime.fromtimestamp(
                                            comment.created_utc).isoformat(),
                                        "url": f"https://reddit.com{comment.permalink}",
                                        "author": str(comment.author),
                                        "engagement": comment.score
                                    })
                    except Exception:
                        pass

                time.sleep(0.3)

            except Exception as e:
                err_str = str(e)
                if "401" in err_str or "403" in err_str or "Unauthorized" in err_str:
                    if not auth_failed:
                        print(f"    ⚠️  Reddit API: accès refusé (demande en attente?)")
                        print(f"        → Bascule sur Stocktwits uniquement")
                        auth_failed = True
                    continue
                else:
                    print(f"    ⚠️  r/{sub_name}: {e}")
                continue

            if len(posts) >= self.cfg.max_posts_per_source:
                break

        return posts[:self.cfg.max_posts_per_source]

    def _fetch_public_json(self, kw_precise: list, kw_broad: list, subreddits: list) -> list:
        posts = []
        cutoff = datetime.now() - timedelta(hours=self.cfg.lookback_hours)
        seen_titles = set()
        all_keywords = kw_precise + kw_broad[:2]

        for sub_name in subreddits[:6]:
            for kw in all_keywords[:3]:
                try:
                    query = urllib.parse.quote(kw)
                    url = (f"https://www.reddit.com/r/{sub_name}/search.json"
                           f"?q={query}&sort=new&restrict_sr=1&limit=25&t=week")
                    req = urllib.request.Request(url, headers={
                        "User-Agent": self.cfg.reddit_user_agent,
                        "Accept": "application/json"
                    })
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read().decode("utf-8", errors="replace"))

                    for child in data.get("data", {}).get("children", []):
                        d = child.get("data", {})
                        title = d.get("title", "")
                        if title[:50] in seen_titles:
                            continue
                        seen_titles.add(title[:50])

                        created = datetime.fromtimestamp(d.get("created_utc", 0))
                        if created < cutoff:
                            continue
                        score = d.get("score", 0)
                        if score < self.cfg.min_score_reddit:
                            continue

                        posts.append({
                            "source": "reddit",
                            "subreddit": sub_name,
                            "title": _clean_text(title, 300),
                            "text": _clean_text((d.get("selftext", "") or ""), 500),
                            "score": score,
                            "num_comments": d.get("num_comments", 0),
                            "created_at": created.isoformat(),
                            "url": f"https://reddit.com{d.get('permalink', '')}",
                            "author": d.get("author", "unknown"),
                            "engagement": score + d.get("num_comments", 0) * 2
                        })

                    time.sleep(1.5)

                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        print(f"    ⚠️  Reddit rate limit — pause 5s")
                        time.sleep(5)
                    continue
                except Exception:
                    continue

        return posts[:self.cfg.max_posts_per_source]

    def _is_bot(self, post: dict) -> bool:
        author = post.get("author", "").lower()
        text = (post.get("title", "") + " " + post.get("text", "")).lower()
        if any(p in author for p in ["bot", "auto", "_shill", "promo", "alertbot"]):
            return True
        spam = [
            r"(?:join|subscribe).*(?:discord|telegram|channel)",
            r"(?:guaranteed|100%).*(?:profit|return|gain)",
            r"(?:sign up|register).*(?:free|bonus)",
        ]
        return any(re.search(p, text) for p in spam)


# ─────────────────────────────────────────────
# Stocktwits Fetcher (GRATUIT, sans clé API)
# ─────────────────────────────────────────────

class StocktwitsFetcher:
    """
    API 100% publique, pas de clé nécessaire.
    Limite: 200 requêtes/heure par IP.
    """

    DEFAULT_BASE_URL = "https://api.stocktwits.com/api/2"

    def __init__(self, cfg: SocialConfig):
        self.cfg = cfg
        self.base_url = str(getattr(cfg, "stocktwits_base_url", "") or self.DEFAULT_BASE_URL).strip().rstrip("/")
        if not self.base_url:
            self.base_url = self.DEFAULT_BASE_URL
        print(f"  ✅ Stocktwits (gratuit, aucune clé requise) [{self.base_url}]")

    def fetch(self, ticker: str) -> list:
        search_info = TICKER_SEARCH_MAP.get(ticker, {})
        symbols = search_info.get("stocktwits_symbols", [ticker.replace(".PA", "")])
        relevance_kws = search_info.get("relevance_keywords", [])

        posts = []
        cutoff = datetime.now() - timedelta(hours=self.cfg.lookback_hours)

        # Fetch chaque symbole
        for symbol in symbols:
            stream_posts = self._fetch_symbol_stream(symbol, cutoff)
            posts.extend(stream_posts)

        # Si peu de résultats, recherche par nom
        if len(posts) < 3:
            kw = search_info.get("keywords_precise", [ticker])
            posts.extend(self._fetch_search(kw[:2], cutoff))

        # Filtre de pertinence — virer les posts qui ne parlent pas principalement de notre ticker
        if relevance_kws:
            filtered = []
            for p in posts:
                text = (p.get("text", "") + " " + p.get("title", "")).lower()

                # Trouver tous les cashtags dans le texte ($XXX)
                cashtags_found = re.findall(r'\$([a-zA-Z]{1,6})', text)

                if cashtags_found:
                    # Le PREMIER cashtag = le sujet principal du post
                    first_tag = cashtags_found[0].upper()
                    our_tags = {s.upper() for s in symbols}

                    if first_tag in our_tags:
                        # Notre ticker est le sujet principal → garder
                        filtered.append(p)
                    elif any(tag.upper() in our_tags for tag in cashtags_found):
                        # Notre ticker est mentionné mais pas en premier
                        # → garder seulement si peu d'autres tickers (pas un post "liste")
                        if len(cashtags_found) <= 4:
                            filtered.append(p)
                        # Sinon c'est un post spam "$LMT $RTX $NOC $BA $GD..." → virer
                    # Si notre ticker n'apparaît même pas → virer
                else:
                    # Pas de cashtag → vérifier les keywords de pertinence
                    if any(kw in text for kw in relevance_kws):
                        filtered.append(p)

            posts = filtered

        # Dédupliquer
        seen = set()
        unique = []
        for p in posts:
            key = p.get("text", "")[:60]
            if key not in seen:
                seen.add(key)
                unique.append(p)

        return unique[:self.cfg.max_posts_per_source]

    def _fetch_symbol_stream(self, symbol: str, cutoff: datetime) -> list:
        posts = []
        try:
            url = f"{self.base_url}/streams/symbol/{symbol}.json"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Bubo/1.0",
                "Accept": "application/json"
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            for msg in data.get("messages", []):
                created_str = msg.get("created_at", "")
                try:
                    created = datetime.strptime(created_str, "%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    try:
                        created = datetime.fromisoformat(
                            created_str.replace("Z", "+00:00")).replace(tzinfo=None)
                    except Exception:
                        created = datetime.now() - timedelta(hours=12)

                if created < cutoff:
                    continue

                # Sentiment natif Stocktwits (users votent bullish/bearish)
                st_sent = msg.get("entities", {}).get("sentiment", {})
                native = st_sent.get("basic", "neutral") if st_sent else "neutral"

                likes = msg.get("likes", {})
                like_count = likes.get("total", 0) if isinstance(likes, dict) else 0

                post = {
                    "source": "stocktwits",
                    "title": "",
                    "text": _clean_text(msg.get("body", ""), 500),
                    "score": like_count,
                    "num_comments": msg.get("conversation", {}).get("replies", 0)
                        if msg.get("conversation") else 0,
                    "created_at": created.isoformat(),
                    "url": f"https://stocktwits.com/{msg.get('user', {}).get('username', '')}/message/{msg.get('id', '')}",
                    "author": msg.get("user", {}).get("username", "unknown"),
                    "author_followers": msg.get("user", {}).get("followers", 0),
                    "engagement": like_count + 1,
                    "native_sentiment": native,
                    "symbol": symbol
                }

                if self.cfg.bot_filter_enabled and self._is_bot(post):
                    continue
                posts.append(post)

        except urllib.error.HTTPError as e:
            if e.code == 404:
                pass
            elif e.code == 429:
                print(f"    ⚠️  Stocktwits rate limit — pause")
                time.sleep(5)
            else:
                print(f"    ⚠️  Stocktwits {symbol}: HTTP {e.code}")
        except Exception as e:
            print(f"    ⚠️  Stocktwits {symbol}: {e}")

        return posts

    def _fetch_search(self, keywords: list, cutoff: datetime) -> list:
        posts = []
        for kw in keywords:
            try:
                encoded = urllib.parse.quote(kw)
                url = f"{self.base_url}/search/symbols.json?q={encoded}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Bubo/1.0",
                    "Accept": "application/json"
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="replace"))

                for r in data.get("results", [])[:2]:
                    sym = r.get("symbol", "")
                    if sym:
                        posts.extend(self._fetch_symbol_stream(sym, cutoff))
                time.sleep(0.5)
            except Exception:
                continue
        return posts

    def _is_bot(self, post: dict) -> bool:
        text = post.get("text", "").lower()
        spam = [
            r"(?:join|subscribe).*(?:discord|telegram)",
            r"(?:guaranteed|100%).*profit",
            r"(?:sign up|register).*free",
            r"(?:tap|follow|click).*@\w+",          # "tap @NasdaqKnowledge"
            r"your biggest payday",
            r"(?:🚨|⚠️).*(?:🚨|⚠️)",               # double alert emoji = spam
            r"(?:buy|sell) signal",
        ]
        return any(re.search(p, text) for p in spam)


# ─────────────────────────────────────────────
# Social Sentiment Engine
# ─────────────────────────────────────────────

class SocialSentimentEngine:
    def __init__(self, cfg: SocialConfig, finbert_analyzer=None):
        self.cfg = cfg
        self.finbert = finbert_analyzer
        self.tokenizer = None
        self.model = None
        self.device = "cpu"
        self._init_finbert()

    def _init_finbert(self):
        if self.finbert is not None:
            print("  ✅ FinBERT partagé avec Phase 2b")
            return
        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch

            model_name = "ProsusAI/finbert"
            print(f"  ⏳ Chargement {model_name}...")
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
            self.model.to(self.device)
            self.model.eval()
            self.finbert = self
            print(f"  ✅ FinBERT chargé sur {self.device}")
        except ImportError:
            print("  ⚠️  transformers/torch non installé — sentiment par heuristiques")
            self.finbert = None

    def analyze_batch(self, texts: list) -> list:
        if not texts:
            return []
        if self.finbert is None or self.tokenizer is None:
            return [self._heuristic(t) for t in texts]

        import torch
        batch_size = 16 if self.device == "cuda" else 4
        results = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            inputs = self.tokenizer(batch, return_tensors="pt", truncation=True,
                                    max_length=512, padding=True)
            inputs = {k: v.to(self.device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self.model(**inputs)
                probs = torch.softmax(outputs.logits, dim=1).cpu().numpy()

            for p in probs:
                labels = ["positive", "negative", "neutral"]
                idx = p.argmax()
                results.append({
                    "positive": float(p[0]),
                    "negative": float(p[1]),
                    "neutral": float(p[2]),
                    "label": labels[idx],
                    "confidence": float(p[idx]),
                    "net_score": float(p[0] - p[1])
                })
        return results

    def _heuristic(self, text: str) -> dict:
        text_lower = text.lower()
        bullish = ["buy", "bull", "long", "moon", "calls", "undervalued", "breakout",
                   "upgrade", "beat", "strong", "growth", "record", "surge", "soar",
                   "acheter", "hausse", "contrat", "commande", "bullish"]
        bearish = ["sell", "bear", "short", "puts", "overvalued", "breakdown",
                   "downgrade", "miss", "weak", "decline", "crash", "drop", "fall",
                   "vendre", "baisse", "perte", "annulation", "bearish"]

        bull_c = sum(1 for w in bullish if w in text_lower)
        bear_c = sum(1 for w in bearish if w in text_lower)
        total = bull_c + bear_c
        if total == 0:
            return {"positive": 0.33, "negative": 0.33, "neutral": 0.34,
                    "label": "neutral", "confidence": 0.34, "net_score": 0.0}
        pos = bull_c / (total + 1)
        neg = bear_c / (total + 1)
        label = "positive" if pos > neg else ("negative" if neg > pos else "neutral")
        return {"positive": pos, "negative": neg, "neutral": max(0, 1 - pos - neg),
                "label": label, "confidence": max(pos, neg), "net_score": pos - neg}


# ─────────────────────────────────────────────
# Score Aggregator
# ─────────────────────────────────────────────

class SocialScorer:
    def __init__(self, cfg: SocialConfig):
        self.cfg = cfg

    def compute_score(self, posts: list, sentiments: list) -> dict:
        if not posts or len(posts) < self.cfg.min_posts_for_signal:
            return {
                "social_score": 50.0,
                "social_label": f"NEUTRAL ({len(posts)} post{'s' if len(posts) != 1 else ''}, min={self.cfg.min_posts_for_signal})",
                "mention_count": len(posts),
                "avg_sentiment": 0.0,
                "weighted_sentiment": 0.0,
                "volume_spike": False,
                "top_posts": [],
                "source_breakdown": {},
                "confidence": 0.0
            }

        now = datetime.now()
        weighted_scores = []
        total_weight = 0
        reddit_scores = []
        stocktwits_scores = []

        for post, sent in zip(posts, sentiments):
            eng = post.get("engagement", 1)
            eng_weight = min(eng / 30, 3.0)

            try:
                created = datetime.fromisoformat(post["created_at"])
            except (ValueError, TypeError):
                created = now - timedelta(hours=24)
            age_h = max(0.1, (now - created).total_seconds() / 3600)
            freshness = 2 ** (-age_h / 12)

            cred = 1.0
            followers = post.get("author_followers", 0)
            if followers > 10000:
                cred = 1.5
            elif followers > 1000:
                cred = 1.2

            # Bonus Stocktwits native sentiment
            native = post.get("native_sentiment", "neutral")
            native_bonus = 0.0
            if native == "Bullish":
                native_bonus = 0.1
            elif native == "Bearish":
                native_bonus = -0.1

            final_sent = sent["net_score"] + native_bonus
            weight = eng_weight * freshness * cred
            weighted_scores.append((final_sent, weight))
            total_weight += weight

            if post.get("source", "").startswith("reddit"):
                reddit_scores.append((final_sent, weight))
            elif post.get("source") == "stocktwits":
                stocktwits_scores.append((final_sent, weight))

        weighted_avg = sum(s * w for s, w in weighted_scores) / total_weight if total_weight > 0 else 0
        avg_sent = sum(s["net_score"] for s in sentiments) / len(sentiments)

        volume_spike = len(posts) > 15
        social_score = 50 + weighted_avg * 50
        if volume_spike:
            social_score = min(100, social_score * 1.15) if social_score > 50 else max(0, social_score * 0.85)

        concordance = 1 - (sum(1 for s in sentiments if s["label"] == "neutral") / len(sentiments))
        confidence = min(1.0, (len(posts) / 10) * concordance)

        if social_score >= 60:
            label = "BULLISH 🟢"
        elif social_score <= 40:
            label = "BEARISH 🔴"
        else:
            label = "NEUTRAL ⚪"
        if volume_spike:
            label += " 🔥 SPIKE"

        # Top posts
        pairs = sorted(zip(posts, sentiments),
                       key=lambda x: x[0].get("engagement", 0) * abs(x[1]["net_score"]),
                       reverse=True)
        top_posts = []
        for p, s in pairs[:5]:
            text = p.get("title") or p.get("text", "")
            native = p.get("native_sentiment", "")
            native_tag = f" [{native}]" if native and native != "neutral" else ""
            src = f"r/{p['subreddit']}" if p.get("subreddit") else p.get("source", "?")
            top_posts.append({
                "text": text[:120], "source": src,
                "subreddit": p.get("subreddit", ""),
                "sentiment": s["label"],
                "net_score": round(s["net_score"], 3),
                "engagement": p.get("engagement", 0),
                "native": native_tag,
                "url": p.get("url", "")
            })

        source_breakdown = {}
        if reddit_scores:
            r_avg = sum(s * w for s, w in reddit_scores) / sum(w for _, w in reddit_scores)
            source_breakdown["reddit"] = {
                "count": len(reddit_scores),
                "avg_sentiment": round(r_avg, 3),
                "score": round(50 + r_avg * 50, 1)
            }
        if stocktwits_scores:
            st_avg = sum(s * w for s, w in stocktwits_scores) / sum(w for _, w in stocktwits_scores)
            source_breakdown["stocktwits"] = {
                "count": len(stocktwits_scores),
                "avg_sentiment": round(st_avg, 3),
                "score": round(50 + st_avg * 50, 1)
            }

        return {
            "social_score": round(social_score, 1),
            "social_label": label,
            "mention_count": len(posts),
            "avg_sentiment": round(avg_sent, 3),
            "weighted_sentiment": round(weighted_avg, 3),
            "volume_spike": volume_spike,
            "top_posts": top_posts,
            "source_breakdown": source_breakdown,
            "confidence": round(confidence, 2)
        }


# ─────────────────────────────────────────────
# Pipeline Principal
# ─────────────────────────────────────────────

class SocialPipeline:
    def __init__(self, cfg: SocialConfig = None, finbert_analyzer=None):
        self.cfg = cfg or SocialConfig()
        self.cache = SocialCache()

        print("\n🌐 Initialisation Social Pipeline...")
        self.reddit = RedditFetcher(self.cfg) if self.cfg.reddit_enabled else None
        if not self.cfg.reddit_enabled:
            print("  ℹ️  Reddit désactivé — Stocktwits uniquement")
        self.stocktwits = StocktwitsFetcher(self.cfg)
        self.sentiment = SocialSentimentEngine(self.cfg, finbert_analyzer)
        self.scorer = SocialScorer(self.cfg)

    def analyze_ticker(self, ticker: str) -> dict:
        print(f"\n  📱 Social: {ticker}")
        all_posts = []

        # Reddit (optionnel, désactivé par défaut)
        if self.cfg.reddit_enabled and self.reddit is not None:
            cached = self.cache.get("reddit", ticker, self.cfg.cache_ttl_minutes)
            if cached is not None:
                print(f"    📦 Reddit: {len(cached)} posts (cache)")
                all_posts.extend(cached)
            else:
                reddit_posts = self.reddit.fetch(ticker)
                print(f"    🟠 Reddit: {len(reddit_posts)} posts")
                self.cache.set("reddit", ticker, reddit_posts)
                all_posts.extend(reddit_posts)

        # Stocktwits
        cached = self.cache.get("stocktwits", ticker, self.cfg.cache_ttl_minutes)
        if cached is not None:
            print(f"    📦 Stocktwits: {len(cached)} messages (cache)")
            all_posts.extend(cached)
        else:
            st_posts = self.stocktwits.fetch(ticker)
            print(f"    💬 Stocktwits: {len(st_posts)} messages")
            self.cache.set("stocktwits", ticker, st_posts)
            all_posts.extend(st_posts)

        # Sentiment
        if all_posts:
            texts = [(p.get("title", "") + " " + p.get("text", "")).strip()[:512]
                     for p in all_posts]
            sentiments = self.sentiment.analyze_batch(texts)
        else:
            sentiments = []

        result = self.scorer.compute_score(all_posts, sentiments)
        result["ticker"] = ticker
        return result

    def analyze_all(self, tickers: list) -> dict:
        return {t: self.analyze_ticker(t) for t in tickers}


def get_social_score(ticker: str, pipeline: SocialPipeline = None) -> dict:
    """Point d'entrée pour bubo.py."""
    if pipeline is None:
        pipeline = SocialPipeline()
    return pipeline.analyze_ticker(ticker)


# ─────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────

def display_dashboard(results: dict):
    print("\n" + "=" * 70)
    print("  🌐 BUBO — SOCIAL SENTIMENT DASHBOARD")
    print("=" * 70)

    for ticker, data in results.items():
        score = data["social_score"]
        label = data["social_label"]
        mentions = data["mention_count"]
        confidence = data.get("confidence", 0)

        bar_len = 30
        filled = int((score / 100) * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        print(f"\n{'─' * 70}")
        print(f"  {ticker}")
        print(f"  {label}")
        print(f"  Score:  [{bar}] {score:.1f}/100")
        print(f"  Mentions: {mentions} | "
              f"Sentiment: {data['weighted_sentiment']:+.3f} | "
              f"Confiance: {confidence:.0%}")

        breakdown = data.get("source_breakdown", {})
        if breakdown:
            parts = [f"{src}: {info['count']} ({info['score']:.1f})"
                     for src, info in breakdown.items()]
            print(f"  Sources: {' | '.join(parts)}")

        top = data.get("top_posts", [])
        if top:
            print(f"  Top posts:")
            for i, tp in enumerate(top[:3], 1):
                icon = {"positive": "🟢", "negative": "🔴"}.get(tp["sentiment"], "⚪")
                print(f"    {i}. {icon} [{tp['source']}]{tp.get('native', '')} "
                      f"{tp['text'][:85]}")
                print(f"       eng: {tp['engagement']} | sent: {tp['net_score']:+.3f}")

    print(f"\n{'═' * 70}")
    print(f"  {'Ticker':<10} {'Score':>7} {'Label':<22} {'Posts':>5} {'Conf':>6}")
    print(f"  {'─' * 58}")
    for ticker, data in results.items():
        label_short = data['social_label'][:20]
        print(f"  {ticker:<10} {data['social_score']:>6.1f} "
              f"{label_short:<22} {data['mention_count']:>5} "
              f"{data.get('confidence', 0):>5.0%}")
    print("═" * 70)


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  BUBO — Phase 3b: Social Sentiment (Reddit + Stocktwits)")
    print("  100% gratuit — aucune API payante")
    print("=" * 70)

    cfg = load_config()

    if not os.path.exists("social_config.json"):
        print("\n⚠️  social_config.json non trouvé.")
        generate_config_template()
        print()

    if cfg.reddit_enabled and not cfg.reddit_client_id:
        print("⚠️  Reddit activé sans credentials: fallback public (instable)")
    elif not cfg.reddit_enabled:
        print("ℹ️  Reddit désactivé: pipeline social basé sur Stocktwits uniquement")

    tickers = ["AM.PA", "AIR.PA", "LMT", "RTX"]

    pipeline = SocialPipeline(cfg)
    results = pipeline.analyze_all(tickers)
    display_dashboard(results)

    # Export CSV
    os.makedirs("data", exist_ok=True)
    rows = [{
        "ticker": t, "social_score": d["social_score"],
        "social_label": d["social_label"], "mention_count": d["mention_count"],
        "weighted_sentiment": d["weighted_sentiment"],
        "volume_spike": d["volume_spike"],
        "confidence": d.get("confidence", 0),
        "timestamp": datetime.now().isoformat()
    } for t, d in results.items()]

    df = pd.DataFrame(rows)
    csv_path = "data/social_signals.csv"
    if os.path.exists(csv_path):
        existing = pd.read_csv(csv_path)
        df = pd.concat([existing, df], ignore_index=True)
    df.to_csv(csv_path, index=False)
    print(f"\n📊 Export: {csv_path}")

    return results


if __name__ == "__main__":
    main()
