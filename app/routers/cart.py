# app/routers/cart.py
"""Корзина и оформление заказа с атомарной блокировкой стока."""
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import (APIRouter, BackgroundTasks, Depends, Form,
                     HTTPException, Request)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import Order, OrderItem, Product, SupplyItem, Supply
from ..security import check_csrf, check_rate_limit, safe_equal
from ..services.notifier import (notify_admin_new_order,
                                 notify_client_order_created)
from ..services.preorder_service import (
    MAX_PREORDER_PACKS, add_preorder_to_db, get_user_preorders,
    remove_preorder_db, get_all_user_preorders,
    move_fulfilled_preorder_to_cart, remove_fulfilled_preorder_db,
)
from ..services import preorder_rules
from ..services.pricing import apply_discount, line_total, money
from ..services.site_settings import get_setting
from ..templating import render
from ..timeutils import shop_tz
from ..web import safe_back
from .. import models

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_COMMENT_LENGTH = 1000
MAX_DESIRED_DAYS = 365          # на сколько вперёд можно указать дату получения


def _cart_subtotal(cart: list[dict]) -> float:
    # каждая позиция округляется до копейки — сумма сходится со счётом и 1С
    return money(sum(
        line_total(it["price"], it.get("package_size"), it["quantity"])
        for it in cart
    ))


def _apply_discount(subtotal: float, discount_percent: float):
    return apply_discount(subtotal, discount_percent)


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
    """Редирект «назад» ТОЛЬКО на свой сайт (защита от open redirect)."""
    return safe_back(request, default)


# ───────────────────────── ДОБАВЛЕНИЕ В КОРЗИНУ ─────────────────────────

