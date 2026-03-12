# BUBO

BUBO est un moteur de trading moyen terme avec prescreening d'univers, scoring multi-signaux et mode paper trading.
Le mode de deploiement cible est Docker (NAS/serveur) avec interface web sur le port `7654`.
L'image Docker est basee sur `python:3.12-slim` (necessaire notamment pour `pandas-ta`).
`pandas-ta` est epingle sur `0.4.71b0` (version beta compatible 3.12+).

Ce README est la source de verite pour l'execution.
Les fichiers Compose de reference sont:
- `docker-compose.yml` (build local)
- `docker-compose.ghcr.yml` (image prebuild via GitHub Container Registry)

## Demarrage rapide (NAS recommande)

1. Copier les variables d'environnement:

```bash
cp .env.example .env
```

2. Editer au minimum ces variables dans `.env`:
- `BUBO_IMAGE=ghcr.io/zevlek/bubo-trading:latest`
- `BUBO_WEB_PASSWORD=...`
- `BUBO_WEB_SECRET=...` (chaine longue et unique)

3. Lancer:

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

4. Ouvrir l'UI:

```text
http://IP_DU_NAS:7654
```

## Build local (sans GHCR)

```bash
cp .env.example .env
docker compose build
docker compose up -d
```

## Exemples docker-compose (qui marchent)

### Exemple A: build local (`docker-compose.yml`)

```yaml
services:
  bubo-web:
    build:
      context: .
      dockerfile: Dockerfile
      args:
        INSTALL_AI_DEPS: ${INSTALL_AI_DEPS:-0}
    image: bubo-trading:latest
    container_name: bubo-web
    working_dir: /app
    environment:
      TZ: ${TZ:-Europe/Paris}
      PYTHONUNBUFFERED: "1"
      GEMINI_API_KEY: ${GEMINI_API_KEY:-}
      BUBO_NEWSAPI_KEY: ${BUBO_NEWSAPI_KEY:-}
      BUBO_FINNHUB_KEY: ${BUBO_FINNHUB_KEY:-}
      BUBO_REDDIT_CLIENT_ID: ${BUBO_REDDIT_CLIENT_ID:-}
      BUBO_REDDIT_CLIENT_SECRET: ${BUBO_REDDIT_CLIENT_SECRET:-}
      BUBO_REDDIT_USER_AGENT: ${BUBO_REDDIT_USER_AGENT:-}
      BUBO_UNIVERSE_FILE: ${BUBO_UNIVERSE_FILE:-data/universe_global_v1.txt}
      BUBO_PRESELECT_TOP: ${BUBO_PRESELECT_TOP:-60}
      BUBO_MAX_DEEP: ${BUBO_MAX_DEEP:-20}
      BUBO_CAPITAL: ${BUBO_CAPITAL:-10000}
      BUBO_PAPER_ENABLED: ${BUBO_PAPER_ENABLED:-1}
      BUBO_PAPER_STATE: ${BUBO_PAPER_STATE:-data/paper_portfolio_state.json}
      BUBO_PAPER_WEBHOOK: ${BUBO_PAPER_WEBHOOK:-}
      BUBO_PAPER_BROKER: ${BUBO_PAPER_BROKER:-local}
      BUBO_IBKR_HOST: ${BUBO_IBKR_HOST:-127.0.0.1}
      BUBO_IBKR_PORT: ${BUBO_IBKR_PORT:-7497}
      BUBO_IBKR_CLIENT_ID: ${BUBO_IBKR_CLIENT_ID:-42}
      BUBO_IBKR_ACCOUNT: ${BUBO_IBKR_ACCOUNT:-}
      BUBO_IBKR_EXCHANGE: ${BUBO_IBKR_EXCHANGE:-SMART}
      BUBO_IBKR_CURRENCY: ${BUBO_IBKR_CURRENCY:-USD}
      BUBO_NO_FINBERT: ${BUBO_NO_FINBERT:-1}
      BUBO_NO_BUDGET_GATE: ${BUBO_NO_BUDGET_GATE:-0}
      BUBO_WEB_PORT: ${BUBO_WEB_PORT:-7654}
      BUBO_WEB_AUTH_ENABLED: ${BUBO_WEB_AUTH_ENABLED:-1}
      BUBO_WEB_USER: ${BUBO_WEB_USER:-admin}
      BUBO_WEB_PASSWORD: ${BUBO_WEB_PASSWORD:-change-me}
      BUBO_WEB_SECRET: ${BUBO_WEB_SECRET:-change-this-secret}
    volumes:
      - ./data:/app/data
      - ./charts:/app/charts
    ports:
      - "${BUBO_WEB_PORT:-7654}:7654"
    command:
      - python
      - web_app.py
      - --host
      - 0.0.0.0
      - --port
      - "7654"
    restart: unless-stopped
```

