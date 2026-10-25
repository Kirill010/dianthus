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
import time

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import config


class ExchangeLogMiddleware:
    """Пишет каждое обращение 1С (/api/1c/*) в журнал обмена.

    Обычный ASGI-слой, а не BaseHTTPMiddleware: ему нужен только код ответа и
    пометки, которые обработчик оставил в scope["state"]["exchange"]. Запись в
    базу идёт в отдельном потоке и уже ПОСЛЕ отправки ответа — 1С не ждёт.
    """

    PREFIX = "/api/1c/"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.PREFIX):
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            from .services import exchange_log
            client = scope.get("client")
            await run_in_threadpool(
                exchange_log.record,
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=status["code"],
                ip=(client[0] if client else "")[:45],
                duration_ms=int((time.perf_counter() - started) * 1000),
                info=(scope.get("state") or {}).get("exchange"),
            )


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
