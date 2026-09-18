"""Профиль клиента и страница контактов."""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import hash_password, verify_password
from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..security import check_csrf
from ..templating import render
from ..validators import (validate_company_name, validate_full_name,
                          validate_password, validate_phone)
from .. import models

logger = logging.getLogger(__name__)
router = APIRouter()


# ═══════ ПРОФИЛЬ ════════════════════════════════════════════

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    orders_count = (
        db.query(models.Order)
        .filter(models.Order.user_id == user.id)
        .count()
    )
    prices = (
        db.query(models.Order.total_price)
        .filter(models.Order.user_id == user.id)
        .all()
    )
    total_sum = sum((p[0] or 0) for p in prices)

    return render(request, "profile.html", db,
                  user=user,
                  orders_count=orders_count,
                  total_sum=total_sum,
                  profile_errors=request.session.pop("profile_errors", None),
                  profile_ok=request.session.pop("profile_ok", None),
                  password_errors=request.session.pop("password_errors", None),
                  password_ok=request.session.pop("password_ok", None))


@router.post("/profile")
async def profile_update(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    company_name: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    full_name = full_name.strip()
    phone = phone.strip()
    company_name = company_name.strip()

    errors: list[str] = []
    for check, value in [
        (validate_full_name, full_name),
        (validate_phone, phone),
        (validate_company_name, company_name),
    ]:
        msg = check(value)
        if msg:
            errors.append(msg)

    if errors:
        request.session["profile_errors"] = errors
        return RedirectResponse(url="/profile", status_code=303)

    user.full_name = full_name
    user.phone = phone
    user.company_name = company_name
    db.commit()
    logger.info("Профиль обновлён: %s", user.email)
    request.session["profile_ok"] = "Данные профиля сохранены"
    return RedirectResponse(url="/profile", status_code=303)


@router.post("/profile/password")
async def profile_change_password(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password2: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    errors: list[str] = []

    if not verify_password(old_password, user.hashed_password):
        errors.append("Текущий пароль неверный")

    if new_password != new_password2:
        errors.append("Новые пароли не совпадают")

    msg = validate_password(new_password)
    if msg:
        errors.append(msg)

    if errors:
        request.session["password_errors"] = errors
        return RedirectResponse(url="/profile", status_code=303)

    user.hashed_password = hash_password(new_password)
    db.commit()
    logger.info("Пароль изменён: %s", user.email)
    request.session["password_ok"] = "Пароль успешно изменён"
    return RedirectResponse(url="/profile", status_code=303)


# ═══════ КОНТАКТЫ ═══════════════════════════════════════════

@router.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    shop = {
        "name": config.SHOP_NAME,
        "phone": config.SHOP_PHONE,
        "phone_2": config.SHOP_PHONE_2,
        "address": config.SHOP_ADDRESS,
        "hours": config.SHOP_WORKING_HOURS,
        "max_link": config.SHOP_MAX_LINK,
        "map_url": config.SHOP_MAP_URL,
    }
    return render(request, "contacts.html", db, user=user, shop=shop)