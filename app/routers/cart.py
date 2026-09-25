# app/routers/cart.py
"""Корзина и оформление заказа с атомарной блокировкой стока."""
import logging

from fastapi import (APIRouter, BackgroundTasks, Depends, Form,
                     HTTPException, Request)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import Order, OrderItem, Product, SupplyItem, Supply
from ..security import check_csrf
from ..services.notifier import notify_admin_new_order
from ..services.preorder_service import (
    add_preorder_to_db, get_user_preorders, remove_preorder_db,
    get_all_user_preorders, move_fulfilled_preorder_to_cart,
    remove_fulfilled_preorder_db,
)
from ..templating import render
from .. import models

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_COMMENT_LENGTH = 1000


def _cart_subtotal(cart: list[dict]) -> float:
    total = 0.0
    for it in cart:
        pack = it.get("package_size") or 1
        total += it["price"] * pack * it["quantity"]
    return total


def _apply_discount(subtotal: float, discount_percent: float):
    pct = max(0.0, min(100.0, float(discount_percent or 0)))
    if pct <= 0:
        return 0.0, subtotal
    discount = subtotal * pct / 100.0
    return discount, subtotal - discount


def _clean_cart(cart: list[dict]) -> list[dict]:
    result = []
    for c in cart:
        if not isinstance(c, dict):
            continue
        if not c.get("supply_item_id"):
            continue
        if not c.get("quantity") or c["quantity"] <= 0:
            continue
        if c.get("price") is None:
            continue
        result.append(c)
    return result


def _merge_into_cart(cart: list[dict], new_item: dict) -> None:
    si_id = new_item["supply_item_id"]
    new_from_preorder = bool(new_item.get("from_preorder"))

    for ci in cart:
        if ci.get("supply_item_id") != si_id:
            continue
        ci_from_preorder = bool(ci.get("from_preorder"))
        if ci_from_preorder != new_from_preorder:
            continue
        ci["quantity"] += new_item["quantity"]
        ci["package_size"] = new_item.get("package_size") or 1
        ci["price"] = new_item.get("price") or ci["price"]
        return

    entry = dict(new_item)
    if new_from_preorder:
        entry["from_preorder"] = True
    cart.append(entry)


def _back(request: Request, default: str = "/catalog") -> str:
    return request.headers.get("referer") or default


# ───────────────────────── ДОБАВЛЕНИЕ В КОРЗИНУ ─────────────────────────

