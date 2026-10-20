# 🌸 Диантус — B2B-портал оптовых продаж цветов

Серверный веб-сайт оптового цветочного магазина: каталог с живыми остатками,
предзаказы под поставки, персональные скидки, интеграция с 1С, счета в PDF,
выгрузки в Excel. Рендеринг — серверный (Jinja2), без отдельного фронтенд-приложения.

## Стек

- **FastAPI + Jinja2** (server-side rendering, Bootstrap 5)
- **SQLAlchemy 2.0** — PostgreSQL в проде, SQLite для разработки и тестов
- **Redis** — серверные сессии, rate limiting, блокировки входа
- **Docker + docker-compose** — app / db / redis / nginx / certbot
- **pytest** — тесты критичных сценариев

## Возможности

**Для клиентов**
- Каталог с фильтрами (страна, категория, цена, длина стебля, поиск с подсказками)
- Живые остатки по поставкам; резервирование под предзаказы
- Корзина, оформление заказа с персональной скидкой, счёт на оплату в PDF
- История заказов, уведомления, профиль

**Для администраторов**
- Дашборд: заказы (поиск/фильтр/пагинация), клиенты (модерация заявок, скидки),
  товары, поставки — с живым поиском по вкладкам
- Импорт товаров и накладных из Excel, экспорт заказов/клиентов
- Двухфакторная аутентификация (TOTP) для админов, журнал действий (аудит)
- Интеграция с 1С: синхронизация товаров/остатков/цен, webhook с HMAC-подписью

## Быстрый старт (разработка)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # при отсутствии — задайте переменные вручную
uvicorn app.main:app --reload
```

По умолчанию используется SQLite (`dianthus.db`) и in-memory rate limiting —
Redis для разработки не обязателен. Сайт: http://127.0.0.1:8000

Тесты:

```bash
ENV=test pytest -q
```

## Деплой на VPS (production)

```bash
git clone <repo> && cd dianthus
cp .env.example .env          # заполнить: домен, SECRET_KEY, SMTP, пароли
scripts/prepare.sh            # подставляет домен в конфиги nginx
scripts/init-ssl.sh           # выпускает сертификат Let's Encrypt
docker compose up -d --build
```

Обновление на сервере после `git push`:

```bash
scripts/update.sh             # git pull + пересборка + перезапуск
```

Подробнее о схеме сети и резервном копировании — в `deploy/` и `scripts/backup.sh`.

## Безопасность (кратко)

- Пароли — bcrypt; вход — с прогрессивной блокировкой и лимитами (Redis)
- Серверные сессии в Redis, подписанная cookie только с id; ротация id при входе/выходе
- CSRF-токены + проверка Origin на всех POST; TrustedHost в prod
- CSP с per-request nonce (без `unsafe-inline` для скриптов), HSTS, COOP/CORP
- Заказ оформляется атомарно: `SELECT … FOR UPDATE` с единым порядком блокировок,
  сверка цены и остатков на момент покупки
- 1С: Basic Auth со сравнением в постоянном времени + HMAC-подпись webhook'ов
- TOTP-2FA для админов с защитой от перебора кодов; аудит действий админов
- Санитизация Excel-экспортов (формульные инъекции), SSRF-защита PDF-генератора,
  проверка и нормализация загружаемых изображений

Полный список проведённых доработок — в `REVIEW.md`.

## Переменные окружения

Ключевые: `ENV`, `SECRET_KEY`, `APP_URL`, `DATABASE_URL`, `REDIS_URL`,
`SMTP_*`, `INTEGRATION_USER`/`INTEGRATION_PASSWORD` (1С), `ADMIN_EMAILS`,
`MAX_UPLOAD_SIZE`, `SENTRY_DSN` (опционально). См. `app/config.py` — все переменные
с дефолтами и валидацией описаны там.

## Структура

```
app/
├── main.py            # сборка приложения, middleware
├── config.py          # настройки из .env + валидация
├── models.py          # SQLAlchemy-модели
├── auth.py            # регистрация/вход/выход, 2FA-проверка
├── session.py         # серверные сессии в Redis
├── security.py        # CSRF, rate limit, блокировки входа
├── middleware.py      # security-заголовки, CSP nonce
├── routers/           # страницы: каталог, корзина, заказы, админка, 1С API…
├── services/          # бизнес-логика: заказы, предзаказы, Excel, PDF, TOTP…
└── templates/         # Jinja2-шаблоны (Bootstrap 5)
tests/                 # pytest
scripts/               # prepare / init-ssl / update / backup
nginx/                 # конфиги nginx (HTTP→HTTPS, rate-limit зоны)
```
