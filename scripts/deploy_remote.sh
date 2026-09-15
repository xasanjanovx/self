#!/usr/bin/env bash
# Запуск с локальной машины: пушит main и деплоит на сервер по SSH.
#   bash scripts/deploy_remote.sh
set -euo pipefail
HOST="${DEPLOY_HOST:-root@167.235.249.200}"
KEY="${DEPLOY_KEY:-$HOME/.ssh/id_ed25519_selfbot}"
git push origin main
ssh -i "$KEY" -o BatchMode=yes "$HOST" 'bash /opt/codex-bots/self/scripts/deploy.sh'
