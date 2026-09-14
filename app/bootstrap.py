"""
Создание администратора по умолчанию при старте приложения.

Работает только если в .env заданы ADMIN_EMAIL и ADMIN_PASSWORD.
Безопасно для повторных запусков.
"""
import logging
import os

import bcrypt
from dotenv import load_dotenv

from .database import SessionLocal
from . import models


load_dotenv()
logger = logging.getLogger(__name__)


def _hash_password(password: str) -> str:
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > 72:
        raise ValueError("Пароль длиннее 72 байт — bcrypt не примет")
    return bcrypt.hashpw(pwd_bytes, bcrypt.gensalt()).decode()


def _check_password(password: str, hashed: str) -> bool:
    pwd_bytes = password.encode("utf-8")
    if len(pwd_bytes) > 72:
        return False
    try:
        return bcrypt.checkpw(pwd_bytes, hashed.encode())
    except ValueError:
        return False


def ensure_default_admin() -> None:
    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or ""

    if not email or not password:
        logger.info("ADMIN_EMAIL/ADMIN_PASSWORD не заданы — админ не создаётся")
        return

    if len(password) < 8:
        logger.warning("ADMIN_PASSWORD короче 8 символов — пропускаем")
        return
    if len(password.encode("utf-8")) > 72:
        logger.warning("ADMIN_PASSWORD длиннее 72 байт — пропускаем")
        return

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            # Пользователь с таким email уже есть.
            # Повышаем ТОЛЬКО если пароль совпадает — иначе любой,
            # кто угадал ADMIN_EMAIL, стал бы админом после рестарта.
            if user.is_admin and user.is_approved:
                return
            if not _check_password(password, user.hashed_password):
                logger.warning(
                    "Пользователь %s с email ADMIN_EMAIL существует, "
                    "но пароль не совпадает — не повышаем", email,
                )
                return
            changed = False
            if not user.is_admin:
                user.is_admin = True
                changed = True
            if not user.is_approved:
                user.is_approved = True
                changed = True
            if changed:
                db.commit()
                logger.info("👑 Пользователь %s повышен до админа", email)
            return

        db.add(models.User(
            email=email,
            hashed_password=_hash_password(password),
            full_name=os.getenv("ADMIN_NAME", "Администратор"),
            phone=os.getenv("ADMIN_PHONE", ""),
            company_name=os.getenv("ADMIN_COMPANY", "Диантус"),
            is_admin=True,
            is_approved=True,
        ))
        db.commit()
        logger.info("👑 Создан администратор по умолчанию: %s", email)
    except Exception as e:
        db.rollback()
        logger.exception("Не удалось создать админа: %s", e)
    finally:
        db.close()