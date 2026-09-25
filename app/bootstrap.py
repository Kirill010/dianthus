# Создание админа при старте приложения.
import logging
import os

from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

from .database import SessionLocal
from .services.passwords import hash_password, verify_password
from . import models

load_dotenv()
logger = logging.getLogger(__name__)


def _bool_env(key: str, default: bool = False) -> bool:
    raw = (os.getenv(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def ensure_default_admin() -> None:
    email = (os.getenv("ADMIN_EMAIL") or "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD") or ""
    force_reset = _bool_env("ADMIN_FORCE_PASSWORD_RESET", False)

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
            if force_reset:
                user.hashed_password = hash_password(password)
                user.is_admin = True
                user.is_approved = True
                db.commit()
                logger.info(
                    "🔑 Пароль админа %s принудительно обновлён "
                    "(ADMIN_FORCE_PASSWORD_RESET=1)",
                    email,
                )
                return

            if user.is_admin and user.is_approved:
                return

            if not verify_password(password, user.hashed_password):
                logger.warning(
                    "Email занят (%s), пароль не совпал. "
                    "Если это ваш аккаунт — задайте "
                    "ADMIN_FORCE_PASSWORD_RESET=1 и перезапустите.",
                    email,
                )
                return

            user.is_admin = True
            user.is_approved = True
            db.commit()
            logger.info("👑 %s повышен до админа", email)
            return

        db.add(models.User(
            email=email,
            hashed_password=hash_password(password),
            full_name=os.getenv("ADMIN_NAME", "Администратор"),
            phone=os.getenv("ADMIN_PHONE", ""),
            company_name=os.getenv("ADMIN_COMPANY", "Диантус"),
            is_admin=True,
            is_approved=True,
        ))
        db.commit()
        logger.info("👑 Создан админ: %s", email)

    except IntegrityError:
        # fix #27: воркер №2 проиграл гонку — это нормально
        db.rollback()
        logger.info("Админ уже создан другим воркером: %s", email)

    except Exception as e:
        db.rollback()
        logger.exception("Ошибка создания админа: %s", e)
    finally:
        db.close()