### Exemple B: pull image GHCR (`docker-compose.ghcr.yml`)

```yaml
services:
  bubo-web:
    image: ${BUBO_IMAGE:-ghcr.io/your-github-user/bubo-trading:latest}
    container_name: bubo-web
    working_dir: /app
    environment:
      TZ: ${TZ:-Europe/Paris}
      PYTHONUNBUFFERED: "1"
      GEMINI_API_KEY: ${GEMINI_API_KEY:-}
      BUBO_NEWSAPI_KEY: ${BUBO_NEWSAPI_KEY:-}
      BUBO_FINNHUB_KEY: ${BUBO_FINNHUB_KEY:-}
      BUBO_REDDIT_CLIENT_ID: ${BUBO_REDDIT_CLIENT_ID:-}
      BUBO_REDDIT_CLIENT_SECRET: ${BUBO_REDDIT_CLIENT_SECRET:-}
      BUBO_REDDIT_USER_AGENT: ${BUBO_REDDIT_USER_AGENT:-}
      BUBO_UNIVERSE_FILE: ${BUBO_UNIVERSE_FILE:-data/universe_global_v1.txt}
      BUBO_PRESELECT_TOP: ${BUBO_PRESELECT_TOP:-60}
      BUBO_MAX_DEEP: ${BUBO_MAX_DEEP:-20}
      BUBO_CAPITAL: ${BUBO_CAPITAL:-10000}
      BUBO_PAPER_ENABLED: ${BUBO_PAPER_ENABLED:-1}
      BUBO_PAPER_STATE: ${BUBO_PAPER_STATE:-data/paper_portfolio_state.json}
      BUBO_PAPER_WEBHOOK: ${BUBO_PAPER_WEBHOOK:-}
      BUBO_PAPER_BROKER: ${BUBO_PAPER_BROKER:-local}
      BUBO_IBKR_HOST: ${BUBO_IBKR_HOST:-127.0.0.1}
      BUBO_IBKR_PORT: ${BUBO_IBKR_PORT:-7497}
      BUBO_IBKR_CLIENT_ID: ${BUBO_IBKR_CLIENT_ID:-42}
      BUBO_IBKR_ACCOUNT: ${BUBO_IBKR_ACCOUNT:-}
      BUBO_IBKR_EXCHANGE: ${BUBO_IBKR_EXCHANGE:-SMART}
      BUBO_IBKR_CURRENCY: ${BUBO_IBKR_CURRENCY:-USD}
      BUBO_NO_FINBERT: ${BUBO_NO_FINBERT:-1}
      BUBO_NO_BUDGET_GATE: ${BUBO_NO_BUDGET_GATE:-0}
      BUBO_WEB_PORT: ${BUBO_WEB_PORT:-7654}
      BUBO_WEB_AUTH_ENABLED: ${BUBO_WEB_AUTH_ENABLED:-1}
      BUBO_WEB_USER: ${BUBO_WEB_USER:-admin}
      BUBO_WEB_PASSWORD: ${BUBO_WEB_PASSWORD:-change-me}
      BUBO_WEB_SECRET: ${BUBO_WEB_SECRET:-change-this-secret}
    volumes:
      - ./data:/app/data
      - ./charts:/app/charts
    ports:
      - "${BUBO_WEB_PORT:-7654}:7654"
    command:
      - python
      - web_app.py
      - --host
      - 0.0.0.0
      - --port
      - "7654"
    restart: unless-stopped

  watchtower:
    image: containrrr/watchtower:latest
    container_name: bubo-watchtower
    profiles: ["autoupdate"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command:
      - "--interval"
      - "300"
      - "bubo-web"
    restart: unless-stopped
```

