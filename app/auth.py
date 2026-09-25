# app/auth.py
"""Регистрация, вход, выход."""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .security import (check_csrf, check_rate_limit, reset_rate_limit,
                       ensure_csrf_token)
from .services.passwords import hash_password, verify_password
from .validators import (validate_company_name, validate_email,
                         validate_full_name, validate_password,
                         validate_phone, validate_inn, validate_city)
from . import models
from .services.notifier import notify_admin_new_client

logger = logging.getLogger(__name__)
router = APIRouter()


# fix #15: rate limit ВЫПОЛНЯЕТСЯ ДО CSRF — защищает от брутфорса
async def _rate_register(request: Request) -> None:
    await check_rate_limit(request, "register", max_hits=5, window=300)


async def _rate_login(request: Request) -> None:
    await check_rate_limit(request, "login", max_hits=10, window=60)


@router.post("/register")
async def register(
    request: Request,
    _rate: None = Depends(_rate_register),
    _csrf: None = Depends(check_csrf),
    email: str = Form(""),
    password: str = Form(""),
    full_name: str = Form(""),
    phone: str = Form(""),
    company_name: str = Form(""),
    inn: str = Form(""),
    city: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    company_name = company_name.strip()
    full_name = full_name.strip()
    phone = phone.strip()
    inn = inn.strip()
    city = city.strip()

    errors: list[str] = []
    for check, value in [
        (validate_company_name, company_name),
        (validate_full_name, full_name),
        (validate_phone, phone),
        (validate_email, email),
        (validate_password, password),
        (validate_inn, inn),
        (validate_city, city),
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
            "inn": inn, "city": city,
        }
        return RedirectResponse(url="/login", status_code=303)

    try:
        pwd_hash = hash_password(password)
    except ValueError:
        request.session["register_errors"] = ["Пароль слишком длинный"]
        return RedirectResponse(url="/login", status_code=303)

    user = models.User(
        email=email, hashed_password=pwd_hash,
        full_name=full_name, phone=phone,
        company_name=company_name, inn=inn, city=city,
        is_approved=False,
    )
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        request.session["register_errors"] = ["Этот email уже зарегистрирован"]
        return RedirectResponse(url="/login", status_code=303)

    try:
        notify_admin_new_client(user)
    except Exception as e:
        logger.warning("Не удалось уведомить админа: %s", e)

    logger.info("Заявка: %s (%s)", email, company_name)
    return RedirectResponse(url="/registration_pending", status_code=303)


@router.post("/login")
async def login(
    request: Request,
    _rate: None = Depends(_rate_login),
    _csrf: None = Depends(check_csrf),
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
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

    await reset_rate_limit(request, "login")
    request.session["user_id"] = user.id
    request.session["flash"] = f"Добро пожаловать, {user.company_name}!"
    logger.info("Вход: %s", email)
    return RedirectResponse(url="/catalog", status_code=303)


# fix #1: logout теперь POST + CSRF
@router.post("/logout")
async def logout(
    request: Request,
    _csrf: None = Depends(check_csrf),
):
    request.session.clear()
    ensure_csrf_token(request)
    return RedirectResponse(url="/login", status_code=303)