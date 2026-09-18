"""Админка: заказы, клиенты, справочник, поставки, разгрузка, Excel."""
import logging
from datetime import datetime

from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import (HTMLResponse, RedirectResponse,
                                StreamingResponse)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from ..database import get_db
from ..deps import require_admin
from ..models import (ORDER_STATUSES, PRODUCT_CATEGORIES, SUPPLY_STATUSES,
                      Order, OrderItem, Product, Supply, SupplyItem, User)
from ..security import check_csrf
from ..services.excel_service import (
    build_products_import_template, export_customers_to_excel,
    export_orders_to_excel, import_products_from_excel,
    parse_invoice,
)
from ..services.upload_service import delete_upload, save_upload
from ..templating import render
from ..validators import (validate_country, validate_positive_int,
                          validate_price, validate_stock)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")
XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")
MAX_EXCEL_SIZE = 10 * 1024 * 1024
CHUNK = 64 * 1024


def _xlsx(stream, filename: str) -> StreamingResponse:
    return StreamingResponse(
        stream, media_type=XLSX_MIME,
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


# ═══════ ДАШБОРД ════════════════════════════════════════════

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db),
                    admin=Depends(require_admin)):
    orders = (db.query(Order)
              .options(selectinload(Order.user), selectinload(Order.items))
              .order_by(Order.created_at.desc()).all())
    products = db.query(Product).order_by(Product.name).all()
    supplies = (db.query(Supply)
                .options(selectinload(Supply.items))
                .order_by(Supply.arrival_date.asc()).all())
    users = db.query(User).order_by(User.created_at.desc()).all()
    pending_count = sum(1 for u in users
                        if not u.is_approved and not u.is_admin)
    active_items = (db.query(SupplyItem)
                    .options(selectinload(SupplyItem.product),
                             selectinload(SupplyItem.supply))
                    .filter(SupplyItem.is_active == True,  # noqa: E712
                            SupplyItem.stock > 0).all())
    return render(request, "admin.html", db,
                  user=admin, orders=orders, products=products,
                  supplies=supplies, users=users,
                  pending_count=pending_count,
                  active_items=active_items,
                  statuses=ORDER_STATUSES,
                  supply_statuses=SUPPLY_STATUSES,
                  categories=PRODUCT_CATEGORIES)


# ═══════ МОДЕРАЦИЯ ══════════════════════════════════════════