## A quoi sert Watchtower ?

`watchtower` (image `containrrr/watchtower`) est un service optionnel qui:
- surveille les nouvelles versions d'image Docker,
- pull automatiquement la nouvelle image,
- redemarre le container cible (`bubo-web`).

Dans ce projet il est desactive par defaut (profile `autoupdate`).
Tu l'actives seulement si tu veux des mises a jour automatiques.

## Variables docker-compose (.env)

Le tableau ci-dessous couvre toutes les variables parametrees dans les fichiers compose.

| Variable | Utilite | Obligatoire | Valeurs possibles | Defaut |
| --- | --- | --- | --- | --- |
| `TZ` | Fuseau horaire du container | Non | Ex: `Europe/Paris`, `UTC` | `Europe/Paris` |
| `INSTALL_AI_DEPS` | Installe les deps IA optionnelles au build local | Non (mode build local uniquement) | `0` (leger), `1` (avec torch/transformers/praw/google-genai) | `0` |
| `BUBO_IMAGE` | Image a pull en mode GHCR | Oui en mode GHCR (sinon image fallback) | Ex: `ghcr.io/zevlek/bubo-trading:latest` | `ghcr.io/your-github-user/bubo-trading:latest` |
| `BUBO_WEB_PORT` | Port HTTP de l'UI | Non | Port TCP valide (ex: `7654`) | `7654` |
| `BUBO_WEB_AUTH_ENABLED` | Active le login UI | Non | `0` ou `1` | `1` |
| `BUBO_WEB_USER` | Utilisateur login UI | Requis si auth active | Texte libre (ex: `admin`) | `admin` |
| `BUBO_WEB_PASSWORD` | Mot de passe login UI | Requis si auth active (fortement recommande) | Texte libre | `change-me` |
| `BUBO_WEB_SECRET` | Secret de session Flask | Requis en production | Chaine longue aleatoire | `change-this-secret` |
| `BUBO_UNIVERSE_FILE` | Fichier univers actions | Non | Chemin lisible dans le container (ex: `data/universe_global_v1.txt`) | `data/universe_global_v1.txt` |
| `BUBO_PRESELECT_TOP` | Taille shortlist apres prescan | Non | Entier `>= 1` | `60` |
| `BUBO_MAX_DEEP` | Nombre de titres analyses en profondeur | Non | Entier `>= 1` (souvent `<= BUBO_PRESELECT_TOP`) | `20` |
| `BUBO_CAPITAL` | Capital paper trading | Non | Nombre `> 0` (ex: `10000`) | `10000` |
| `BUBO_PAPER_ENABLED` | Active paper trading | Non | `0` ou `1` | `1` |
| `BUBO_PAPER_STATE` | Fichier d'etat paper trading | Non | Chemin ecrivable (ex: `data/paper_portfolio_state.json`) | `data/paper_portfolio_state.json` |
| `BUBO_PAPER_WEBHOOK` | Webhook alertes paper | Non | URL webhook ou vide | vide |
| `BUBO_PAPER_BROKER` | Moteur paper: local ou ordres paper IBKR | Non | `local` ou `ibkr` | `local` |
| `BUBO_IBKR_HOST` | Host TWS/IB Gateway | Non (utile si broker=`ibkr`) | Ex: `127.0.0.1` | `127.0.0.1` |
| `BUBO_IBKR_PORT` | Port TWS/IB Gateway | Non (utile si broker=`ibkr`) | Ex: `7497` (paper) | `7497` |
| `BUBO_IBKR_CLIENT_ID` | Client id IB API | Non (utile si broker=`ibkr`) | Entier `>= 1` | `42` |
| `BUBO_IBKR_ACCOUNT` | Compte paper IBKR (optionnel) | Non | Ex: `DUXXXXXX` | vide |
| `BUBO_IBKR_EXCHANGE` | Routing exchange IBKR | Non (utile si broker=`ibkr`) | Ex: `SMART` | `SMART` |
| `BUBO_IBKR_CURRENCY` | Devise contrat actions | Non (utile si broker=`ibkr`) | Ex: `USD`, `EUR` | `USD` |
| `BUBO_NO_FINBERT` | Desactive FinBERT si `1` | Non | `0` (actif) ou `1` (desactive) | `1` |
| `BUBO_NO_BUDGET_GATE` | Desactive gate budget API si `1` | Non | `0` ou `1` | `0` |
| `GEMINI_API_KEY` | Cle Gemini pour `bubo_brain.py` | Non (requise seulement si feature utilisee) | Cle API Google Gemini ou vide | vide |
| `BUBO_NEWSAPI_KEY` | Cle NewsAPI pour sentiment news | Non (requise pour news) | Cle API ou vide | vide |
| `BUBO_FINNHUB_KEY` | Cle Finnhub pour events/news feed | Non (requise pour Finnhub) | Cle API ou vide | vide |
| `BUBO_REDDIT_CLIENT_ID` | Reddit API client id | Non (requis avec les 2 autres Reddit pour social) | Valeur OAuth Reddit ou vide | vide |
| `BUBO_REDDIT_CLIENT_SECRET` | Reddit API client secret | Non (requis avec les 2 autres Reddit pour social) | Valeur OAuth Reddit ou vide | vide |
| `BUBO_REDDIT_USER_AGENT` | Reddit API user-agent | Non (requis avec les 2 autres Reddit pour social) | Ex: `Bubo/1.0 by u/USERNAME` | vide dans compose / exemple rempli dans `.env.example` |

