"""Корзина и оформление заказа."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..security import check_csrf
from ..templating import render
from .. import models

router = APIRouter()

MAX_COMMENT_LENGTH = 1000


def _cart_total(cart: list[dict]) -> float:
    return sum(item["price"] * item["quantity"] for item in cart)


@router.post("/add_to_cart")
async def add_to_cart(
    request: Request,
    product_id: int = Form(...),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    product = db.query(models.Product).filter(
        models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    if quantity < product.min_quantity:
        quantity = product.min_quantity

    if product.stock <= 0:
        request.session["flash"] = f"«{product.name}» закончился"
        return RedirectResponse(url=f"/product/{product_id}", status_code=303)

    cart = request.session.get("cart", [])

    for item in cart:
        if item["product_id"] == product_id:
            new_qty = item["quantity"] + quantity
            if new_qty > product.stock:
                new_qty = product.stock
                request.session["flash"] = (
                    f"Больше {product.stock} {product.unit} нет на складе"
                )
            item["quantity"] = new_qty
            break
    else:
        if quantity > product.stock:
            quantity = product.stock
        cart.append({
            "product_id": product.id,
            "name": product.name,
            "unit": product.unit,
            "price": product.price,
            "image_url": product.image_url,
            "quantity": quantity,
        })
        request.session["flash"] = f"«{product.name}» добавлен в корзину"

    request.session["cart"] = cart
    return RedirectResponse(url="/catalog", status_code=303)


@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    cart = request.session.get("cart", [])
    total = _cart_total(cart)
    return render(request, "cart.html", db, user=user, cart=cart, total=total)


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

    total = _cart_total(cart)
    return render(request, "checkout.html", db,
                  user=user, cart=cart, total=total)


@router.post("/place_order")
async def place_order(
    request: Request,
    comment: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = request.session.get("cart", [])
    if not cart:
        return RedirectResponse(url="/catalog", status_code=303)

    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        request.session["flash"] = (
            f"Комментарий слишком длинный (максимум {MAX_COMMENT_LENGTH})"
        )
        return RedirectResponse(url="/checkout", status_code=303)

    product_ids = [item["product_id"] for item in cart]

    # Загружаем товары одним запросом.
    # Для PostgreSQL дополнительно блокируем строки — защита от oversell.
    query = db.query(models.Product).filter(models.Product.id.in_(product_ids))
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        query = query.with_for_update()
    products_map = {p.id: p for p in query.all()}

    # Перепроверяем остатки
    for item in cart:
        product = products_map.get(item["product_id"])
        if not product:
            request.session["flash"] = f"«{item['name']}» больше не доступен"
            return RedirectResponse(url="/cart", status_code=303)
        if product.stock < item["quantity"]:
            request.session["flash"] = (
                f"«{product.name}»: только {product.stock} на складе. "
                f"Измените количество."
            )
            return RedirectResponse(url="/cart", status_code=303)

    total = _cart_total(cart)
    order = models.Order(user_id=user.id, total_price=total,
                         status="Новый", comment=comment)
    db.add(order)
    db.flush()

    for item in cart:
        db.add(models.OrderItem(
            order_id=order.id,
            product_id=item["product_id"],
            product_name=item["name"],
            unit=item.get("unit", ""),
            price=item["price"],
            quantity=item["quantity"],
        ))
        product = products_map.get(item["product_id"])
        if product:
            product.stock -= item["quantity"]

    db.commit()
    request.session["cart"] = []
    request.session["flash"] = (
        f"Заказ №{order.id} оформлен! Менеджер свяжется с вами."
    )
    return RedirectResponse(url="/orders", status_code=303)


@router.get("/orders", response_class=HTMLResponse)
async def order_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    orders = (db.query(models.Order)
              .filter(models.Order.user_id == user.id)
              .order_by(models.Order.created_at.desc()).all())
    return render(request, "orders.html", db, user=user, orders=orders)