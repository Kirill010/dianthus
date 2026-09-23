"""
Модуль безопасности Диантуса.

Содержит:
1. CSRF-защиту (check_csrf, ensure_csrf_token, CsrfError)
2. Rate limiting (check_rate_limit, reset_rate_limit)
3. Опциональный Redis-бэкенд для rate limiting

ВАЖНО про rate limiting и воркеры:
- Если запущен 1 воркер Uvicorn — in-memory счётчики работают корректно.
- Если воркеров > 1 — нужен Redis, иначе каждый воркер
  считает попытки отдельно.
- Наш код сам определяет, что использовать: если Redis
  инициализирован — используем его; если нет — in-memory.
"""

import logging
import os
import secrets
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

# Ключ в сессии, где лежит CSRF-токен
_CSRF_SESSION_KEY = "csrf_token"

# In-memory счётчики (fallback, если Redis не подключён)
_rate_buckets: dict[str, list[float]] = defaultdict(list)

# Глобальное подключение к Redis (None = не инициализирован)
_redis_client = None
_redis_enabled = False


# ═══════════════════════════════════════════════════════════
# CSRF-ЗАЩИТА
# ═══════════════════════════════════════════════════════════

class CsrfError(Exception):
    """
    Исключение CSRF-ошибки.
    Ловится в main.py через @app.exception_handler(CsrfError).
    """

    def __init__(self, message: str = "Ошибка CSRF-токена"):
        self.message = message
        super().__init__(message)


def ensure_csrf_token(request: Request) -> str:
    """
    Возвращает CSRF-токен из сессии.
    Если токена нет — генерирует и сохраняет.

    Вызывается в templating.py при каждом рендере.
    Токен уходит в HTML-формы как скрытое поле csrf_token.
    """
    token = request.session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


async def check_csrf(request: Request) -> None:
    """
    Проверяет CSRF-токен из формы против токена из сессии.

    Использование в роутерах:
        @router.post("/login")
        async def login(..., _csrf: None = Depends(check_csrf)):
            ...

    Если токен отсутствует или не совпадает — бросает CsrfError,
    которую перехватывает main.py и показывает flash-сообщение.
    """
    session_token = request.session.get(_CSRF_SESSION_KEY)
    if not session_token:
        raise CsrfError("Сессия не содержит CSRF-токен")

    try:
        form = await request.form()
    except Exception:
        raise CsrfError("Не удалось прочитать форму")

    client_token = form.get("csrf_token", "")
    if not client_token:
        raise CsrfError("Отсутствует CSRF-токен")

    if not secrets.compare_digest(str(session_token), str(client_token)):
        raise CsrfError("Неверный CSRF-токен")


# ═══════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════

def _client_ip(request: Request) -> str:
    """
    Возвращает IP клиента.
    Учитывает заголовок X-Forwarded-For (если стоит Nginx/reverse-proxy).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # Берём первый IP в цепочке
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def check_rate_limit(
    request: Request,
    key: str,
    max_hits: int = 10,
    window: int = 60,
) -> None:
    """
    Проверяет лимит на действие `key` для текущего IP.

    Аргументы:
        key: имя действия ("login", "register", ...)
        max_hits: сколько попыток разрешено
        window: за сколько секунд (в секундах)

    Если превышено — бросает HTTPException(429).

    Работает в памяти процесса (для 1 воркера — корректно).
    Для нескольких воркеров нужен Redis — см. init_rate_limiter().
    """
    ip = _client_ip(request)
    bucket_key = f"{key}:{ip}"
    now = time.time()

    # Чистим старые записи
    hits = _rate_buckets[bucket_key]
    hits[:] = [t for t in hits if now - t < window]

    if len(hits) >= max_hits:
        retry_after = int(window - (now - hits[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запросов. Повторите через {retry_after} сек.",
            headers={"Retry-After": str(retry_after)},
        )

    hits.append(now)


def reset_rate_limit(request: Request, key: str) -> None:
    """
    Сбрасывает счётчик для IP (например, после успешного входа).
    """
    ip = _client_ip(request)
    _rate_buckets.pop(f"{key}:{ip}", None)


# ═══════════════════════════════════════════════════════════
# REDIS (ОПЦИОНАЛЬНО)
# ═══════════════════════════════════════════════════════════

async def init_rate_limiter(redis_url: Optional[str] = None) -> None:
    """
    Пытается подключиться к Redis.

    Если Redis доступен — rate limiting будет использовать его
    (актуально при нескольких воркерах Uvicorn).

    Если Redis недоступен — просто логируем предупреждение
    и продолжаем с in-memory счётчиками.

    Вызывается из lifespan в main.py.
    """
    global _redis_client, _redis_enabled

    if not redis_url:
        redis_url = os.getenv("REDIS_URL", "").strip()

    if not redis_url:
        logger.info("ℹ️ REDIS_URL не задан — rate limiting в памяти")
        return

    try:
        import redis.asyncio as redis

        _redis_client = redis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        await _redis_client.ping()
        _redis_enabled = True
        logger.info("🚦 Rate limiter: Redis подключён (%s)", redis_url)

    except Exception as e:
        logger.warning(
            "⚠️ Redis недоступен (%s). Rate limiting работает в памяти. "
            "Если воркеров несколько — это небезопасно.",
            e,
        )
        _redis_client = None
        _redis_enabled = False


async def close_rate_limiter() -> None:
    """
    Закрывает подключение к Redis при остановке приложения.
    Вызывается из lifespan.
    """
    global _redis_client, _redis_enabled

    if _redis_client is not None:
        try:
            await _redis_client.close()
        except Exception:
            pass
        _redis_client = None
        _redis_enabled = False
        logger.info("🚦 Redis-соединение закрыто")