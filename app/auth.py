# app/auth.py — исправленная версия
"""Регистрация, вход, выход."""
import logging
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .security import (check_csrf, check_rate_limit, reset_rate_limit,
                       ensure_csrf_token, _client_ip, consume_once,
                       check_login_lock, record_login_failure,
                       reset_login_failures)
from .services import legal, totp
from .services.passwords import hash_password, verify_password
from .validators import (validate_address, validate_company_name, validate_email,
                         validate_full_name, validate_password,
                         validate_phone, validate_inn, validate_city)
from . import models
from .services.notifier import notify_admin_new_client

logger = logging.getLogger(__name__)
router = APIRouter()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Хэш-пустышка: чтобы проверка пароля занимала одно и то же время,
    есть такой email в базе или нет."""
    return hash_password("dummy-password-for-timing")


async def _rate_register(request: Request) -> None:
    await check_rate_limit(request, "register", max_hits=5, window=300)


async def _rate_login(request: Request) -> None:
    await check_rate_limit(request, "login", max_hits=10, window=60)


@router.post("/register")
async def register(
    request: Request,
    background_tasks: BackgroundTasks,
    _rate: None = Depends(_rate_register),
    _csrf: None = Depends(check_csrf),
    email: str = Form(""),
    password: str = Form(""),
    full_name: str = Form(""),
    phone: str = Form(""),
    company_name: str = Form(""),
    inn: str = Form(""),
    city: str = Form(""),
    address: str = Form(""),
    consent: str = Form(""),
    marketing_consent: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    company_name = company_name.strip()
    full_name = full_name.strip()
    phone = phone.strip()
    inn = inn.strip()
    city = city.strip()
    address = address.strip()

    errors: list[str] = []
    for check, value in [
        (validate_company_name, company_name),
        (validate_full_name, full_name),
        (validate_phone, phone),
        (validate_email, email),
        (validate_password, password),
        (validate_inn, inn),
        (validate_city, city),
        (validate_address, address),
    ]:
        msg = check(value)
        if msg:
            errors.append(msg)

    # 152-ФЗ: персональные данные обрабатываются только с согласием субъекта.
    # Согласие на рекламу — отдельное и необязательное (ст. 18 закона «О рекламе»).
    if not consent:
        errors.append("Нужно согласие на обработку персональных данных")

    if not errors and db.query(models.User).filter(
            models.User.email == email).first():
        errors.append("Этот email уже зарегистрирован")

    if errors:
        request.session["register_errors"] = errors
        request.session["register_data"] = {
            "email": email, "full_name": full_name,
            "phone": phone, "company_name": company_name,
            "inn": inn, "city": city, "address": address, "consent": bool(consent),
            "marketing_consent": bool(marketing_consent),
        }
        return RedirectResponse(url="/register", status_code=303)

    try:
        pwd_hash = hash_password(password)
    except ValueError:
        request.session["register_errors"] = ["Пароль слишком длинный"]
        return RedirectResponse(url="/register", status_code=303)

    user = models.User(
        email=email, hashed_password=pwd_hash,
        full_name=full_name, phone=phone,
        company_name=company_name, inn=inn, city=city, address=address,
        is_approved=False,
    )
    try:
        db.add(user)
        db.flush()                       # нужен id клиента для журнала согласий
        # Фиксируем согласия: дата, версия текста, IP — доказательство на случай
        # проверки. Рекламное согласие записывается только если клиент его отметил.
        legal.record_consent(db, user, "pd", True, request,
                             source="registration", commit=False)
        if marketing_consent:
            legal.record_consent(db, user, "marketing", True, request,
                                 source="registration", commit=False)
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        request.session["register_errors"] = ["Этот email уже зарегистрирован"]
        return RedirectResponse(url="/register", status_code=303)

    background_tasks.add_task(notify_admin_new_client, user)

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

    remaining = await check_login_lock(email)
    if remaining > 0:
        minutes = max(1, remaining // 60)
        request.session["login_error"] = (
            f"Слишком много неудачных попыток. "
            f"Попробуйте снова через {minutes} мин."
        )
        return RedirectResponse(url="/login", status_code=303)

    user = db.query(models.User).filter(models.User.email == email).first()
    # bcrypt считается ВСЕГДА, даже если такого email нет. Иначе по времени
    # ответа (≈0,2 с у существующих и ≈0 у несуществующих) можно было бы
    # подбирать, какие адреса зарегистрированы в магазине.
    password_ok = verify_password(
        password, user.hashed_password if user else _dummy_hash()
    )
    if not user or not password_ok:
        lock_sec = await record_login_failure(email)
        if lock_sec > 0:
            minutes = max(1, lock_sec // 60)
            request.session["login_error"] = (
                f"Слишком много неудачных попыток. "
                f"Вход заблокирован на {minutes} мин."
            )
        else:
            request.session["login_error"] = "Неверный email или пароль"
        return RedirectResponse(url="/login", status_code=303)

    if not user.is_approved and not user.is_admin:
        request.session["login_error"] = (
            "Ваш профиль ещё не активирован. "
            "Менеджер свяжется с вами и откроет доступ."
        )
        return RedirectResponse(url="/login", status_code=303)

    if user.is_admin and user.totp_secret:
        code = "".join(str((await request.form()).get("totp_code", "") or "").split())
        if not code:
            # Пароль верный, но кода ещё нет — просто просим его. Раньше такой
            # вход считался НЕУДАЧНЫМ: у админа с 2FA первая попытка всегда
            # «сгорала», и две обычные опечатки подряд блокировали вход.
            request.session["login_error"] = "Введите код из приложения-аутентификатора"
            request.session["show_totp"] = True
            return RedirectResponse(url="/login", status_code=303)

        # Один и тот же код нельзя использовать дважды (перехваченный код
        # не пригодится): консумируем успешный код на 90 секунд.
        code_ok = totp.verify(user.totp_secret, code) and await consume_once(
            f"totp:{user.id}:{code}", ttl=90
        )
        if not code_ok:
            # Неверный код 2FA считается НЕУДАЧНОЙ попыткой входа:
            # иначе 6-значный код можно было бы перебирать бесконечно
            # (лимит IP на /login легко обходится сменой адресов).
            lock_sec = await record_login_failure(email)
            if lock_sec > 0:
                minutes = max(1, lock_sec // 60)
                request.session["login_error"] = (
                    f"Слишком много неудачных попыток. "
                    f"Вход заблокирован на {minutes} мин."
                )
            else:
                request.session["login_error"] = "Неверный код двухфакторной аутентификации"
            request.session["show_totp"] = True
            logger.warning("2FA: неверный код для %s, IP=%s", email, _client_ip(request))
            return RedirectResponse(url="/login", status_code=303)

    request.session.clear()
    request.session["_rotate"] = True      # новый id сессии (защита от фиксации)
    ensure_csrf_token(request)

    await reset_login_failures(email)
    await reset_rate_limit(request, "login")

    try:                                  # «когда клиент заходил в последний раз»
        user.last_login_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
    except Exception as e:                # не мешаем входу из-за служебной отметки
        db.rollback()
        logger.warning("last_login_at: %s", e)

    request.session["user_id"] = user.id
    request.session["flash"] = f"Добро пожаловать, {user.company_name}!"
    logger.info("Вход: %s", email)
    return RedirectResponse(url="/catalog", status_code=303)


@router.post("/logout")
async def logout(
    request: Request,
    _csrf: None = Depends(check_csrf),
):
    request.session.clear()
    request.session["_rotate"] = True
    ensure_csrf_token(request)
    return RedirectResponse(url="/login", status_code=303)