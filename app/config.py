# app/config.py
"""Настройки из .env с валидацией безопасности."""
import logging
import os
import secrets

from dotenv import load_dotenv

if os.getenv("ENV") != "prod":
    load_dotenv()

logger = logging.getLogger(__name__)


def _int_env(key: str, default: int) -> int:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Переменная %s='%s' не число", key, raw)
        return default


def _str_env(key: str, default: str) -> str:
    value = os.getenv(key)
    return value if value else default


def _bool_env(key: str, default: bool = False) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Config:
    ENV: str = _str_env("ENV", "dev")
    SECRET_KEY: str = _str_env("SECRET_KEY", "dev-change-me")
    APP_URL: str = _str_env("APP_URL", "http://127.0.0.1:8000")
    DATABASE_URL: str = _str_env("DATABASE_URL", "sqlite:///./dianthus.db")
    PAGE_SIZE: int = _int_env("PAGE_SIZE", 12)
    CACHE_TTL: int = _int_env("CACHE_TTL", 300)

    # Максимальный размер загружаемого изображения товара, байт
    MAX_UPLOAD_SIZE: int = _int_env("MAX_UPLOAD_SIZE", 5 * 1024 * 1024)

    # Часовой пояс магазина. В БД все даты лежат в UTC, а клиентам и
    # менеджерам время показывается в этом поясе (IANA-имя, напр. Europe/Moscow).
    SHOP_TZ: str = _str_env("SHOP_TZ", "Europe/Moscow")

    SHOP_NAME: str = _str_env("SHOP_NAME", "ООО «Диантус»")
    SHOP_PHONE: str = _str_env("SHOP_PHONE", "")
    SHOP_PHONE_2: str = _str_env("SHOP_PHONE_2", "")
    SHOP_ADDRESS: str = _str_env("SHOP_ADDRESS", "")
    # Почта для обращений клиентов (политика конфиденциальности, контакты)
    SHOP_EMAIL: str = _str_env("SHOP_EMAIL", "")
    # Банковские реквизиты для PDF-счёта. Строки разделяются знаком «|», например:
    #   ИНН 7720965534 | КПП 772001001 | р/с 40702810… | БИК 044525225 | Банк «…»
    SHOP_REQUISITES: str = _str_env("SHOP_REQUISITES", "")
    # Реквизиты оператора персональных данных: подставляются в юридические
    # документы (политика, согласие, условия) и в подвал сайта.
    SHOP_INN: str = _str_env("SHOP_INN", "")
    SHOP_OGRN: str = _str_env("SHOP_OGRN", "")
    SHOP_LEGAL_ADDRESS: str = _str_env("SHOP_LEGAL_ADDRESS", "")   # пусто = SHOP_ADDRESS
    # Ответственный за организацию обработки персональных данных (ФИО, должность)
    SHOP_PD_RESPONSIBLE: str = _str_env("SHOP_PD_RESPONSIBLE", "")
    # Строка про НДС в счёте, например «НДС не облагается (УСН)». Пусто = не печатать:
    # налоговый режим знает только бухгалтер, сайт его не угадывает.
    SHOP_VAT_NOTE: str = _str_env("SHOP_VAT_NOTE", "")
    # Условия оплаты: показываются при оформлении заказа и в условиях работы
    SHOP_PAYMENT_TERMS: str = _str_env("SHOP_PAYMENT_TERMS", "Предоплата 100% по выставленному счёту")
    # За сколько дней до нужной даты получения принимается предзаказ — по
    # происхождению цветов. Формат: «Страна=дней, Страна=дней». Правило бизнеса:
    # срок считается от даты, когда цветы нужны клиенту.
    PREORDER_LEAD_DAYS: str = _str_env(
        "PREORDER_LEAD_DAYS",
        "Эквадор=10, Голландия=10, Китай=10, Кения=10, Колумбия=7, Россия=3",
    )
    # Сколько фото можно приложить к рекламации
    CLAIMS_MAX_PHOTOS: int = _int_env("CLAIMS_MAX_PHOTOS", 8)
    SHOP_WORKING_HOURS: str = _str_env("SHOP_WORKING_HOURS", "")
    SHOP_MAX_LINK: str = _str_env("SHOP_MAX_LINK", "")
    SHOP_MAP_URL: str = _str_env("SHOP_MAP_URL", "")

    INTEGRATION_USER: str = _str_env("INTEGRATION_USER", "1c_dianthus")
    INTEGRATION_PASSWORD: str = (
        _str_env("INTEGRATION_PASSWORD", "")
        or _str_env("INTEGRATION_SECRET", "")
    )
    # 1 = при загрузке остатков из 1С вычитать заказы, которые 1С ещё не
    # «подтвердила» (POST /api/1c/orders/ack). Закрывает окно, когда клиент
    # оформил заказ уже ПОСЛЕ того, как 1С посчитала остатки: без этого
    # свежая выгрузка вернёт проданные упаковки на полку (перепродажа).
    # Включайте, только если 1С реально вызывает orders/ack — иначе
    # подтверждения не придут и остатки будут занижены.
    ONEC_RESERVE_UNEXPORTED: bool = _bool_env("ONEC_RESERVE_UNEXPORTED", False)

    SMTP_HOST: str = _str_env("SMTP_HOST", "")
    SMTP_PORT: int = _int_env("SMTP_PORT", 465)
    SMTP_USER: str = _str_env("SMTP_USER", "")
    SMTP_PASSWORD: str = _str_env("SMTP_PASSWORD", "")
    SMTP_FROM: str = _str_env("SMTP_FROM", "")
    SMTP_TO: str = _str_env("SMTP_TO", "")
    SMTP_USE_SSL: bool = _bool_env("SMTP_USE_SSL", True)

    ALLOW_SQLITE_IN_PROD: bool = _bool_env("ALLOW_SQLITE_IN_PROD", False)

    # Безопасность.
    # (Флаги LOGIN_LOCK_ENABLED / CSRF_ENABLED удалены: они нигде не читались,
    # и казалось, что защиту можно «выключить» настройкой, хотя это не так.)
    SESSION_MAX_AGE: int = _int_env("SESSION_MAX_AGE", 604800)  # 7 дней


