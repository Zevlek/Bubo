#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

echo "[BUBO] Pull latest image..."
docker compose -f docker-compose.ghcr.yml pull

echo "[BUBO] Apply update..."
docker compose -f docker-compose.ghcr.yml up -d

echo "[BUBO] Done."
