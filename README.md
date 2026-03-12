# BUBO

Moteur de trading moyen terme avec:
- scoring multi-sources (technique, events, news, social),
- présélection d’univers (large scan -> shortlist),
- paper trading persistant (state JSON + exports),
- interface web de pilotage (start/stop/watch/logs/downloads),
- déploiement Docker/NAS prêt à l’emploi.

## Fonctionnalités principales

- `bubo_engine.py`
  - Analyse ponctuelle ou mode `--watch`
  - Prescreen univers (`--universe-file`)
  - Risk gates portefeuille
  - Paper trading (`--paper`)
- `web_app.py`
  - UI web (port par défaut `7654`)
  - Authentification login/password
  - Lancement des runs depuis le navigateur
  - Logs live + téléchargement des fichiers de sortie
- Exports automatiques paper:
  - `data/paper_trades_latest.csv`
  - `data/paper_equity_curve_latest.csv`
  - `data/paper_daily_stats_latest.csv`
  - `data/paper_daily_report_latest.md`

## Arborescence utile

- `bubo_engine.py` : moteur principal
- `web_app.py` : serveur web Flask
- `templates/` : pages UI (`index.html`, `login.html`)
- `docker-compose.yml` : stack build local
- `docker-compose.ghcr.yml` : stack pull image GHCR (idéal NAS)
- `Dockerfile` : image application
- `.github/workflows/publish-ghcr.yml` : build/push GHCR auto
- `scripts/nas-update.sh` : update pull + up

## Lancement local (Python)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python bubo_engine.py --watch --universe-file data/universe_global_v1.txt --preselect-top 60 --max-deep 20 --paper --no-finbert
```

## UI web avec Docker (recommandé NAS)

1. Copier la config:

```powershell
copy .env.example .env
```

2. Changer impérativement dans `.env`:
- `BUBO_WEB_PASSWORD`
- `BUBO_WEB_SECRET`

3. Lancer:

```powershell
docker compose build
docker compose up -d
docker compose logs -f bubo-web
```

4. Ouvrir:

```text
http://IP_DU_NAS:7654
```

## Déploiement NAS via GHCR (pull only)

Objectif: le NAS ne build pas, il ne fait que `pull`.

1. Le workflow GitHub publie l’image:
- `.github/workflows/publish-ghcr.yml`

2. Dans `.env`, définir:

```text
BUBO_IMAGE=ghcr.io/zevlek/bubo-trading:latest
```

3. Déployer:

```powershell
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

4. Mise à jour rapide:

```bash
sh scripts/nas-update.sh
```

## Variables d’environnement clés

- `BUBO_WEB_PORT` (défaut `7654`)
- `BUBO_WEB_AUTH_ENABLED` (`1`/`0`)
- `BUBO_WEB_USER`
- `BUBO_WEB_PASSWORD`
- `BUBO_WEB_SECRET`
- `BUBO_UNIVERSE_FILE`
- `BUBO_PRESELECT_TOP`
- `BUBO_MAX_DEEP`
- `BUBO_PAPER_ENABLED`
- `BUBO_NO_FINBERT`

## Tests

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

## Notes perf

- Goulot principal à grande échelle: quotas/rate limits API (news/social), pas la GPU.
- Sans GPU NVIDIA, FinBERT tourne en CPU (plus lent).
- Pour un NAS, le couple prescreen large + deep shortlist est le mode conseillé.

## Avertissement

Projet expérimental/éducatif. Pas un conseil financier.
Toujours valider les signaux et les risques avant usage réel.

## Exemples docker-compose

Exemple minimal (build local):

```yaml
services:
  bubo-web:
    build:
      context: .
      dockerfile: Dockerfile
      args:
        INSTALL_AI_DEPS: 0
    container_name: bubo-web
    environment:
      BUBO_WEB_PORT: 7654
      BUBO_WEB_AUTH_ENABLED: 1
      BUBO_WEB_USER: admin
      BUBO_WEB_PASSWORD: "change-me"
      BUBO_WEB_SECRET: "change-this-secret"
      BUBO_UNIVERSE_FILE: data/universe_global_v1.txt
      BUBO_PRESELECT_TOP: 60
      BUBO_MAX_DEEP: 20
      BUBO_PAPER_ENABLED: 1
      BUBO_NO_FINBERT: 1
    ports:
      - "7654:7654"
    volumes:
      - ./data:/app/data
      - ./charts:/app/charts
    command: ["python", "web_app.py", "--host", "0.0.0.0", "--port", "7654"]
    restart: unless-stopped
```

Exemple NAS en pull d'image GHCR:

```yaml
services:
  bubo-web:
    image: ghcr.io/zevlek/bubo-trading:latest
    container_name: bubo-web
    environment:
      BUBO_WEB_PORT: 7654
      BUBO_WEB_AUTH_ENABLED: 1
      BUBO_WEB_USER: admin
      BUBO_WEB_PASSWORD: "change-me"
      BUBO_WEB_SECRET: "change-this-secret"
    ports:
      - "7654:7654"
    volumes:
      - ./data:/app/data
      - ./charts:/app/charts
    command: ["python", "web_app.py", "--host", "0.0.0.0", "--port", "7654"]
    restart: unless-stopped
```

## Configuration API via Docker Compose

Toutes les integrations API peuvent etre configurees directement via variables d'environnement (compose/.env):

- `GEMINI_API_KEY` -> Gemini (bubo_brain.py)
- `BUBO_NEWSAPI_KEY` ou `NEWSAPI_KEY` -> NewsAPI (phase2b)
- `BUBO_FINNHUB_KEY` ou `FINNHUB_KEY` -> Finnhub (phase2b)
- `BUBO_REDDIT_CLIENT_ID` ou `REDDIT_CLIENT_ID` -> Reddit API (phase3b)
- `BUBO_REDDIT_CLIENT_SECRET` ou `REDDIT_CLIENT_SECRET` -> Reddit API (phase3b)
- `BUBO_REDDIT_USER_AGENT` ou `REDDIT_USER_AGENT` -> Reddit API (phase3b)
- `BUBO_PAPER_WEBHOOK` -> alertes webhook paper (bubo_engine)

Tu peux donc tout piloter depuis `docker-compose.yml` / `docker-compose.ghcr.yml` sans editer le code.
