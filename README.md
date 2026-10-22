# 🌸 Диантус — B2B-портал оптовых продаж цветов

Серверный веб-сайт оптового цветочного магазина: каталог с живыми остатками,
предзаказы под поставки, персональные скидки, **двусторонний обмен с 1С**, счета в
PDF, выгрузки в Excel. Рендеринг — серверный (Jinja2), без отдельного
фронтенд-приложения.

## Документация

| Документ | Для чего |
|---|---|
| **[docs/GUIDE_FROM_SCRATCH.md](docs/GUIDE_FROM_SCRATCH.md)** | Всё «с нуля»: запуск у себя на компьютере, покупка домена и сервера, DNS, SSH, Docker, HTTPS, почта, бэкапы, обновления, безопасность, решение проблем |
| **[docs/1C_INTEGRATION.md](docs/1C_INTEGRATION.md)** | Обмен с 1С: порядок, форматы, примеры запросов и кода 1С, диагностика |
| [DEPLOY.md](DEPLOY.md) | Перенос уже работающего сайта на новый сервер |
| [REVIEW.md](REVIEW.md) | Что проверено и исправлено (три раунда ревью) и что осталось на будущее |

## Стек

- **FastAPI + Jinja2** (server-side rendering, Bootstrap 5)
- **SQLAlchemy 2.0** — PostgreSQL в проде, SQLite для разработки и тестов
- **Redis** — серверные сессии (в них корзина), лимиты запросов, блокировки входа
- **Docker + docker-compose** — app / db / redis / nginx / certbot
- **pytest** — автотесты (128), в том числе на обмен с 1С и оформление заказов

## Возможности

**Для клиентов**
- Каталог с фильтрами (страна, категория, цена, длина стебля, поиск с подсказками)
- Живые остатки по поставкам; резервирование под предзаказы
- Корзина, оформление заказа с персональной скидкой, счёт на оплату в PDF (с банковскими реквизитами)
- История заказов, уведомления в кабинете и **письма** (заявка одобрена, заказ принят, смена статуса)
- Регистрация с согласием на обработку персональных данных (152-ФЗ), страница политики

**Для администраторов**
- Дашборд: заказы (поиск/фильтр/пагинация), клиенты (модерация заявок, скидки),
  товары, поставки, остатки — с живым поиском по вкладкам
- Смена статуса заказа с **возвратом товара на склад при отмене**
- Импорт товаров и накладных из Excel (повторная загрузка не дублирует), экспорт заказов/клиентов
- Двухфакторная аутентификация (TOTP, коды одноразовые), **журнал действий** (`/admin/audit`)

**Обмен с 1С** (`/api/1c/...`, HTTP Basic по HTTPS)
- 1С → сайт: каталог/цены/остатки (в упаковках или штуках, «полный снимок»), статусы заказов, скидки клиентов
- сайт → 1С: заказы («забрал и подтвердил»: `orders/export?new=1` + `orders/ack`), каталог, клиенты
- устойчивость: плохие строки не роняют обмен, поля обрезаются под колонки БД, защита от перепродажи

## Быстрый старт (разработка)

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }   # не перезаписывает существующий .env
python seed_demo.py        # демо-товары, клиент и заказ (только для разработки)
python run.py              # http://127.0.0.1:8000
```

**macOS / Linux:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
[ -f .env ] || cp .env.example .env        # не перезаписывает существующий .env
python seed_demo.py && python run.py
```

Вход: админ `admin@example.ru` / `Admin12345`, клиент `client@example.ru` / `Client12345`.
Если у вас уже есть `.env` с боевыми настройками (`ENV=prod`) — для разработки переименуйте его
(`Rename-Item .env .env.server`), иначе вход на `http://127.0.0.1` не сработает (cookie только по HTTPS).
По умолчанию SQLite (`dianthus.db`) и in-memory лимиты — Redis для разработки не нужен.

Тесты (временная база в памяти, почта не отправляется):

```bash
pytest -q
```

Проверка зависимостей на известные уязвимости:

```bash
pip-audit -r requirements.txt --no-deps --disable-pip
```

## Деплой на VPS (production)

Подробно, для новичка, с покупкой домена и сервера — в
[docs/GUIDE_FROM_SCRATCH.md](docs/GUIDE_FROM_SCRATCH.md). Коротко:

