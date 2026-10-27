# app/timeutils.py
"""Время: в БД всё хранится в UTC (naive), людям показываем в поясе магазина.

Раньше шаблоны, письма, PDF-счета и Excel печатали время прямо из БД,
поэтому заказ, сделанный в 14:00 по Москве, показывался как «11:00».
"""
import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import config

logger = logging.getLogger(__name__)

DATETIME_FMT = "%d.%m.%Y %H:%M"
DATE_FMT = "%d.%m.%Y"


@lru_cache(maxsize=1)
def shop_tz():
    try:
        return ZoneInfo(config.SHOP_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            "SHOP_TZ=%r не найден (нужен пакет tzdata) — использую UTC",
            config.SHOP_TZ,
        )
        return timezone.utc


def as_utc(dt: datetime) -> datetime:
    """Naive-время из БД (это UTC) → aware UTC. Aware-время не трогаем."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return as_utc(dt).astimezone(shop_tz())


def fmt_local(dt: datetime | None, fmt: str = DATETIME_FMT, empty: str = "—") -> str:
    local = to_local(dt)
    return local.strftime(fmt) if local else empty


def fmt_when(dt: datetime | None, empty: str = "—") -> str:
    """«сегодня в 14:05» / «вчера в 18:30» / «25.10 в 09:00» — по времени магазина.

    Для плашек вроде «Цены и остатки обновлены …»: клиенту так понятнее,
    чем «27.10.2026 11:05».
    """
    local = to_local(dt)
    if local is None:
        return empty
    now = datetime.now(shop_tz())
    clock = local.strftime("%H:%M")
    days = (now.date() - local.date()).days
    if days <= 0:
        return f"сегодня в {clock}"
    if days == 1:
        return f"вчера в {clock}"
    day = local.strftime("%d.%m" if local.year == now.year else "%d.%m.%Y")
    return f"{day} в {clock}"


def utc_iso(dt: datetime | None) -> str:
    """ISO-8601 с явным смещением (2026-02-14T09:31:00+00:00) — для обмена с 1С."""
    return as_utc(dt).isoformat() if dt else ""


def local_iso(dt: datetime | None) -> str:
    """То же, но в поясе магазина (2026-02-14T12:31:00+03:00)."""
    local = to_local(dt)
    return local.isoformat() if local else ""


def local_midnight_utc(days_ago: int = 0) -> datetime:
    """Полночь по поясу магазина N дней назад — в UTC без tzinfo (как в БД).

    Нужна статистике «сегодня / за неделю»: раньше границей служила полночь
    по UTC, то есть 03:00 по Москве, и ночные заказы попадали во «вчера».
    """
    now_local = datetime.now(shop_tz())
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(days=days_ago)
    return start.astimezone(timezone.utc).replace(tzinfo=None)
