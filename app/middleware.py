"""
Заголовки безопасности для всех ответов.
Защищают от XSS, clickjacking, MIME-sniffing и утечек Referer.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import config


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Базовые заголовки
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )

        # CSP (добавлено)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            # Карта Яндекса на странице «Контакты» (iframe).
            # Без frame-src браузер блокирует её из-за default-src 'self'.
            "frame-src 'self' https://yandex.ru https://*.yandex.ru "
            "https://yandex.com https://*.yandex.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )

        # HSTS только в prod
        if config.ENV == "prod":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Скрываем серверные заголовки
        response.headers["Server"] = "Dianthus"

        return response