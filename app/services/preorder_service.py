# Логика предзаказов: проверка и перенос в обычную корзину.
import logging
from sqlalchemy.orm import Session
from ..models import SupplyItem

logger = logging.getLogger(__name__)


def sync_preorders(request, db: Session) -> dict:
    """
    Проверяет предзаказы в сессии.
    Если SupplyItem стал активным (машина пришла) — переносит
    в обычную корзину.

    Вызывается при каждом рендере страницы.
    Возвращает: {"moved": [...], "still_waiting": [...]}.
    """
    preorder = request.session.get("preorder_cart", [])
    if not preorder:
        return {"moved": [], "still_waiting": []}

    cart = request.session.get("cart", [])
    still_waiting = []
    moved_names = []
    changed = False

    ids = [p["supply_item_id"] for p in preorder]
    items = {
        i.id: i for i in
        db.query(SupplyItem).filter(SupplyItem.id.in_(ids)).all()
    }

    for p in preorder:
        item = items.get(p["supply_item_id"])

        # SupplyItem удалён — убираем из предзаказов
        if item is None:
            changed = True
            continue

        # Товар прибыл: активен и есть остаток
        if item.is_active and item.stock > 0:
            p["price"] = item.price
            p["package_size"] = max(1, item.product.package_size or 1)
            p["quantity"] = min(p["quantity"], item.stock)
            if p["quantity"] <= 0:
                changed = True
                continue

            # Сливаем с обычной корзиной
            for ci in cart:
                if ci["supply_item_id"] == p["supply_item_id"]:
                    total = ci["quantity"] + p["quantity"]
                    ci["quantity"] = min(total, item.stock)
                    break
            else:
                cart.append(p)

            moved_names.append(p["name"])
            changed = True
        else:
            still_waiting.append(p)

    if changed:
        request.session["cart"] = cart
        request.session["preorder_cart"] = still_waiting
        if moved_names:
            request.session["flash"] = (
                "🌸 Товар прибыл! Перенесено из предзаказа: "
                + ", ".join(moved_names)
            )

    return {"moved": moved_names, "still_waiting": still_waiting}


def add_to_preorder(request, item: SupplyItem, quantity_packs: int) -> bool:
    """
    Добавляет SupplyItem в предзаказ.
    Возвращает True, если добавили, False — если что-то не так.
    """
    if item.is_active:
        return False  # это уже складской товар, не предзаказ

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)

    quantity_packs = max(min_packs, quantity_packs)

    preorder = request.session.get("preorder_cart", [])
    for p in preorder:
        if p["supply_item_id"] == item.id:
            p["quantity"] += quantity_packs
            p["package_size"] = pack
            break
    else:
        arrival = item.supply.arrival_date
        preorder.append({
            "supply_item_id": item.id,
            "product_id": product.id,
            "name": product.name,
            "unit": product.unit,
            "package_size": pack,
            "price": item.price,
            "image_url": product.image_url,
            "quantity": quantity_packs,
            "arrival_date": arrival.strftime("%Y-%m-%d") if arrival else "",
        })

    request.session["preorder_cart"] = preorder
    return True