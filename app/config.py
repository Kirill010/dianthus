# Настройки из .env.
import logging
import os

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

    SHOP_NAME: str = _str_env("SHOP_NAME", "ООО «Диантус»")
    SHOP_PHONE: str = _str_env("SHOP_PHONE", "")
    SHOP_PHONE_2: str = _str_env("SHOP_PHONE_2", "")
    SHOP_ADDRESS: str = _str_env("SHOP_ADDRESS", "")
    SHOP_WORKING_HOURS: str = _str_env("SHOP_WORKING_HOURS", "")
    SHOP_MAX_LINK: str = _str_env("SHOP_MAX_LINK", "")
    SHOP_MAP_URL: str = _str_env("SHOP_MAP_URL", "")

    # 1С интеграция
    INTEGRATION_USER: str = _str_env("INTEGRATION_USER", "1c_dianthus")
    INTEGRATION_PASSWORD: str = (
        _str_env("INTEGRATION_PASSWORD", "")
        or _str_env("INTEGRATION_SECRET", "")
    )

    # SMTP
    SMTP_HOST: str = _str_env("SMTP_HOST", "")
    SMTP_PORT: int = _int_env("SMTP_PORT", 465)
    SMTP_USER: str = _str_env("SMTP_USER", "")
    SMTP_PASSWORD: str = _str_env("SMTP_PASSWORD", "")
    SMTP_FROM: str = _str_env("SMTP_FROM", "")
    SMTP_TO: str = _str_env("SMTP_TO", "")
    SMTP_USE_SSL: bool = _bool_env("SMTP_USE_SSL", True)

    ALLOW_SQLITE_IN_PROD: bool = _bool_env("ALLOW_SQLITE_IN_PROD", False)


config = Config()

if config.ENV == "prod":
    if not config.SECRET_KEY or config.SECRET_KEY == "dev-change-me":
        raise RuntimeError(
            "❌ В ENV=prod обязательно задайте SECRET_KEY! "
            'Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("❌ SECRET_KEY слишком короткий (минимум 32 символа).")

    if not config.INTEGRATION_PASSWORD:
        logger.warning(
            "⚠️ INTEGRATION_PASSWORD не задан — 1С-интеграция вернёт 503"
        )

    if config.APP_URL.startswith("http://127.0.0.1"):
        logger.warning(
            "⚠️ APP_URL указывает на localhost — CSRF/письма будут "
            "со ссылками на localhost. Установите https://ваш-домен."
        )

    if config.DATABASE_URL.startswith("sqlite"):
        if not config.ALLOW_SQLITE_IN_PROD:
            raise RuntimeError(
                "❌ ENV=prod + SQLite несовместимы: "
                "SELECT ... FOR UPDATE игнорируется, race condition "
                "при оформлении заказов НЕ защищён.\n"
                "   ▸ Решение: перейдите на PostgreSQL "
                "(DATABASE_URL=postgresql://...).\n"
                "   ▸ Осознанно принять риск (НЕ рекомендуется): "
                "установите ALLOW_SQLITE_IN_PROD=1."
            )
        logger.warning(
            "⚠️ SQLite в prod с ALLOW_SQLITE_IN_PROD=1 — "
            "race condition при оформлении заказов НЕ защищён!"
        )