#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Восстановление базы из дампа (.sql или .sql.gz).
#   ./scripts/restore-db.sh /путь/к/dianthus_db.sql.gz
# Подходит и для переноса со старого сервера, и для отката из бэкапа.
# Заменяет ВСЕ данные в текущей базе.
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

FILE="${1:-}"
[[ -f "$FILE" ]] || { echo "Использование: $0 <файл дампа .sql или .sql.gz>"; exit 1; }

echo "⚠️  Текущие данные в базе будут ЗАМЕНЕНЫ содержимым: $FILE"
read -r -p "Введите yes чтобы продолжить: " ans
[[ "$ans" == "yes" ]] || { echo "Отменено."; exit 1; }

docker compose up -d db
echo "Жду готовности базы…"
until docker compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; do
    sleep 2
done

# Приложение на время восстановления останавливаем
docker compose stop app || true

if [[ "$FILE" == *.gz ]]; then READER=(gunzip -c "$FILE"); else READER=(cat "$FILE"); fi

# --single-transaction + ON_ERROR_STOP: либо всё восстановилось, либо ничего не изменилось
"${READER[@]}" | docker compose exec -T db sh -c \
    'psql -v ON_ERROR_STOP=1 --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    > /dev/null

echo "✅ База восстановлена. Проверка:"
docker compose exec -T db sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tc "SELECT '\''users: '\'' || count(*) FROM users UNION ALL SELECT '\''products: '\'' || count(*) FROM products UNION ALL SELECT '\''orders: '\'' || count(*) FROM orders;"'

echo
echo "Запустите приложение: docker compose up -d"
