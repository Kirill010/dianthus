# dianthus
# 🌸 Диантус — оптовый магазин цветов

Веб-приложение на **FastAPI + SQLAlchemy + Jinja2** для оптовой продажи цветов.
Только для юрлиц и ИП: клиент регистрируется → админ одобряет → клиент делает заказы.

---

## ✨ Возможности

### Для клиента
- Регистрация с модерацией (админ одобряет вручную)
- Каталог с фильтрами (поиск, страна, цена, длина) и пагинацией
- Карточка товара + корзина + оформление заказа
- История заказов со статусами

### Для админа
- Модерация заявок (одобрить / отклонить)
- Управление заказами (статусы: Новый, В работе, Отправлен, Выполнен, Отменён)
- CRUD товаров (с загрузкой картинок → WebP, авто-сжатие)
- CRUD поставок (рейсы машин из стран-производителей)
- Импорт товаров из Excel
- Экспорт заказов и клиентов в Excel
- Баннер «Машина в пути» для всех клиентов

---

## 🚀 Быстрый старт

### 1. Клонирование и окружение

```bash
git clone <your-repo> dianthus
cd dianthus

python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install -r requirements.txt
```

### 2. Настройка `.env`

Создайте файл `.env` в корне проекта:

```env
ENV=dev
SECRET_KEY=замените_на_свой_секрет_минимум_32_символа
APP_URL=

# Вариант A: SQLite (для быстрого старта)
DATABASE_URL=

# Вариант B: PostgreSQL (для продакшена)
# DATABASE_URL=

PAGE_SIZE=12

# Автосоздание админа при первом запуске
ADMIN_EMAIL=
ADMIN_PASSWORD=
ADMIN_NAME=
ADMIN_COMPANY=
ADMIN_PHONE=
```

**Сгенерировать SECRET_KEY:**

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### 3. Запуск

```bash
python run.py
```

Откройте http://127.0.0.1:8000 — редиректнет на `/login`.
Войдите с `ADMIN_EMAIL` / `ADMIN_PASSWORD` из `.env`.

---

## 🗄️ Работа с базой данных

### SQLite (по умолчанию)
Файл `dianthus.db` создаётся автоматически при первом запуске.

### PostgreSQL (продакшен)

```bash
sudo -u postgres psql

CREATE USER dianthus_user WITH PASSWORD 'dianthus_pass';
CREATE DATABASE dianthus OWNER dianthus_user;

ALTER SCHEMA public OWNER TO dianthus_user;

GRANT ALL ON SCHEMA public TO dianthus_user;
GRANT ALL PRIVILEGES ON DATABASE dianthus TO dianthus_user;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO dianthus_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO dianthus_user;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dianthus_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dianthus_user;

SELECT current_database(), current_user, current_schema();
\q
```

Затем в `.env`:
```env
DATABASE_URL=postgresql://dianthus_user:dianthus_pass@localhost:5432/dianthus
```

### Сброс БД (удалит все данные!)

```bash
python reset_db.py
```

### Выдать админку существующему пользователю

```bash
python make_admin.py user@example.com
```

---

## 📊 Excel

### Шаблон импорта товаров

Скачать: `/admin` → «Товары» → «Шаблон Excel»

Колонки:
| Название | Цена (опт) | Остаток | Единица | В упаковке | Мин. заказ | Страна | Длина, см | Описание |
|---|---|---|---|---|---|---|---|---|
| Роза Freedom | 110 | 40 | упаковка | 25 | 1 | Эквадор | 60 | Классическая красная |

### Экспорт

- `/admin/orders/export` — все заказы (одна строка на позицию)
- `/admin/customers/export` — клиенты с заказами

---

## 🏗️ Архитектура

### Слои

```
HTTP-запрос → Middleware (Session) → Роутер → Зависимости → БД → Jinja2-шаблон → HTML
```

### Роутеры

- `app/auth.py` — регистрация / вход / выход
- `app/routers/catalog.py` — каталог, фильтры, карточка товара
- `app/routers/cart.py` — корзина, оформление, история заказов
- `app/routers/admin.py` — админка + Excel

### Модели (`app/models.py`)

- `User` — клиент / админ (is_approved, is_admin)
- `Supply` — рейс машины (страна, даты, статус)
- `Product` — товар (цена, остаток, страна, длина)
- `Order` — заказ клиента
- `OrderItem` — позиция заказа (product_name, price копируются)

### Сессии

`SessionMiddleware` хранит `user_id`, `cart`, `flash`, `login_error`.
`get_current_user` достаёт пользователя из БД по `user_id`.

### Права доступа

- `get_current_user` → `User | None`
- `require_user` → редирект на `/login`, если гость
- `require_admin` → редирект, если не админ

---

## 🐛 Что было исправлено

1. **bcrypt 72-байтный лимит** — кириллица в пароле падала с 500.
2. **`require_admin` возвращал 403** — теперь редирект на `/login`.
3. **`app/static/uploads` не создавалась** — падало на чистой репе.
4. **Импорт Excel без проверки расширения** — теперь только `.xlsx`.
5. **Двойной запрос `get_current_user`** — устранено.
6. **`make_admin.py` не работал вне корня** — исправлен `sys.path`.
7. **`update_order_status`** молчал при несуществующем заказе — добавлен flash.
8. **`bootstrap.py`** не логировал пропуск админа — добавлено.

---

## 🧪 Тесты (ручные)

1. Регистрация клиента → видим `/registration_pending`.
2. Вход админом → `/admin` → вкладка «Клиенты» → «Одобрить».
3. Вход клиентом → каталог → добавить в корзину → оформить.
4. Админ меняет статус заказа → клиент видит новый статус.
5. Импорт Excel с двумя товарами → появляются в каталоге.
6. Экспорт заказов → открывается .xlsx.

---

## 🔒 Безопасность

- Пароли — bcrypt с солью.
- `SECRET_KEY` в prod-режиме обязателен (см. `config.py`).
- Session cookie: `SameSite=Lax`, `https_only=True` в prod.
- SQL-инъекции исключены (SQLAlchemy ORM).
- XSS — Jinja2 автоэкранирует переменные.
- Валидация форм — `app/validators.py`.

---

## 📁 Полезные команды

```bash
# Запуск
python run.py

# Сброс БД
python reset_db.py

# Выдать админа
python make_admin.py user@example.com

# Проверить здоровье
curl http://127.0.0.1:8000/health
```

---

## 📞 Контакты

ООО «Диантус» — оптовые поставки цветов.