@router.post("/add_to_cart")
async def add_to_cart(
    request: Request,
    supply_item_id: int = Form(...),
    quantity_stems: int = Form(0),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(
            SupplyItem.id == supply_item_id,
            SupplyItem.is_active.is_(True),
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)
    min_stems = pack * min_packs
    max_stems = item.available_stock * pack

    if item.available_stock <= 0:
        request.session["flash"] = f"«{product.name}» закончился"
        return RedirectResponse(
            url=f"/product/{supply_item_id}", status_code=303
        )

    if quantity_stems <= 0:
        quantity_stems = min_stems
    if quantity_stems < min_stems:
        quantity_stems = min_stems
    if quantity_stems > max_stems:
        quantity_stems = max_stems
    quantity_stems = (quantity_stems // pack) * pack
    if quantity_stems < min_stems:
        quantity_stems = min_stems

    quantity_packs = quantity_stems // pack

    cart = request.session.get("cart", [])
    merged = False
    for ci in cart:
        if (ci.get("supply_item_id") == supply_item_id
                and not ci.get("from_preorder")):
            new_packs = ci["quantity"] + quantity_packs
            if new_packs > item.available_stock:
                new_packs = item.available_stock
                request.session["flash"] = (
                    f"Максимум {item.available_stock} упак. "
                    f"({item.available_stock * pack} шт)"
                )
            ci["quantity"] = new_packs
            ci["package_size"] = pack
            merged = True
            break

    if not merged:
        cart.append({
            "supply_item_id": item.id,
            "product_id": product.id,
            "name": product.name,
            "unit": product.unit or "упаковка",
            "package_size": pack,
            "price": item.price,
            "image_url": product.image_url or "",
            "quantity": quantity_packs,
        })
        request.session["flash"] = (
            f"«{product.name}» — {quantity_packs} упак. "
            f"({quantity_stems} шт × {item.price:.2f} ₽)"
        )

    request.session["cart"] = cart
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ДОБАВЛЕНИЕ В ПРЕДЗАКАЗ ─────────────────────────

@router.post("/add_to_preorder")
async def add_to_preorder_route(
    request: Request,
    supply_item_id: int = Form(...),
    quantity_stems: int = Form(0),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = (
        db.query(SupplyItem)
        .options(selectinload(SupplyItem.supply),
                 joinedload(SupplyItem.product))
        .join(Supply, SupplyItem.supply_id == Supply.id)
        .filter(
            SupplyItem.id == supply_item_id,
            SupplyItem.is_active.is_(False),
            Supply.status.in_(["Ожидается", "В пути"]),
        )
        .first()
    )
    if not item:
        request.session["flash"] = "Товар недоступен для предзаказа"
        return RedirectResponse(url=_back(request), status_code=303)

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)
    min_stems = pack * min_packs

    if quantity_stems <= 0:
        quantity_stems = min_stems
    if quantity_stems < min_stems:
        quantity_stems = min_stems
    quantity_stems = (quantity_stems // pack) * pack
    packs = quantity_stems // pack

    if add_preorder_to_db(db, user.id, item.id, packs):
        arrival = item.supply.arrival_date if item.supply else None
        arrival_txt = arrival.strftime("%d.%m.%Y") if arrival else "—"
        request.session["flash"] = (
            f"🌸 «{product.name}» в предзаказе: {packs} упак. "
            f"Прибытие: {arrival_txt}"
        )
    else:
        request.session["flash"] = "Не удалось добавить предзаказ"
    return RedirectResponse(url=_back(request), status_code=303)


# ───────────────────────── ЗАКАЗАТЬ ПОСТУПИВШИЙ ПРЕДЗАКАЗ ─────────────────────────

@router.post("/preorders/{preorder_id}/to_cart")
async def preorder_to_cart(
    preorder_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    ok, result = move_fulfilled_preorder_to_cart(db, user, preorder_id)
    if not ok:
        request.session["flash"] = result
        return RedirectResponse(url="/preorders", status_code=303)

    cart = request.session.get("cart", [])
    _merge_into_cart(cart, result)
    request.session["cart"] = cart

    remove_fulfilled_preorder_db(db, user.id, preorder_id)

    request.session["flash"] = (
        f"🌸 «{result['name']}» добавлен в корзину "
        f"({result['quantity']} упак.)"
    )
    return RedirectResponse(url="/cart", status_code=303)


# ───────────────────────── СТРАНИЦА ПРЕДЗАКАЗОВ ─────────────────────────

@router.get("/preorders", response_class=HTMLResponse)
async def preorders_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    preorders = get_all_user_preorders(db, user.id)

    return render(
        request, "preorders.html", db,
        user=user,
        preorders=preorders,
    )


# ───────────────────────── ПРОСМОТР КОРЗИНЫ ─────────────────────────

@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = _clean_cart(request.session.get("cart", []))
    preorder = get_user_preorders(db, user.id)

    if not cart and not preorder:
        return render(
            request, "cart.html", db, user=user,
            cart=[], preorder=[], subtotal=0, discount_amount=0,
            total=0, discount_percent=0,
        )

    if cart:
        ids = [c["supply_item_id"] for c in cart]

        alive = {
            i.id: i
            for i in db.query(SupplyItem)
            .options(joinedload(SupplyItem.product))
            .filter(
                SupplyItem.id.in_(ids),
                SupplyItem.is_active.is_(True),
            )
            .all()
        }

        all_items = {
            i.id: i
            for i in db.query(SupplyItem)
            .options(joinedload(SupplyItem.product))
            .filter(SupplyItem.id.in_(ids))
            .all()
        }
        product_ids = [i.product_id for i in all_items.values()]

        new_by_product = {}
        if product_ids:
            new_items = (
                db.query(SupplyItem)
                .options(joinedload(SupplyItem.product))
                .filter(
                    SupplyItem.product_id.in_(product_ids),
                    SupplyItem.is_active.is_(True),
                    SupplyItem.stock > func.coalesce(
                        SupplyItem.reserved_stock, 0
                    ),
                )
                .order_by(SupplyItem.id.desc())
                .all()
            )
            for i in new_items:
                if i.product_id not in new_by_product:
                    new_by_product[i.product_id] = i

        clean_cart = []
        removed_names = []
        migrated_names = []

        for c in cart:
            item = alive.get(c["supply_item_id"])
            migrated = False

            if item is None and not c.get("from_preorder"):
                old = all_items.get(c["supply_item_id"])
                if old:
                    new_item = new_by_product.get(old.product_id)
                    if new_item and new_item.product:
                        c["supply_item_id"] = new_item.id
                        c["price"] = new_item.price
                        c["package_size"] = max(
                            1, new_item.product.package_size or 1
                        )
                        c["quantity"] = min(
                            c["quantity"], new_item.available_stock
                        )
                        item = new_item
                        migrated = True

            if item is None or not item.product:
                removed_names.append(c.get("name", "?"))
                continue

            if item.available_stock <= 0 and not c.get("from_preorder"):
                removed_names.append(c.get("name", "?"))
                continue

            c["price"] = item.price
            c["package_size"] = max(1, item.product.package_size or 1)
            if not c.get("from_preorder"):
                c["quantity"] = min(c["quantity"], item.available_stock)

            if c["quantity"] <= 0:
                removed_names.append(c.get("name", "?"))
                continue

            if migrated:
                migrated_names.append(c.get("name", "?"))

            clean_cart.append(c)

        request.session["cart"] = clean_cart

        if removed_names:
            request.session["flash"] = (
                "Из корзины убраны недоступные товары: "
                + ", ".join(removed_names)
            )
            return RedirectResponse(url="/cart", status_code=303)

        if migrated_names:
            request.session["flash"] = (
                "Корзина обновлена: товары доступны в новой партии: "
                + ", ".join(migrated_names)
            )
            return RedirectResponse(url="/cart", status_code=303)

        cart = clean_cart

    subtotal = _cart_subtotal(cart)
    discount_percent = user.discount_percent or 0
    discount_amount, total = _apply_discount(subtotal, discount_percent)

    return render(
        request, "cart.html", db,
        user=user, cart=cart, preorder=preorder,
        subtotal=subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
    )


@router.post("/remove_from_cart")
async def remove_from_cart(
    request: Request,
    item_index: int = Form(...),
    _csrf: None = Depends(check_csrf),
):
    cart = request.session.get("cart", [])
    if 0 <= item_index < len(cart):
        cart.pop(item_index)
    request.session["cart"] = cart
    return RedirectResponse(url=_back(request, "/cart"), status_code=303)


@router.post("/remove_from_preorder")
async def remove_from_preorder(
    request: Request,
    preorder_id: int = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if remove_preorder_db(db, user.id, preorder_id):
        request.session["flash"] = "Предзаказ удалён"
        try:
            from ..templating import _invalidate_preorder_cache
            _invalidate_preorder_cache(user.id)
        except Exception:
            pass
    return RedirectResponse(url=_back(request, "/cart"), status_code=303)


@router.post("/clear_cart")
async def clear_cart(request: Request, _csrf: None = Depends(check_csrf)):
    request.session["cart"] = []
    return RedirectResponse(url=_back(request, "/cart"), status_code=303)


# ───────────────────────── ОФОРМЛЕНИЕ (АТОМАРНОЕ) ─────────────────────────

@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    cart = _clean_cart(request.session.get("cart", []))
    if not cart:
        request.session["flash"] = "Корзина пуста"
        return RedirectResponse(url="/catalog", status_code=303)

    subtotal = _cart_subtotal(cart)
    discount_percent = user.discount_percent or 0
    discount_amount, total = _apply_discount(subtotal, discount_percent)

    return render(
        request, "checkout.html", db,
        user=user, cart=cart,
        subtotal=subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
    )


@router.post("/place_order")
async def place_order(
    request: Request,
    background_tasks: BackgroundTasks,
    comment: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    """
    Оформление заказа с ПЕССИМИСТИЧНОЙ БЛОКИРОВКОЙ строк supply_items.

    with_for_update() гарантирует, что между проверкой available_stock
    и списанием stock другой запрос не сможет выкупить тот же товар.
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = _clean_cart(request.session.get("cart", []))
    if not cart:
        request.session["flash"] = "Корзина пуста"
        return RedirectResponse(url="/catalog", status_code=303)

    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        request.session["flash"] = "Комментарий слишком длинный"
        return RedirectResponse(url="/checkout", status_code=303)

    cart_ids = [c["supply_item_id"] for c in cart]

    try:
        rows = (
            db.query(SupplyItem)
            .filter(SupplyItem.id.in_(cart_ids))
            .with_for_update()          # ← блокировка строк supply_items
            .all()
        )

        product_ids = [r.product_id for r in rows if r.product_id]
        products_by_id: dict[int, Product] = {}
        if product_ids:
            products = (
                db.query(Product)
                .filter(Product.id.in_(product_ids))
                .all()
            )
            products_by_id = {p.id: p for p in products}

        # Прогреваем relationship — дальше чтение item.product
        # не сходит в БД и не снимет блокировку.
        for r in rows:
            r.product = products_by_id.get(r.product_id)

        items_map = {i.id: i for i in rows}
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка загрузки товаров корзины: %s", e)
        request.session["flash"] = "Ошибка БД. Попробуйте ещё раз."
        return RedirectResponse(url="/cart", status_code=303)

    # Проверки ПОД блокировкой (уже безопасно от race condition)
    for c in cart:
        item = items_map.get(c["supply_item_id"])
        if item is None:
            request.session["flash"] = (
                f"«{c.get('name', '?')}» больше недоступен. "
                f"Удалите его из корзины."
            )
            return RedirectResponse(url="/cart", status_code=303)
        if not item.is_active and not c.get("from_preorder"):
            request.session["flash"] = (
                f"«{c.get('name', '?')}» снят с продажи. "
                f"Удалите его из корзины."
            )
            return RedirectResponse(url="/cart", status_code=303)
        if not c.get("from_preorder"):
            if item.available_stock < c["quantity"]:
                request.session["flash"] = (
                    f"«{c.get('name', '?')}»: "
                    f"только {item.available_stock} упак. на складе"
                )
                return RedirectResponse(url="/cart", status_code=303)

    subtotal = _cart_subtotal(cart)
    discount_percent = float(user.discount_percent or 0)
    _, total = _apply_discount(subtotal, discount_percent)

    # Списание ПОД блокировкой — атомарно
    for c in cart:
        item = items_map[c["supply_item_id"]]
        item.stock -= c["quantity"]
        if item.available_stock <= 0:
            item.is_active = False

    try:
        order = Order(
            user_id=user.id,
            subtotal=subtotal,
            discount_percent=discount_percent,
            total_price=total,
            status="Новый",
            comment=comment,
        )
        db.add(order)
        db.flush()

        for c in cart:
            db.add(OrderItem(
                order_id=order.id,
                product_id=c.get("product_id"),
                supply_item_id=c.get("supply_item_id"),
                product_name=str(c.get("name") or "Товар"),
                unit=str(c.get("unit") or "упаковка"),
                price=float(c.get("price") or 0),
                package_size=int(c.get("package_size") or 1),
                quantity=int(c.get("quantity") or 1),
            ))

        db.commit()
        db.refresh(order)

    except Exception as e:
        db.rollback()
        logger.exception(
            "❌ Ошибка создания заказа (user_id=%s): %s", user.id, e
        )
        request.session["flash"] = (
            "Не удалось оформить заказ. Попробуйте ещё раз или "
            "свяжитесь с менеджером."
        )
        return RedirectResponse(url="/cart", status_code=303)

    order_loaded = (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order.id)
        .first()
    )
    user_loaded = (
        db.query(models.User).filter(models.User.id == user.id).first()
    )
    background_tasks.add_task(
        notify_admin_new_order, order_loaded, user_loaded,
    )

    request.session["cart"] = []
    request.session["flash"] = f"Заказ №{order.id} оформлен!"
    return RedirectResponse(url="/orders", status_code=303)


# ───────────────────────── ИСТОРИЯ ЗАКАЗОВ ─────────────────────────

@router.get("/orders", response_class=HTMLResponse)
async def order_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    orders = (
        db.query(Order)
        .options(selectinload(Order.items))          # ← Eager loading!
        .filter(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .all()
    )
    return render(request, "orders.html", db, user=user, orders=orders)


@router.post("/orders/{order_id}/repeat")
async def repeat_order(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    order = (
        db.query(Order)
        .filter(Order.id == order_id, Order.user_id == user.id)
        .first()
    )
    if not order:
        request.session["flash"] = "Заказ не найден"
        return RedirectResponse(url="/orders", status_code=303)

    cart = request.session.get("cart", [])
    added = 0
    skipped = []

    for item in order.items:
        if item.product_id is None:
            skipped.append(item.product_name)
            continue

        si = (
            db.query(SupplyItem)
            .options(joinedload(SupplyItem.product))
            .filter(
                SupplyItem.product_id == item.product_id,
                SupplyItem.is_active.is_(True),
                SupplyItem.stock > func.coalesce(
                    SupplyItem.reserved_stock, 0
                ),
            )
            .first()
        )
        if not si:
            skipped.append(item.product_name)
            continue

        pack = max(1, si.product.package_size or 1)
        min_packs = max(1, si.product.min_quantity or 1)
        want_packs = item.quantity or min_packs
        want_packs = max(min_packs, min(want_packs, si.available_stock))

        _merge_into_cart(cart, {
            "supply_item_id": si.id,
            "product_id": si.product.id,
            "name": si.product.name,
            "unit": si.product.unit or "упаковка",
            "package_size": pack,
            "price": si.price,
            "image_url": si.product.image_url or "",
            "quantity": want_packs,
        })
        added += 1

    request.session["cart"] = cart
    msg = f"Добавлено в корзину из заказа №{order.id}: {added} поз."
    if skipped:
        msg += f" Недоступно: {len(skipped)}"
    request.session["flash"] = msg

    if added:
        return RedirectResponse(url="/cart", status_code=303)
    return RedirectResponse(url="/catalog", status_code=303)


@router.get("/orders/{order_id}/invoice")
async def download_invoice(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    order = (
        db.query(Order)
        .options(selectinload(Order.items))          # ← Eager loading!
        .filter(Order.id == order_id, Order.user_id == user.id)
        .first()
    )
    if not order:
        raise HTTPException(404, "Заказ не найден")

    try:
        from ..services.pdf_service import generate_invoice_pdf
        shop_info = {
            "name": config.SHOP_NAME,
            "phone": config.SHOP_PHONE,
            "address": config.SHOP_ADDRESS,
        }
        pdf_bytes = generate_invoice_pdf(order, user, shop_info)
    except ImportError:
        request.session["flash"] = "PDF-генерация недоступна"
        return RedirectResponse(url="/orders", status_code=303)
    except Exception as e:
        logger.exception("Ошибка генерации PDF: %s", e)
        request.session["flash"] = "Не удалось создать PDF"
        return RedirectResponse(url="/orders", status_code=303)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename=invoice_{order.id}.pdf"
        },
    )