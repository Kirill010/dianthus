"""Корзина и оформление заказа."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Order, OrderItem, SupplyItem
from ..security import check_csrf
from ..templating import render

router = APIRouter()
MAX_COMMENT_LENGTH = 1000


def _cart_total(cart: list[dict]) -> float:
    return sum(item["price"] * item["quantity"] for item in cart)


@router.post("/add_to_cart")
async def add_to_cart(
    request: Request,
    supply_item_id: int = Form(...),
    quantity_stems: int = Form(0),          # ← количество ШТУК
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    """
    Клиент вводит количество ШТУК. Кратно размеру упаковки.
    Внутри корзины quantity хранится в УПАКОВКАХ —
    так проще работать с stock и price (они в упаковках).
    """
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = (db.query(SupplyItem)
            .filter(SupplyItem.id == supply_item_id,
                    SupplyItem.is_active == True).first())  # noqa: E712
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    product = item.product

    # ─── Безопасные значения упаковки и минимума ───
    pack = product.package_size if (product.package_size and
                                    product.package_size > 0) else 1
    min_packs = product.min_quantity if (product.min_quantity and
                                         product.min_quantity > 0) else 1
    min_stems = pack * min_packs
    max_stems = item.stock * pack

    if item.stock <= 0:
        request.session["flash"] = f"«{product.name}» закончился"
        return RedirectResponse(url=f"/product/{supply_item_id}",
                                status_code=303)

    # ─── Нормализация введённого количества ШТУК ───
    if quantity_stems <= 0:
        quantity_stems = min_stems
    if quantity_stems < min_stems:
        quantity_stems = min_stems
    if quantity_stems > max_stems:
        quantity_stems = max_stems
    # Кратность размеру упаковки (округление ВНИЗ до целой упаковки)
    if quantity_stems % pack != 0:
        quantity_stems = (quantity_stems // pack) * pack
        if quantity_stems < min_stems:
            quantity_stems = min_stems

    # Переводим в упаковки для хранения в корзине
    quantity_packs = quantity_stems // pack

    cart = request.session.get("cart", [])
    for ci in cart:
        if ci["supply_item_id"] == supply_item_id:
            new_packs = ci["quantity"] + quantity_packs
            if new_packs > item.stock:
                new_packs = item.stock
                request.session["flash"] = (
                    f"Максимум {item.stock} упак. "
                    f"({item.stock * pack} шт)"
                )
            ci["quantity"] = new_packs
            # На случай, если у старой позиции не было package_size
            ci.setdefault("package_size", pack)
            break
    else:
        cart.append({
            "supply_item_id": item.id,
            "product_id": product.id,
            "name": product.name,
            "unit": product.unit,
            "package_size": pack,
            "price": item.price,
            "image_url": product.image_url,
            "quantity": quantity_packs,     # ← УПАКОВКИ
        })
        request.session["flash"] = (
            f"«{product.name}» добавлен в корзину "
            f"({quantity_stems} шт = {quantity_packs} упак.)"
        )

    request.session["cart"] = cart
    return RedirectResponse(url="/catalog", status_code=303)


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = request.session.get("cart", [])
    if not cart:
        return render(request, "cart.html", db,
                      user=user, cart=[], total=0)

    # ─── Автоочистка: убираем позиции, которых больше нет ───────
    ids = [c["supply_item_id"] for c in cart]
    alive = {
        i.id: i for i in
        db.query(SupplyItem)
        .filter(SupplyItem.id.in_(ids),
                SupplyItem.is_active == True)  # noqa: E712
        .all()
    }

    clean_cart = []
    removed_names = []
    for c in cart:
        item = alive.get(c["supply_item_id"])
        if item is None:
            removed_names.append(c["name"])
            continue
        # Обновляем цену, если админ поменял
        c["price"] = item.price
        c["quantity"] = min(c["quantity"], item.stock)
        if c["quantity"] <= 0:
            removed_names.append(c["name"])
            continue
        clean_cart.append(c)

    if removed_names:
        request.session["cart"] = clean_cart
        request.session["flash"] = (
            "Из корзины убраны недоступные товары: "
            + ", ".join(removed_names)
        )
        return RedirectResponse(url="/cart", status_code=303)

    return render(request, "cart.html", db,
                  user=user, cart=clean_cart, total=_cart_total(clean_cart))


@router.post("/remove_from_cart")
async def remove_from_cart(request: Request,
                           item_index: int = Form(...),
                           _csrf: None = Depends(check_csrf)):
    cart = request.session.get("cart", [])
    if 0 <= item_index < len(cart):
        cart.pop(item_index)
    request.session["cart"] = cart
    return RedirectResponse(url="/cart", status_code=303)


@router.post("/clear_cart")
async def clear_cart(request: Request, _csrf: None = Depends(check_csrf)):
    request.session["cart"] = []
    return RedirectResponse(url="/cart", status_code=303)


@router.get("/checkout", response_class=HTMLResponse)
async def checkout_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    cart = request.session.get("cart", [])
    if not cart:
        request.session["flash"] = "Корзина пуста"
        return RedirectResponse(url="/catalog", status_code=303)
    return render(request, "checkout.html", db,
                  user=user, cart=cart, total=_cart_total(cart))


@router.post("/place_order")
async def place_order(request: Request, comment: str = Form(""),
                      db: Session = Depends(get_db),
                      _csrf: None = Depends(check_csrf)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = request.session.get("cart", [])
    if not cart:
        return RedirectResponse(url="/catalog", status_code=303)

    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        request.session["flash"] = "Комментарий слишком длинный"
        return RedirectResponse(url="/checkout", status_code=303)

    # ─── ИСПРАВЛЕНИЕ: фильтруем только АКТИВНЫЕ позиции ───────────
    ids = [c["supply_item_id"] for c in cart]
    query = (db.query(SupplyItem)
             .filter(SupplyItem.id.in_(ids),
                     SupplyItem.is_active == True))  # noqa: E712
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    items_map = {i.id: i for i in query.all()}

    # Проверяем, что каждая позиция корзины ещё существует и активна
    for c in cart:
        item = items_map.get(c["supply_item_id"])
        if item is None:
            request.session["flash"] = (
                f"«{c['name']}» больше недоступен. Удалите его из корзины."
            )
            return RedirectResponse(url="/cart", status_code=303)
        if item.stock < c["quantity"]:
            request.session["flash"] = (
                f"«{c['name']}»: только {item.stock} на складе"
            )
            return RedirectResponse(url="/cart", status_code=303)

    order = Order(user_id=user.id, total_price=_cart_total(cart),
                  status="Новый", comment=comment)
    db.add(order)
    db.flush()

    for c in cart:
        db.add(OrderItem(
            order_id=order.id,
            product_id=c["product_id"],
            supply_item_id=c["supply_item_id"],
            product_name=c["name"],
            unit=c.get("unit", ""),
            price=c["price"],
            quantity=c["quantity"],
        ))
        items_map[c["supply_item_id"]].stock -= c["quantity"]

    db.commit()
    request.session["cart"] = []
    request.session["flash"] = f"Заказ №{order.id} оформлен!"
    return RedirectResponse(url="/orders", status_code=303)


@router.get("/orders", response_class=HTMLResponse)
async def order_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    orders = (db.query(Order).filter(Order.user_id == user.id)
              .order_by(Order.created_at.desc()).all())
    return render(request, "orders.html", db, user=user, orders=orders)