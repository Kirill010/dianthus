"""Корзина и оформление заказа. Цена — за штуку, количество — упаковки."""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (HTMLResponse, RedirectResponse,
                                Response)
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import Order, OrderItem, SupplyItem
from ..security import check_csrf
from ..services.notifier import notify_admin_new_order
from ..templating import render

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_COMMENT_LENGTH = 1000


# ───────── УТИЛИТЫ ─────────

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
    return [c for c in cart if c.get("quantity", 0) > 0]


# ───────── ДОБАВЛЕНИЕ ─────────

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

    item = (db.query(SupplyItem)
            .filter(SupplyItem.id == supply_item_id,
                    SupplyItem.is_active == True).first())  # noqa: E712
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)
    min_stems = pack * min_packs
    max_stems = item.stock * pack

    if item.stock <= 0:
        request.session["flash"] = f"«{product.name}» закончился"
        return RedirectResponse(url=f"/product/{supply_item_id}",
                                status_code=303)

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
    for ci in cart:
        if ci["supply_item_id"] == supply_item_id:
            new_packs = ci["quantity"] + quantity_packs
            if new_packs > item.stock:
                new_packs = item.stock
                request.session["flash"] = (
                    f"Максимум {item.stock} упак. ({item.stock * pack} шт)"
                )
            ci["quantity"] = new_packs
            ci["package_size"] = pack
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
            "quantity": quantity_packs,
        })
        request.session["flash"] = (
            f"«{product.name}» — {quantity_packs} упак. "
            f"({quantity_stems} шт × {item.price:.2f} ₽)"
        )

    request.session["cart"] = cart
    return RedirectResponse(url="/catalog", status_code=303)


# ───────── ПРОСМОТР ─────────

@router.get("/cart", response_class=HTMLResponse)
async def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = request.session.get("cart", [])
    if not cart:
        return render(request, "cart.html", db, user=user, cart=[],
                      subtotal=0, discount_amount=0, total=0,
                      discount_percent=0)

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
        c["price"] = item.price
        c["package_size"] = max(1, item.product.package_size or 1)
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

    subtotal = _cart_subtotal(clean_cart)
    discount_percent = user.discount_percent or 0
    discount_amount, total = _apply_discount(subtotal, discount_percent)

    return render(request, "cart.html", db,
                  user=user, cart=clean_cart,
                  subtotal=subtotal,
                  discount_percent=discount_percent,
                  discount_amount=discount_amount,
                  total=total)


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


# ───────── ОФОРМЛЕНИЕ ─────────

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

    return render(request, "checkout.html", db,
                  user=user, cart=cart,
                  subtotal=subtotal,
                  discount_percent=discount_percent,
                  discount_amount=discount_amount,
                  total=total)


@router.post("/place_order")
async def place_order(request: Request, comment: str = Form(""),
                      db: Session = Depends(get_db),
                      _csrf: None = Depends(check_csrf)):
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

    ids = [c["supply_item_id"] for c in cart]
    query = (db.query(SupplyItem)
             .filter(SupplyItem.id.in_(ids),
                     SupplyItem.is_active == True))  # noqa: E712
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    items_map = {i.id: i for i in query.all()}

    for c in cart:
        item = items_map.get(c["supply_item_id"])
        if item is None:
            request.session["flash"] = (
                f"«{c['name']}» больше недоступен. Удалите его из корзины."
            )
            return RedirectResponse(url="/cart", status_code=303)
        if item.stock < c["quantity"]:
            request.session["flash"] = (
                f"«{c['name']}»: только {item.stock} упак. на складе"
            )
            return RedirectResponse(url="/cart", status_code=303)

    subtotal = _cart_subtotal(cart)
    discount_percent = user.discount_percent or 0
    _, total = _apply_discount(subtotal, discount_percent)

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
            product_id=c["product_id"],
            supply_item_id=c["supply_item_id"],
            product_name=c["name"],
            unit=c.get("unit", ""),
            price=c["price"],
            package_size=c.get("package_size") or 1,
            quantity=c["quantity"],
        ))
        items_map[c["supply_item_id"]].stock -= c["quantity"]

    db.commit()
    db.refresh(order)

    # ── Уведомление админу на email ──
    try:
        notify_admin_new_order(order, user)
    except Exception as e:
        logger.warning("Не удалось уведомить админа: %s", e)

    request.session["cart"] = []
    request.session["flash"] = f"Заказ №{order.id} оформлен!"
    return RedirectResponse(url="/orders", status_code=303)


# ───────── ИСТОРИЯ ─────────

@router.get("/orders", response_class=HTMLResponse)
async def order_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    orders = (db.query(Order).filter(Order.user_id == user.id)
              .order_by(Order.created_at.desc()).all())
    return render(request, "orders.html", db, user=user, orders=orders)


# ───────── ПОВТОРИТЬ ЗАКАЗ ─────────

@router.post("/orders/{order_id}/repeat")
async def repeat_order(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    """Добавляет позиции старого заказа в корзину."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    order = (db.query(Order)
             .filter(Order.id == order_id, Order.user_id == user.id)
             .first())
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

        si = (db.query(SupplyItem)
              .filter(SupplyItem.product_id == item.product_id,
                      SupplyItem.is_active == True,  # noqa: E712
                      SupplyItem.stock > 0)
              .first())
        if not si:
            skipped.append(item.product_name)
            continue

        pack = max(1, si.product.package_size or 1)
        min_packs = max(1, si.product.min_quantity or 1)

        want_packs = item.quantity or min_packs
        want_packs = max(min_packs, min(want_packs, si.stock))

        found = False
        for ci in cart:
            if ci["supply_item_id"] == si.id:
                new_packs = ci["quantity"] + want_packs
                if new_packs > si.stock:
                    new_packs = si.stock
                ci["quantity"] = new_packs
                ci["package_size"] = pack
                found = True
                break

        if not found:
            cart.append({
                "supply_item_id": si.id,
                "product_id": si.product.id,
                "name": si.product.name,
                "unit": si.product.unit,
                "package_size": pack,
                "price": si.price,
                "image_url": si.product.image_url,
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


# ───────── PDF-СЧЁТ ─────────

@router.get("/orders/{order_id}/invoice")
async def download_invoice(
    order_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Скачать счёт в PDF."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    order = (db.query(Order)
             .filter(Order.id == order_id, Order.user_id == user.id)
             .first())
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
        request.session["flash"] = "PDF-генерация недоступна (нет weasyprint)"
        return RedirectResponse(url="/orders", status_code=303)
    except Exception as e:
        logger.exception("Ошибка генерации PDF: %s", e)
        request.session["flash"] = "Не удалось создать PDF"
        return RedirectResponse(url="/orders", status_code=303)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"inline; filename=invoice_{order.id}.pdf"
            )
        },
    )