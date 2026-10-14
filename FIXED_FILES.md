# Полные исправленные файлы

Ниже — полные содержимые всех файлов, которые были исправлены. Эти версии уже
находятся в репозитории (коммиты `d98d542`, `55eb329`, `ef32b77`). Если пушнул
эти коммиты в GitHub — на новом сервере достаточно обычного `git clone`,
ручной перенос не нужен. Если пуша нет — скопируй содержимое ниже в файлы вручную.

---

## 1) scripts/prepare.sh — ИСПРАВЛЕНА ОПЕЧАТКА (site-http.conf → site_http.conf)

```bash
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
```

**Что исправлено:** блок создания конфига nginx теперь сначала ищет `nginx/site_http.conf`
(правильное имя с подчёркиванием), а `site-http.conf` (с дефисом) — только как запасной
вариант. Раньше скрипт читал несуществующий файл и `nginx/conf.d/site.conf` не создавался.

---

## 2) scripts/init-ssl.sh — ИСПРАВЛЕНА ОПЕЧАТКА (site-https.conf → site_https.conf)

```bash
#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
# Получает бесплатный сертификат Let's Encrypt и включает HTTPS.
#   ./scripts/init-ssl.sh admin@dianthus-opt.ru
# Условия: DNS-записи A для домена и www уже указывают на ЭТОТ сервер,
# а стек запущен (docker compose up -d).
# ───────────────────────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")/.."

EMAIL="${1:-}"
if [[ -z "$EMAIL" ]]; then
    echo "Использование: $0 <email для уведомлений Let's Encrypt>"
    exit 1
fi
[[ -f .env ]] || { echo "❌ Нет .env — сначала scripts/prepare.sh"; exit 1; }

DOMAIN="$(grep -E '^APP_URL=' .env | head -1 | sed -E 's|^APP_URL=https?://||; s|/.*$||')"
[[ -n "$DOMAIN" ]] || { echo "❌ Не удалось определить домен из APP_URL"; exit 1; }
echo "Домен: ${DOMAIN}"

# Проверка DNS: домен и www должны вести на этот сервер
MY_IP="$(curl -4fsS --max-time 8 https://ifconfig.me 2>/dev/null || true)"
for host in "$DOMAIN" "www.${DOMAIN}"; do
    ip="$(getent ahostsv4 "$host" | awk '{print $1; exit}' || true)"
    echo "  ${host} → ${ip:-НЕ НАЙДЕН}"
    if [[ -z "$ip" ]]; then
        echo "❌ Для ${host} нет A-записи. Добавьте её в DNS и подождите." >&2
        exit 1
    fi
    if [[ -n "$MY_IP" && "$ip" != "$MY_IP" ]]; then
        echo "❌ ${host} указывает на ${ip}, а этот сервер — ${MY_IP}." >&2
        echo "   DNS ещё не обновился (или запись неверная). Подождите и повторите." >&2
        exit 1
    fi
done

# Nginx с HTTP-конфигом должен работать (он отдаёт проверочный файл)
docker compose up -d nginx

echo "==> Запрашиваю сертификат"
docker compose run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" -d "www.${DOMAIN}" \
    --email "$EMAIL" --agree-tos --no-eff-email --non-interactive

echo "==> Включаю HTTPS-конфиг"
cp nginx/conf.d/site.conf nginx/conf.d/site.conf.bak
# В репозитории файл называется site_https.conf (подчёркивание, как и site_http.conf)
HTTPS_SRC=nginx/site_https.conf
[[ -f "$HTTPS_SRC" ]] || HTTPS_SRC=nginx/site-https.conf   # на случай переименования
sed "s/__DOMAIN__/${DOMAIN}/g" "$HTTPS_SRC" > nginx/conf.d/site.conf

if docker compose exec -T nginx nginx -t; then
    docker compose exec -T nginx nginx -s reload
    rm -f nginx/conf.d/site.conf.bak
    echo "✅ HTTPS включён: https://${DOMAIN}"
    echo "   Продление сертификата идёт автоматически (контейнер certbot)."
else
    echo "❌ Ошибка в конфиге nginx — возвращаю HTTP-вариант." >&2
    mv nginx/conf.d/site.conf.bak nginx/conf.d/site.conf
    docker compose exec -T nginx nginx -s reload || true
    exit 1
fi
```

