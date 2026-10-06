# app/services/passwords.py
import logging
import bcrypt

logger = logging.getLogger(__name__)
MAX_PASSWORD_BYTES = 72


def _truncate(password: str) -> bytes:
    b = (password or "").encode("utf-8")
    if len(b) > MAX_PASSWORD_BYTES:
        b = b[:MAX_PASSWORD_BYTES]
    return b


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_truncate(password), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not password or not hashed:
        return False
    try:
        return bcrypt.checkpw(_truncate(password), hashed.encode())
    except ValueError as e:
        logger.warning("verify_password: bcrypt ValueError: %s", e)
        return False
    except Exception as e:
        logger.exception("verify_password: неожиданная ошибка: %s", e)
        return False