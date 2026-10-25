# app/services/site_settings.py
"""Настройки магазина, которые владелец правит в админке.

Значение по умолчанию берётся из .env (config), а если владелец сохранил своё
в админке — используется оно. Так сайт работает «из коробки», а менять сроки
предзаказа или условия оплаты можно без сервера и без перезапуска.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..config import config
from ..models import SiteSetting

logger = logging.getLogger(__name__)

# ключ → (подпись в админке, значение по умолчанию из config, подсказка)
EDITABLE: dict[str, tuple[str, str, str]] = {
    "payment_terms": (
        "Условия оплаты",
        config.SHOP_PAYMENT_TERMS,
        "Показывается клиенту при оформлении заказа и в условиях работы.",
    ),
    "preorder_lead_days": (
        "За сколько дней нужно заказывать (по странам)",
        config.PREORDER_LEAD_DAYS,
        "Формат: «Страна=дней, Страна=дней». Срок считается от даты, к которой "
        "цветы нужны клиенту. Пример: Эквадор=10, Колумбия=7, Россия=3.",
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_setting(db: Session, key: str, default: str | None = None) -> str:
    """Значение из БД; если не задано — default, а для известных ключей — из .env."""
    row = db.get(SiteSetting, key)
    if row is not None and row.value is not None and row.value != "":
        return row.value
    if default is not None:
        return default
    if key in EDITABLE:
        return EDITABLE[key][1]
    return ""


def set_setting(db: Session, key: str, value: str, admin=None) -> None:
    value = (value or "").strip()
    row = db.get(SiteSetting, key)
    if row is None:
        row = SiteSetting(key=key[:80], value=value)
        db.add(row)
    else:
        row.value = value
    row.updated_at = _now()
    row.updated_by_id = getattr(admin, "id", None)
    db.commit()


def flag_is_set(db: Session, key: str) -> bool:
    row = db.get(SiteSetting, key)
    return bool(row and (row.value or "").strip())