config = Config()

# ─── Проверки в prod ───
if config.ENV == "prod":
    # Раньше при плохом ключе генерировался случайный — но у каждого воркера
    # он получался СВОЙ, и подпись cookie сессии не проходила между воркерами
    # (клиентов случайно «выкидывало» из аккаунта). Теперь — остановка с понятной ошибкой.
    if not config.SECRET_KEY or config.SECRET_KEY == "dev-change-me":
        raise RuntimeError(
            "SECRET_KEY не задан. Создайте .env командой scripts/prepare.sh "
            "или задайте SECRET_KEY (openssl rand -hex 32)."
        )
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError(
            "SECRET_KEY короче 32 символов. Сгенерируйте новый: openssl rand -hex 32"
        )

    if not config.INTEGRATION_PASSWORD:
        # Это нормально: 1С входит по ключам из админки («Интеграция»).
        # Если ключей тоже нет, обмен ответит 503 — об этом скажет та же страница.
        logger.info(
            "1С: старый пароль INTEGRATION_PASSWORD не задан — доступ только по ключам "
            "(админка → «Интеграция»)"
        )

    if config.APP_URL.startswith("http://127.0.0.1"):
        logger.warning(
            "⚠️ APP_URL указывает на localhost — CSRF/письма будут "
            "со ссылками на localhost. Установите https://ваш-домен."
        )
    elif config.APP_URL.startswith("http://"):
        logger.warning(
            "⚠️ APP_URL начинается с http:// — в prod cookie сессии выдаётся "
            "только по HTTPS, войти на сайт не получится. Укажите https://."
        )

    if not (os.getenv("REDIS_URL") or "").strip():
        logger.critical(
            "❌ ENV=prod, но REDIS_URL не задан: сессии и лимиты запросов "
            "хранятся в памяти КАЖДОГО воркера отдельно — клиентов будет "
            "«выкидывать» из аккаунта. Подключите Redis (см. docker-compose.yml)."
        )

    if config.DATABASE_URL.startswith("sqlite"):
        if not config.ALLOW_SQLITE_IN_PROD:
            logger.critical(
                "❌ ENV=prod + SQLite: race conditions в place_order "
                "НЕ защищены! Перейдите на PostgreSQL или "
                "установите ALLOW_SQLITE_IN_PROD=1 (НЕ рекомендуется)."
            )
        else:
            logger.warning(
                "⚠️ SQLite в prod с ALLOW_SQLITE_IN_PROD=1 — "
                "race condition при оформлении заказов НЕ защищён!"
            )