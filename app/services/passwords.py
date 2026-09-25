"""Общие функции хеширования/проверки паролей. Единственный источник правды."""
import logging

import bcrypt

logger = logging.getLogger(__name__)

MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    b = password.encode("utf-8")
    if len(b) > MAX_PASSWORD_BYTES:
        raise ValueError("Пароль > 72 байт")
    return bcrypt.hashpw(b, bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Строгая проверка. НЕ усекает длинные пароли (fix #11)."""
    if not password or not hashed:
        return False
    b = password.encode("utf-8")
    if len(b) > MAX_PASSWORD_BYTES:
        logger.warning("verify_password: получен пароль > 72 байт — отказ")
        return False
    try:
        return bcrypt.checkpw(b, hashed.encode())
    except ValueError as e:
        # повреждённый хеш или битый bcrypt
        logger.warning("verify_password: bcrypt ValueError: %s", e)
        return False
    except Exception as e:
        logger.exception("verify_password: неожиданная ошибка: %s", e)
        return False