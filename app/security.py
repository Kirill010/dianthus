# app/security.py
"""
CSRF + rate limiting (Redis или in-memory).
"""
import logging
import os
import secrets
import time
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_CSRF_SESSION_KEY = "csrf_token"

# In-memory rate limit
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_CLEANUP_THRESHOLD = 5000  # после этого числа ключей чистим

_redis_client = None
_redis_enabled = False

# Lua-скрипт: атомарный INCR + EXPIRE (fix #10 — race между INCR и TTL)
_LUA_INCR_EXPIRE = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return c
"""


# CSRF

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


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


async def check_csrf(request: Request) -> None:
    session_token = request.session.get(_CSRF_SESSION_KEY)
    if not session_token:
        raise CsrfError("Сессия не содержит CSRF-токен")

    client_token = request.headers.get("x-csrf-token", "") or ""
    if not client_token:
        try:
            form = await request.form()
            value = form.get("csrf_token")
            if value is not None:
                client_token = str(value)
        except Exception as e:
            logger.debug("check_csrf: form() failed: %s", e)

    if not client_token:
        raise CsrfError("Отсутствует CSRF-токен")

    if not secrets.compare_digest(str(session_token), str(client_token)):
        raise CsrfError("Неверный CSRF-токен")

    origin = (
        request.headers.get("origin")
        or request.headers.get("referer", "")
    )
    if origin:
        origin_host = _host_of(origin)
        req_host = (request.url.hostname or "").lower()
        from .config import config
        app_host = _host_of(config.APP_URL)
        allowed = {h for h in (req_host, app_host) if h}
        if origin_host and origin_host not in allowed:
            logger.warning(
                "CSRF: неверный Origin/Referer: %s (allowed=%s)",
                origin, allowed,
            )
            raise CsrfError("Неверный источник запроса")


# RATE LIMITING

def _client_ip(request: Request) -> str:
    """
    fix #5: доверяем только тому, что уже распарсил uvicorn
    (--proxy-headers + --forwarded-allow-ips=127.0.0.1).
    XFF-заголовок читаем ТОЛЬКО если uvicorn его не подменил
    (например, dev-режим без nginx).
    """
    if request.client and request.client.host:
        return request.client.host[:45]
    # Фоллбэк для нестандартных прокси
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[0][:45]
    return "unknown"


def _cleanup_memory_buckets(window: int) -> None:
    """fix #8: не даём _rate_buckets расти бесконечно."""
    if len(_rate_buckets) < _RATE_CLEANUP_THRESHOLD:
        return
    now = time.time()
    dead = [k for k, hits in _rate_buckets.items()
            if not hits or now - hits[-1] > window * 2]
    for k in dead:
        _rate_buckets.pop(k, None)
    logger.info("Rate-limit cleanup: удалено %d ключей, осталось %d",
                len(dead), len(_rate_buckets))


async def _redis_incr(bucket: str, window: int) -> Optional[int]:
    if not _redis_enabled or _redis_client is None:
        return None
    try:
        key = f"rl:{bucket}"
        # fix #10: атомарный INCR+EXPIRE через Lua
        count = await _redis_client.eval(_LUA_INCR_EXPIRE, 1, key, window)
        return int(count)
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

    _cleanup_memory_buckets(window)

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


# REDIS

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
            redis_url, encoding="utf-8", decode_responses=True,
            socket_timeout=2, socket_connect_timeout=2,
            health_check_interval=30,
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


def is_redis_enabled() -> bool:
    return _redis_enabled


def get_redis_client():
    return _redis_client