@router.post("/users/{user_id}/make_admin")
async def user_make_admin(user_id: int, request: Request,
                          db: Session = Depends(get_db),
                          current_admin=Depends(require_admin),
                          _csrf: None = Depends(check_csrf)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.is_admin:
        request.session["flash"] = f"«{user.full_name}» уже админ"
        return RedirectResponse(url="/admin", status_code=303)
    user.is_admin = True
    user.is_approved = True
    db.commit()
    logger.info("👑 %s повышен до админа (кем: %s)",
                user.email, current_admin.email)
    request.session["flash"] = (
        f"👑 «{user.full_name}» ({user.email}) теперь администратор"
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/demote")
async def user_demote(user_id: int, request: Request,
                      db: Session = Depends(get_db),
                      current_admin=Depends(require_admin),
                      _csrf: None = Depends(check_csrf)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(404, "Пользователь не найден")
    if user.id == current_admin.id:
        request.session["flash"] = "❌ Нельзя снять права с самого себя"
        return RedirectResponse(url="/admin", status_code=303)
    if not user.is_admin:
        request.session["flash"] = "Пользователь и так не админ"
        return RedirectResponse(url="/admin", status_code=303)
    admins_count = (db.query(User)
                    .filter(User.is_admin == True).count())  # noqa: E712
    if admins_count <= 1:
        request.session["flash"] = "❌ Нельзя снять последнего администратора"
        return RedirectResponse(url="/admin", status_code=303)
    user.is_admin = False
    db.commit()
    logger.info("👤 %s снят с админов (кем: %s)",
                user.email, current_admin.email)
    request.session["flash"] = f"«{user.full_name}» больше не администратор"
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/approve")
async def user_approve(user_id: int, request: Request,
                       db: Session = Depends(get_db),
                       current_admin=Depends(require_admin),
                       _csrf: None = Depends(check_csrf)):
    """Одобрить заявку на регистрацию клиента."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session["flash"] = "Пользователь не найден"
        return RedirectResponse(url="/admin", status_code=303)
    if user.is_approved:
        request.session["flash"] = f"«{user.full_name}» уже одобрен"
        return RedirectResponse(url="/admin", status_code=303)
    user.is_approved = True
    db.commit()
    logger.info("✅ Одобрен клиент: %s (кем: %s)",
                user.email, current_admin.email)
    request.session["flash"] = (
        f"✅ «{user.company_name}» ({user.full_name}) одобрен"
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/reject")
async def user_reject(user_id: int, request: Request,
                      db: Session = Depends(get_db),
                      current_admin=Depends(require_admin),
                      _csrf: None = Depends(check_csrf)):
    """Удалить пользователя. История его заказов СОХРАНЯЕТСЯ."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        request.session["flash"] = "Пользователь не найден"
        return RedirectResponse(url="/admin", status_code=303)
    if user.id == current_admin.id:
        request.session["flash"] = "❌ Нельзя удалить себя"
        return RedirectResponse(url="/admin", status_code=303)
    if user.is_admin:
        request.session["flash"] = "❌ Сначала снимите права администратора"
        return RedirectResponse(url="/admin", status_code=303)
    db.query(Order).filter(Order.user_id == user.id).update(
        {Order.user_id: None}, synchronize_session=False
    )
    name, email = user.full_name, user.email
    db.delete(user)
    db.commit()
    logger.info("🗑 Удалён пользователь: %s (кем: %s)",
                email, current_admin.email)
    request.session["flash"] = f"🗑 «{name}» удалён"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ЗАКАЗЫ ═════════════════════════════════════════════

@router.post("/update_order_status")
async def update_order_status(request: Request,
                              order_id: int = Form(...),
                              status: str = Form(...),
                              db: Session = Depends(get_db),
                              _a=Depends(require_admin),
                              _csrf: None = Depends(check_csrf)):
    if status not in ORDER_STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        request.session["flash"] = f"Заказ №{order_id} не найден"
        return RedirectResponse(url="/admin", status_code=303)
    order.status = status
    db.commit()
    request.session["flash"] = f"Заказ №{order.id}: «{status}»"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ EXCEL ══════════════════════════════════════════════

@router.get("/orders/export")
async def orders_export(db: Session = Depends(get_db),
                        _a=Depends(require_admin)):
    orders = (db.query(Order)
              .options(selectinload(Order.user), selectinload(Order.items))
              .order_by(Order.created_at.desc()).all())
    return _xlsx(export_orders_to_excel(orders), "dianthus_orders.xlsx")


@router.get("/customers/export")
async def customers_export(db: Session = Depends(get_db),
                           _a=Depends(require_admin)):
    stmt = (select(Order.user_id)
            .where(Order.user_id.isnot(None))
            .distinct())
    users = (db.query(User)
             .options(selectinload(User.orders))
             .filter(User.id.in_(stmt)).all())
    customers = []
    for u in users:
        orders = u.orders
        if not orders:
            continue
        customers.append({
            "company_name": u.company_name, "full_name": u.full_name,
            "email": u.email, "phone": u.phone,
            "orders_count": len(orders),
            "total_sum": sum(o.total_price for o in orders),
            "last_order_date": max(o.created_at for o in orders)
                                 .strftime("%d.%m.%Y"),
        })
    customers.sort(key=lambda c: c["total_sum"], reverse=True)
    return _xlsx(export_customers_to_excel(customers),
                 "dianthus_customers.xlsx")


@router.get("/products/import/template")
async def products_import_template(_a=Depends(require_admin)):
    return _xlsx(build_products_import_template(),
                 "dianthus_products_template.xlsx")


@router.post("/products/import")
async def products_import(request: Request, file: UploadFile = File(...),
                          db: Session = Depends(get_db),
                          _a=Depends(require_admin),
                          _csrf: None = Depends(check_csrf)):
    fn = (file.filename or "").lower()
    if not fn.endswith(".xlsx"):
        request.session["flash"] = "Нужен файл .xlsx"
        return RedirectResponse(url="/admin", status_code=303)

    content = bytearray()
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_EXCEL_SIZE:
            request.session["flash"] = "Файл слишком большой (>10 МБ)"
            return RedirectResponse(url="/admin", status_code=303)

    if not content:
        request.session["flash"] = "Файл пустой"
        return RedirectResponse(url="/admin", status_code=303)

    products, warnings = import_products_from_excel(bytes(content))
    if not products:
        msg = "Импорт не дал результатов."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url="/admin", status_code=303)

    try:
        for data in products:
            db.add(Product(**data))
        db.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.rollback()
        logger.exception("Ошибка импорта товаров: %s", e)
        request.session["flash"] = "Импорт не удался, БД откатана."
        return RedirectResponse(url="/admin", status_code=303)

    msg = f"Импортировано товаров: {len(products)}"
    if warnings:
        msg += f". Предупреждений: {len(warnings)}"
    request.session["flash"] = msg
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ СПРАВОЧНИК ТОВАРОВ ═════════════════════════════════

def _validate_product(name, length_cm, package_size, min_quantity) -> list[str]:
    errors = []
    if not (name or "").strip():
        errors.append("Укажите название")
    elif len(name.strip()) > 200:
        errors.append("Название слишком длинное")

    for check, val in [
        (lambda v: validate_positive_int(v, "Размер упаковки"), package_size),
        (lambda v: validate_positive_int(v, "Мин. заказ"), min_quantity),
    ]:
        msg = check(val)
        if msg:
            errors.append(msg)

    if length_cm is not None:
        if length_cm < 0:
            errors.append("Длина не может быть отрицательной")
        elif length_cm > 500:
            errors.append("Длина > 500 см")

    # ─── Проверка: минимум к заказу не должен быть больше 100 000 шт ───
    if package_size and min_quantity:
        total_min = package_size * min_quantity
        if total_min > 100_000:
            errors.append(
                f"Минимум к заказу получается {total_min:,} шт — "
                f"это слишком много. Проверьте «В упаковке» и «Мин. заказ»."
            )

    return errors


@router.get("/products/new", response_class=HTMLResponse)
async def product_new_page(request: Request, db: Session = Depends(get_db),
                           admin=Depends(require_admin)):
    return render(request, "product_form.html", db,
                  user=admin, product=None, categories=PRODUCT_CATEGORIES)


@router.post("/products/new")
async def product_new(
    request: Request,
    name: str = Form(""), description: str = Form(""),
    country: str = Form(""), length_cm: int = Form(0),
    unit: str = Form("упаковка"), package_size: int = Form(25),
    min_quantity: int = Form(1), category: str = Form("Прочее"),
    image_url: str = Form(""),
    image_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors = _validate_product(name, length_cm, package_size, min_quantity)
    if errors:
        _err(request, errors)
        return RedirectResponse(url="/admin/products/new", status_code=303)

    uploaded = save_upload(image_file)
    final_image = uploaded or image_url.strip()

    db.add(Product(
        name=name.strip(), description=description.strip(),
        country=country.strip(), length_cm=length_cm,
        unit=unit, package_size=package_size, min_quantity=min_quantity,
        category=category, image_url=final_image,
    ))
    db.commit()
    request.session["flash"] = "Товар добавлен в справочник"
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_page(product_id: int, request: Request,
                            db: Session = Depends(get_db),
                            admin=Depends(require_admin)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Товар не найден")
    return render(request, "product_form.html", db,
                  user=admin, product=product, categories=PRODUCT_CATEGORIES)


@router.post("/products/{product_id}/edit")
async def product_edit(
    product_id: int, request: Request,
    name: str = Form(""), description: str = Form(""),
    country: str = Form(""), length_cm: int = Form(0),
    unit: str = Form("упаковка"), package_size: int = Form(25),
    min_quantity: int = Form(1), category: str = Form("Прочее"),
    image_url: str = Form(""),
    image_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(404, "Товар не найден")

    errors = _validate_product(name, length_cm, package_size, min_quantity)
    if errors:
        _err(request, errors)
        return RedirectResponse(
            url=f"/admin/products/{product_id}/edit", status_code=303)

    product.name = name.strip()
    product.description = description.strip()
    product.country = country.strip()
    product.length_cm = length_cm
    product.unit = unit
    product.package_size = package_size
    product.min_quantity = min_quantity
    product.category = category

    uploaded = save_upload(image_file)
    if uploaded:
        if product.image_url and product.image_url.startswith("/static/uploads/"):
            delete_upload(product.image_url)
        product.image_url = uploaded
    elif image_url.strip():
        product.image_url = image_url.strip()

    db.commit()
    request.session["flash"] = "Товар обновлён"
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/products/{product_id}/delete")
async def product_delete(product_id: int, request: Request,
                         db: Session = Depends(get_db),
                         _a=Depends(require_admin),
                         _csrf: None = Depends(check_csrf)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        request.session["flash"] = "Товар не найден"
        return RedirectResponse(url="/admin", status_code=303)

    supply_item_ids = [si.id for si in product.supply_items]

    if supply_item_ids:
        db.query(OrderItem).filter(
            OrderItem.supply_item_id.in_(supply_item_ids)
        ).update({OrderItem.supply_item_id: None},
                 synchronize_session=False)
    db.query(OrderItem).filter(
        OrderItem.product_id == product_id
    ).update({OrderItem.product_id: None}, synchronize_session=False)

    if product.image_url and product.image_url.startswith("/static/uploads/"):
        delete_upload(product.image_url)

    name = product.name
    db.delete(product)
    db.commit()
    request.session["flash"] = f"«{name}» удалён из справочника"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ПОСТАВКИ ═══════════════════════════════════════════

@router.get("/supplies/new", response_class=HTMLResponse)
async def supply_new_page(request: Request, db: Session = Depends(get_db),
                          admin=Depends(require_admin)):
    return render(request, "supply_form.html", db,
                  user=admin, supply=None, items=[],
                  supply_statuses=SUPPLY_STATUSES,
                  all_products=db.query(Product).order_by(Product.name).all())


@router.post("/supplies/new")
async def supply_new(
    request: Request, country: str = Form(""),
    status: str = Form("Ожидается"),
    arrival_date: str = Form(""), notes: str = Form(""),
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
        errors.append("Укажите дату прибытия — её увидят клиенты")
    elif not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")
    if errors:
        _err(request, errors)
        return RedirectResponse(url="/admin/supplies/new", status_code=303)

    supply = Supply(country=country.strip(), status=status,
                    departure_date=None,
                    arrival_date=_parse_date(arrival_date),
                    notes=notes.strip())
    db.add(supply)
    db.commit()
    db.refresh(supply)
    request.session["flash"] = f"Поставка №{supply.id} создана. Добавьте товары."
    return RedirectResponse(url=f"/admin/supplies/{supply.id}/edit",
                            status_code=303)


@router.get("/supplies/{supply_id}/edit", response_class=HTMLResponse)
async def supply_edit_page(supply_id: int, request: Request,
                           db: Session = Depends(get_db),
                           admin=Depends(require_admin)):
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
    country: str = Form(""), status: str = Form("Ожидается"),
    arrival_date: str = Form(""), notes: str = Form(""),
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
        errors.append("Укажите дату прибытия — её увидят клиенты")
    elif not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")
    if errors:
        _err(request, errors)
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    supply.country = country.strip()
    supply.status = status
    supply.arrival_date = _parse_date(arrival_date)
    supply.notes = notes.strip()
    db.commit()
    request.session["flash"] = "Поставка обновлена"
    return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                            status_code=303)


@router.post("/supplies/{supply_id}/delete")
async def supply_delete(supply_id: int, request: Request,
                        db: Session = Depends(get_db),
                        _a=Depends(require_admin),
                        _csrf: None = Depends(check_csrf)):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if supply:
        item_ids = [item.id for item in supply.items]
        if item_ids:
            db.query(OrderItem).filter(
                OrderItem.supply_item_id.in_(item_ids)
            ).update({OrderItem.supply_item_id: None},
                     synchronize_session=False)
        db.delete(supply)
        db.commit()
    request.session["flash"] = "Поставка удалена"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ИМПОРТ НАКЛАДНОЙ ═══════════════════════════════════

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
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                                status_code=303)

    content = bytearray()
    while True:
        chunk = await file.read(CHUNK)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_EXCEL_SIZE:
            request.session["flash"] = "Файл слишком большой (>10 МБ)"
            return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                                    status_code=303)

    if not content:
        request.session["flash"] = "Файл пустой"
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                                status_code=303)

    items, warnings = parse_invoice(bytes(content), fn)
    if not items:
        msg = "Не нашёл позиций в накладной."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                                status_code=303)

    created_products = 0
    updated_items = 0
    new_items = 0

    try:
        for it in items:
            product = (db.query(Product)
                       .filter(Product.name == it["name"])
                       .first())
            if not product:
                product = Product(
                    name=it["name"],
                    description="",
                    country=it.get("country") or supply.country,
                    length_cm=it.get("length_cm", 0),
                    unit=it.get("unit", "шт"),
                    package_size=1,
                    min_quantity=1,
                    image_url="",
                    category=it.get("category", "Прочее"),
                )
                db.add(product)
                db.flush()
                created_products += 1

            existing = (db.query(SupplyItem)
                        .filter(SupplyItem.supply_id == supply_id,
                                SupplyItem.product_id == product.id)
                        .first())
            if existing:
                existing.price = it["price"]
                existing.stock += it["quantity"]
                updated_items += 1
            else:
                db.add(SupplyItem(
                    supply_id=supply_id,
                    product_id=product.id,
                    price=it["price"],
                    stock=it["quantity"],
                ))
                new_items += 1

        db.commit()
    except (IntegrityError, SQLAlchemyError) as e:
        db.rollback()
        logger.exception("Ошибка импорта накладной: %s", e)
        request.session["flash"] = "Ошибка БД при импорте накладной, откат."
        return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                                status_code=303)

    msg = (f"📄 Импорт накладной: +{new_items} позиций, "
           f"~{updated_items} обновлено. "
           f"Создано товаров в справочнике: {created_products}.")
    if warnings:
        msg += f" ⚠️ Предупреждений: {len(warnings)} (см. лог)"
        for w in warnings[:5]:
            logger.warning("Накладная: %s", w)

    logger.info("Импорт накладной в поставку №%d: %d позиций, "
                "+%d товаров", supply_id, len(items), created_products)
    request.session["flash"] = msg
    return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                            status_code=303)


