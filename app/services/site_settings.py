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
    # Реквизиты оператора персональных данных: подставляются в политику, согласия, условия,
    # счёт и подвал сайта. Раньше задавались только в .env на сервере — теперь их можно
    # внести в админке (то, что сохранено здесь, главнее .env).
    "shop_inn": (
        "ИНН организации",
        "",
        "10 цифр для организации, 12 для ИП. Берётся из свидетельства или выписки ЕГРЮЛ.",
    ),
    "shop_ogrn": (
        "ОГРН (для ИП — ОГРНИП)",
        "",
        "13 цифр для организации, 15 для ИП.",
    ),
    "shop_legal_address": (
        "Юридический адрес",
        "",
        "Как в ЕГРЮЛ: печатается в политике, согласиях и счёте. Если оставить пустым, подставится фактический адрес.",
    ),
    "shop_address": (
        "Фактический адрес (где забирают цветы)",
        "",
        "Адрес склада или магазина: показывается клиентам на главной, в «Контактах» и при самовывозе. "
        "Если оставить пустым, берётся адрес из настроек сервера.",
    ),
    "shop_email": (
        "Почта для обращений клиентов",
        "",
        "Сюда клиенты пишут по вопросам данных и отзыва согласий. Лучше рабочий ящик на домене "
        "сайта (например, info@dianthus-opt.ru), а не личный.",
    ),
    "shop_pd_responsible": (
        "Ответственный за персональные данные",
        "",
        "ФИО и должность (например: «Иванов Иван Иванович, генеральный директор»). "
        "Назначается приказом — образец в docs/legal/order_responsible_pd.md.",
    ),
    "pd_processors": (
        "Кто обрабатывает данные клиентов по нашему поручению",
        "",
        "Список для политики и согласия: хостинг, почта, облако 1С, перевозчики. Каждая строка — "
        "один получатель, лучше с названием организации и адресом из договора. "
        "Если оставить пустым, на сайте покажется стандартный список без названий.",
    ),
}

# Что показывается в политике и в согласии, пока владелец не внёс свой список.
# Названия организаций и адреса здесь не выдуманы: их берут из договоров с этими компаниями.
DEFAULT_PROCESSORS = """\
- хостинг-провайдер, на серверах которого в России размещены сайт и база данных;
- почтовый сервис, через который отправляются уведомления о заказах и заявках;
- поставщик облачной учётной системы 1С, в которой ведутся заказы и расчёты;
- сервис «Яндекс Карты» на странице «Контакты» (при показе карты Яндекс получает технические данные браузера);
- транспортные компании и курьеры — только имя контактного лица, телефон и адрес доставки;
- банки — для расчётов по выставленным счетам."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# реквизиты: запасное значение читается из .env в момент обращения (а не при запуске)
ENV_FALLBACK = {
    "shop_inn": "SHOP_INN", "shop_ogrn": "SHOP_OGRN", "shop_legal_address": "SHOP_LEGAL_ADDRESS",
    "shop_address": "SHOP_ADDRESS",
    "shop_email": "SHOP_EMAIL", "shop_pd_responsible": "SHOP_PD_RESPONSIBLE",
}


def _fallback(key: str) -> str:
    if key in ENV_FALLBACK:
        return getattr(config, ENV_FALLBACK[key], "") or ""
    return EDITABLE[key][1] if key in EDITABLE else ""


def get_setting(db: Session, key: str, default: str | None = None) -> str:
    """Значение из БД; если не задано — default, а для известных ключей — из .env."""
    row = db.get(SiteSetting, key)
    if row is not None and row.value is not None and row.value != "":
        return row.value
    if default is not None:
        return default
    return _fallback(key)


def get_settings(db: Session, keys) -> dict[str, str]:
    """Несколько настроек одним запросом (для подвала сайта — он на каждой странице)."""
    keys = list(keys)
    saved = {r.key: r.value for r in
             db.query(SiteSetting).filter(SiteSetting.key.in_(keys)).all()}
    return {k: (saved.get(k) or _fallback(k)) for k in keys}


def shop_address(db: Session | None = None) -> str:
    """Фактический адрес магазина: из админки (если внесён), иначе из .env."""
    return get_setting(db, "shop_address") if db is not None else (config.SHOP_ADDRESS or "")


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
