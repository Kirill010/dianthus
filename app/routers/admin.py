# app/routers/admin.py
"""Админ-панель: заказы, клиенты, справочник, поставки."""
import logging
from datetime import datetime, timedelta
from typing import List

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select, or_, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload, joinedload

from ..database import get_db
from ..deps import require_admin
from ..models import (ORDER_STATUSES, PRODUCT_CATEGORIES, SUPPLY_STATUSES,
                      Notification, Order, OrderItem, Preorder, Product,
                      Supply, SupplyItem, User)
from ..security import check_csrf
from ..services.excel_service import (build_products_import_template,
                                      export_customers_to_excel,
                                      export_orders_to_excel,
                                      guess_package_size,
                                      import_products_from_excel,
                                      parse_invoice)
from ..services.notifier import notify_client_status_changed
from ..services.upload_service import delete_upload, save_uploads
from ..templating import render
from ..validators import (validate_country, validate_positive_int,
                          validate_price, validate_stock)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_EXCEL_SIZE = 10 * 1024 * 1024
CHUNK = 64 * 1024
ORDERS_PER_PAGE = 20


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


def _err(request: Request, errors: list[str]) -> None:
    request.session["flash"] = "Ошибки: " + "; ".join(errors)


def _back(request: Request, default: str = "/admin") -> str:
    ref = request.headers.get("referer") or ""
    if ref and ref.startswith(str(request.base_url)):
        return ref
    return default


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


# ───────────────────────── ДАШБОРД (С N+1 FIX) ─────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
    q: str = "",
    status: str = "",
    page: int = 1,
):
    q, status, page = (q or "").strip(), (status or "").strip(), max(1, page)

    orders_query = db.query(Order).options(
        selectinload(Order.user),
        selectinload(Order.items),
    )
    if q:
        conditions = []
        if q.isdigit() and len(q) < 15:
            conditions.append(Order.id == int(q))
        conditions.extend([
            User.company_name.ilike(f"%{q}%"),
            User.email.ilike(f"%{q}%"),
            User.full_name.ilike(f"%{q}%"),
        ])
        orders_query = orders_query.outerjoin(
            User, Order.user_id == User.id
        ).filter(or_(*conditions))
    if status:
        orders_query = orders_query.filter(Order.status == status)

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

    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = today - timedelta(days=7)
    stats = {
        "orders_today": db.query(Order).filter(Order.created_at >= today).count(),
        "orders_week": db.query(Order).filter(Order.created_at >= week_ago).count(),
        "revenue_today": db.query(func.coalesce(func.sum(Order.total_price), 0))
            .filter(Order.created_at >= today, Order.status != "Отменён")
            .scalar() or 0,
        "revenue_week": db.query(func.coalesce(func.sum(Order.total_price), 0))
            .filter(Order.created_at >= week_ago, Order.status != "Отменён")
            .scalar() or 0,
        "pending_orders": db.query(Order).filter(Order.status == "Новый").count(),
        "new_clients_week": db.query(User).filter(User.created_at >= week_ago).count(),
    }
    products = db.query(Product).order_by(Product.name).all()

    supplies = (
        db.query(Supply)
        .options(
            selectinload(Supply.items).selectinload(SupplyItem.product)
        )
        .filter(Supply.is_service.is_(False))
        .order_by(Supply.arrival_date.asc())
        .all()
    )
    users = db.query(User).order_by(User.created_at.desc()).all()
    pending_count = sum(1 for u in users if not u.is_approved and not u.is_admin)

    active_items = (
        db.query(SupplyItem)
        .options(
            joinedload(SupplyItem.product),
            joinedload(SupplyItem.supply),
        )
        .filter(SupplyItem.is_active.is_(True), SupplyItem.stock > 0)
        .all()
    )

    return render(
        request, "admin.html", db,
        user=admin, orders=orders, total_orders=total_orders,
        orders_page=page, orders_total_pages=orders_total_pages,
        orders_q=q, orders_status=status, products=products,
        supplies=supplies, users=users, pending_count=pending_count,
        active_items=active_items, statuses=ORDER_STATUSES,
        supply_statuses=SUPPLY_STATUSES, categories=PRODUCT_CATEGORIES,
        stats=stats,
    )

# ───────────────────────── МОДЕРАЦИЯ КЛИЕНТОВ ─────────────────────────

