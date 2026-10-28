# app/routers/admin.py
"""Админ-панель: заказы, клиенты, справочник, поставки."""
import logging
from datetime import datetime, timedelta
from typing import List

from starlette.concurrency import run_in_threadpool
from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, joinedload

from ..database import get_db
from ..deps import require_admin, require_staff
from ..models import (ORDER_STATUSES, SUPPLY_STATUSES,
                      AuditLog, Claim, Order, OrderItem, Preorder, Product,
                      Supply, SupplyItem, User)
from ..security import check_csrf
from ..services import api_keys, exchange_log, legal, taxonomy
from ..services.audit import log_action
from ..services.catalog_sync import Catalog, apply_classification, resolve_classification
from ..services.orders import (apply_status_change, load_order_for_update,
                               notify_client_of_status)
from ..services.excel_service import (build_products_import_template,
                                      export_customers_to_excel,
                                      export_orders_to_excel,
                                      guess_package_size,
                                      import_products_from_excel,
                                      parse_invoice)
from ..services.notifier import notify_client_approved
from ..services.upload_service import delete_upload, save_uploads
from ..templating import render
from ..timeutils import DATE_FMT, fmt_local, local_midnight_utc, shop_tz
from ..validators import (validate_country, validate_price, validate_stock,
                          validate_city)
from ..web import LIKE_ESCAPE, contains_pattern, safe_back

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_EXCEL_SIZE = 10 * 1024 * 1024
CHUNK = 64 * 1024
ORDERS_PER_PAGE = 20
LIST_LIMIT = 500          # сколько строк показывать во вкладках админки


# ───────────────────────── helpers ─────────────────────────

def _xlsx(stream, filename: str) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_date(v: str):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return None


def _day_start(days_ahead: int = 0) -> datetime:
    """Полночь (naive) через N дней от сегодняшней даты по времени магазина.

    Желаемая дата получения лежит в заказе как «дата в полночь» без пояса,
    поэтому и границы считаем в тех же календарных сутках магазина.
    """
    today = datetime.now(shop_tz()).date() + timedelta(days=days_ahead)
    return datetime(today.year, today.month, today.day)


def _err(request: Request, errors: list[str]) -> None:
    request.session["flash"] = "Ошибки: " + "; ".join(errors)


def _back(request: Request, default: str = "/admin") -> str:
    # Раньше сравнивали Referer с request.base_url: за nginx схема/хост
    # расходились, проверка всегда падала, и менеджера возвращало в /admin
    # вместо открытой страницы заказа. safe_back делает это корректно.
    return safe_back(request, default)


def _detach_supply_items(db: Session, item_ids: list[int]) -> None:
    if not item_ids:
        return
    db.query(OrderItem).filter(
        OrderItem.supply_item_id.in_(item_ids)
    ).update({OrderItem.supply_item_id: None}, synchronize_session=False)

    db.query(Preorder).filter(
        Preorder.supply_item_id.in_(item_ids)
    ).update({Preorder.supply_item_id: None}, synchronize_session=False)


def _detach_product(db: Session, product_id: int, supply_item_ids: list[int]) -> None:
    db.query(OrderItem).filter(
        OrderItem.product_id == product_id
    ).update({OrderItem.product_id: None}, synchronize_session=False)

    db.query(Preorder).filter(
        Preorder.product_id == product_id
    ).update({Preorder.product_id: None}, synchronize_session=False)

    _detach_supply_items(db, supply_item_ids)


# ───────────────────────── проверка полей товара ─────────────────────────

def _check_product_fields(db: Session, name, sku, barcode, description, unit,
                          package_size, min_quantity, product_id: int | None = None) -> list[str]:
    """Единые правила для создания и правки товара.

    Раньше правка не проверяла ничего: package_size=0 или длина названия
    больше колонки (200) приводили к странным остаткам и ошибке 500 на
    PostgreSQL.
    """
    errors = []
    name, sku, barcode = (name or "").strip(), (sku or "").strip(), (barcode or "").strip()
    if not name:
        errors.append("Укажите название")
    elif len(name) > 200:
        errors.append("Название длиннее 200 символов")
    if len(sku) > 100:
        errors.append("Артикул длиннее 100 символов")
    if len(barcode) > 64:
        errors.append("Штрихкод длиннее 64 символов")
    elif barcode:
        # по штрихкоду товар находится при обмене с 1С: у двух товаров он не должен совпадать
        twin = db.query(Product).filter(Product.barcode == barcode, Product.id != (product_id or 0)).first()
        if twin is not None:
            errors.append(f"Штрихкод {barcode} уже указан у товара «{twin.name}»")
    if len((unit or "").strip()) > 30:
        errors.append("Единица измерения длиннее 30 символов")
    if len((description or "")) > 5000:
        errors.append("Описание длиннее 5000 символов")
    if not (1 <= (package_size or 0) <= 10_000):
        errors.append("Размер упаковки: от 1 до 10 000 шт")
    if not (1 <= (min_quantity or 0) <= 10_000):
        errors.append("Мин. заказ: от 1 до 10 000 упаковок")
    return errors


