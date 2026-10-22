# app/web.py
"""Мелкие общие помощники для роутеров."""
from urllib.parse import urlparse

from fastapi import Request

from .config import config


LIKE_ESCAPE = "\\"


def contains_pattern(q: str) -> str:
    """Шаблон для ILIKE «содержит q» с экранированием % и _.

    Без экранирования запрос «_» или «%» совпадал со всеми товарами, а
    «50%» ломал поиск. Использовать вместе с `.ilike(p, escape=LIKE_ESCAPE)`.
    """
    esc = (q.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
            .replace("%", LIKE_ESCAPE + "%")
            .replace("_", LIKE_ESCAPE + "_"))
    return f"%{esc}%"


def _own_hosts(request: Request) -> set[str]:
    return {
        h for h in (
            (request.url.hostname or "").lower(),
            (urlparse(config.APP_URL).hostname or "").lower(),
        ) if h
    }


def safe_back(request: Request, default: str = "/") -> str:
    """Куда вернуть пользователя после POST: страница НАШЕГО сайта из Referer.

    Referer контролирует клиент, поэтому без проверки это open redirect
    (после действия человека можно увести на чужой сайт). Что делаем:
      * хост из Referer должен совпасть с хостом запроса или APP_URL;
      * возвращаем только «путь?запрос» — без схемы и хоста. Так редирект
        всегда остаётся на текущем домене, а не зависит от http/https
        в заголовке (за nginx они часто расходятся);
      * пути вида //evil.com и /\\evil.com отбрасываем (браузер считает их
        адресом другого сайта).
    """
    ref = request.headers.get("referer") or ""
    if not ref:
        return default
    try:
        parsed = urlparse(ref)
    except ValueError:
        return default

    host = (parsed.hostname or "").lower()
    if not host or host not in _own_hosts(request):
        return default

    path = parsed.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return default
    return path + (f"?{parsed.query}" if parsed.query else "")
