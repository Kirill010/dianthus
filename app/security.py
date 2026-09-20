"""CSRF-токены и rate limiting."""
import logging
import secrets
import time
from collections import defaultdict
from threading import Lock

from fastapi import Form, HTTPException, Request, status

logger = logging.getLogger(__name__)


class CsrfError(Exception):
    """CSRF не прошёл. Ловим в main.py и редиректим красиво."""
    def __init__(self, message: str = "Сессия истекла. Обновите страницу."):
        self.message = message
        super().__init__(message)


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


async def check_csrf(request: Request, csrf_token: str = Form("")) -> None:
    expected = request.session.get("csrf_token")
    if not expected:
        raise CsrfError("Сессия истекла. Обновите страницу.")
    if not csrf_token or not secrets.compare_digest(expected, csrf_token):
        raise CsrfError("Недействительный CSRF-токен. Обновите страницу.")


_RATE_WINDOW = 60
_RATE_MAX = 10
_rate_buckets: dict[str, list[float]] = defaultdict(list)
_rate_lock = Lock()


def check_rate_limit(request: Request, scope: str = "default",
                     max_hits: int = _RATE_MAX,
                     window: int = _RATE_WINDOW) -> None:
    ip = request.client.host if request.client else "unknown"
    key = f"{scope}:{ip}"
    now = time.time()
    with _rate_lock:
        if len(_rate_buckets) > 10_000:
            for k in list(_rate_buckets.keys()):
                _rate_buckets[k][:] = [t for t in _rate_buckets[k]
                                       if now - t < window]
                if not _rate_buckets[k]:
                    del _rate_buckets[k]
        bucket = _rate_buckets[key]
        bucket[:] = [t for t in bucket if now - t < window]
        if len(bucket) >= max_hits:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Слишком много попыток. Подождите.")
        bucket.append(now)


def reset_rate_limit(request: Request, scope: str = "default") -> None:
    ip = request.client.host if request.client else "unknown"
    with _rate_lock:
        _rate_buckets.pop(f"{scope}:{ip}", None)