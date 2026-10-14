#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Подготовка проекта к первому запуску.
#   ./scripts/prepare.sh dianthus-opt.ru admin@dianthus-opt.ru
# Создаёт .env со СЛУЧАЙНЫМИ паролями и кладёт HTTP-конфиг nginx.
# Существующий .env не перезаписывает.
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

DOMAIN="${1:-}"
ADMIN_EMAIL="${2:-}"
if [[ -z "$DOMAIN" || -z "$ADMIN_EMAIL" ]]; then
    echo "Использование: $0 <домен> <email администратора>"
    echo "Пример:        $0 dianthus-opt.ru admin@dianthus-opt.ru"
    exit 1
fi

if [[ -f .env ]]; then
    echo "ℹ️  .env уже существует — не трогаю."
else
    SECRET_KEY="$(openssl rand -hex 32)"
    DB_PASS="$(openssl rand -hex 24)"
    REDIS_PASS="$(openssl rand -hex 24)"
    INTEGRATION_PASS="$(openssl rand -hex 20)"
    ADMIN_PASS="$(openssl rand -hex 10)Aa1"     # буквы + цифры, длина > 8

    umask 077
    cat > .env <<EOF
# ── Создано scripts/prepare.sh. НЕ коммитьте этот файл в Git! ──
ENV=prod
SECRET_KEY=${SECRET_KEY}
APP_URL=https://${DOMAIN}

# База данных (хост db — это имя контейнера внутри Docker-сети)
POSTGRES_USER=dianthus_user
POSTGRES_PASSWORD=${DB_PASS}
POSTGRES_DB=dianthus
DATABASE_URL=postgresql://dianthus_user:${DB_PASS}@db:5432/dianthus

# Redis
REDIS_PASSWORD=${REDIS_PASS}
REDIS_URL=redis://:${REDIS_PASS}@redis:6379/0

CACHE_TTL=300
PAGE_SIZE=12
SESSION_MAX_AGE=604800

# Администратор (создаётся при первом запуске)
ADMIN_EMAIL=${ADMIN_EMAIL}
ADMIN_PASSWORD=${ADMIN_PASS}
ADMIN_NAME=Администратор
ADMIN_COMPANY=ООО Диантус
ADMIN_PHONE=
ADMIN_FORCE_PASSWORD_RESET=0

# Магазин (показывается на странице «Контакты» и в письмах)
SHOP_NAME=ООО «Диантус»
SHOP_PHONE=+7 (995) 152-21-31
SHOP_PHONE_2=+7 (901) 188-77-87
SHOP_ADDRESS=г. Москва, Вешняковская ул., 39Г, этаж 1
SHOP_WORKING_HOURS=Пн–Вс 09:00–21:00
SHOP_MAX_LINK=https://max.ru/id7720965534_biz
SHOP_MAP_URL=https://yandex.ru/map-widget/v1/?ll=37.816578%2C55.717630&z=17

# Почта (уведомления о новых клиентах и заказах).
# Заполните сами: для Яндекса нужен «пароль приложения», а не пароль от ящика.
SMTP_HOST=smtp.yandex.ru
SMTP_PORT=465
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_TO=
SMTP_USE_SSL=true

# Интеграция с 1С (Basic Auth). Эти же логин и пароль вводятся в 1С.
INTEGRATION_USER=1c_dianthus
INTEGRATION_PASSWORD=${INTEGRATION_PASS}

# Мониторинг ошибок (необязательно): DSN из sentry.io
SENTRY_DSN=

# Web Push (необязательно)
VAPID_PRIVATE_KEY=
VAPID_PUBLIC_KEY=
VAPID_CLAIMS_EMAIL=mailto:${ADMIN_EMAIL}
EOF
    chmod 600 .env
    echo "✅ Создан .env (права 600)."
    echo
    echo "════════════════════════════════════════════════════"
    echo "  ЗАПИШИТЕ В МЕНЕДЖЕР ПАРОЛЕЙ (показывается один раз):"
    echo "  Админ сайта:  ${ADMIN_EMAIL}"
    echo "  Пароль:       ${ADMIN_PASS}"
    echo "  Логин 1С:     1c_dianthus"
    echo "  Пароль 1С:    ${INTEGRATION_PASS}"
    echo "  (все пароли лежат также в файле .env)"
    echo "════════════════════════════════════════════════════"
fi

umask 022

# HTTP-конфиг nginx на первый этап (до получения сертификата)
mkdir -p nginx/conf.d backups
if [[ ! -f nginx/conf.d/site.conf ]]; then
    # В репозитории файл называется site_http.conf (подчёркивание)
    HTTP_SRC=nginx/site_http.conf
    [[ -f "$HTTP_SRC" ]] || HTTP_SRC=nginx/site-http.conf   # на случай переименования
    sed "s/__DOMAIN__/${DOMAIN}/g" "$HTTP_SRC" > nginx/conf.d/site.conf
    echo "✅ Создан nginx/conf.d/site.conf (HTTP)."
fi

echo
echo "Дальше: откройте .env (nano .env) и заполните SMTP_*, затем: docker compose up -d --build"
