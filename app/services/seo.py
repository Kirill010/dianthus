# app/services/seo.py
"""SEO-помощники: адрес «главной» карточки товара и часы работы для разметки.

Одному товару может соответствовать несколько позиций (по одной на поставку: у «Кустовая роза 60 SIAN»
их две). Для человека это разные строки с разными остатками, а для поисковика — две страницы с одним
и тем же названием и текстом. Поэтому у товара есть ОДНА главная позиция (самая новая из тех, что в
наличии): остальные карточки ссылаются на неё через canonical, а в sitemap попадает только она.
"""
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import SupplyItem

_DAYS = {"пн": "Mo", "вт": "Tu", "ср": "We", "чт": "Th", "пт": "Fr", "сб": "Sa", "вс": "Su"}
_HOURS = re.compile(
    r"(пн|вт|ср|чт|пт|сб|вс)\s*[–—−-]\s*(пн|вт|ср|чт|пт|сб|вс)\D+(\d{1,2}):(\d{2})\s*[–—−-]\s*(\d{1,2}):(\d{2})")


def schema_hours(text: str) -> str:
    """«Пн–Вс 09:00–21:00» → «Mo-Su 09:00-21:00» (формат schema.org). Не разобрали — пустая строка:
    в разметку лучше ничего не писать, чем писать неверное."""
    m = _HOURS.search((text or "").strip().lower())
    if not m:
        return ""
    d1, d2, h1, m1, h2, m2 = m.groups()
    return f"{_DAYS[d1]}-{_DAYS[d2]} {int(h1):02d}:{m1}-{int(h2):02d}:{m2}"


def canonical_item_id(db: Session, item: SupplyItem) -> int:
    """id позиции, чей адрес указывать как canonical для карточки `item`.

    Позиция, которой нет в наличии (товар «в пути»), указывает на саму себя: это отдельная страница
    предзаказа. Для позиции в наличии — самая новая активная позиция того же товара."""
    if not item.is_active:
        return item.id
    newest = (db.query(func.max(SupplyItem.id))
              .filter(SupplyItem.product_id == item.product_id, SupplyItem.is_active.is_(True))
              .scalar())
    return int(newest or item.id)


def sitemap_items(db: Session) -> list[tuple[int, object]]:
    """[(id позиции, когда создана)] — по одной главной позиции на товар, по возрастанию id."""
    newest = (db.query(func.max(SupplyItem.id).label("item_id"))
              .filter(SupplyItem.is_active.is_(True))
              .group_by(SupplyItem.product_id)
              .subquery())
    return (db.query(SupplyItem.id, SupplyItem.created_at)
            .join(newest, SupplyItem.id == newest.c.item_id)
            .order_by(SupplyItem.id)
            .all())