# ═══════ ПОЗИЦИИ ПОСТАВКИ ═══════════════════════════════════

@router.post("/supplies/{supply_id}/items/add")
async def supply_item_add(
    supply_id: int, request: Request,
    product_id: int = Form(...), price: float = Form(0),
    stock: int = Form(0),
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
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    errors = []
    for check, v in [(validate_price, price), (validate_stock, stock)]:
        msg = check(v)
        if msg:
            errors.append(msg)
    if errors:
        _err(request, errors)
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    existing = (db.query(SupplyItem)
                .filter(SupplyItem.supply_id == supply_id,
                        SupplyItem.product_id == product_id).first())
    if existing:
        existing.price = price
        existing.stock += stock
        request.session["flash"] = "Позиция обновлена"
    else:
        db.add(SupplyItem(supply_id=supply_id, product_id=product_id,
                          price=price, stock=stock))
        request.session["flash"] = "Товар добавлен в поставку"

    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.exception("Ошибка добавления позиции: %s", e)
        request.session["flash"] = "Не удалось добавить позицию"
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    return RedirectResponse(url=f"/admin/supplies/{supply_id}/edit",
                            status_code=303)


@router.post("/supply_items/{item_id}/update")
async def supply_item_update(
    item_id: int, request: Request,
    price: float = Form(...), stock: int = Form(...),
    db: Session = Depends(get_db),
    _a=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if not item:
        raise HTTPException(404, "Позиция не найдена")
    errors = []
    for check, v in [(validate_price, price), (validate_stock, stock)]:
        msg = check(v)
        if msg:
            errors.append(msg)
    if errors:
        _err(request, errors)
        return RedirectResponse(
            url=f"/admin/supplies/{item.supply_id}/edit", status_code=303)
    item.price = price
    item.stock = stock
    db.commit()
    request.session["flash"] = "Позиция обновлена"
    return RedirectResponse(url=f"/admin/supplies/{item.supply_id}/edit",
                            status_code=303)


@router.post("/supply_items/{item_id}/delete")
async def supply_item_delete(item_id: int, request: Request,
                             db: Session = Depends(get_db),
                             _a=Depends(require_admin),
                             _csrf: None = Depends(check_csrf)):
    item = db.query(SupplyItem).filter(SupplyItem.id == item_id).first()
    if item:
        sid = item.supply_id
        db.query(OrderItem).filter(
            OrderItem.supply_item_id == item_id
        ).update({OrderItem.supply_item_id: None},
                 synchronize_session=False)
        db.delete(item)
        db.commit()
        request.session["flash"] = "Позиция удалена"
        return RedirectResponse(url=f"/admin/supplies/{sid}/edit",
                                status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ РАЗГРУЗКА ПОСТАВКИ ═════════════════════════════════

@router.post("/supplies/{supply_id}/unload")
async def supply_unload(supply_id: int, request: Request,
                        db: Session = Depends(get_db),
                        _a=Depends(require_admin),
                        _csrf: None = Depends(check_csrf)):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    if not supply.items:
        request.session["flash"] = "В поставке нет товаров"
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    product_ids = [i.product_id for i in supply.items]
    others = (db.query(SupplyItem)
              .filter(SupplyItem.product_id.in_(product_ids),
                      SupplyItem.supply_id != supply_id,
                      SupplyItem.is_active == True).all())  # noqa: E712
    for o in others:
        o.is_active = False
        o.stock = 0

    for item in supply.items:
        item.is_active = True

    supply.status = "Разгружен"
    db.commit()
    logger.info("Разгружена поставка №%d (%d товаров)",
                supply_id, len(supply.items))
    request.session["flash"] = (
        f"Поставка №{supply_id} разгружена. "
        f"{len(supply.items)} товаров в каталоге."
    )
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/supplies/{supply_id}/deactivate")
async def supply_deactivate(supply_id: int, request: Request,
                            db: Session = Depends(get_db),
                            _a=Depends(require_admin),
                            _csrf: None = Depends(check_csrf)):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(404, "Поставка не найдена")
    for item in supply.items:
        item.is_active = False
    db.commit()
    request.session["flash"] = f"Поставка №{supply_id} снята с полок"
    return RedirectResponse(url="/admin", status_code=303)