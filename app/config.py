# Настройки из .env.
import logging
import os

from dotenv import load_dotenv

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


class Config:
    ENV: str = _str_env("ENV", "dev")
    SECRET_KEY: str = _str_env("SECRET_KEY", "dev-change-me")
    APP_URL: str = _str_env("APP_URL", "http://127.0.0.1:8000")
    DATABASE_URL: str = _str_env("DATABASE_URL", "sqlite:///./dianthus.db")
    PAGE_SIZE: int = _int_env("PAGE_SIZE", 12)

    SHOP_NAME: str = _str_env("SHOP_NAME", "ООО «Диантус»")
    SHOP_PHONE: str = _str_env("SHOP_PHONE", "")
    SHOP_PHONE_2: str = _str_env("SHOP_PHONE_2", "")
    SHOP_ADDRESS: str = _str_env("SHOP_ADDRESS", "")
    SHOP_WORKING_HOURS: str = _str_env("SHOP_WORKING_HOURS", "")
    SHOP_MAX_LINK: str = _str_env("SHOP_MAX_LINK", "")
    SHOP_MAP_URL: str = _str_env("SHOP_MAP_URL", "")

    # 1С интеграция
    INTEGRATION_USER: str = _str_env("INTEGRATION_USER", "1c_dianthus")
    INTEGRATION_PASSWORD: str = _str_env("INTEGRATION_PASSWORD", "")

    # Email-уведомления (SMTP)
    SMTP_HOST: str = _str_env("SMTP_HOST", "")
    SMTP_PORT: int = _int_env("SMTP_PORT", 465)
    SMTP_USER: str = _str_env("SMTP_USER", "")
    SMTP_PASSWORD: str = _str_env("SMTP_PASSWORD", "")
    SMTP_FROM: str = _str_env("SMTP_FROM", "")
    SMTP_TO: str = _str_env("SMTP_TO", "")
    SMTP_USE_SSL: bool = (
        _str_env("SMTP_USE_SSL", "true").lower() in ("1", "true", "yes", "on")
    )


config = Config()

if config.ENV == "prod":
    if not config.SECRET_KEY or config.SECRET_KEY == "dev-change-me":
        raise RuntimeError(
            "❌ В ENV=prod обязательно задайте SECRET_KEY! "
            'Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("❌ SECRET_KEY слишком короткий.")
    if not config.INTEGRATION_PASSWORD:
        logger.warning(
            "⚠️ INTEGRATION_PASSWORD не задан — 1С-интеграция вернёт 503"
        )