"""Настройки из .env."""
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
    """Вернуть значение переменной окружения ИЛИ default, если пусто.

    Отличие от os.getenv(key, default): если переменная есть, но пустая
    (например DATABASE_URL=), os.getenv вернёт "", а нам нужен default.
    """
    value = os.getenv(key)
    return value if value else default


class Config:
    ENV: str = _str_env("ENV", "dev")
    SECRET_KEY: str = _str_env("SECRET_KEY", "dev-change-me")
    APP_URL: str = _str_env("APP_URL", "http://127.0.0.1:8000")
    DATABASE_URL: str = _str_env("DATABASE_URL", "sqlite:///./dianthus.db")
    PAGE_SIZE: int = _int_env("PAGE_SIZE", 12)


config = Config()

if config.ENV == "prod":
    if not config.SECRET_KEY or config.SECRET_KEY == "dev-change-me":
        raise RuntimeError(
            "❌ В ENV=prod обязательно задайте SECRET_KEY! "
            'Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("❌ SECRET_KEY слишком короткий (минимум 32 символа).")