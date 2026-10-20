"""
Заголовки безопасности для всех ответов.
Защищают от XSS, clickjacking, MIME-sniffing и утечек Referer.

CSP НА НОНСАХ. Для каждого запроса генерируется случайный nonce,
который подставляется и в заголовок Content-Security-Policy, и в
атрибуты встроенных <script nonce="..."> шаблонов (через контекст
csp_nonce в templating.render). Браузер выполняет только скрипты
с совпавшим nonce — внедрённый через XSS инлайн-скрипт без nonce
не запустится, даже если атакующий смог вписать его в HTML.
"""
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import config


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Nonce нужен ДО формирования ответа: его читает templating.render
        # и подставляет в <script nonce="...">.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)

        # Базовые заголовки
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        # Изоляция контекста: нашу страницу нельзя открыть в чужом окне/iframe
        # и внешним сайтам нельзя читать наши ответы из своих скриптов.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

        # CSP. В script-src БОЛЬШЕ НЕТ 'unsafe-inline': выполняются только
        # скрипты с нашим nonce или из наших же файлов. Это главная
        # страховка от хранимого/отражённого XSS.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
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
