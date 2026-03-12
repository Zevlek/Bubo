# BUBO

BUBO est un moteur de trading moyen terme avec prescreening d'univers, scoring multi-signaux et mode paper trading.
Le mode de deploiement cible est Docker (NAS/serveur) avec interface web sur le port `7654`.

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

## Configuration API (via Compose/.env uniquement)

Tu peux tout configurer sans modifier le code:

- `GEMINI_API_KEY` -> LLM principal (`bubo_brain.py`)
- `BUBO_NEWSAPI_KEY` (ou `NEWSAPI_KEY`) -> news sentiment (`phase2b_sentiment.py`)
- `BUBO_FINNHUB_KEY` (ou `FINNHUB_KEY`) -> events/news feed (`phase2b_sentiment.py`)
- `BUBO_REDDIT_CLIENT_ID` (ou `REDDIT_CLIENT_ID`) -> social (`phase3b_social.py`)
- `BUBO_REDDIT_CLIENT_SECRET` (ou `REDDIT_CLIENT_SECRET`) -> social (`phase3b_social.py`)
- `BUBO_REDDIT_USER_AGENT` (ou `REDDIT_USER_AGENT`) -> social (`phase3b_social.py`)
- `BUBO_PAPER_WEBHOOK` -> alertes paper trading (`bubo_engine.py`)

Le fichier `.env.example` contient deja toutes les cles.

## Variables importantes

- `BUBO_WEB_PORT=7654`
- `BUBO_WEB_AUTH_ENABLED=1`
- `BUBO_WEB_USER=admin`
- `BUBO_WEB_PASSWORD=...`
- `BUBO_WEB_SECRET=...`
- `BUBO_PRESELECT_TOP=60`
- `BUBO_MAX_DEEP=20`
- `BUBO_NO_FINBERT=1` (mettre `0` si modele local actif)

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
