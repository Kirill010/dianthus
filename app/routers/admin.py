"""Админка: заказы, клиенты (модерация), товары, поставки, Excel."""
import logging
from datetime import datetime

from fastapi import (APIRouter, Depends, File, Form, HTTPException,
                     Request, UploadFile)
from fastapi.responses import (HTMLResponse, RedirectResponse,
                                StreamingResponse)
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..models import (ORDER_STATUSES, SUPPLY_STATUSES,
                      Order, OrderItem, Product, Supply, User)
from ..security import check_csrf
from ..services.excel_service import (
    build_products_import_template,
    export_customers_to_excel,
    export_orders_to_excel,
    import_products_from_excel,
)
from ..services.upload_service import delete_upload, save_upload
from ..templating import render
from ..validators import (
    validate_country, validate_positive_int, validate_price, validate_stock,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")

XLSX_MIME = ("application/vnd.openxmlformats-officedocument"
             ".spreadsheetml.sheet")


def _xlsx_response(stream, filename: str) -> StreamingResponse:
    return StreamingResponse(
        stream, media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _parse_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


def _flash_error(request: Request, errors: list[str]) -> None:
    request.session["flash"] = "Ошибки: " + "; ".join(errors)


# ═══════ ДАШБОРД ════════════════════════════════════════════

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db),
                    admin=Depends(require_admin)):
    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    products = db.query(Product).order_by(Product.id).all()
    supplies = db.query(Supply).order_by(Supply.arrival_date.asc()).all()
    users = db.query(User).order_by(User.created_at.desc()).all()

    pending_count = sum(
        1 for u in users if not u.is_approved and not u.is_admin
    )

    return render(request, "admin.html", db,
                  user=admin,
                  orders=orders, products=products, supplies=supplies,
                  users=users, pending_count=pending_count,
                  statuses=ORDER_STATUSES, supply_statuses=SUPPLY_STATUSES)


# ═══════ МОДЕРАЦИЯ ══════════════════════════════════════════

