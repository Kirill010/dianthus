# app/routers/staff.py
"""Инструменты сотрудников: карточка клиента, заметки, листы сборки, роли,
сброс пароля, настройки магазина.

Права:
  * сотрудник (менеджер или владелец): карточка клиента и заметка, заметка к заказу,
    листы сборки, счёт PDF любого заказа;
  * только владелец: роль «менеджер», сброс пароля клиента, настройки магазина.
"""
import logging
import re
import secrets
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import require_admin, require_staff
from ..models import Claim, Order, User
from ..security import check_csrf, reset_login_failures
from ..services import legal, preorder_rules
from ..services.audit import log_action
from ..services.passwords import hash_password
from ..services.site_settings import EDITABLE, get_setting, set_setting
from ..templating import render
from ..timeutils import shop_tz
from ..web import safe_back

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

ACTIVE_STATUSES = ["Новый", "Подтверждён", "В работе"]
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без 0/O/1/l/I


def _back(request: Request, default: str) -> str:
    return safe_back(request, default)


def new_temp_password(length: int = 10) -> str:
    """Читаемый временный пароль: гарантированно есть буква и цифра."""
    while True:
        pwd = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if any(c.isdigit() for c in pwd) and any(c.isalpha() for c in pwd):
            return pwd


# ═══════════════════════════════════════════════════════════
# ЗАКАЗ: ЗАМЕТКА, ПЕЧАТЬ, СЧЁТ
# ═══════════════════════════════════════════════════════════

def _order_or_404(db: Session, order_id: int) -> Order:
    order = (db.query(Order)
             .options(selectinload(Order.items), joinedload(Order.user))
             .filter(Order.id == order_id).first())
    if order is None:
        raise HTTPException(404, "Заказ не найден")
    return order


@router.post("/orders/{order_id:int}/note")
def order_note(order_id: int, request: Request, staff_note: str = Form(""),
               db: Session = Depends(get_db), staff=Depends(require_staff),
               _csrf: None = Depends(check_csrf)):
    order = _order_or_404(db, order_id)
    order.staff_note = staff_note.strip()[:2000]
    db.commit()
    log_action(db, request, staff, "order_note", "order", order.id, "Изменена заметка к заказу")
    request.session["flash"] = "Заметка сохранена"
    return RedirectResponse(url=f"/admin/orders/{order.id}", status_code=303)


@router.get("/orders/{order_id:int}/print", response_class=HTMLResponse)
def order_print(order_id: int, request: Request, db: Session = Depends(get_db),
                staff=Depends(require_staff)):
    """Лист сборки одного заказа — печатается и идёт на склад."""
    order = _order_or_404(db, order_id)
    return render(request, "admin_print_order.html", db, user=staff, order=order,
                  total_stems=sum(i.total_stems for i in order.items))


@router.get("/orders/{order_id:int}/invoice")
def order_invoice(order_id: int, request: Request, db: Session = Depends(get_db),
                  staff=Depends(require_staff)):
    from ..services.pdf_service import generate_invoice_pdf, shop_info_for_invoice
    order = _order_or_404(db, order_id)
    try:
        pdf = generate_invoice_pdf(order, order.user, shop_info_for_invoice(db))
    except Exception as e:                                   # noqa: BLE001
        logger.exception("Счёт PDF (сотрудник): %s", e)
        request.session["flash"] = "Не удалось создать PDF"
        return RedirectResponse(url=f"/admin/orders/{order_id}", status_code=303)
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename=invoice_{order.id}.pdf"})