@router.post("/add_to_cart")
def add_to_cart(
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
def add_to_preorder_route(
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
    packs = min(quantity_stems // pack, MAX_PREORDER_PACKS)

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
def preorder_to_cart(
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
def preorders_page(request: Request, db: Session = Depends(get_db)):
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
def cart_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    cart = _clean_cart(request.session.get("cart", []))
    preorder = get_user_preorders(db, user.id)
    supply_labels: dict[int, str] = {}        # «№ 00000622 от 29.10.2026» — из какой поставки позиция

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
            supply_labels[item.id] = item.supply.label if item.supply is not None else ""

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
        user=user, cart=cart, preorder=preorder, supply_labels=supply_labels,
        subtotal=subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
    )


def cart_key(entry: dict) -> str:
    """Устойчивый «адрес» позиции корзины: товар + из предзаказа или нет.

    По номеру в списке удалять нельзя: в двух вкладках браузера номера
    расходятся, и клиент удалял не ту позицию.
    """
    return f"{entry.get('supply_item_id')}:{1 if entry.get('from_preorder') else 0}"


def _find_cart_index(cart: list, key: str) -> int | None:
    for i, entry in enumerate(cart):
        if isinstance(entry, dict) and cart_key(entry) == key:
            return i
    return None


@router.post("/remove_from_cart")
def remove_from_cart(
    request: Request,
    item_key: str = Form(""),
    item_index: int = Form(-1),
    _csrf: None = Depends(check_csrf),
):
    cart = request.session.get("cart", [])
    idx = _find_cart_index(cart, item_key) if item_key else None
    if idx is None and not item_key and 0 <= item_index < len(cart):
        idx = item_index                                  # старые формы без ключа
    if idx is not None:
        cart.pop(idx)
    request.session["cart"] = cart
    return RedirectResponse(url=_back(request, "/cart"), status_code=303)


@router.post("/cart/update")
def update_cart_item(
    request: Request,
    item_key: str = Form(...),
    quantity_packs: int = Form(...),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    """Изменить количество (в упаковках) прямо в корзине."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    cart = request.session.get("cart", [])
    idx = _find_cart_index(cart, item_key)
    if idx is None:
        request.session["flash"] = "Эта позиция уже убрана из корзины"
        return RedirectResponse(url="/cart", status_code=303)
    entry = cart[idx]

    if quantity_packs <= 0:
        cart.pop(idx)
        request.session["cart"] = cart
        request.session["flash"] = f"«{entry.get('name', 'Товар')}» убран из корзины"
        return RedirectResponse(url="/cart", status_code=303)

    item = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.id == entry["supply_item_id"])
        .first()
    )
    if item is None or item.product is None:
        cart.pop(idx)
        request.session["cart"] = cart
        request.session["flash"] = f"«{entry.get('name', 'Товар')}» больше недоступен"
        return RedirectResponse(url="/cart", status_code=303)

    min_packs = max(1, item.product.min_quantity or 1)
    # из предзаказа остаток уже «зарезервирован» за клиентом — смотрим на склад целиком
    limit = (item.stock or 0) if entry.get("from_preorder") else item.available_stock
    if limit <= 0:
        cart.pop(idx)
        request.session["cart"] = cart
        request.session["flash"] = f"«{entry.get('name', 'Товар')}» закончился"
        return RedirectResponse(url="/cart", status_code=303)

    wanted = quantity_packs
    quantity_packs = min(max(quantity_packs, min_packs), limit)
    if quantity_packs != wanted:
        reason = (f"минимум {min_packs} упак." if wanted < min_packs
                  else f"на складе только {limit} упак.")
        request.session["flash"] = (
            f"«{entry.get('name', 'Товар')}»: {reason} — количество исправлено"
        )
    entry["quantity"] = quantity_packs
    request.session["cart"] = cart
    return RedirectResponse(url="/cart", status_code=303)


@router.post("/remove_from_preorder")
def remove_from_preorder(
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
def clear_cart(request: Request, _csrf: None = Depends(check_csrf)):
    request.session["cart"] = []
    return RedirectResponse(url=_back(request, "/cart"), status_code=303)


# ───────────────────────── ОФОРМЛЕНИЕ (АТОМАРНОЕ) ─────────────────────────

@router.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request, db: Session = Depends(get_db)):
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
    if not request.session.get("order_token"):
        request.session["order_token"] = secrets.token_urlsafe(24)
    today = datetime.now(shop_tz()).date()
    previous = (
        db.query(Order.delivery_method, Order.delivery_address)
        .filter(Order.user_id == user.id, Order.delivery_method != "",
                Order.delivery_method.isnot(None))
        .order_by(Order.id.desc()).first()
    )
    return render(
        request, "checkout.html", db,
        user=user, cart=cart,
        subtotal=subtotal,
        discount_percent=discount_percent,
        discount_amount=discount_amount,
        total=total,
        order_token=request.session["order_token"],
        min_date=today.isoformat(),
        max_date=(today + timedelta(days=MAX_DESIRED_DAYS)).isoformat(),
        payment_terms=get_setting(db, "payment_terms"),
        last_method=previous[0] if previous else "",
        last_address=previous[1] if previous else "",
        checkout_errors=request.session.pop("checkout_errors", None),
        checkout_form=request.session.pop("checkout_form", {}),
    )


def _parse_checkout_extras(desired_date: str, delivery_method: str,
                           delivery_address: str):
    """Проверка необязательных полей оформления. → (дата, способ, адрес, ошибки)."""
    errors: list[str] = []
    desired = None
    if (desired_date or "").strip():
        d = preorder_rules.parse_user_date(desired_date)
        today = datetime.now(shop_tz()).date()
        if d is None:
            errors.append("Дата получения указана неверно")
        elif d < today:
            errors.append("Дата получения не может быть в прошлом")
        elif d > today + timedelta(days=MAX_DESIRED_DAYS):
            errors.append("Дата получения слишком далеко — выберите дату в пределах года")
        else:
            desired = datetime(d.year, d.month, d.day)
    method = delivery_method if delivery_method in ("pickup", "delivery") else ""
    address = " ".join((delivery_address or "").split())
    if len(address) > 500:
        errors.append("Адрес доставки слишком длинный")
    if method == "delivery" and len(address) < 5:
        errors.append("Для доставки укажите адрес")
    if method != "delivery":
        address = ""
    return desired, method, address[:500], errors


async def _rate_place_order(request: Request) -> None:
    """Антиспам: не более 10 оформлений заказа в минуту с одного IP."""
    await check_rate_limit(request, "place_order", max_hits=10, window=60)


@router.post("/place_order")
def place_order(
    request: Request,
    background_tasks: BackgroundTasks,
    order_token: str = Form(""),
    comment: str = Form(""),
    desired_date: str = Form(""),
    delivery_method: str = Form(""),
    delivery_address: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
    _rate: None = Depends(_rate_place_order),
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

    expected = request.session.get("order_token")
    if not expected or not safe_equal(order_token, expected):
        request.session["flash"] = "Заказ уже оформлен (повторная отправка формы)"
        return RedirectResponse(url="/orders", status_code=303)

    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        request.session["flash"] = "Комментарий слишком длинный"
        return RedirectResponse(url="/checkout", status_code=303)

    desired, method, address, extra_errors = _parse_checkout_extras(
        desired_date, delivery_method, delivery_address)
    if extra_errors:
        request.session["checkout_errors"] = extra_errors
        request.session["checkout_form"] = {
            "desired_date": desired_date, "delivery_method": delivery_method,
            "delivery_address": delivery_address, "comment": comment,
        }
        return RedirectResponse(url="/checkout", status_code=303)

    cart_ids = [c["supply_item_id"] for c in cart]

    try:
        rows = (
            db.query(SupplyItem)
            .filter(SupplyItem.id.in_(cart_ids))
            # Единый порядок блокировки: два одновременных заказа с
            # пересекающимися товарами не создают взаимную блокировку (deadlock).
            .order_by(SupplyItem.id)
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
        elif (item.stock or 0) < c["quantity"]:
            # Резерв под предзаказ уже снят при переносе в корзину,
            # поэтому физический остаток тоже нужно проверить.
            request.session["flash"] = (
                f"«{c.get('name', '?')}»: "
                f"на складе осталось только {item.stock} упак."
            )
            return RedirectResponse(url="/cart", status_code=303)

    # Цены берём из БД, а не из корзины: за время «жизни» корзины цена могла
    # измениться (правка админом или обмен с 1С). Если изменилась — не
    # оформляем молча по старой цене, а показываем клиенту актуальную.
    changed = []
    for c in cart:
        item = items_map[c["supply_item_id"]]
        prod = item.product
        cur_pack = max(1, (prod.package_size if prod else 0) or 1)
        cur_price = float(item.price or 0)
        if (abs(cur_price - float(c.get("price") or 0)) > 0.004
                or cur_pack != int(c.get("package_size") or 1)):
            changed.append(c.get("name", "?"))
            c["price"] = cur_price
            c["package_size"] = cur_pack
    if changed:
        db.rollback()                    # снимаем блокировки
        full = request.session.get("cart", [])
        by_id = {x["supply_item_id"]: x for x in cart}
        for row in full:
            upd = by_id.get(row.get("supply_item_id"))
            if upd:
                row["price"] = upd["price"]
                row["package_size"] = upd["package_size"]
        request.session["cart"] = full
        request.session["flash"] = (
            "Цена изменилась: " + ", ".join(f"«{n}»" for n in changed[:5])
            + ". Проверьте корзину и оформите заказ ещё раз."
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
            order_token=order_token[:64] or None,
            desired_date=desired,
            delivery_method=method,
            delivery_address=address,
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

    except IntegrityError as e:
        # Чаще всего — тот же order_token уже использован: форма отправлена дважды
        # (двойной клик, повтор запроса). Заказ создан ровно один раз, а списание
        # остатков второй попыткой откатывается вместе с ней.
        db.rollback()
        duplicate = bool(order_token) and db.query(Order.id).filter(
            Order.order_token == order_token[:64]).first() is not None
        if duplicate:
            request.session.pop("order_token", None)
            request.session["cart"] = []
            request.session["flash"] = "Заказ уже оформлен (повторная отправка формы)"
            return RedirectResponse(url="/orders", status_code=303)
        # другая причина (например, товар удалили во время оформления)
        logger.exception("❌ Ошибка целостности при создании заказа (user_id=%s): %s",
                         user.id, e)
        request.session["flash"] = (
            "Не удалось оформить заказ. Проверьте корзину и попробуйте ещё раз."
        )
        return RedirectResponse(url="/cart", status_code=303)
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

    request.session.pop("order_token", None)

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
    background_tasks.add_task(
        notify_client_order_created, order_loaded, user_loaded,
    )

    request.session["cart"] = []
    request.session["flash"] = f"Заказ №{order.id} оформлен!"
    return RedirectResponse(url="/orders", status_code=303)


# ───────────────────────── ИСТОРИЯ ЗАКАЗОВ ─────────────────────────

@router.get("/orders", response_class=HTMLResponse)
def order_history(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    orders = (
        db.query(Order)
        .options(selectinload(Order.items))          # ← Eager loading!
        .filter(Order.user_id == user.id)
        .order_by(Order.created_at.desc())
        .limit(100)          # у постоянного клиента заказов тысячи — страницу не раздуваем
        .all()
    )
    # обращения по качеству, уже заведённые по этим заказам (одним запросом)
    claims_by_order: dict[int, list] = {}
    if orders:
        for claim in (db.query(models.Claim)
                      .filter(models.Claim.user_id == user.id,
                              models.Claim.order_id.in_([o.id for o in orders]))
                      .order_by(models.Claim.id).all()):
            claims_by_order.setdefault(claim.order_id, []).append(claim)
    return render(request, "orders.html", db, user=user, orders=orders,
                  claims_by_order=claims_by_order)


@router.post("/orders/{order_id}/repeat")
def repeat_order(
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
            .order_by(SupplyItem.id.desc())      # самая свежая партия, а не «любая»
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
def download_invoice(
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
        from ..services.pdf_service import generate_invoice_pdf, shop_info_for_invoice
        pdf_bytes = generate_invoice_pdf(order, user, shop_info_for_invoice(db))
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