@router.post("/users/{user_id}/approve")
async def user_approve(user_id: int, request: Request,
                       db: Session = Depends(get_db),
                       _admin=Depends(require_admin),
                       _csrf: None = Depends(check_csrf)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.is_admin:
        request.session["flash"] = "Этот пользователь уже админ"
        return RedirectResponse(url="/admin", status_code=303)
    if user.is_approved:
        request.session["flash"] = f"«{user.company_name}» уже одобрен"
        return RedirectResponse(url="/admin", status_code=303)
    user.is_approved = True
    db.commit()
    logger.info("Одобрен клиент: %s", user.email)
    request.session["flash"] = f"Клиент «{user.company_name}» одобрен"
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/users/{user_id}/reject")
async def user_reject(user_id: int, request: Request,
                      db: Session = Depends(get_db),
                      _admin=Depends(require_admin),
                      _csrf: None = Depends(check_csrf)):
    """
    Отклонить заявку / удалить клиента.

    Если у клиента есть заказы — удалять нельзя (FK orders.user_id),
    просто снимаем одобрение.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="Нельзя удалить админа")

    # Лёгкая проверка: SELECT id вместо полной строки
    has_orders = db.query(Order.id).filter(
        Order.user_id == user_id).first() is not None

    if has_orders:
        user.is_approved = False
        db.commit()
        request.session["flash"] = (
            f"У клиента «{user.company_name}» есть заказы. "
            f"Удалить нельзя — доступ закрыт, запись сохранена."
        )
        return RedirectResponse(url="/admin", status_code=303)

    company = user.company_name
    email = user.email
    db.delete(user)
    db.commit()
    logger.info("Удалён клиент: %s", email)
    request.session["flash"] = f"Заявка «{company}» отклонена"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ЗАКАЗЫ ═════════════════════════════════════════════

@router.post("/update_order_status")
async def update_order_status(request: Request,
                              order_id: int = Form(...),
                              status: str = Form(...),
                              db: Session = Depends(get_db),
                              _admin=Depends(require_admin),
                              _csrf: None = Depends(check_csrf)):
    if status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Неизвестный статус")
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        request.session["flash"] = f"Заказ №{order_id} не найден"
        return RedirectResponse(url="/admin", status_code=303)
    order.status = status
    db.commit()
    request.session["flash"] = f"Заказ №{order.id}: статус «{status}»"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ EXCEL ══════════════════════════════════════════════

@router.get("/orders/export")
async def orders_export(db: Session = Depends(get_db),
                        _admin=Depends(require_admin)):
    orders = db.query(Order).order_by(Order.created_at.desc()).all()
    return _xlsx_response(export_orders_to_excel(orders),
                          "dianthus_orders.xlsx")


@router.get("/customers/export")
async def customers_export(db: Session = Depends(get_db),
                           _admin=Depends(require_admin)):
    """
    Экспорт клиентов, сделавших хотя бы один заказ.
    Безопасный подзапрос через IN (работает и в SQLite, и в PG).
    """
    customer_ids = (
        db.query(Order.user_id)
        .filter(Order.user_id.isnot(None))
        .distinct()
        .subquery()
    )
    users = db.query(User).filter(User.id.in_(customer_ids)).all()

    customers = []
    for u in users:
        orders = u.orders
        if not orders:
            continue
        customers.append({
            "company_name": u.company_name,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "orders_count": len(orders),
            "total_sum": sum(o.total_price for o in orders),
            "last_order_date": max(o.created_at for o in orders)
                                 .strftime("%d.%m.%Y"),
        })

    customers.sort(key=lambda c: c["total_sum"], reverse=True)
    return _xlsx_response(export_customers_to_excel(customers),
                          "dianthus_customers.xlsx")


@router.get("/products/import/template")
async def products_import_template(_admin=Depends(require_admin)):
    return _xlsx_response(build_products_import_template(),
                          "dianthus_products_template.xlsx")


@router.post("/products/import")
async def products_import(request: Request,
                          file: UploadFile = File(...),
                          db: Session = Depends(get_db),
                          _admin=Depends(require_admin),
                          _csrf: None = Depends(check_csrf)):
    filename = (file.filename or "").lower()
    if not filename.endswith(".xlsx"):
        request.session["flash"] = "Нужен файл .xlsx"
        return RedirectResponse(url="/admin", status_code=303)

    contents = await file.read()
    if not contents:
        request.session["flash"] = "Файл пустой"
        return RedirectResponse(url="/admin", status_code=303)

    products, warnings = import_products_from_excel(contents)

    if not products:
        msg = "Импорт не дал результатов."
        if warnings:
            msg += " " + "; ".join(warnings[:3])
        request.session["flash"] = msg
        return RedirectResponse(url="/admin", status_code=303)

    for data in products:
        db.add(Product(**data))
    db.commit()

    msg = f"Импортировано товаров: {len(products)}"
    if warnings:
        msg += f". Предупреждений: {len(warnings)}"
    request.session["flash"] = msg
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ТОВАРЫ ═════════════════════════════════════════════

def _validate_product_form(name, price, stock, package_size,
                           min_quantity, length_cm, country) -> list[str]:
    errors: list[str] = []
    if not (name or "").strip():
        errors.append("Укажите название товара")
    elif len(name.strip()) > 200:
        errors.append("Название товара слишком длинное")
    for check, value in [
        (validate_price, price),
        (validate_stock, stock),
        (lambda v: validate_positive_int(v, "Размер упаковки"), package_size),
        (lambda v: validate_positive_int(v, "Минимальный заказ"), min_quantity),
    ]:
        msg = check(value)
        if msg:
            errors.append(msg)
    if length_cm is not None:
        if length_cm < 0:
            errors.append("Длина не может быть отрицательной")
        elif length_cm > 500:
            errors.append("Длина не может быть больше 500 см")
    if country and len(country.strip()) > 100:
        errors.append("Название страны слишком длинное")
    return errors


@router.get("/products/new", response_class=HTMLResponse)
async def product_new_page(request: Request, db: Session = Depends(get_db),
                           admin=Depends(require_admin)):
    supplies = db.query(Supply).order_by(Supply.arrival_date.asc()).all()
    return render(request, "product_form.html", db,
                  user=admin, product=None, supplies=supplies)


@router.post("/products/new")
async def product_new(
    request: Request,
    name: str = Form(""),
    price: float = Form(0),
    stock: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(1),
    min_quantity: int = Form(1),
    country: str = Form(""),
    length_cm: int = Form(0),
    description: str = Form(""),
    image_url: str = Form(""),
    supply_id: str = Form(""),
    image_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors = _validate_product_form(name, price, stock, package_size,
                                    min_quantity, length_cm, country)
    if errors:
        _flash_error(request, errors)
        return RedirectResponse(url="/admin/products/new", status_code=303)

    uploaded = save_upload(image_file)
    final_image = uploaded or image_url.strip()

    db.add(Product(
        name=name.strip(), description=description.strip(),
        price=price, stock=stock,
        unit=unit, package_size=package_size, min_quantity=min_quantity,
        country=country.strip(), length_cm=length_cm,
        image_url=final_image,
        supply_id=int(supply_id) if supply_id else None,
    ))
    db.commit()
    request.session["flash"] = "Товар добавлен"
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/products/{product_id}/edit", response_class=HTMLResponse)
async def product_edit_page(product_id: int, request: Request,
                            db: Session = Depends(get_db),
                            admin=Depends(require_admin)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")
    supplies = db.query(Supply).order_by(Supply.arrival_date.asc()).all()
    return render(request, "product_form.html", db,
                  user=admin, product=product, supplies=supplies)


@router.post("/products/{product_id}/edit")
async def product_edit(
    product_id: int,
    request: Request,
    name: str = Form(""),
    price: float = Form(0),
    stock: int = Form(0),
    unit: str = Form("упаковка"),
    package_size: int = Form(1),
    min_quantity: int = Form(1),
    country: str = Form(""),
    length_cm: int = Form(0),
    description: str = Form(""),
    image_url: str = Form(""),
    supply_id: str = Form(""),
    image_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    errors = _validate_product_form(name, price, stock, package_size,
                                    min_quantity, length_cm, country)
    if errors:
        _flash_error(request, errors)
        return RedirectResponse(
            url=f"/admin/products/{product_id}/edit", status_code=303)

    product.name = name.strip()
    product.description = description.strip()
    product.price = price
    product.stock = stock
    product.unit = unit
    product.package_size = package_size
    product.min_quantity = min_quantity
    product.country = country.strip()
    product.length_cm = length_cm
    product.supply_id = int(supply_id) if supply_id else None

    uploaded = save_upload(image_file)
    if uploaded:
        # Удаляем старую локальную картинку, чтобы диск не пух
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
                         _admin=Depends(require_admin),
                         _csrf: None = Depends(check_csrf)):
    """
    Удалить товар. Перед этим отвязываем его от order_items (FK = NULL),
    чтобы Postgres не блокировал DELETE. История заказов сохраняется.
    """
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        request.session["flash"] = "Товар не найден"
        return RedirectResponse(url="/admin", status_code=303)

    # Удаляем локальную картинку с диска
    if product.image_url and product.image_url.startswith("/static/uploads/"):
        delete_upload(product.image_url)

    db.query(OrderItem).filter(OrderItem.product_id == product_id).update(
        {OrderItem.product_id: None},
        synchronize_session=False,
    )

    name = product.name
    db.delete(product)
    db.commit()
    request.session["flash"] = f"Товар «{name}» удалён"
    return RedirectResponse(url="/admin", status_code=303)


# ═══════ ПОСТАВКИ ═══════════════════════════════════════════

@router.get("/supplies/new", response_class=HTMLResponse)
async def supply_new_page(request: Request, db: Session = Depends(get_db),
                          admin=Depends(require_admin)):
    return render(request, "supply_form.html", db,
                  user=admin, supply=None, supply_statuses=SUPPLY_STATUSES)


@router.post("/supplies/new")
async def supply_new(
    request: Request,
    country: str = Form(""),
    status: str = Form("Ожидается"),
    departure_date: str = Form(""),
    arrival_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    errors: list[str] = []
    msg = validate_country(country)
    if msg:
        errors.append(msg)
    if status not in SUPPLY_STATUSES:
        errors.append("Неизвестный статус поставки")
    if departure_date and not _parse_date(departure_date):
        errors.append("Некорректная дата отправки")
    if arrival_date and not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")

    if errors:
        _flash_error(request, errors)
        return RedirectResponse(url="/admin/supplies/new", status_code=303)

    db.add(Supply(
        country=country.strip(), status=status,
        departure_date=_parse_date(departure_date),
        arrival_date=_parse_date(arrival_date),
        notes=notes.strip(),
    ))
    db.commit()
    request.session["flash"] = "Поставка добавлена"
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/supplies/{supply_id}/edit", response_class=HTMLResponse)
async def supply_edit_page(supply_id: int, request: Request,
                           db: Session = Depends(get_db),
                           admin=Depends(require_admin)):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(status_code=404, detail="Поставка не найдена")
    return render(request, "supply_form.html", db,
                  user=admin, supply=supply, supply_statuses=SUPPLY_STATUSES)


@router.post("/supplies/{supply_id}/edit")
async def supply_edit(
    supply_id: int,
    request: Request,
    country: str = Form(""),
    status: str = Form("Ожидается"),
    departure_date: str = Form(""),
    arrival_date: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    _admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if not supply:
        raise HTTPException(status_code=404, detail="Поставка не найдена")

    errors: list[str] = []
    msg = validate_country(country)
    if msg:
        errors.append(msg)
    if status not in SUPPLY_STATUSES:
        errors.append("Неизвестный статус поставки")
    if departure_date and not _parse_date(departure_date):
        errors.append("Некорректная дата отправки")
    if arrival_date and not _parse_date(arrival_date):
        errors.append("Некорректная дата прибытия")

    if errors:
        _flash_error(request, errors)
        return RedirectResponse(
            url=f"/admin/supplies/{supply_id}/edit", status_code=303)

    supply.country = country.strip()
    supply.status = status
    supply.departure_date = _parse_date(departure_date)
    supply.arrival_date = _parse_date(arrival_date)
    supply.notes = notes.strip()
    db.commit()
    request.session["flash"] = "Поставка обновлена"
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/supplies/{supply_id}/delete")
async def supply_delete(supply_id: int, request: Request,
                        db: Session = Depends(get_db),
                        _admin=Depends(require_admin),
                        _csrf: None = Depends(check_csrf)):
    """Удалить поставку. Товары отвязываются (supply_id = NULL)."""
    supply = db.query(Supply).filter(Supply.id == supply_id).first()
    if supply:
        for p in supply.products:
            p.supply_id = None
        db.delete(supply)
        db.commit()
    request.session["flash"] = "Поставка удалена"
    return RedirectResponse(url="/admin", status_code=303)