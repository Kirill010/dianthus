# 🌸 Диантус — Оптовый магазин цветов (B2B)

## ✅ Статус проекта: ГОТОВ К ДЕПЛОЮ

### Что включено:

#### 🎨 Фронтенд (React + Tailwind CSS)
- ✅ Главная страница с Hero-секцией
- ✅ Каталог с фильтрами (страна, категория, цена, поиск)
- ✅ Карточки товаров с модалками
- ✅ Корзина с расчётом доставки
- ✅ Регистрация с валидацией
- ✅ Вход в систему
- ✅ Личный кабинет с историей заказов
- ✅ Админ-панель (заказы, клиенты, товары, поставки)
- ✅ Баннер "Машина в пути"
- ✅ Адаптивный дизайн (mobile-first)
- ✅ Анимации и переходы

#### 🐍 Бэкенд (FastAPI + SQLAlchemy)
- ✅ REST API для всех операций
- ✅ Модели: User, Product, Order, OrderItem, Supply
- ✅ Авторизация (bcrypt + SHA-256)
- ✅ Сессии (itsdangerous)
- ✅ Rate limiting (встроенный)
- ✅ Валидация форм
- ✅ CRUD для товаров, заказов, клиентов
- ✅ Интеграция с 1С (REST API)
- ✅ Excel импорт/экспорт
- ✅ Обработка изображений (WebP)
- ✅ Docker + docker-compose
- ✅ Nginx + SSL (Let's Encrypt)

#### 🔒 Безопасность
- ✅ Убран Sentry (не нужен)
- ✅ Исправлен bcrypt 72-byte лимит
- ✅ CSRF защита
- ✅ Rate limiting
- ✅ Security headers (HSTS, CSP, X-Frame-Options)
- ✅ Валидация файлов
- ✅ SQL-инъекции исключены (ORM)
- ✅ XSS защита (Jinja2 автоэкранирование)

---

## 🚀 Быстрый старт

### Фронтенд (демо)

```bash
# Установка зависимостей
npm install

# Запуск в режиме разработки
npm run dev

# Сборка для production
npm run build
```

Откройте http://localhost:5173

### Бэкенд (Python FastAPI)

```bash
cd backend

# Создание виртуального окружения
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Установка зависимостей
pip install -r requirements.txt

# Копирование .env
cp .env.example .env
# Отредактируйте .env (SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD)

# Запуск
python run.py
```

Откройте http://localhost:8000/docs (Swagger UI)

### Docker (production)

```bash
cd backend

# Сборка и запуск
docker-compose up -d

# Просмотр логов
docker-compose logs -f app
```

---

## 📁 Структура проекта

```
dianthus/
├── src/                          # Фронтенд (React)
│   ├── components/               # Компоненты
│   │   ├── Header.tsx
│   │   ├── Hero.tsx
│   │   ├── Catalog.tsx
│   │   ├── About.tsx
│   │   ├── Delivery.tsx
│   │   ├── Registration.tsx
│   │   ├── Login.tsx
│   │   ├── Dashboard.tsx
│   │   ├── Cart.tsx
│   │   ├── Footer.tsx
│   │   ├── SupplyBanner.tsx
│   │   └── AdminPanel.tsx
│   ├── data/
│   │   └── mockData.ts          # Моковые данные
│   ├── types.ts                  # TypeScript типы
│   ├── App.tsx                   # Главный компонент
│   ├── main.tsx                  # Точка входа
│   └── index.css                 # Стили (Tailwind)
│
├── backend/                      # Бэкенд (FastAPI)
│   ├── app/
│   │   ├── routers/
│   │   │   ├── catalog.py       # Каталог
│   │   │   ├── cart.py          # Корзина и заказы
│   │   │   ├── admin.py         # Админ-панель
│   │   │   └── onec_api.py      # Интеграция с 1С
│   │   ├── services/
│   │   │   ├── excel.py         # Excel импорт/экспорт
│   │   │   └── images.py        # Обработка изображений
│   │   ├── auth.py              # Авторизация
│   │   ├── bootstrap.py         # Создание админа
│   │   ├── config.py            # Конфигурация
│   │   ├── database.py          # Подключение к БД
│   │   ├── deps.py              # Зависимости
│   │   ├── main.py              # Точка входа
│   │   ├── middleware.py        # Middleware
│   │   ├── models.py            # SQLAlchemy модели
│   │   ├── security.py          # Безопасность
│   │   ├── session.py           # Сессии
│   │   └── validators.py        # Валидация
│   ├── .env.example             # Пример конфигурации
│   ├── requirements.txt         # Python зависимости
│   ├── run.py                   # Запуск приложения
│   ├── make_admin.py            # Выдать админа
│   ├── reset_db.py              # Сброс БД
│   ├── Dockerfile               # Docker образ
│   └── docker-compose.yml       # Docker Compose
│
├── index.html                    # HTML шаблон
├── package.json                  # NPM зависимости
├── vite.config.ts               # Vite конфигурация
├── tailwind.config.js           # Tailwind конфигурация
├── tsconfig.json                # TypeScript конфигурация
├── DEPLOY_GUIDE.md              # Подробная инструкция по деплою
└── README.md                    # Этот файл
```

---

## 🔧 Исправленные баги

1. ✅ **bcrypt 72-byte лимит** — кириллица в пароле падала с 500
   - Решение: SHA-256 → bcrypt

2. ✅ **`require_admin` возвращал 403** — теперь редирект на `/login`
   - Исправлено в `deps.py`

3. ✅ **`app/static/uploads` не создавалась** — падало на чистой репе
   - Исправлено в `main.py`: `mkdir(parents=True, exist_ok=True)`

4. ✅ **Импорт Excel без проверки расширения** — теперь только `.xlsx`
   - Исправлено в `services/excel.py`

5. ✅ **Двойной запрос `get_current_user`** — устранено
   - Оптимизировано в `deps.py`

6. ✅ **`make_admin.py` не работал вне корня** — исправлен `sys.path`
   - Исправлено в `make_admin.py`

7. ✅ **`update_order_status` молчал при несуществующем заказе**
   - Добавлена явная проверка и HTTPException

8. ✅ **`bootstrap.py` не логировал пропуск админа**
   - Добавлено логирование

9. ✅ **Sentry** — убран (платно, не нужен)
   - Удалён из `requirements.txt`
   - Заменён на стандартное логирование

---

## 🌐 Интеграция с 1С

### API Endpoints

```
POST /api/1c/products/import     # Импорт товаров из 1С
GET  /api/1c/orders/export       # Экспорт заказов в 1С
POST /api/1c/stocks/update       # Обновление остатков
POST /api/1c/orders/{id}/status  # Обновление статуса заказа
GET  /api/1c/health              # Проверка работоспособности
```

### Аутентификация

Все запросы к `/api/1c/*` требуют заголовок:
```
X-API-Key: ваш_секретный_ключ
```

Ключ указывается в `.env` как `ONEC_API_KEY`.

### Пример запроса (импорт товаров)

```bash
curl -X POST https://dianthus-opt.ru/api/1c/products/import \
  -H "X-API-Key: your_api_key" \
  -H "Content-Type: application/json" \
  -d '[
    {
      "id_1c": "12345",
      "name": "Роза Freedom",
      "price": 110,
      "stock": 450,
      "country": "Эквадор",
      "length": 60,
      "unit": "шт",
      "pack_size": 25,
      "min_order": 25
    }
  ]'
```

---

## 💰 Стоимость хостинга

| Услуга | Провайдер | Цена/мес |
|--------|-----------|----------|
| VPS 2CPU/4GB | Timeweb | ~600₽ |
| Домен .ru | Reg.ru | ~200₽/год |
| SSL | Let's Encrypt | Бесплатно |
| Redis | Встроен в VPS | Бесплатно |
| PostgreSQL | Встроен в VPS | Бесплатно |
| **Итого** | | **~650₽/мес** |

---

## 📚 Документация

- **DEPLOY_GUIDE.md** — подробная инструкция по деплою (10 этапов)
- **Swagger UI** — http://localhost:8000/docs (только в dev)
- **ReDoc** — http://localhost:8000/redoc (только в dev)

---

## 🧪 Тестирование

### Фронтенд
1. Откройте http://localhost:5173
2. Проверьте все страницы (Главная, Каталог, О нас, Доставка)
3. Зарегистрируйтесь (демо-режим)
4. Войдите как админ (кнопка "Вход для администратора")
5. Проверьте админ-панель

### Бэкенд
1. Откройте http://localhost:8000/docs
2. Протестируйте все endpoints
3. Создайте товар через `/admin/products`
4. Зарегистрируйте клиента через `/register`
5. Одобрите клиента через `/admin/customers/{id}/approve`
6. Оформите заказ через `/cart/checkout`

---

## 📞 Поддержка

При возникновении проблем:

1. **Фронтенд:**
   ```bash
   npm run build  # Проверка сборки
   npm run dev    # Запуск в dev-режиме
   ```

2. **Бэкенд:**
   ```bash
   # Логи
   journalctl -u dianthus -f

   # Проверка БД
   psql -U dianthus dianthus -c "SELECT count(*) FROM products;"

   # Проверка Redis
   redis-cli ping
   ```

3. **Docker:**
   ```bash
   docker-compose logs -f app
   docker-compose restart
   ```

---

## 📝 Лицензия

MIT License

---

## 👨‍💻 Автор

Проект создан для ООО «Диантус» — оптовые поставки цветов.

**Версия:** 1.0.0
**Дата:** Октябрь 2026
**Статус:** ✅ Готов к production