@router.get("/picking", response_class=HTMLResponse)
def picking_list(request: Request, date: str = "", db: Session = Depends(get_db),
                 staff=Depends(require_staff)):
    """Сводный лист сборки: сколько чего достать со склада для всех активных заказов
    (или только для заказов на выбранную дату)."""
    query = (db.query(Order)
             .options(selectinload(Order.items), joinedload(Order.user))
             .filter(Order.status.in_(ACTIVE_STATUSES)))
    chosen = preorder_rules.parse_user_date(date) if date.strip() else None
    if chosen:
        query = query.filter(Order.desired_date == datetime(chosen.year, chosen.month, chosen.day))
    orders = query.order_by(Order.desired_date.is_(None), Order.desired_date, Order.id).limit(300).all()

    totals: "OrderedDict[tuple, dict]" = OrderedDict()
    for order in orders:
        for item in order.items:
            key = (item.product_name, item.package_size or 1)
            row = totals.setdefault(key, {"name": item.product_name, "pack": item.package_size or 1,
                                          "packs": 0, "orders": 0})
            row["packs"] += item.quantity or 0
            row["orders"] += 1
    rows = sorted(totals.values(), key=lambda r: r["name"])
    return render(request, "admin_picking.html", db, user=staff, orders=orders, rows=rows,
                  chosen=chosen, total_packs=sum(r["packs"] for r in rows),
                  total_stems=sum(r["packs"] * r["pack"] for r in rows),
                  today=datetime.now(shop_tz()).date())


# ═══════════════════════════════════════════════════════════
# КАРТОЧКА КЛИЕНТА
# ═══════════════════════════════════════════════════════════

def _client_or_404(db: Session, user_id: int) -> User:
    client = db.get(User, user_id)
    if client is None:
        raise HTTPException(404, "Клиент не найден")
    return client


@router.get("/clients/{user_id:int}", response_class=HTMLResponse)
def client_card(user_id: int, request: Request, db: Session = Depends(get_db),
                staff=Depends(require_staff)):
    client = _client_or_404(db, user_id)
    orders = (db.query(Order).options(selectinload(Order.items))
              .filter(Order.user_id == client.id).order_by(Order.id.desc()).limit(50).all())
    agg = db.query(func.count(Order.id), func.coalesce(func.sum(Order.total_price), 0),
                   func.max(Order.created_at)).filter(
        Order.user_id == client.id, Order.status != "Отменён").one()
    claims = db.query(Claim).filter(Claim.user_id == client.id).order_by(Claim.id.desc()).limit(20).all()
    return render(
        request, "admin_client.html", db, user=staff, client=client, orders=orders,
        claims=claims, orders_count=agg[0], orders_sum=float(agg[1] or 0), last_order=agg[2],
        average=(float(agg[1] or 0) / agg[0]) if agg[0] else 0,
        temp_password=None,
    )


@router.post("/clients/{user_id:int}/note")
def client_note(user_id: int, request: Request, staff_note: str = Form(""),
                db: Session = Depends(get_db), staff=Depends(require_staff),
                _csrf: None = Depends(check_csrf)):
    client = _client_or_404(db, user_id)
    client.staff_note = staff_note.strip()[:3000]
    db.commit()
    log_action(db, request, staff, "client_note", "user", client.id, "Изменена заметка о клиенте")
    request.session["flash"] = "Заметка сохранена"
    return RedirectResponse(url=f"/admin/clients/{client.id}", status_code=303)


@router.post("/clients/{user_id:int}/marketing_off")
def client_marketing_off(user_id: int, request: Request, db: Session = Depends(get_db),
                         staff=Depends(require_staff), _csrf: None = Depends(check_csrf)):
    """Клиент по телефону/письму попросил больше не присылать предложения: фиксируем
    отзыв согласия (дата, кто записал) и прекращаем рекламные звонки и сообщения."""
    client = _client_or_404(db, user_id)
    if client.marketing_consent:
        legal.record_consent(db, client, "marketing", False, request, source="admin")
        log_action(db, request, staff, "marketing_off", "user", client.id,
                   f"{client.email}: согласие на рекламу отозвано сотрудником по просьбе клиента")
        request.session["flash"] = "Согласие на рекламу отозвано. Больше не звоним и не пишем с предложениями."
    else:
        request.session["flash"] = "У клиента и так нет согласия на рекламу"
    return RedirectResponse(url=f"/admin/clients/{client.id}", status_code=303)


