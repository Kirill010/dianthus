# app/services/onec_brief.py
"""Памятка программисту 1С и форматы обмена.

Два исходника лежат в app/content/:
  1c_brief.md      — текст памятки; {{URL}} заменяется адресом сайта, а {{json:запрос.часть}}
                     (например {{json:orders_export.response}}) — примером из файла форматов;
  1c_formats.json  — все примеры запросов и ответов в одном месте. По нему же тесты сверяют
                     настоящий API, поэтому пример не может разойтись с кодом.

Видно их по адресам /1c-docs, /1c-docs.md и /1c-docs.json без входа в админку: секретов в них
нет, владелец просто пересылает программисту ссылку (ключ он передаёт отдельно).
"""
import json
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

from ..config import config
from . import markdown_lite

CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"
CONTENT = CONTENT_DIR / "1c_brief.md"
FORMATS = CONTENT_DIR / "1c_formats.json"

_EXAMPLE = re.compile(r"\{\{json:([a-z_]+)\.([a-z_]+)\}\}")


@lru_cache(maxsize=1)
def _source() -> str:
    return CONTENT.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _formats_source() -> str:
    return FORMATS.read_text(encoding="utf-8")


def base_url() -> str:
    return config.APP_URL.rstrip("/")


def formats() -> dict:
    """Все форматы обмена как словарь (адрес сайта в строках ещё не подставлен)."""
    return json.loads(_formats_source())


def pretty(value, level: int = 0, width: int = 100) -> str:
    """JSON с отступами: короткие объекты и списки — одной строкой, длинные — столбиком."""
    flat = json.dumps(value, ensure_ascii=False)
    pad = "  " * level
    if not isinstance(value, (dict, list)) or not value or len(pad) + len(flat) <= width:
        return flat
    inner = "  " * (level + 1)
    if isinstance(value, list):
        lines = [inner + pretty(v, level + 1, width) for v in value]
        return "[\n" + ",\n".join(lines) + "\n" + pad + "]"
    lines = [f"{inner}{json.dumps(k, ensure_ascii=False)}: {pretty(v, level + 1, width)}"
             for k, v in value.items()]
    return "{\n" + ",\n".join(lines) + "\n" + pad + "}"


def render_brief(text: str, url: str) -> str:
    """Памятка из исходника: примеры из файла форматов и адрес сайта на своих местах."""
    data = formats()["endpoints"]
    filled = _EXAMPLE.sub(lambda m: pretty(data[m.group(1)][m.group(2)]), text)
    return filled.replace("{{URL}}", url)


def brief_markdown() -> str:
    return render_brief(_source(), base_url())


def brief_html() -> Markup:
    return markdown_lite.render(brief_markdown())


def formats_json() -> str:
    """Файл форматов для программиста: тот же JSON, но с настоящим адресом сайта."""
    return _formats_source().replace("{{URL}}", base_url())


def message_for_programmer(key: str | None = None) -> str:
    """Готовое письмо, которое владелец пересылает программисту 1С."""
    url = base_url()
    key_line = key if key else "(ключ владелец передаст отдельно)"
    return (
        f"Здравствуйте! Нужно настроить обмен между 1С и сайтом «{config.SHOP_NAME}» "
        f"(оптовые цветы).\n\n"
        f"Работаем по поставкам (инвойсам): вы присылаете инвойсы с товаром, ценой и "
        f"количеством, а заказы сайт возвращает с привязкой к поставке. Во всём обмене "
        f"одинаковые короткие названия полей: price (цена за штуку), quantity (штуки), "
        f"sum, total.\n\n"
        f"Памятка с форматами и примерами кода: {url}/1c-docs\n"
        f"Все форматы одним JSON-файлом: {url}/1c-docs.json\n"
        f"Адрес обмена: {url}/api/1c/\n"
        f"Ключ доступа: {key_line}\n"
        f"Проверка связи: curl -H \"X-API-Key: <ключ>\" {url}/api/1c/ping\n\n"
        f"Что нужно: 1) инвойсы поставщиков с товаром, ценой и количеством (supplies/sync); "
        f"2) загрузка новых заказов сайта в 1С вместе с поставкой по каждой строке "
        f"(orders/export, orders/ack); 3) статусы заказов обратно на сайт (orders/status).\n"
        f"Если из вашей 1С нельзя ходить в интернет — напишите, сделаем обмен через файлы."
    )
