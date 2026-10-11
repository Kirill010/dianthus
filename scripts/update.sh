#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Обновление сайта до последней версии из GitHub (с бэкапом перед этим).
#   ./scripts/update.sh
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Бэкап перед обновлением"
./scripts/backup.sh

echo "==> Получаю изменения из GitHub"
git pull --ff-only

echo "==> Пересборка и запуск"
docker compose up -d --build

# Nginx должен увидеть новый контейнер приложения и новый конфиг
docker compose exec -T nginx nginx -s reload || true
docker image prune -f >/dev/null

echo "==> Состояние:"
docker compose ps
echo "✅ Обновлено до $(git rev-parse --short HEAD)"