**Что исправлено:** строка `sed ... nginx/site-https.conf` заменена на поиск
`nginx/site_https.conf` (с подчёркиванием) с запасным вариантом. Именно из-за этой
опечатки у тебя сертификат получился, но HTTPS сам не включился (`sed: can't read
nginx/site-https.conf: No such file or directory`) и пришлось включать вручную.
Также добавлен автооткат на HTTP-конфиг, если nginx -t не пройдёт.

---

## 3) app/migrations.py — КОЛОНОЧКА totp_secret ПЕРЕНЕСЕНА ИЗ products В users

Было (баг — колонка была в списке таблицы products):

```python
    "products": {
        ...
        "totp_secret":      "VARCHAR(64) DEFAULT ''",   # ❌ НЕПРАВИЛЬНАЯ ТАБЛИЦА
    },
    "users": {
        "phone":            "VARCHAR(30) DEFAULT ''",
        ...
    },
```

Стало (фрагмент EXPECTED_COLUMNS, исправленная часть):

```python
    "users": {
        "phone":            "VARCHAR(30) DEFAULT ''",
        "company_name":     "VARCHAR(200) DEFAULT ''",
        "inn":              "VARCHAR(12) DEFAULT ''",
        "city":             "VARCHAR(100) DEFAULT ''",
        "is_admin":         "BOOLEAN DEFAULT FALSE",
        "is_approved":      "BOOLEAN DEFAULT FALSE",
        "discount_percent": "FLOAT DEFAULT 0",
        "totp_secret":      "VARCHAR(64) DEFAULT ''",   # ✅ теперь в нужной таблице
    },
```

**Почему это важно:** модель `User` в `app/models.py` содержит поле `totp_secret`,
и любой запрос к таблице users падал с ошибкой
`column users.totp_secret does not exist` после импорта старого дампа — то есть
вход /login не работал физически. Теперь при старте приложение само добавит эту
колонку в нужную таблицу (функция auto_migrate), и дамп со старого сервера
восстановится без ручных ALTER TABLE.

---

## 4) docker-compose.yml — ДОБАВЛЕНЫ ulimits ДЛЯ NGINX (убирает warning)

Фрагмент сервиса nginx (изменённая часть):

```yaml
  nginx:
    image: nginx:1.27-alpine
    restart: unless-stopped
    depends_on:
      app:
        condition: service_healthy
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/proxy_params.conf:/etc/nginx/proxy_params.conf:ro
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - letsencrypt:/etc/letsencrypt:ro
      - certbot_www:/var/www/certbot:ro
      - uploads:/srv/uploads:ro
    command:
      - /bin/sh
      - -c
      - 'while :; do sleep 6h; nginx -s reload; done & exec nginx -g "daemon off;"'
    networks: [frontend]
    mem_limit: 128m
    # worker_connections (2048) в nginx.conf требует более высокого
    # лимита открытых файлов, иначе nginx алертит при каждом старте/reload
    ulimits:
      nofile:
        soft: 65536
        hard: 65536
    logging: *logging
```

Это косметическое исправление: предупреждение
`2048 worker_connections exceed open file resource limit: 1024` исчезнет.

---

## Как применить всё это на НОВОМ (большом) сервере

Если эти коммиты запушены в GitHub — ничего копировать руками НЕ НУЖНО:

```bash
# 1. клонировать (код уже с исправлениями)
cd /root
git clone https://github.com/Kirill010/dianthus.git dianthus
cd dianthus && chmod +x scripts/*.sh

# 2. секреты и конфиг nginx (создаст .env + nginx/conf.d/site.conf сам, без опечаток)
./scripts/prepare.sh dianthus-opt.ru sortme@mail.ru
nano .env          # заполнить SMTP_*

# 3. база + redis, импорт дампа со СТАРОГО сервера
docker compose up -d db redis
cat db.dump | docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# (если в дампе нет totp_secret — auto_migrate добавит её сам при старте app)

# 4. фото товаров → в volume uploads
docker run --rm -v dianthus_uploads:/dst -v $(pwd):/src busybox tar xzf /src/uploads.tar.gz -C /dst

# 5. приложение и весь стек
docker compose build app
docker compose up -d

# 6. DNS (A-записи @ и www на новый IP) → затем HTTPS (скрипт теперь работает без ошибок)
./scripts/init-ssl.sh sortme@mail.ru
```

Если пуша в GitHub ещё нет — замени три файла (`scripts/prepare.sh`,
`scripts/init-ssl.sh`, фрагмент `app/migrations.py`) содержимым выше,
затем сделай коммит и push, чтобы на всех будущих серверах код был сразу рабочий.
