#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Восстановление загруженных фото из архива .tar.gz.
#   ./scripts/restore-uploads.sh /путь/к/uploads.tar.gz
# Архив должен содержать сами файлы в корне (см. DEPLOY.md, шаг переноса):
#   tar -czf uploads.tar.gz -C /var/www/dianthus/app/static/uploads .
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
[[ -f "$FILE" ]] || { echo "Использование: $0 <архив .tar.gz>"; exit 1; }
FILE="$(realpath "$FILE")"

# Том создаётся при первом `docker compose up`
docker volume inspect dianthus_uploads >/dev/null 2>&1 || docker compose create >/dev/null

docker run --rm \
    -v dianthus_uploads:/data \
    -v "$FILE":/in/uploads.tar.gz:ro \
    alpine:3.20 sh -c '
        set -e
        tar xzf /in/uploads.tar.gz -C /data
        chown -R 10001:10001 /data      # владелец — пользователь appuser из контейнера
        chmod -R u=rwX,go=rX /data
        echo "Файлов в томе: $(find /data -type f | wc -l)"
    '
echo "✅ Фото восстановлены."
