"""
Модуль безопасности Диантуса.
CSRF + rate limiting (Redis или in-memory).
"""
import logging
import os
import secrets
import time
from collections import defaultdict
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_CSRF_SESSION_KEY = "csrf_token"
_rate_buckets: dict[str, list[float]] = defaultdict(list)

_redis_client = None
_redis_enabled = False


# ═══════════════════════════════════════════════════════════
# CSRF
# ═══════════════════════════════════════════════════════════

class CsrfError(Exception):
    def __init__(self, message: str = "Ошибка CSRF-токена"):
        self.message = message
        super().__init__(message)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


async def check_csrf(request: Request) -> None:
    """
    Проверяет CSRF-токен.

    Для multipart/form-data — берём только из заголовка X-CSRF-Token,
    потому что request.form() съедает тело и File() потом пуст.
    """
    session_token = request.session.get(_CSRF_SESSION_KEY)
    if not session_token:
        raise CsrfError("Сессия не содержит CSRF-токен")

    ctype = (request.headers.get("content-type") or "").lower()
    is_multipart = "multipart/form-data" in ctype

    client_token = ""
    if is_multipart:
        client_token = request.headers.get("x-csrf-token", "")
        if not client_token:
            # Фолбэк: иногда шлют просто в form, но у нас File() отвалится.
            raise CsrfError(
                "Для загрузки файлов нужен заголовок X-CSRF-Token"
            )
    else:
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
    IP клиента за Nginx.
    Берём ПЕРВЫЙ адрес из X-Forwarded-For (наш Nginx добавляет реальный IP
    в конец цепочки, но мы доверяем только своему прокси).
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            # Первый — самый ранний; для одиночного прокси это клиент.
            return parts[0][:45]
    if request.client:
        return request.client.host
    return "unknown"


async def _redis_incr(bucket: str, window: int) -> Optional[int]:
    """Инкрементит счётчик в Redis. None если недоступен."""
    if not _redis_enabled or _redis_client is None:
        return None
    try:
        key = f"rl:{bucket}"
        pipe = _redis_client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        res = await pipe.execute()
        return int(res[0])
    except Exception as e:
        logger.warning("Redis rate limit failed: %s", e)
        return None


async def _redis_reset(bucket: str) -> None:
    if not _redis_enabled or _redis_client is None:
        return
    try:
        await _redis_client.delete(f"rl:{bucket}")
    except Exception:
        pass


async def check_rate_limit(
    request: Request,
    key: str,
    max_hits: int = 10,
    window: int = 60,
) -> None:
    """
    Асинхронная версия — использует Redis, если подключён.
    """
    ip = _client_ip(request)
    bucket = f"{key}:{ip}"

    if _redis_enabled:
        count = await _redis_incr(bucket, window)
        if count is not None and count > max_hits:
            raise HTTPException(
                status_code=429,
                detail=f"Слишком много запросов. Повторите через {window} сек.",
                headers={"Retry-After": str(window)},
            )
        return

    # In-memory fallback
    now = time.time()
    hits = _rate_buckets[bucket]
    hits[:] = [t for t in hits if now - t < window]

    if len(hits) >= max_hits:
        retry_after = int(window - (now - hits[0])) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много запросов. Повторите через {retry_after} сек.",
            headers={"Retry-After": str(retry_after)},
        )
    hits.append(now)


async def reset_rate_limit(request: Request, key: str) -> None:
    ip = _client_ip(request)
    bucket = f"{key}:{ip}"
    _rate_buckets.pop(bucket, None)
    await _redis_reset(bucket)


# ═══════════════════════════════════════════════════════════
# REDIS
# ═══════════════════════════════════════════════════════════

async def init_rate_limiter(redis_url: Optional[str] = None) -> None:
    global _redis_client, _redis_enabled

    if not redis_url:
        redis_url = os.getenv("REDIS_URL", "").strip()

    if not redis_url:
        logger.info("REDIS_URL не задан — rate limiting в памяти (1 воркер)")
        return

    try:
        import redis.asyncio as redis

        _redis_client = redis.from_url(
            redis_url, encoding="utf-8", decode_responses=True
        )
        await _redis_client.ping()
        _redis_enabled = True
        logger.info("🚦 Rate limiter: Redis подключён")
    except Exception as e:
        logger.warning("⚠️ Redis недоступен (%s). In-memory режим", e)
        _redis_client = None
        _redis_enabled = False


async def close_rate_limiter() -> None:
    global _redis_client, _redis_enabled
    if _redis_client is not None:
        try:
            await _redis_client.close()
        except Exception:
            pass
        _redis_client = None
        _redis_enabled = False