# Создание админа при старте приложения.
import logging
import os

import bcrypt
from dotenv import load_dotenv

from .database import SessionLocal
from . import models

load_dotenv()
logger = logging.getLogger(__name__)


def _hash(password: str) -> str:
    b = password.encode("utf-8")
    if len(b) > 72:
        raise ValueError("Пароль > 72 байт")
    return bcrypt.hashpw(b, bcrypt.gensalt()).decode()


def _check(password: str, hashed: str) -> bool:
    b = password.encode("utf-8")
    if len(b) > 72:
        return False
    try:
        return bcrypt.checkpw(b, hashed.encode())
    except ValueError:
        return False


def ensure_default_admin() -> None:
    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or ""
    if not email or not password:
        logger.info("ADMIN_EMAIL/PASSWORD не заданы")
        return
    if len(password) < 8 or len(password.encode("utf-8")) > 72:
        logger.warning("ADMIN_PASSWORD некорректной длины")
        return

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            if user.is_admin and user.is_approved:
                return
            if not _check(password, user.hashed_password):
                logger.warning("Email занят, пароль не совпал")
                return
            user.is_admin = True
            user.is_approved = True
            db.commit()
            logger.info("👑 %s повышен до админа", email)
            return
        db.add(models.User(
            email=email,
            hashed_password=_hash(password),
            full_name=os.getenv("ADMIN_NAME", "Администратор"),
            phone=os.getenv("ADMIN_PHONE", ""),
            company_name=os.getenv("ADMIN_COMPANY", "Диантус"),
            is_admin=True,
            is_approved=True,
        ))
        db.commit()
        logger.info("👑 Создан админ: %s", email)
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка создания админа: %s", e)
    finally:
        db.close()