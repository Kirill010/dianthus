# app/session.py
"""Серверные сессии (Redis, с запасным вариантом в памяти).

ЗАЧЕМ. Стандартный SessionMiddleware Starlette хранит ВСЕ данные сессии
в самой cookie, а браузер принимает cookie максимум ~4 КБ. Корзина оптового
клиента с 10–15 позициями уже не помещается: cookie молча отбрасывается,
клиента «выкидывает» из аккаунта, корзина пропадает.

КАК РАБОТАЕТ. В cookie лежит только подписанный случайный идентификатор
сессии, сами данные — в Redis. Интерфейс для кода приложения не меняется:
по-прежнему `request.session["cart"] = [...]`.

БЕЗОПАСНОСТЬ.
  * идентификатор — 256 бит случайности + подпись HMAC с SECRET_KEY
    (подделку отсекаем, не обращаясь к Redis);
  * cookie: HttpOnly, SameSite, Secure (в prod);
  * анонимные сессии (до входа) живут недолго — боты не забьют Redis;
  * `session["_rotate"] = True` выдаёт НОВЫЙ идентификатор (вход/выход) —
    защита от фиксации сессии.
"""
import json
import logging
import secrets
import time
from typing import Any, Callable, Optional

from itsdangerous import BadSignature, TimestampSigner
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

ANON_TTL = 6 * 3600          # сессия без входа в аккаунт — 6 часов
_MEM_CLEANUP_THRESHOLD = 5000


class ServerSessionMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        max_age: int = 7 * 24 * 3600,
        cookie_name: str = "dianthus_sid",
        same_site: str = "lax",
        https_only: bool = False,
        redis_getter: Optional[Callable[[], Any]] = None,
        key_prefix: str = "sess:",
    ) -> None:
        self.app = app
        self.signer = TimestampSigner(str(secret_key))
        self.max_age = int(max_age)
        self.cookie_name = cookie_name
        self.same_site = same_site
        self.https_only = https_only
        self.redis_getter = redis_getter or (lambda: None)
        self.prefix = key_prefix
        self._mem: dict[str, tuple[float, str]] = {}

    # ───────────── хранилище ─────────────

    def _redis(self):
        try:
            return self.redis_getter()
        except Exception:
            return None

    def _mem_cleanup(self) -> None:
        if len(self._mem) < _MEM_CLEANUP_THRESHOLD:
            return
        now = time.time()
        for k in [k for k, (exp, _) in self._mem.items() if exp < now]:
            self._mem.pop(k, None)

    async def _load(self, sid: str) -> Optional[dict]:
        raw: Optional[str] = None
        client = self._redis()
        if client is not None:
            try:
                raw = await client.get(self.prefix + sid)
            except Exception as e:
                logger.warning("session: Redis get: %s", e)
        if raw is None:
            item = self._mem.get(sid)
            if item and item[0] > time.time():
                raw = item[1]
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    async def _save(self, sid: str, payload: str, ttl: int) -> None:
        client = self._redis()
        if client is not None:
            try:
                await client.setex(self.prefix + sid, ttl, payload)
                self._mem.pop(sid, None)
                return
            except Exception as e:
                logger.warning("session: Redis set: %s — храню в памяти", e)
        self._mem_cleanup()
        self._mem[sid] = (time.time() + ttl, payload)

    async def _touch(self, sid: str, ttl: int) -> None:
        client = self._redis()
        if client is not None:
            try:
                await client.expire(self.prefix + sid, ttl)
                return
            except Exception as e:
                logger.warning("session: Redis expire: %s", e)
        item = self._mem.get(sid)
        if item:
            self._mem[sid] = (time.time() + ttl, item[1])

    async def _delete(self, sid: str) -> None:
        self._mem.pop(sid, None)
        client = self._redis()
        if client is not None:
            try:
                await client.delete(self.prefix + sid)
            except Exception as e:
                logger.warning("session: Redis delete: %s", e)

    # ───────────── ASGI ─────────────

    def _cookie_header(self, value: str, max_age: int) -> str:
        parts = [
            f"{self.cookie_name}={value}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            f"SameSite={self.same_site}",
        ]
        if self.https_only:
            parts.append("Secure")
        return "; ".join(parts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        sid: Optional[str] = None
        data: dict = {}

        raw_cookie = conn.cookies.get(self.cookie_name)
        if raw_cookie:
            try:
                sid = self.signer.unsign(
                    raw_cookie, max_age=self.max_age
                ).decode()
            except (BadSignature, UnicodeDecodeError):
                sid = None
            if sid:
                loaded = await self._load(sid)
                if loaded is None:
                    sid = None          # истекла или удалена
                else:
                    data = loaded

        initial = (
            json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
            if sid else None
        )
        scope["session"] = data

        async def send_wrapper(message: Message) -> None:
            nonlocal sid
            if message["type"] == "http.response.start":
                session = scope["session"]
                rotate = bool(session.pop("_rotate", False))
                headers = MutableHeaders(scope=message)

                if session:
                    ttl = self.max_age if session.get("user_id") else ANON_TTL
                    ttl = min(ttl, self.max_age)
                    payload = json.dumps(
                        session, sort_keys=True, ensure_ascii=False, default=str
                    )
                    new_sid = sid is None or rotate
                    if rotate and sid:
                        await self._delete(sid)
                    if new_sid:
                        sid = secrets.token_urlsafe(32)

                    if new_sid or payload != initial:
                        await self._save(sid, payload, ttl)
                        signed = self.signer.sign(sid).decode()
                        headers.append(
                            "Set-Cookie", self._cookie_header(signed, ttl)
                        )
                    else:
                        await self._touch(sid, ttl)
                elif sid:
                    # сессию очистили (выход) — удаляем данные и cookie
                    await self._delete(sid)
                    headers.append("Set-Cookie", self._cookie_header("", 0))
            await send(message)

        await self.app(scope, receive, send_wrapper)
