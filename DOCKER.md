# Docker Compose - BUBO Web UI

## 1) Preparation

```powershell
copy .env.example .env
```

Edite ensuite `.env` et change au minimum:

- `BUBO_WEB_PASSWORD`
- `BUBO_WEB_SECRET`

Port par defaut: `7654`.

Si tu utilises des APIs externes, configure aussi:

- `GEMINI_API_KEY` (Bubo Brain)
- `BUBO_NEWSAPI_KEY` (news)
- `BUBO_FINNHUB_KEY` (news)
- `BUBO_REDDIT_CLIENT_ID` (social)
- `BUBO_REDDIT_CLIENT_SECRET` (social)
- `BUBO_REDDIT_USER_AGENT` (social)
- `BUBO_PAPER_WEBHOOK` (alertes paper)

## 2) Lancer localement avec build

```powershell
docker compose build
docker compose up -d
docker compose logs -f bubo-web
```

Acces navigateur:

```text
http://IP_DU_NAS:7654
```

## 3) Authentification web

Variables dans `.env`:

- `BUBO_WEB_AUTH_ENABLED=1`
- `BUBO_WEB_USER=admin`
- `BUBO_WEB_PASSWORD=...`
- `BUBO_WEB_SECRET=...`

Tous les endpoints UI/API (sauf `/health`) sont proteges par login.

## 4) Utilisation dans l'UI

- `Start Watch`: surveillance continue.
- `Run Once`: cycle complet unique.
- `Screen Only`: preselction uniquement.
- `Stop`: arret du process en cours.
- `Connectivite API`: etat Gemini/NewsAPI/Finnhub/Reddit/Stocktwits + IB Gateway (bouton `Tester maintenant`).
- Logs live + telechargement des exports `data/` et `charts/`.

## 5) Deploiement GitHub (image pull sur NAS)

Objectif: le NAS ne build plus, il fait seulement `docker compose pull`.

1. Pousser ce repo sur GitHub.
2. Activer le workflow `publish-ghcr.yml` (ajoute dans ce projet) pour publier l'image GHCR.
3. Sur le NAS, utiliser `docker-compose.ghcr.yml` + `.env`.
4. Mettre a jour:

```powershell
docker compose -f docker-compose.ghcr.yml pull
docker compose -f docker-compose.ghcr.yml up -d
```

Ou via script:

```bash
sh scripts/nas-update.sh
```

## 6) Auto-update optionnel

Avec le profil `autoupdate` (Watchtower) dans `docker-compose.ghcr.yml`:

```powershell
docker compose -f docker-compose.ghcr.yml --profile autoupdate up -d
```

## Notes

- `data/` et `charts/` restent sur le NAS via volumes.
- Les identifiants API sont lus directement depuis les variables d'environnement Docker Compose.
- Sans GPU NVIDIA, FinBERT tourne en CPU (plus lent).
