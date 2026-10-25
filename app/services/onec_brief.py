# app/services/onec_brief.py
"""Памятка программисту 1С: хранится в app/content/1c_brief.md (одна на весь
проект), а при показе в неё подставляется настоящий адрес сайта.

Её видно по адресу /1c-docs без входа в админку — в памятке нет секретов, зато
владелец может просто переслать программисту ссылку (ключ он передаёт отдельно).
"""
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

from ..config import config
from . import markdown_lite

CONTENT = Path(__file__).resolve().parent.parent / "content" / "1c_brief.md"


@lru_cache(maxsize=1)
def _source() -> str:
    return CONTENT.read_text(encoding="utf-8")


def base_url() -> str:
    return config.APP_URL.rstrip("/")


def brief_markdown() -> str:
    return _source().replace("{{URL}}", base_url())


def brief_html() -> Markup:
    return markdown_lite.render(brief_markdown())


def message_for_programmer(key: str | None = None) -> str:
    """Готовое письмо, которое владелец пересылает программисту 1С."""
    url = base_url()
    key_line = key if key else "(ключ владелец передаст отдельно)"
    return (
        f"Здравствуйте! Нужно настроить обмен между 1С и сайтом «{config.SHOP_NAME}» "
        f"(оптовые цветы).\n\n"
        f"Подробная памятка с форматами данных и примерами кода: {url}/1c-docs\n"
        f"Адрес обмена: {url}/api/1c/\n"
        f"Ключ доступа: {key_line}\n"
        f"Проверка связи: curl -H \"X-API-Key: <ключ>\" {url}/api/1c/ping\n\n"
        f"Что нужно: 1) выгрузка товаров, цен и остатков из 1С на сайт; "
        f"2) загрузка новых заказов сайта в 1С; 3) по желанию — статусы заказов "
        f"обратно на сайт.\n"
        f"Если из вашей 1С нельзя ходить в интернет — напишите, сделаем обмен через файлы."
    )
