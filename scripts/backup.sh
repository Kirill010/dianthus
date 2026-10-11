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

echo "[$(date '+%F %T')] ✅ Готово: $(du -h "$DB_FILE" | cut -f1) БД, $(du -h "$UP_FILE" | cut -f1) фото"