def _picked_values(db: Session, errors: list[str], **ids) -> dict:
    """Что выбрано в выпадающих списках формы товара: {kind: запись справочника | None}."""
    picked = {}
    for kind in taxonomy.MODELS:
        value = ids.get(f"{kind}_id") or 0
        row = db.get(taxonomy.MODELS[kind], value) if value else None
        if value and row is None:
            errors.append(f"{taxonomy.LABELS[kind]}: выбранного значения нет в списке")
        picked[kind] = row
    return picked


def _is_photo_url(url: str) -> bool:
    return url.startswith(("http://", "https://", "/static/")) and len(url) <= 500


# ───────────────────────── ДАШБОРД ─────────────────────────

@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    staff=Depends(require_staff),
    q: str = "",
    status: str = "",
    due: int = 0,
    page: int = 1,
):
    q, status, page = (q or "").strip(), (status or "").strip(), max(1, page)
    is_admin = bool(staff.is_admin)

    orders_query = db.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items),
    )
    if q:
        conditions = []
        if q.isdigit() and len(q) < 15:
            conditions.append(Order.id == int(q))
        pattern = contains_pattern(q)
        conditions.extend([
            User.company_name.ilike(pattern, escape=LIKE_ESCAPE),
            User.email.ilike(pattern, escape=LIKE_ESCAPE),
            User.full_name.ilike(pattern, escape=LIKE_ESCAPE),
            User.phone.ilike(pattern, escape=LIKE_ESCAPE),
        ])
        orders_query = orders_query.outerjoin(
            User, Order.user_id == User.id
        ).filter(or_(*conditions))
    if status:
        orders_query = orders_query.filter(Order.status == status)
    urgent_filter = (
        Order.desired_date.isnot(None),
        Order.desired_date < _day_start(2),            # сегодня, завтра и просроченные
        Order.status.notin_(["Выполнен", "Отменён"]),
    )
    if due:
        orders_query = orders_query.filter(*urgent_filter)

    total_orders = orders_query.count()
    orders_total_pages = max(1, (total_orders + ORDERS_PER_PAGE - 1) // ORDERS_PER_PAGE)
    page = min(page, orders_total_pages)
    orders = (
        orders_query
        .order_by(Order.created_at.desc())
        .offset((page - 1) * ORDERS_PER_PAGE)
        .limit(ORDERS_PER_PAGE)
        .all()
    )

    # Границы «сегодня» и «неделя назад» — полночь по времени магазина,
    # переведённая в UTC (в БД всё лежит в UTC).
    today = local_midnight_utc(0)
    week_ago = local_midnight_utc(7)
    stats = {
        "orders_today": db.query(Order).filter(Order.created_at >= today).count(),
        "orders_week": db.query(Order).filter(Order.created_at >= week_ago).count(),
        "pending_orders": db.query(Order).filter(Order.status == "Новый").count(),
        "new_clients_week": db.query(User).filter(User.created_at >= week_ago).count(),
    }
    if is_admin:                       # выручку видит только владелец
        stats["revenue_today"] = db.query(func.coalesce(func.sum(Order.total_price), 0)) \
            .filter(Order.created_at >= today, Order.status != "Отменён").scalar() or 0
        stats["revenue_week"] = db.query(func.coalesce(func.sum(Order.total_price), 0)) \
            .filter(Order.created_at >= week_ago, Order.status != "Отменён").scalar() or 0

    status_counts = dict(db.query(Order.status, func.count(Order.id)).group_by(Order.status).all())
    todo = {
        "new_orders": status_counts.get("Новый", 0),
        "urgent_orders": db.query(Order).filter(*urgent_filter).count(),
        "applications": db.query(User).filter(
            User.is_approved.is_(False), User.is_admin.is_(False),
            User.anonymized_at.is_(None)).count(),
        "claims": db.query(Claim).filter(Claim.status.in_(["Новая", "В работе"])).count(),
    }
    if is_admin:
        todo["data_requests"] = legal.open_data_requests(db)
        todo["waiting_1c"] = (exchange_log.waiting_orders(db)
                              if api_keys.active_keys_count(db) else 0)

    users_total = db.query(User).count()
    users = (db.query(User).filter(User.anonymized_at.is_(None))
             .order_by(User.is_approved, User.created_at.desc()).limit(LIST_LIMIT).all())

    ctx = dict(
        user=staff, orders=orders, total_orders=total_orders,
        orders_page=page, orders_total_pages=orders_total_pages,
        orders_q=q, orders_status=status, orders_due=due,
        statuses=ORDER_STATUSES, status_counts=status_counts, todo=todo,
        today_dt=_day_start(0), soon_dt=_day_start(2),
        users=users, pending_count=todo["applications"], stats=stats,
        list_limit=LIST_LIMIT, users_total=users_total,
        products=[], supplies=[], active_items=[], products_total=0, active_total=0,
        supply_statuses=SUPPLY_STATUSES,
    )
    if is_admin:
        active_filter = (SupplyItem.is_active.is_(True), SupplyItem.stock > 0)
        ctx.update(
            products_total=db.query(Product).count(),
            products=db.query(Product).order_by(Product.name).limit(LIST_LIMIT).all(),
            supplies=(
                db.query(Supply)
                .options(selectinload(Supply.items).selectinload(SupplyItem.product))
                .filter(Supply.is_service.is_(False))
                .order_by(Supply.arrival_date.asc())
                .all()
            ),
            active_total=db.query(SupplyItem).filter(*active_filter).count(),
            # Раньше грузились ВСЕ позиции: при каталоге из 1С (тысячи товаров)
            # вкладка «На складе» делала тяжёлый запрос на каждое открытие админки.
            active_items=(
                db.query(SupplyItem)
                .options(joinedload(SupplyItem.product), joinedload(SupplyItem.supply))
                .filter(*active_filter)
                .order_by(SupplyItem.id.desc())
                .limit(LIST_LIMIT)
                .all()
            ),
        )
    return render(request, "admin.html", db, **ctx)


# ───────────────────────── ЖУРНАЛ ДЕЙСТВИЙ ─────────────────────────

@router.get("/audit", response_class=HTMLResponse)
def audit_log_page(
    request: Request,
    limit: int = 200,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Кто и что менял: скидки, статусы заказов, товары, поставки, действия 1С.

    Записи писались давно, но посмотреть их было негде.
    """
    limit = max(10, min(int(limit), 1000))
    rows = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .order_by(AuditLog.id.desc())
        .limit(limit)
        .all()
    )
    return render(request, "admin_audit.html", db, user=admin, rows=rows, limit=limit)


# ───────────────────────── МОДЕРАЦИЯ КЛИЕНТОВ ─────────────────────────

@router.post("/users/{user_id}/make_admin")
def user_make_admin(
    user_id: int, request: Request,
    db: Session = Depends(get_db),
    current_admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_admin:
        request.session["flash"] = f"«{user.full_name}» уже админ"
        return RedirectResponse(url=_back(request), status_code=303)
    user.is_admin = True
    user.is_approved = True
    db.commit()
    log_action(db, request, current_admin, "user_make_admin",
               "user", user.id, f"{user.email} назначен администратором")
    request.session["flash"] = f"👑 «{user.full_name}» теперь администратор"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/demote")
def user_demote(
    user_id: int, request: Request,
    db: Session = Depends(get_db),
    current_admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.id == current_admin.id:
        request.session["flash"] = "❌ Нельзя снять права с самого себя"
        return RedirectResponse(url=_back(request), status_code=303)
    if not user.is_admin:
        request.session["flash"] = "Пользователь и так не админ"
        return RedirectResponse(url=_back(request), status_code=303)
    if db.query(User).filter(User.is_admin.is_(True)).count() <= 1:
        request.session["flash"] = "❌ Нельзя снять последнего администратора"
        return RedirectResponse(url=_back(request), status_code=303)
    user.is_admin = False
    db.commit()
    log_action(db, request, current_admin, "user_demote",
               "user", user.id, f"{user.email} лишён прав администратора")
    request.session["flash"] = f"«{user.full_name}» больше не администратор"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/approve")
def user_approve(
    user_id: int, request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_admin=Depends(require_staff),
    _csrf: None = Depends(check_csrf),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session["flash"] = "Пользователь не найден"
        return RedirectResponse(url=_back(request), status_code=303)
    if user.is_approved:
        request.session["flash"] = f"«{user.full_name}» уже одобрен"
        return RedirectResponse(url=_back(request), status_code=303)
    user.is_approved = True
    db.commit()
    log_action(db, request, current_admin, "user_approve",
               "user", user.id, f"{user.email} ({user.company_name}) одобрен")
    # Клиент должен узнать, что может входить (раньше письма не было вообще)
    background_tasks.add_task(notify_client_approved, user)
    request.session["flash"] = f"✅ «{user.company_name}» одобрен"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/reject")
def user_reject(
    user_id: int, request: Request,
    db: Session = Depends(get_db),
    current_admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session["flash"] = "Пользователь не найден"
        return RedirectResponse(url=_back(request), status_code=303)
    if user.id == current_admin.id:
        request.session["flash"] = "❌ Нельзя удалить себя"
        return RedirectResponse(url=_back(request), status_code=303)
    if user.is_admin:
        request.session["flash"] = "❌ Сначала снимите права администратора"
        return RedirectResponse(url=_back(request), status_code=303)

    try:
        name, email = user.full_name, user.email
        has_orders = db.query(Order.id).filter(Order.user_id == user.id).first() is not None
        if has_orders:
            # Заказы нужны для учёта — стираем не клиента вместе с заказами,
            # а только его личные данные. Заказы остаются привязанными к
            # «Клиент №N (данные удалены)».
            if user.consent_at:
                legal.record_consent(db, user, "pd", False, request,
                                     source="admin", commit=False)
            if user.marketing_consent:
                legal.record_consent(db, user, "marketing", False, request,
                                     source="admin", commit=False)
            legal.anonymize_user(db, user)
            db.commit()
            log_action(db, request, current_admin, "user_anonymize",
                       "user", user_id, f"Обезличен клиент {email} ({name})")
            request.session["flash"] = (
                f"🗑 «{name}»: личные данные стёрты, заказы сохранены без имени и контактов"
            )
        else:
            # Заявка без заказов (или тестовый клиент): удаляем целиком, но
            # сначала убираем ссылки из журналов, рекламаций, запросов и т. п.
            legal.purge_user_refs(db, user.id)
            db.delete(user)
            db.commit()
            log_action(db, request, current_admin, "user_reject",
                       "user", user_id, f"Удалён пользователь {email} ({name})")
            request.session["flash"] = f"🗑 «{name}» удалён"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления пользователя: %s", e)
        request.session["flash"] = "❌ Не удалось удалить пользователя"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/discount")
def user_set_discount(
    user_id: int, request: Request,
    discount_percent: float = Form(0),
    db: Session = Depends(get_db),
    current_admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session["flash"] = "Пользователь не найден"
        return RedirectResponse(url=_back(request), status_code=303)
    if discount_percent < 0 or discount_percent > 100:
        request.session["flash"] = "❌ Скидка должна быть в диапазоне 0..100"
        return RedirectResponse(url=_back(request), status_code=303)
    old_discount = user.discount_percent or 0
    user.discount_percent = float(discount_percent)
    db.commit()
    log_action(db, request, current_admin, "user_discount",
               "user", user.id,
               f"{user.email}: скидка {old_discount:.1f}% → {discount_percent:.1f}%")
    request.session["flash"] = f"💸 «{user.company_name}»: скидка {discount_percent:.1f}%"
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ЗАКАЗЫ ─────────────────────────

@router.post("/update_order_status")
def update_order_status(
    request: Request,
    background_tasks: BackgroundTasks,
    order_id: int = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
    _a=Depends(require_staff),
    _csrf: None = Depends(check_csrf),
):
    if status not in ORDER_STATUSES:
        raise HTTPException(400, "Неизвестный статус")

    # Заказ блокируется на время смены статуса: два одновременных «Отменить»
    # не вернут остаток на склад дважды.
    order = load_order_for_update(db, order_id)
    if not order:
        request.session["flash"] = f"Заказ №{order_id} не найден"
        return RedirectResponse(url=_back(request), status_code=303)

    result = apply_status_change(db, order, status)
    if result.error:
        db.rollback()
        request.session["flash"] = f"❌ Заказ №{order_id}: {result.error}"
        return RedirectResponse(url=_back(request), status_code=303)
    if not result.changed:
        db.rollback()
        request.session["flash"] = f"Заказ №{order_id}: статус уже «{status}»"
        return RedirectResponse(url=_back(request), status_code=303)

    db.commit()
    details = f"{result.old} → {result.new}"
    if result.stock_note:
        details += f" ({result.stock_note})"
    log_action(db, request, _a, "order_status", "order", order.id, details)
    notify_client_of_status(db, order, status, background_tasks)

    flash = f"Заказ №{order.id}: «{status}»"
    if result.stock_note:
        flash += f" — {result.stock_note}"
    request.session["flash"] = flash
    return RedirectResponse(url=_back(request), status_code=303)


# {order_id:int} — только цифры. Без этого адрес /admin/orders/export (кнопка
# «Экспорт» в админке) попадал сюда как order_id="export" и отдавал 422.
@router.get("/orders/{order_id:int}", response_class=HTMLResponse)
def order_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_staff),
):
    order = (
        db.query(Order)
        .options(
            selectinload(Order.user),
            selectinload(Order.items),
        )
        .filter(Order.id == order_id)
        .first()
    )
    if not order:
        raise HTTPException(404, "Заказ не найден")

    # кто и когда менял статус (журнал действий) — чтобы не спрашивать «а кто отменил?»
    history = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .filter(AuditLog.entity == "order", AuditLog.entity_id == order.id,
                AuditLog.action.in_(["order_status", "order_status_1c"]))
        .order_by(AuditLog.id.desc()).limit(30).all()
    )
    claims = (db.query(Claim).filter(Claim.order_id == order.id)
              .order_by(Claim.id.desc()).all())
    return render(
        request, "admin_order_detail.html", db,
        user=admin,
        order=order,
        statuses=ORDER_STATUSES,
        history=history,
        claims=claims,
    )

# ───────────────────────── EXCEL ─────────────────────────

@router.get("/orders/export")
def orders_export(db: Session = Depends(get_db), _a=Depends(require_staff)):
    orders = (
        db.query(Order)
        .options(selectinload(Order.user), selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .all()
    )
    return _xlsx(export_orders_to_excel(orders), "dianthus_orders.xlsx")


@router.get("/customers/export")
def customers_export(db: Session = Depends(get_db), _a=Depends(require_admin)):
    """Все клиенты (не только с заказами) + согласие на рекламу: для обзвона и рассылок
    звонить и писать с предложениями можно только тем, у кого согласие «Да»."""
    # итоги по заказам — одним запросом (отменённые не считаем)
    agg = {
        uid: (cnt, total, last)
        for uid, cnt, total, last in db.query(
            Order.user_id, func.count(Order.id),
            func.coalesce(func.sum(Order.total_price), 0), func.max(Order.created_at),
        ).filter(Order.user_id.isnot(None), Order.status != "Отменён")
        .group_by(Order.user_id).all()
    }
    users = (db.query(User)
             .filter(User.is_admin.is_(False), User.is_manager.is_(False),
                     User.anonymized_at.is_(None)).all())
    customers = []
    for u in users:
        cnt, total, last = agg.get(u.id, (0, 0.0, None))
        customers.append({
            "id": u.id,
            "company_name": u.company_name,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "inn": u.inn,
            "city": u.city,
            "address": u.address or "",
            "status": "Активен" if u.is_approved else "Ожидает одобрения",
            "discount_percent": u.discount_percent or 0,
            "orders_count": cnt,
            "total_sum": float(total or 0),
            "last_order_date": fmt_local(last, DATE_FMT, empty=""),
            "last_login": fmt_local(u.last_login_at, DATE_FMT, empty=""),
            "registered": fmt_local(u.created_at, DATE_FMT, empty=""),
            "marketing": "Да" if u.marketing_consent else "Нет",
            "pd_consent": fmt_local(u.consent_at, DATE_FMT, empty="нет"),
        })
    customers.sort(key=lambda c: (-c["total_sum"], c["company_name"]))
    return _xlsx(export_customers_to_excel(customers), "dianthus_customers.xlsx")


@router.get("/products/import/template")
def products_import_template(_a=Depends(require_admin)):
    return _xlsx(build_products_import_template(), "dianthus_products_template.xlsx")


@router.post("/products/import")
async def products_import(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    fn = (file.filename or "").lower()
    if not fn.endswith(".xlsx"):
        request.session["flash"] = "Нужен файл .xlsx"
        return RedirectResponse(url=_back(request), status_code=303)

    content = bytearray()
    try:
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_EXCEL_SIZE:
                raise ValueError("too big")
    except ValueError:
        request.session["flash"] = "Файл слишком большой (>10 МБ)"
        return RedirectResponse(url=_back(request), status_code=303)
    finally:
        await file.close()

    if not content:
        request.session["flash"] = "Файл пустой"
        return RedirectResponse(url=_back(request), status_code=303)

    # Разбор Excel — тяжёлая работа: не блокируем сервер для остальных клиентов
    products, warnings = await run_in_threadpool(
        import_products_from_excel, bytes(content)
    )
    if not products:
        msg = "Импорт не дал результатов."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url=_back(request), status_code=303)

    # Повторная загрузка того же файла раньше дублировала весь справочник. Теперь товар
    # ищется так же, как при обмене с 1С — штрихкод → артикул → ТОЧНОЕ название (регистр
    # намеренно не сглаживаем: lower() в SQLite не понимает кириллицу, а в PostgreSQL
    # зависит от локали): найден — обновляем, нет — создаём. Вид, цвет, рост и страна —
    # значения справочников; нового значения ещё нет — оно создаётся (без дублей).
    created = updated = 0
    try:
        catalog = Catalog(db, products)
        tax = taxonomy.Taxonomy(db)
        for data in products:
            filled = {key: value for key, value in data.items() if value}      # пусто = «не указано»
            row = {"refs": {kind: taxonomy.extract_ref(filled, kind) for kind in taxonomy.JSON_KEYS}}
            try:
                resolved = resolve_classification(tax, row)
            except taxonomy.TaxonomyError as e:
                warnings.append(f"{data['name'][:60]}: {e}")
                continue
            product = catalog.find("", data["sku"], data["name"], data["barcode"])
            if product is None:
                product = Product(
                    name=data["name"], sku=data["sku"], barcode=data["barcode"],
                    description=data["description"], unit=data["unit"] or "упаковка",
                    package_size=data["package_size"] or guess_package_size(data["name"]),
                    min_quantity=data["min_quantity"] or 1)
                db.add(product)
                catalog.add(product)
                created += 1
            else:
                for field in ("sku", "barcode", "unit", "description", "package_size", "min_quantity"):
                    if data[field]:
                        setattr(product, field, data[field])
                updated += 1
            apply_classification(tax, product, resolved, row)
        db.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.rollback()
        logger.exception("Ошибка импорта товаров: %s", e)
        request.session["flash"] = "Импорт не удался, БД откатана."
        return RedirectResponse(url=_back(request), status_code=303)

    msg = f"Импорт: создано {created}, обновлено {updated}"
    if tax.report():
        msg += f". Новые значения фильтров — {taxonomy.describe(tax.report())}"
    if warnings:
        msg += f". Предупреждений: {len(warnings)}: " + "; ".join(warnings[:3])
    request.session["flash"] = msg
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── СПРАВОЧНИК ТОВАРОВ ─────────────────────────

@router.get("/products/new", response_class=HTMLResponse)
def product_new_page(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return render(request, "product_form.html", db,
                  user=admin, product=None, choices=taxonomy.choices(db))


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
def product_edit_page(
    product_id: int, request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Товар не найден")
    return render(request, "product_form.html", db,
                  user=admin, product=product, choices=taxonomy.choices(db))


@router.post("/products/new")
def product_new(
    request: Request,
    name: str = Form(""),
    sku: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    kind_id: int = Form(0),
    color_id: int = Form(0),
    height_id: int = Form(0),
    country_id: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(25),
    min_quantity: int = Form(1),
    image_url: str = Form(""),
    image_files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors = _check_product_fields(db, name, sku, barcode, description, unit,
                                   package_size, min_quantity)
    picked = _picked_values(db, errors, kind_id=kind_id, color_id=color_id,
                            height_id=height_id, country_id=country_id)
    if image_url.strip() and not _is_photo_url(image_url.strip()):
        errors.append("Ссылка на фото должна начинаться с http://, https:// или /static/")
    if errors:
        _err(request, errors)
        return RedirectResponse(url="/admin/products/new", status_code=303)

    photos = []
    if image_files:
        photos.extend(save_uploads(image_files))
    if image_url.strip():
        photos.append(image_url.strip())

    if picked["kind"] is None:                      # вид не выбран — «Прочее», как раньше
        picked["kind"] = taxonomy.Taxonomy(db).by_name("kind", "Прочее")
    product = Product(
        name=name.strip(), sku=sku.strip(), barcode=barcode.strip(),
        description=description.strip(), unit=(unit or "упаковка").strip() or "упаковка",
        package_size=package_size, min_quantity=min_quantity,
        image_url=photos[0] if photos else "", photos=photos,
    )
    for kind, row in picked.items():
        setattr(product, taxonomy.SLOTS[kind], row)
    db.add(product)
    db.commit()
    log_action(db, request, _a, "product_new",
               "product", None, f"Добавлен товар «{name.strip()}»")
    request.session["flash"] = f"Товар добавлен ({len(photos)} фото)"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/products/{product_id}/edit")
def product_edit(
    product_id: int, request: Request,
    name: str = Form(""),
    sku: str = Form(""),
    barcode: str = Form(""),
    description: str = Form(""),
    kind_id: int = Form(0),
    color_id: int = Form(0),
    height_id: int = Form(0),
    country_id: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(25),
    min_quantity: int = Form(1),
    image_url: str = Form(""),
    image_files: List[UploadFile] = File(None),
    remove_photos: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Товар не найден")

    errors = _check_product_fields(db, name, sku, barcode, description, unit,
                                   package_size, min_quantity, product_id=product.id)
    picked = _picked_values(db, errors, kind_id=kind_id, color_id=color_id,
                            height_id=height_id, country_id=country_id)
    if image_url.strip() and not _is_photo_url(image_url.strip()):
        errors.append("Ссылка на фото должна начинаться с http://, https:// или /static/")
    if errors:
        _err(request, errors)
        return RedirectResponse(url=f"/admin/products/{product_id}/edit", status_code=303)

    product.name = name.strip()
    product.sku = sku.strip()
    product.barcode = barcode.strip()
    product.description = description.strip()
    for kind, row in picked.items():            # «не указано» в списке = убрать значение
        setattr(product, taxonomy.SLOTS[kind], row)
    product.unit = (unit or "упаковка").strip() or "упаковка"
    product.package_size = package_size
    product.min_quantity = min_quantity

    current = list(product.photos or [])
    if not current and product.image_url:
        current = [product.image_url]

    if remove_photos:
        for url in remove_photos:
            if url in current:
                current.remove(url)
                if url.startswith("/static/uploads/"):
                    delete_upload(url)

    if image_files:
        current.extend(save_uploads(image_files))
    if image_url.strip():
        current.append(image_url.strip())

    seen, final = set(), []
    for u in current:
        if u and u not in seen:
            seen.add(u)
            final.append(u)

    product.photos = final
    product.image_url = final[0] if final else ""
    db.commit()
    log_action(db, request, _a, "product_edit",
               "product", product.id, f"Обновлён товар «{product.name}»")
    request.session["flash"] = f"Товар обновлён ({len(final)} фото)"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/products/{product_id}/delete")
def product_delete(
    product_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        request.session["flash"] = "Товар не найден"
        return RedirectResponse(url=_back(request), status_code=303)

    supply_item_ids = [si.id for si in product.supply_items]

    try:
        _detach_product(db, product.id, supply_item_ids)

        if product.image_url and product.image_url.startswith("/static/uploads/"):
            delete_upload(product.image_url)

        name = product.name
        db.delete(product)
        db.commit()
        log_action(db, request, _a, "product_delete",
                   "product", product_id, f"Удалён товар «{name}»")
        request.session["flash"] = f"«{name}» удалён из справочника"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления товара: %s", e)
        request.session["flash"] = "❌ Не удалось удалить товар"
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ПОСТАВКИ ─────────────────────────

@router.get("/supplies/new", response_class=HTMLResponse)
def supply_new_page(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return render(request, "supply_form.html", db,
                  user=admin, supply=None, items=[],
                  supply_statuses=SUPPLY_STATUSES,
                  all_products=db.query(Product).order_by(Product.name).all())


@router.post("/supplies/new")
def supply_new(
    request: Request,
    country: str = Form(""),
    city: str = Form(""),
    status: str = Form("Ожидается"),
    arrival_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors = []
    msg = validate_country(country)
    if msg:
        errors.append(msg)
    msg = validate_city(city, required=False)
    if msg:
        errors.append(msg)
    if status not in SUPPLY_STATUSES:
        errors.append("Неизвестный статус поставки")
    if not arrival_date:
        errors.append("Укажите дату прибытия")
    elif not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")
    if errors:
        _err(request, errors)
        return RedirectResponse(url="/admin/supplies/new", status_code=303)

    supply = Supply(
        country=country.strip(),
        city=city.strip(),
        status=status,
        departure_date=None,
        arrival_date=_parse_date(arrival_date),
        notes=notes.strip(),
    )
    db.add(supply)
    db.commit()
    db.refresh(supply)
    log_action(db, request, _a, "supply_new",
               "supply", supply.id,
               f"Создана поставка: {supply.location}, прибытие {arrival_date}")
    request.session["flash"] = f"Поставка №{supply.id} создана. Добавьте товары."
    return RedirectResponse(url=f"/admin/supplies/{supply.id}/edit", status_code=303)


@router.get("/supplies/{supply_id}/edit", response_class=HTMLResponse)
def supply_edit_page(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    return render(request, "supply_form.html", db,
                  user=admin, supply=supply, items=supply.items,
                  supply_statuses=SUPPLY_STATUSES,
                  all_products=db.query(Product).order_by(Product.name).all())


@router.post("/supplies/{supply_id}/edit")
def supply_edit(
    supply_id: int, request: Request,
    country: str = Form(""),
    city: str = Form(""),
    status: str = Form("Ожидается"),
    arrival_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")

    errors = []
    msg = validate_country(country)
    if msg:
        errors.append(msg)
    msg = validate_city(city, required=False)
    if msg:
        errors.append(msg)
    if status not in SUPPLY_STATUSES:
        errors.append("Неизвестный статус поставки")
    if not arrival_date:
        errors.append("Укажите дату прибытия")
    elif not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")
    if errors:
        _err(request, errors)
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    supply.country = country.strip()
    supply.city = city.strip()
    supply.status = status
    supply.arrival_date = _parse_date(arrival_date)
    supply.notes = notes.strip()
    db.commit()
    log_action(db, request, _a, "supply_edit",
               "supply", supply.id,
               f"Поставка №{supply.id}: статус «{status}», прибытие {arrival_date}")
    request.session["flash"] = "Поставка обновлена"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/supplies/{supply_id}/delete")
def supply_delete(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        request.session["flash"] = "Поставка не найдена"
        return RedirectResponse(url=_back(request), status_code=303)
    if supply.is_service:
        request.session["flash"] = (
            "❌ Это служебная поставка 1С (в ней текущие остатки каталога). "
            "Удалять её нельзя — она обновляется обменом с 1С."
        )
        return RedirectResponse(url=_back(request), status_code=303)

    try:
        item_ids = [item.id for item in supply.items]
        _detach_supply_items(db, item_ids)
        db.delete(supply)
        db.commit()
        log_action(db, request, _a, "supply_delete",
                   "supply", supply_id, f"Удалена поставка №{supply_id}")
        request.session["flash"] = f"Поставка №{supply_id} удалена"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления поставки №%d: %s", supply_id, e)
        request.session["flash"] = (
            f"❌ Не удалось удалить поставку №{supply_id}. "
            f"Попробуйте сначала снять её с полок."
        )
    return RedirectResponse(url="/admin", status_code=303)


# ───────────────────────── ИМПОРТ НАКЛАДНОЙ ─────────────────────────

@router.post("/supplies/{supply_id}/import/invoice")
async def supply_import_invoice(
    supply_id: int, request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")

    fn = (file.filename or "").lower()
    if not (fn.endswith(".xls") or fn.endswith(".xlsx")):
        request.session["flash"] = "Нужен файл .xls или .xlsx"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    content = bytearray()
    try:
        while True:
            chunk = await file.read(CHUNK)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_EXCEL_SIZE:
                raise ValueError("too big")
    except ValueError:
        request.session["flash"] = "Файл слишком большой (>10 МБ)"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)
    finally:
        await file.close()

    if not content:
        request.session["flash"] = "Файл пустой"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    # Разбор накладной (xls/xlsx) тяжёлый — выносим из event loop
    items, warnings = await run_in_threadpool(parse_invoice, bytes(content), fn)
    if not items:
        msg = "Не нашёл позиций в накладной."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    created_products = 0
    updated_items = 0
    new_items = 0
    skipped = []

    try:
        tax = taxonomy.Taxonomy(db)
        for it in items:
            product = db.query(Product).filter(Product.name == it["name"]).first()
            if not product:
                pack = it.get("package_size") or guess_package_size(it["name"])
                product = Product(
                    name=it["name"], description="", unit="упаковка",
                    package_size=pack, min_quantity=1, image_url="",
                )
                # страна — из накладной (или страна поставки); вид, рост и цвет сайт
                # угадывает по названию
                country = taxonomy.Ref(kind="country", name=it.get("country") or supply.country)
                row = {"refs": {"kind": None, "color": None, "height": None, "country": country}}
                apply_classification(tax, product, resolve_classification(tax, row), row)
                db.add(product)
                db.flush()
                created_products += 1

            pack = max(1, product.package_size or 1)
            packs = it["quantity"] // pack
            if packs <= 0:
                skipped.append(f"{it['name']}: {it['quantity']} шт < 1 упак ({pack} шт)")
                continue
            remainder = it["quantity"] - packs * pack
            if remainder:
                warnings.append(f"{it['name']}: {remainder} шт не вошли в целые упаковки")

            existing = db.query(SupplyItem).filter(
                SupplyItem.supply_id == supply_id,
                SupplyItem.product_id == product.id,
            ).first()
            if existing:
                existing.price = it["price"]
                existing.stock += packs
                updated_items += 1
            else:
                db.add(SupplyItem(
                    supply_id=supply_id, product_id=product.id,
                    price=it["price"], stock=packs,
                ))
                new_items += 1
        db.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.rollback()
        logger.exception("Ошибка импорта накладной: %s", e)
        request.session["flash"] = "Ошибка БД при импорте, откат."
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    for w in warnings:
        logger.warning("Импорт накладной: %s", w)
    for s in skipped:
        logger.warning("Импорт накладной (пропущено): %s", s)

    msg = (f"Импорт: +{new_items} позиций, ~{updated_items} обновлено. "
           f"Создано товаров: {created_products}.")
    if warnings:
        msg += f" ⚠️ Предупреждений: {len(warnings)} (см. лог)"
    if skipped:
        msg += f" Пропущено: {len(skipped)} (см. лог)."
    request.session["flash"] = msg
    return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)


# ───────────────────────── ПОЗИЦИИ ПОСТАВКИ ─────────────────────────

@router.post("/supplies/{supply_id}/items/add")
def supply_item_add(
    supply_id: int, request: Request,
    product_id: int = Form(...),
    quantity_stems: int = Form(0),
    price: float = Form(0),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        request.session["flash"] = "Товар не найден в справочнике"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    errors = []
    msg = validate_price(price)
    if msg:
        errors.append(msg)
    if quantity_stems <= 0:
        errors.append("Количество должно быть больше 0")
    if errors:
        _err(request, errors)
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    pack = max(1, product.package_size or 1)
    packs = quantity_stems // pack
    if packs <= 0:
        request.session["flash"] = (
            f"❌ {quantity_stems} шт < 1 упак ({pack} шт). Введите минимум {pack} шт."
        )
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    existing = db.query(SupplyItem).filter(
        SupplyItem.supply_id == supply_id,
        SupplyItem.product_id == product_id,
    ).first()
    if existing:
        existing.price = price
        existing.stock += packs
        request.session["flash"] = f"Позиция обновлена: +{packs} упак."
    else:
        db.add(SupplyItem(
            supply_id=supply_id, product_id=product_id,
            price=price, stock=packs,
        ))
        request.session["flash"] = f"Добавлено: {packs} упак."

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.exception("Ошибка добавления позиции: %s", e)
        request.session["flash"] = "Не удалось добавить позицию"
    return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)


@router.post("/supply_items/{item_id}/update")
def supply_item_update(
    item_id: int, request: Request,
    price: float = Form(...),
    quantity_stems: int = Form(...),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        raise HTTPException(404, "Позиция не найдена")

    errors = []
    for check, v in [(validate_price, price), (validate_stock, quantity_stems)]:
        msg = check(v)
        if msg:
            errors.append(msg)
    if errors:
        _err(request, errors)
        return RedirectResponse(url=f"/admin/supplies/{item.supply_id}/edit", status_code=303)

    pack = max(1, item.product.package_size or 1)
    packs = quantity_stems // pack
    if packs <= 0:
        request.session["flash"] = f"❌ {quantity_stems} шт < 1 упак ({pack} шт)."
        return RedirectResponse(url=f"/admin/supplies/{item.supply_id}/edit", status_code=303)

    item.price = price
    item.stock = packs
    db.commit()
    request.session["flash"] = f"Обновлено: {packs} упак."
    return RedirectResponse(url=f"/admin/supplies/{item.supply_id}/edit", status_code=303)


@router.post("/supply_items/{item_id}/delete")
def supply_item_delete(
    item_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        return RedirectResponse(url=_back(request), status_code=303)
    sid = item.supply_id
    try:
        _detach_supply_items(db, [item_id])
        db.delete(item)
        db.commit()
        request.session["flash"] = "Позиция удалена"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления позиции: %s", e)
        request.session["flash"] = "❌ Не удалось удалить позицию"
    return RedirectResponse(url=f"/admin/supplies/{sid}/edit", status_code=303)


# ───────────────────────── РАЗГРУЗКА ─────────────────────────

@router.post("/supplies/{supply_id}/unload")
def supply_unload(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    if supply.is_service:
        request.session["flash"] = (
            "❌ Служебную поставку 1С нельзя разгружать вручную: "
            "она обновляется обменом с 1С, а разгрузка сняла бы с полок "
            "остатки всего каталога."
        )
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)
    if supply.is_from_1c:
        request.session["flash"] = (
            "❌ Эту поставку ведёт 1С (инвойс), вручную её разгружать нельзя: "
            "товар встанет на полку сам, когда инвойс оприходуют в 1С, а клиентам с предзаказом "
            "придёт уведомление."
        )
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)
    if not supply.items:
        request.session["flash"] = "В поставке нет товаров"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    try:
        from ..scheduler import do_unload
        count = do_unload(db, supply)
        db.commit()
        log_action(db, request, _a, "supply_unload",
                   "supply", supply_id,
                   f"Поставка №{supply_id} разгружена, товаров: {count}")
        request.session["flash"] = (
            f"Поставка №{supply_id} разгружена. {count} товаров в каталоге."
        )
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка разгрузки поставки №%d: %s", supply_id, e)
        request.session["flash"] = "❌ Не удалось разгрузить поставку"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/supplies/{supply_id}/deactivate")
def supply_deactivate(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    if supply.is_service:
        request.session["flash"] = (
            "❌ Служебную поставку 1С нельзя снять с полок: "
            "её остатки управляются обменом с 1С."
        )
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)
    if supply.is_from_1c:
        request.session["flash"] = (
            "❌ Эту поставку ведёт 1С (инвойс): при следующем обмене товар снова встал бы на полку. "
            "Уберите остаток этой партии в 1С или пришлите инвойс со статусом cancelled."
        )
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)
    for item in supply.items:
        item.is_active = False
    db.commit()
    request.session["flash"] = f"Поставка №{supply_id} снята с полок"
    return RedirectResponse(url=_back(request), status_code=303)