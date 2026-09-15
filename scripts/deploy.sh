#!/usr/bin/env bash
# Деплой на сервере: обновить код из GitHub и пересобрать контейнер.
# Запуск на сервере:  bash /opt/codex-bots/self/scripts/deploy.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/codex-bots/self}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/codex-bots}"
SERVICE="${SERVICE:-self-bot}"
BRANCH="${BRANCH:-main}"

cd "$APP_DIR"
echo "==> git pull ($BRANCH)"
git fetch --quiet origin "$BRANCH"
git reset --hard "origin/$BRANCH" --quiet
git log --oneline -1

cd "$COMPOSE_DIR"
echo "==> docker compose build $SERVICE"
docker compose build --quiet "$SERVICE"
echo "==> docker compose up -d $SERVICE"
docker compose up -d "$SERVICE"
docker image prune -f >/dev/null 2>&1 || true

echo "==> waiting for startup"
sleep 6
docker compose logs --tail 25 "$SERVICE"
