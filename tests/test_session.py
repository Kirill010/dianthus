"""Тесты серверных сессий (app/session.py) без запуска всего приложения."""
import asyncio
import json

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.session import ServerSessionMiddleware


async def _home(request):
    return JSONResponse(dict(request.session))


async def _login(request):
    request.session.clear()
    request.session["_rotate"] = True
    request.session["user_id"] = 7
    return JSONResponse({})


async def _cart(request):
    request.session["cart"] = request.session.get("cart", []) + ["x" * 1000]
    return JSONResponse({"n": len(request.session["cart"])})


async def _logout(request):
    request.session.clear()
    request.session["_rotate"] = True
    return JSONResponse({})


def _app():
    app = Starlette(routes=[
        Route("/", _home),
        Route("/login", _login, methods=["POST"]),
        Route("/cart", _cart, methods=["POST"]),
        Route("/logout", _logout, methods=["POST"]),
    ])
    app.add_middleware(ServerSessionMiddleware,
                       secret_key="k" * 40, https_only=True)
    return app


async def _call(app, method, path, cookie=None):
    headers = [(b"host", b"t")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"",
             "headers": headers, "scheme": "https", "server": ("t", 443),
             "client": ("1.1.1.1", 1), "http_version": "1.1", "root_path": ""}
    out = {"headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["headers"] = msg["headers"]
        else:
            out["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    set_cookie = [v.decode() for k, v in out["headers"] if k == b"set-cookie"]
    cookie_out = set_cookie[0].split(";")[0] if set_cookie else None
    return json.loads(out["body"]), set_cookie, cookie_out


def test_session_lifecycle():
    async def run():
        app = _app()
        body, sc, _ = await _call(app, "GET", "/")
        assert body == {} and not sc               # аноним без данных — без cookie

        _, sc, c1 = await _call(app, "POST", "/login")
        assert "Secure" in sc[0] and "HttpOnly" in sc[0]

        body, _, _ = await _call(app, "GET", "/", c1)
        assert body["user_id"] == 7

        # Большая корзина (>20 КБ) не помещалась бы в обычную cookie (4 КБ)
        for _ in range(20):
            await _call(app, "POST", "/cart", c1)
        body, _, _ = await _call(app, "GET", "/", c1)
        assert len(body["cart"]) == 20 and len(c1) < 200

        # Подделанная cookie не даёт доступа
        body, _, _ = await _call(app, "GET", "/", "dianthus_sid=forged.value.sig")
        assert body == {}

        # Повторный вход выдаёт новый id; старый становится недействительным
        _, _, c2 = await _call(app, "POST", "/login", c1)
        assert c2 != c1
        body, _, _ = await _call(app, "GET", "/", c1)
        assert body == {}

        # После выхода старая сессия мертва
        _, _, c3 = await _call(app, "POST", "/logout", c2)
        body, _, _ = await _call(app, "GET", "/", c2)
        assert body == {}
        body, _, _ = await _call(app, "GET", "/", c3)
        assert "user_id" not in body

    asyncio.run(run())