Notes compatibilite:
- Le code accepte aussi `NEWSAPI_KEY` en alternative a `BUBO_NEWSAPI_KEY`.
- Le code accepte aussi `FINNHUB_KEY` en alternative a `BUBO_FINNHUB_KEY`.
- Le code accepte aussi `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` / `REDDIT_USER_AGENT` en alternatives aux variables `BUBO_*`.

## Test paper trading IBKR

1. Activer l'API dans TWS/IB Gateway paper:
- `Enable ActiveX and Socket Clients`
- autoriser `127.0.0.1`
- port paper (souvent `7497`)

2. Configurer `.env`:

```env
BUBO_PAPER_BROKER=ibkr
BUBO_IBKR_HOST=127.0.0.1
BUBO_IBKR_PORT=7497
BUBO_IBKR_CLIENT_ID=42
BUBO_IBKR_ACCOUNT=DUXXXXXX
BUBO_IBKR_EXCHANGE=SMART
BUBO_IBKR_CURRENCY=USD
```

3. Installer deps IA (inclut `ib_insync`) puis lancer:

```bash
INSTALL_AI_DEPS=1 docker compose build
docker compose up -d
```

Notes:
- si la connexion IBKR echoue, BUBO bascule automatiquement en mode `local` pour ne pas bloquer le cycle.
- en mode `ibkr`, les commissions/fills sont pris depuis les retours d'ordre paper IBKR quand disponibles.

## Mise a jour sur NAS

```bash
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

ou:

```bash
sh scripts/nas-update.sh
```

## Auto-update optionnel

Le service `watchtower` est dans `docker-compose.ghcr.yml` (profile `autoupdate`):

```bash
docker compose -f docker-compose.ghcr.yml --profile autoupdate up -d
```

## Tests

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

## Notes

- Le goulot d'etranglement principal a grande echelle est souvent la limite API, pas le GPU.
- Sans GPU NVIDIA, FinBERT tourne sur CPU (plus lent).
- Projet experimental, pas un conseil financier.
