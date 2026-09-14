"""
Безопасность: CSRF-токены и in-memory rate limiting.

Rate limiting хранится в памяти процесса — при нескольких воркерах
u vicorn лимиты будут раздельными. Для продакшена замени на Redis.
"""
import logging
import secrets
import time
from collections import defaultdict
from threading import Lock

from fastapi import Form, HTTPException, Request, status


logger = logging.getLogger(__name__)


# ═══════ CSRF ═══════════════════════════════════════════════

def ensure_csrf_token(request: Request) -> str:
    """Возвращает существующий CSRF-токен или создаёт новый."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def check_csrf(request: Request, csrf_token: str = Form("")) -> None:
    """
    FastAPI-зависимость: проверяет CSRF-токен из формы
    против токена в сессии.
    """
    expected = request.session.get("csrf_token")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Сессия истекла. Обновите страницу.",
        )
    if not csrf_token or not secrets.compare_digest(expected, csrf_token):
        logger.warning(
            "CSRF-токен не совпал: ip=%s path=%s",
            request.client.host if request.client else "?",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недействительный CSRF-токен. Обновите страницу.",
        )


# ═══════ RATE LIMITING ══════════════════════════════════════

_RATE_LIMIT_WINDOW = 60
_RATE_LIMIT_MAX = 10

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def check_rate_limit(
    request: Request,
    scope: str = "default",
    max_hits: int = _RATE_LIMIT_MAX,
    window: int = _RATE_LIMIT_WINDOW,
) -> None:
    """In-memory rate limit. Кидает HTTP 429 при превышении."""
    ip = request.client.host if request.client else "unknown"
    key = f"{scope}:{ip}"
    now = time.time()

    with _rate_lock:
        # Периодически чистим устаревшие ключи
        if len(_rate_buckets) > 10_000:
            for k in list(_rate_buckets.keys()):
                _rate_buckets[k][:] = [t for t in _rate_buckets[k]
                                       if now - t < window]
                if not _rate_buckets[k]:
                    del _rate_buckets[k]

        bucket = _rate_buckets[key]
        bucket[:] = [t for t in bucket if now - t < window]

        if len(bucket) >= max_hits:
            logger.warning("Rate limit сработал: %s (%d попыток)",
                           key, len(bucket))
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много попыток. Подождите минуту.",
            )

        bucket.append(now)


def reset_rate_limit(request: Request, scope: str = "default") -> None:
    """Сбрасывает счётчик после успешной операции (например, логина)."""
    ip = request.client.host if request.client else "unknown"
    key = f"{scope}:{ip}"
    with _rate_lock:
        _rate_buckets.pop(key, None)