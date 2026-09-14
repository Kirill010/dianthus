"""Читает настройки из .env один раз при импорте."""
import logging
import os

from dotenv import load_dotenv


load_dotenv()

logger = logging.getLogger(__name__)


def _int_env(key: str, default: int) -> int:
    """Безопасно читает int из env. При мусоре вернёт default."""
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Переменная %s='%s' не число — берём %d", key, raw, default)
        return default


class Config:
    ENV: str = os.getenv("ENV", "dev")
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-change-me")
    APP_URL: str = os.getenv("APP_URL", "http://127.0.0.1:8000")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./dianthus.db")
    PAGE_SIZE: int = _int_env("PAGE_SIZE", 12)


config = Config()


# ─── Защита от запуска в прод с дефолтным ключом ────────────
if config.ENV == "prod":
    if not config.SECRET_KEY or config.SECRET_KEY == "dev-change-me":
        raise RuntimeError(
            "❌ В ENV=prod обязательно задайте SECRET_KEY в .env! "
            'Сгенерировать: python -c "import secrets; print(secrets.token_hex(32))"'
        )
    if len(config.SECRET_KEY) < 32:
        raise RuntimeError("❌ SECRET_KEY слишком короткий (минимум 32 символа).")
elif config.SECRET_KEY == "dev-change-me":
    logger.warning("⚠️  SECRET_KEY не задан — используется dev-значение. "
                   "Для продакшена обязательно поменяйте!")