@router.post("/users/{user_id:int}/manager")
def toggle_manager(user_id: int, request: Request, db: Session = Depends(get_db),
                   admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    """Выдать/забрать роль «менеджер»: заказы, клиенты, рекламации — без справочника,
    поставок, ключей 1С, юридических текстов и выручки."""
    client = _client_or_404(db, user_id)
    if client.is_admin:
        request.session["flash"] = "Владелец и так видит всё"
    elif not client.is_approved or client.anonymized_at:
        request.session["flash"] = "Сначала одобрите клиента — роль можно выдать только действующей учётной записи"
    else:
        client.is_manager = not client.is_manager
        db.commit()
        log_action(db, request, admin, "user_manager", "user", client.id,
                   f"{client.email}: менеджер = {client.is_manager}")
        request.session["flash"] = (f"«{client.full_name}» теперь менеджер" if client.is_manager
                                    else f"«{client.full_name}» больше не менеджер")
    return RedirectResponse(url=_back(request, f"/admin/clients/{client.id}"), status_code=303)


@router.post("/users/{user_id:int}/reset_password")
async def reset_password(user_id: int, request: Request, db: Session = Depends(get_db),
                         admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    """Новый временный пароль для клиента, который не может войти и не получает письма.
    Показывается один раз — продиктуйте его клиенту по телефону."""
    client = _client_or_404(db, user_id)
    if client.anonymized_at:
        raise HTTPException(400, "Данные клиента удалены")
    if client.is_admin and client.id != admin.id:
        request.session["flash"] = "Пароль другого администратора сбрасывать нельзя"
        return RedirectResponse(url=f"/admin/clients/{client.id}", status_code=303)
    temp = new_temp_password()
    client.hashed_password = hash_password(temp)
    db.commit()
    await reset_login_failures(client.email)        # снять блокировку входа после ошибок
    log_action(db, request, admin, "user_reset_password", "user", client.id,
               f"Сброшен пароль: {client.email}")
    orders = []
    response = render(request, "admin_client.html", db, user=admin, client=client,
                      orders=orders, claims=[], orders_count=0, orders_sum=0.0,
                      last_order=None, average=0, temp_password=temp)
    response.headers["Cache-Control"] = "no-store"
    return response


# ═══════════════════════════════════════════════════════════
# НАСТРОЙКИ МАГАЗИНА
# ═══════════════════════════════════════════════════════════

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db),
                  admin=Depends(require_admin)):
    fields = [{"key": k, "label": label, "hint": hint, "value": get_setting(db, k),
               "default": default} for k, (label, default, hint) in EDITABLE.items()]
    rules = preorder_rules.lead_days(db)
    return render(request, "admin_settings.html", db, user=admin, fields=fields,
                  rules_text=preorder_rules.describe(rules), errors=request.session.pop("settings_errors", None))


@router.post("/settings")
def settings_save(request: Request, payment_terms: str = Form(""), preorder_lead_days: str = Form(""),
                  db: Session = Depends(get_db), admin=Depends(require_admin),
                  _csrf: None = Depends(check_csrf)):
    errors = []
    payment_terms = " ".join(payment_terms.split())[:300]
    if not payment_terms:
        errors.append("Условия оплаты не могут быть пустыми")
    rules = preorder_rules.parse_lead_days(preorder_lead_days)
    parts = [p for p in re.split(r"[,;\n]+", preorder_lead_days) if p.strip()]
    if not rules:
        errors.append("Сроки заказа: нужна хотя бы одна строка вида «Эквадор=10»")
    elif len(rules) < len(parts):
        errors.append("Часть строк в сроках заказа не распознана — формат «Страна=дней», через запятую")
    if errors:
        request.session["settings_errors"] = errors
        return RedirectResponse(url="/admin/settings", status_code=303)
    set_setting(db, "payment_terms", payment_terms, admin)
    set_setting(db, "preorder_lead_days", ", ".join(f"{n}={d}" for n, d in rules.items()), admin)
    log_action(db, request, admin, "settings", "settings", None, "Изменены настройки магазина")
    request.session["flash"] = "Настройки сохранены"
    return RedirectResponse(url="/admin/settings", status_code=303)