```bash
git clone <repo> /opt/dianthus && cd /opt/dianthus
bash scripts/prepare.sh ваш-домен.ru admin@ваш-домен.ru   # .env со случайными паролями + HTTP-конфиг nginx
nano .env                                                 # SMTP, контакты магазина, реквизиты
docker compose up -d --build
bash scripts/init-ssl.sh admin@ваш-домен.ru               # сертификат Let's Encrypt и HTTPS
```

Обновление после `git push`: `bash scripts/update.sh` (бэкап → git pull → пересборка →
проверка nginx → ожидание `healthy`). Бэкапы: `bash scripts/backup.sh` (+ cron, + копия
на другой сервер через `BACKUP_REMOTE`).

## Безопасность (кратко)

- Пароли — bcrypt; вход — с прогрессивной блокировкой, лимитами (Redis, при сбое Redis —
  в памяти) и выравниванием времени ответа (нельзя узнать, есть ли email)
- Серверные сессии в Redis, подписанная cookie только с id; ротация id при входе/выходе
- CSRF-токены + проверка Origin на всех POST; TrustedHost в prod
- CSP с per-request nonce (inline-обработчики запрещены — подтверждения сделаны через
  `data-confirm`), HSTS, COOP/CORP
- Заказ оформляется атомарно: `SELECT … FOR UPDATE` с единым порядком блокировок,
  сверка цены и остатков; повторная отправка формы не создаёт второй заказ
- 1С: Basic Auth со сравнением в постоянное время, лимит запросов, проверка и очистка
  входных данных; рекомендуется ограничение по IP на уровне nginx
- TOTP-2FA для админов: одноразовые коды, защита от перебора; аудит действий админов и 1С
- Санитизация Excel-экспортов (формульные инъекции), SSRF-защита PDF-генератора,
  проверка и нормализация загружаемых изображений (лимит пикселей)
- Зависимости закреплены и проверены `pip-audit`

Полный список проведённых доработок — в [REVIEW.md](REVIEW.md).

## Переменные окружения

Полный список с комментариями — в [.env.example](.env.example); все переменные с
дефолтами и валидацией описаны в `app/config.py`.

| Группа | Переменные |
|---|---|
| Основные | `ENV` (`dev`/`prod`), `SECRET_KEY`, `APP_URL`, `DATABASE_URL`, `REDIS_URL` |
| Магазин | `SHOP_NAME`, `SHOP_PHONE`, `SHOP_ADDRESS`, `SHOP_EMAIL`, `SHOP_REQUISITES`, `SHOP_TZ` (по умолчанию `Europe/Moscow`) |
| Администратор | `ADMIN_EMAIL`, `ADMIN_PASSWORD` (только для первого создания), `ADMIN_FORCE_PASSWORD_RESET` |
| Почта | `SMTP_HOST/PORT/USER/PASSWORD/FROM/TO/USE_SSL` |
| 1С | `INTEGRATION_USER`, `INTEGRATION_PASSWORD`, `ONEC_RESERVE_UNEXPORTED` |
| Прочее | `WEB_CONCURRENCY`, `MAX_UPLOAD_SIZE`, `SENTRY_DSN`, `BACKUP_REMOTE`, `PIP_INDEX_URL` |

## Структура

```
app/
├── main.py            сборка приложения, обработчики ошибок, /health, /sitemap.xml, /privacy
├── config.py          настройки из .env + валидация
├── models.py          SQLAlchemy-модели
├── migrations.py      авто-миграция (колонки, индексы) + подготовка БД при старте
├── auth.py            регистрация/вход/выход, 2FA-проверка
├── session.py         серверные сессии в Redis
├── security.py        CSRF, rate limit, блокировки входа
├── middleware.py      security-заголовки, CSP nonce
├── web.py, timeutils.py   безопасный редирект «назад», время по поясу магазина
├── routers/           каталог, корзина, профиль, админка, обмен с 1С
├── services/          orders (статусы и склад), pricing (деньги), notifier (письма),
│                      excel, pdf, preorder, totp, audit…
└── templates/         Jinja2-шаблоны (Bootstrap 5)
tests/                 pytest (в т.ч. регрессионные тесты на найденные ошибки)
scripts/               prepare / init-ssl / update / backup / restore / проверка 1С
nginx/                 конфиги nginx (HTTP→HTTPS, rate-limit зоны)
docs/                  руководство «с нуля», обмен с 1С
```
