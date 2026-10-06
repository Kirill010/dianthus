# app/security.py
"""
CSRF + rate limiting (Redis или in-memory).
+ Прогрессивная блокировка входа по email.
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

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_RATE_CLEANUP_THRESHOLD = 5000

_redis_client = None
_redis_enabled = False

_LUA_INCR_EXPIRE = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return c
"""


# ═══════════════════════════════════════════════════════════
# ПРОГРЕССИВНАЯ БЛОКИРОВКА ВХОДА
# ═══════════════════════════════════════════════════════════

LOGIN_FAILS_TTL = 24 * 3600
LOGIN_LOCK_LEVELS = [
    (3,  10 * 60),      # >= 3 ошибок  → 10 минут
    (6,  30 * 60),      # >= 6 ошибок  → 30 минут
    (9,  60 * 60),      # >= 9 ошибок  → 1 час
    (12, 120 * 60),     # >= 12 ошибок → 2 часа
    (15, 360 * 60),     # >= 15 ошибок → 6 часов
]

_login_fails_mem: dict[str, int] = {}
_login_locks_mem: dict[str, float] = {}


def _fails_key(email: str) -> str:
    return f"lf:{email}"


def _lock_key(email: str) -> str:
    return f"ll:{email}"


def _lock_duration(fails: int) -> int:
    duration = 0
    for threshold, sec in LOGIN_LOCK_LEVELS:
        if fails >= threshold:
            duration = sec
    return duration


async def check_login_lock(email: str) -> int:
    email = (email or "").strip().lower()
    if not email:
        return 0

    if _redis_enabled and _redis_client is not None:
        try:
            ttl = await _redis_client.ttl(_lock_key(email))
            return max(0, int(ttl)) if ttl and ttl > 0 else 0
        except Exception as e:
            logger.warning("check_login_lock redis: %s", e)

    until = _login_locks_mem.get(email, 0.0)
    remaining = int(until - time.time())
    if remaining > 0:
        return remaining
    _login_locks_mem.pop(email, None)
    return 0


async def record_login_failure(email: str) -> int:
    email = (email or "").strip().lower()
    if not email:
        return 0

    fails = 0

    if _redis_enabled and _redis_client is not None:
        try:
            fails = int(await _redis_client.eval(
                _LUA_INCR_EXPIRE, 1, _fails_key(email), LOGIN_FAILS_TTL
            ))
        except Exception as e:
            logger.warning("record_login_failure redis: %s", e)
            fails = 0

    if fails == 0:
        fails = _login_fails_mem.get(email, 0) + 1
        _login_fails_mem[email] = fails

    duration = _lock_duration(fails)
    if duration <= 0:
        return 0

    if _redis_enabled and _redis_client is not None:
        try:
            await _redis_client.set(_lock_key(email), "1", ex=duration)
        except Exception as e:
            logger.warning("record_login_failure set-lock redis: %s", e)

    _login_locks_mem[email] = time.time() + duration
    logger.warning(
        "🔒 Логин %s заблокирован на %d сек (неудач: %d)",
        email, duration, fails,
    )
    return duration


async def reset_login_failures(email: str) -> None:
    email = (email or "").strip().lower()
    if not email:
        return

    _login_fails_mem.pop(email, None)
    _login_locks_mem.pop(email, None)

    if _redis_enabled and _redis_client is not None:
        try:
            await _redis_client.delete(_fails_key(email))
            await _redis_client.delete(_lock_key(email))
        except Exception as e:
            logger.warning("reset_login_failures redis: %s", e)


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


# ═══════════════════════════════════════════════════════════
# RATE LIMITING
# ═══════════════════════════════════════════════════════════

def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host[:45]
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[0][:45]
    return "unknown"


def _cleanup_memory_buckets(window: int) -> None:
    if len(_rate_buckets) < _RATE_CLEANUP_THRESHOLD:
        return
    now = time.time()
    dead = [k for k, hits in _rate_buckets.items()
            if not hits or now - hits[-1] > window * 2]
    for k in dead:
        _rate_buckets.pop(k, None)


async def _redis_incr(bucket: str, window: int) -> Optional[int]:
    if not _redis_enabled or _redis_client is None:
        return None
    try:
        key = f"rl:{bucket}"
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