@router.post("/users/{user_id}/make_admin")
async def user_make_admin(
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
    request.session["flash"] = f"👑 «{user.full_name}» теперь администратор"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/demote")
async def user_demote(
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
    request.session["flash"] = f"«{user.full_name}» больше не администратор"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/approve")
async def user_approve(
    user_id: int, request: Request,
    db: Session = Depends(get_db),
    current_admin=Depends(require_admin),
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
    request.session["flash"] = f"✅ «{user.company_name}» одобрен"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/reject")
async def user_reject(
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
        order_ids = [r[0] for r in db.query(Order.id).filter(Order.user_id == user.id).all()]
        if order_ids:
            db.query(OrderItem).filter(
                OrderItem.order_id.in_(order_ids)
            ).update(
                {OrderItem.product_id: None, OrderItem.supply_item_id: None},
                synchronize_session=False,
            )
            db.query(Order).filter(Order.id.in_(order_ids)).update(
                {Order.user_id: None}, synchronize_session=False
            )
        db.query(Preorder).filter(Preorder.user_id == user.id).delete(
            synchronize_session=False
        )
        name = user.full_name
        db.delete(user)
        db.commit()
        request.session["flash"] = f"🗑 «{name}» удалён"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления пользователя: %s", e)
        request.session["flash"] = "❌ Не удалось удалить пользователя"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/users/{user_id}/discount")
async def user_set_discount(
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
    user.discount_percent = float(discount_percent)
    db.commit()
    request.session["flash"] = f"💸 «{user.company_name}»: скидка {discount_percent:.1f}%"
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ЗАКАЗЫ ─────────────────────────

@router.post("/update_order_status")
async def update_order_status(
    request: Request,
    background_tasks: BackgroundTasks,
    order_id: int = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    if status not in ORDER_STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        request.session["flash"] = f"Заказ №{order_id} не найден"
        return RedirectResponse(url=_back(request), status_code=303)

    order.status = status
    db.commit()

    if order.user_id:
        status_text = {
            "Подтверждён": "Ваш заказ подтверждён",
            "В работе": "Заказ собирается на складе",
            "Отправлен": "Заказ отправлен в доставку",
            "Выполнен": "Заказ выполнен. Спасибо за покупку!",
            "Отменён": "Заказ отменён. Свяжитесь с менеджером.",
        }.get(status, f"Статус заказа изменён на «{status}»")
        db.add(Notification(
            user_id=order.user_id,
            order_id=order.id,
            text=f"Заказ №{order.id}: {status_text}",
        ))
        db.commit()

        # fix #38: email в фоне, не блокируем event loop
        order_loaded = (
            db.query(Order)
            .options(selectinload(Order.user), selectinload(Order.items))
            .filter(Order.id == order.id)
            .first()
        )
        if order_loaded:
            background_tasks.add_task(
                notify_client_status_changed, order_loaded, status,
            )

    request.session["flash"] = f"Заказ №{order.id}: «{status}»"
    return RedirectResponse(url=_back(request), status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    """Карточка заказа для админа."""
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

    return render(
        request, "admin_order_detail.html", db,
        user=admin,
        order=order,
        statuses=ORDER_STATUSES,
    )

# ───────────────────────── EXCEL ─────────────────────────

@router.get("/orders/export")
async def orders_export(db: Session = Depends(get_db), _a=Depends(require_admin)):
    orders = (
        db.query(Order)
        .options(selectinload(Order.user), selectinload(Order.items))
        .order_by(Order.created_at.desc())
        .all()
    )
    return _xlsx(export_orders_to_excel(orders), "dianthus_orders.xlsx")


@router.get("/customers/export")
async def customers_export(db: Session = Depends(get_db), _a=Depends(require_admin)):
    stmt = select(Order.user_id).where(Order.user_id.isnot(None)).distinct()
    users = db.query(User).options(selectinload(User.orders)).filter(User.id.in_(stmt)).all()
    customers = []
    for u in users:
        if not u.orders:
            continue
        customers.append({
            "company_name": u.company_name,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "discount_percent": u.discount_percent or 0,
            "orders_count": len(u.orders),
            "total_sum": sum(o.total_price for o in u.orders),
            "last_order_date": max(o.created_at for o in u.orders).strftime("%d.%m.%Y"),
        })
    customers.sort(key=lambda c: c["total_sum"], reverse=True)
    return _xlsx(export_customers_to_excel(customers), "dianthus_customers.xlsx")


@router.get("/products/import/template")
async def products_import_template(_a=Depends(require_admin)):
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

    products, warnings = import_products_from_excel(bytes(content))
    if not products:
        msg = "Импорт не дал результатов."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url=_back(request), status_code=303)

    try:
        for data in products:
            db.add(Product(**data))
        db.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.rollback()
        logger.exception("Ошибка импорта товаров: %s", e)
        request.session["flash"] = "Импорт не удался, БД откатана."
        return RedirectResponse(url=_back(request), status_code=303)

    msg = f"Импортировано товаров: {len(products)}"
    if warnings:
        msg += f". Предупреждений: {len(warnings)}"
    request.session["flash"] = msg
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── СПРАВОЧНИК ТОВАРОВ ─────────────────────────

@router.get("/products/new", response_class=HTMLResponse)
async def product_new_page(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return render(request, "product_form.html", db,
                  user=admin, product=None, categories=PRODUCT_CATEGORIES)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_page(
    product_id: int, request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Товар не найден")
    return render(request, "product_form.html", db,
                  user=admin, product=product, categories=PRODUCT_CATEGORIES)


@router.post("/products/new")
async def product_new(
    request: Request,
    name: str = Form(""),
    description: str = Form(""),
    country: str = Form(""),
    length_cm: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(25),
    min_quantity: int = Form(1),
    category: str = Form("Прочее"),
    image_url: str = Form(""),
    image_files: List[UploadFile] = File(None),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors = []
    if not (name or "").strip():
        errors.append("Укажите название")
    if package_size < 1:
        errors.append("Размер упаковки должен быть >= 1")
    if min_quantity < 1:
        errors.append("Мин. заказ должен быть >= 1")
    if errors:
        _err(request, errors)
        return RedirectResponse(url="/admin/products/new", status_code=303)

    photos = []
    if image_files:
        photos.extend(save_uploads(image_files))
    if image_url.strip():
        photos.append(image_url.strip())

    db.add(Product(
        name=name.strip(), description=description.strip(),
        country=country.strip(), length_cm=length_cm, unit=unit,
        package_size=package_size, min_quantity=min_quantity,
        category=category, image_url=photos[0] if photos else "",
        photos=photos,
    ))
    db.commit()
    request.session["flash"] = f"Товар добавлен ({len(photos)} фото)"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/products/{product_id}/edit")
async def product_edit(
    product_id: int, request: Request,
    name: str = Form(""),
    description: str = Form(""),
    country: str = Form(""),
    length_cm: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(25),
    min_quantity: int = Form(1),
    category: str = Form("Прочее"),
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

    if not (name or "").strip():
        _err(request, ["Укажите название"])
        return RedirectResponse(url=f"/admin/products/{product_id}/edit", status_code=303)

    product.name = name.strip()
    product.description = description.strip()
    product.country = country.strip()
    product.length_cm = length_cm
    product.unit = unit
    product.package_size = package_size
    product.min_quantity = min_quantity
    product.category = category

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
    request.session["flash"] = f"Товар обновлён ({len(final)} фото)"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/products/{product_id}/delete")
async def product_delete(
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
        request.session["flash"] = f"«{name}» удалён из справочника"
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка удаления товара: %s", e)
        request.session["flash"] = "❌ Не удалось удалить товар"
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ПОСТАВКИ ─────────────────────────

@router.get("/supplies/new", response_class=HTMLResponse)
async def supply_new_page(
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
):
    return render(request, "supply_form.html", db,
                  user=admin, supply=None, items=[],
                  supply_statuses=SUPPLY_STATUSES,
                  all_products=db.query(Product).order_by(Product.name).all())


@router.post("/supplies/new")
async def supply_new(
    request: Request,
    country: str = Form(""),
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
        country=country.strip(), status=status,
        departure_date=None, arrival_date=_parse_date(arrival_date),
        notes=notes.strip(),
    )
    db.add(supply)
    db.commit()
    db.refresh(supply)
    request.session["flash"] = f"Поставка №{supply.id} создана. Добавьте товары."
    return RedirectResponse(url=f"/admin/supplies/{supply.id}/edit", status_code=303)


@router.get("/supplies/{supply_id}/edit", response_class=HTMLResponse)
async def supply_edit_page(
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
async def supply_edit(
    supply_id: int, request: Request,
    country: str = Form(""),
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
    supply.status = status
    supply.arrival_date = _parse_date(arrival_date)
    supply.notes = notes.strip()
    db.commit()
    request.session["flash"] = "Поставка обновлена"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/supplies/{supply_id}/delete")
async def supply_delete(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        request.session["flash"] = "Поставка не найдена"
        return RedirectResponse(url=_back(request), status_code=303)

    try:
        item_ids = [item.id for item in supply.items]
        _detach_supply_items(db, item_ids)
        db.delete(supply)
        db.commit()
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

    items, warnings = parse_invoice(bytes(content), fn)
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
        for it in items:
            product = db.query(Product).filter(Product.name == it["name"]).first()
            if not product:
                pack = it.get("package_size") or guess_package_size(it["name"])
                product = Product(
                    name=it["name"], description="",
                    country=it.get("country") or supply.country,
                    length_cm=it.get("length_cm", 0), unit="упаковка",
                    package_size=pack, min_quantity=1,
                    image_url="", category=it.get("category", "Прочее"),
                )
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
async def supply_item_add(
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
async def supply_item_update(
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
async def supply_item_delete(
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
async def supply_unload(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    if not supply.items:
        request.session["flash"] = "В поставке нет товаров"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    try:
        from ..scheduler import do_unload
        count = do_unload(db, supply)
        db.commit()
        request.session["flash"] = (
            f"Поставка №{supply_id} разгружена. {count} товаров в каталоге."
        )
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка разгрузки поставки №%d: %s", supply_id, e)
        request.session["flash"] = "❌ Не удалось разгрузить поставку"
    return RedirectResponse(url=_back(request), status_code=303)


@router.post("/supplies/{supply_id}/deactivate")
async def supply_deactivate(
    supply_id: int, request: Request,
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    for item in supply.items:
        item.is_active = False
    db.commit()
    request.session["flash"] = f"Поставка №{supply_id} снята с полок"
    return RedirectResponse(url=_back(request), status_code=303)