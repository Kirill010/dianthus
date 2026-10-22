#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Резервная копия: база данных + загруженные фото.
#   ./scripts/backup.sh
# Хранит копии 14 дней в BACKUP_DIR (по умолчанию ./backups).
# Для автозапуска каждую ночь см. DEPLOY.md (cron).
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-$PWD/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
umask 077

DB_FILE="${BACKUP_DIR}/db-${STAMP}.sql.gz"
UP_FILE="${BACKUP_DIR}/uploads-${STAMP}.tar.gz"

echo "[$(date '+%F %T')] Бэкап БД → ${DB_FILE}"
# --clean --if-exists: дамп можно залить в уже существующую базу
docker compose exec -T db sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl --clean --if-exists' \
    | gzip -9 > "$DB_FILE"

gzip -t "$DB_FILE"
if [[ $(stat -c %s "$DB_FILE") -lt 1024 ]]; then
    echo "❌ Дамп подозрительно маленький — что-то пошло не так" >&2
    exit 1
fi

echo "[$(date '+%F %T')] Бэкап фото → ${UP_FILE}"
docker run --rm \
    -v dianthus_uploads:/data:ro \
    -v "$BACKUP_DIR":/backup \
    alpine:3.20 tar czf "/backup/uploads-${STAMP}.tar.gz" -C /data .

find "$BACKUP_DIR" -maxdepth 1 -type f \( -name 'db-*.sql.gz' -o -name 'uploads-*.tar.gz' \) \
    -mtime +"$KEEP_DAYS" -delete

# ── Копия за пределы сервера (необязательно, но очень рекомендуется) ──
# Копии на том же сервере не спасут, если сервер потеряется или будет взломан.
# Задайте в .env строку (нужен rsync и вход по SSH-ключу без пароля):
#   BACKUP_REMOTE=backup@другой-сервер:/srv/dianthus-backups
REMOTE="${BACKUP_REMOTE:-}"
if [[ -z "$REMOTE" && -f .env ]]; then
    REMOTE="$(grep -E '^BACKUP_REMOTE=' .env | head -1 | cut -d= -f2- || true)"
fi
if [[ -n "$REMOTE" ]]; then
    echo "[$(date '+%F %T')] Копирую на $REMOTE"
    if rsync -a -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
            "$DB_FILE" "$UP_FILE" "${REMOTE%/}/"; then
        echo "[$(date '+%F %T')] ✅ Копия отправлена"
    else
        echo "[$(date '+%F %T')] ⚠️  Не удалось отправить копию на $REMOTE (локальный бэкап сделан)" >&2
    fi
fi

echo "[$(date '+%F %T')] ✅ Готово: $(du -h "$DB_FILE" | cut -f1) БД, $(du -h "$UP_FILE" | cut -f1) фото"
