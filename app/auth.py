"""Регистрация, вход, выход."""
import logging

import bcrypt
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .security import check_csrf, check_rate_limit, reset_rate_limit
from .validators import (validate_company_name, validate_email,
                         validate_full_name, validate_password,
                         validate_phone)
from . import models

logger = logging.getLogger(__name__)
router = APIRouter()


def hash_password(password: str) -> str:
    b = password.encode("utf-8")
    if len(b) > 72:
        raise ValueError("Пароль > 72 байт")
    return bcrypt.hashpw(b, bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    b = password.encode("utf-8")
    if len(b) > 72:
        b = b[:72]
    try:
        return bcrypt.checkpw(b, hashed.encode())
    except ValueError:
        return False


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    full_name: str = Form(""),
    phone: str = Form(""),
    company_name: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    check_rate_limit(request, "register", max_hits=5, window=300)

    email = email.strip().lower()
    company_name = company_name.strip()
    full_name = full_name.strip()
    phone = phone.strip()

    errors: list[str] = []
    for check, value in [
        (validate_company_name, company_name),
        (validate_full_name, full_name),
        (validate_phone, phone),
        (validate_email, email),
        (validate_password, password),
    ]:
        msg = check(value)
        if msg:
            errors.append(msg)

    if not errors and db.query(models.User).filter(
            models.User.email == email).first():
        errors.append("Этот email уже зарегистрирован")

    if errors:
        request.session["register_errors"] = errors
        request.session["register_data"] = {
            "email": email, "full_name": full_name,
            "phone": phone, "company_name": company_name,
        }
        return RedirectResponse(url="/login", status_code=303)

    user = models.User(
        email=email,
        hashed_password=hash_password(password),
        full_name=full_name,
        phone=phone,
        company_name=company_name,
        is_approved=False,
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        request.session["register_errors"] = ["Этот email уже зарегистрирован"]
        request.session["register_data"] = {
            "email": email, "full_name": full_name,
            "phone": phone, "company_name": company_name,
        }
        return RedirectResponse(url="/login", status_code=303)

    logger.info("Заявка на регистрацию: %s (%s)", email, company_name)
    return RedirectResponse(url="/registration_pending", status_code=303)


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    check_rate_limit(request, "login", max_hits=10, window=60)
    email = email.strip().lower()

    if not email or not password:
        request.session["login_error"] = "Заполните email и пароль"
        return RedirectResponse(url="/login", status_code=303)

    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.hashed_password):
        request.session["login_error"] = "Неверный email или пароль"
        return RedirectResponse(url="/login", status_code=303)

    if not user.is_approved and not user.is_admin:
        request.session["login_error"] = (
            "Ваш профиль ещё не активирован. "
            "Менеджер свяжется с вами и откроет доступ."
        )
        return RedirectResponse(url="/login", status_code=303)

    reset_rate_limit(request, "login")
    request.session["user_id"] = user.id
    request.session["flash"] = f"Добро пожаловать, {user.company_name}!"
    logger.info("Вход: %s", email)
    return RedirectResponse(url="/catalog", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)