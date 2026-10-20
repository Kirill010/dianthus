# app/services/preorder_service.py
import logging
from sqlalchemy.orm import Session, joinedload

from ..models import Preorder, SupplyItem, Supply, User

logger = logging.getLogger(__name__)


def _calc_preorder_totals(preorder, item, product, user_discount: float):
    pack = max(1, product.package_size or 1)
    subtotal = round(item.price * preorder.quantity * pack, 2)
    pct = max(0.0, min(100.0, float(user_discount or 0)))
    discount_amount = round(subtotal * pct / 100.0, 2)
    total = round(subtotal - discount_amount, 2)
    return {
        "subtotal": subtotal,
        "discount_percent": pct,
        "discount_amount": discount_amount,
        "total": total,
    }


def add_preorder_to_db(
    db: Session, user_id: int, supply_item_id: int, quantity_packs: int
) -> bool:
    item = db.query(SupplyItem).filter(SupplyItem.id == supply_item_id).first()
    if not item or item.is_active:
        return False

    if not item.supply or item.supply.status not in ("Ожидается", "В пути"):
        logger.warning(
            "Попытка предзаказа из неактивной поставки: supply_id=%s",
            item.supply_id,
        )
        return False

    preorder = (
        db.query(Preorder)
        .filter(
            Preorder.user_id == user_id,
            Preorder.product_id == item.product_id,
            Preorder.is_fulfilled.is_(False),
        )
        .first()
    )

    if preorder:
        preorder.quantity += quantity_packs
        preorder.supply_item_id = item.id
    else:
        preorder = Preorder(
            user_id=user_id,
            product_id=item.product_id,
            supply_item_id=item.id,
            quantity=quantity_packs,
        )
        db.add(preorder)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка предзаказа: %s", e)
        return False
    return True


def _get_user_discount(db: Session, user_id: int) -> float:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0.0
    return float(user.discount_percent or 0)


def _resolve_item_for_preorder(db: Session, preorder: Preorder):
    if preorder.supply_item is not None:
        return preorder.supply_item
    return (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product), joinedload(SupplyItem.supply))
        .filter(SupplyItem.product_id == preorder.product_id)
        .order_by(SupplyItem.id.desc())
        .first()
    )


def get_user_preorders(db: Session, user_id: int) -> list:
    """Активные предзаказы для корзины."""
    discount = _get_user_discount(db, user_id)

    rows = (
        db.query(Preorder)
        .options(
            joinedload(Preorder.supply_item).joinedload(SupplyItem.product),
            joinedload(Preorder.supply_item).joinedload(SupplyItem.supply),
        )
        .filter(
            Preorder.user_id == user_id,
            Preorder.is_fulfilled.is_(False),
        )
        .all()
    )

    result = []
    for p in rows:
        item = _resolve_item_for_preorder(db, p)
        if not item:
            continue
        product = item.product
        totals = _calc_preorder_totals(p, item, product, discount)

        result.append({
            "id": p.id,
            "supply_item_id": item.id,
            "product_id": item.product_id,
            "name": product.name,
            "unit": product.unit or "упаковка",
            "package_size": max(1, product.package_size or 1),
            "price": item.price,
            "image_url": product.image_url or "",
            "quantity": p.quantity,
            "location": item.supply.location if item.supply else "—",
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
            **totals,
        })
    return result


def get_all_user_preorders(db: Session, user_id: int) -> list:
    """ВСЕ предзаказы пользователя (включая выполненные)."""
    discount = _get_user_discount(db, user_id)

    rows = (
        db.query(Preorder)
        .options(
            joinedload(Preorder.supply_item).joinedload(SupplyItem.product),
            joinedload(Preorder.supply_item).joinedload(SupplyItem.supply),
        )
        .filter(Preorder.user_id == user_id)
        .order_by(Preorder.created_at.desc())
        .all()
    )

    result = []
    for p in rows:
        item = _resolve_item_for_preorder(db, p)
        if not item:
            continue
        product = item.product
        totals = _calc_preorder_totals(p, item, product, discount)

        result.append({
            "id": p.id,
            "supply_item_id": item.id,
            "product_id": item.product_id,
            "name": product.name,
            "unit": product.unit or "упаковка",
            "package_size": max(1, product.package_size or 1),
            "price": item.price,
            "image_url": product.image_url or "",
            "quantity": p.quantity,
            "is_fulfilled": p.is_fulfilled,
            "created_at": p.created_at,
            "location": item.supply.location if item.supply else "—",
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
            "available_stock": item.available_stock if item.is_active else 0,
            **totals,
        })
    return result


def preorder_count_db(db: Session, user_id: int) -> int:
    rows = (
        db.query(Preorder)
        .filter(Preorder.user_id == user_id)
        .all()
    )
    return sum(p.quantity for p in rows)


def remove_preorder_db(db: Session, user_id: int, preorder_id: int) -> bool:
    preorder = (
        db.query(Preorder)
        .filter(
            Preorder.id == preorder_id,
            Preorder.user_id == user_id,
            Preorder.is_fulfilled.is_(False),
        )
        .first()
    )
    if preorder:
        db.delete(preorder)
        db.commit()
        return True
    return False


def remove_fulfilled_preorder_db(db: Session, user_id: int, preorder_id: int) -> bool:
    preorder = (
        db.query(Preorder)
        .filter(
            Preorder.id == preorder_id,
            Preorder.user_id == user_id,
        )
        .first()
    )
    if preorder:
        db.delete(preorder)
        db.commit()
        return True
    return False


def move_fulfilled_preorder_to_cart(
    db, user, preorder_id: int
) -> tuple[bool, dict | str]:
    preorder = (
        db.query(Preorder)
        .filter(
            Preorder.id == preorder_id,
            Preorder.user_id == user.id,
            Preorder.is_fulfilled.is_(True),
        )
        .first()
    )
    if not preorder or preorder.supply_item_id is None:
        return False, "Предзаказ не найден или ещё не поступил"

    # fix: ПЕССИМИСТИЧНАЯ БЛОКИРОВКА строки склада. Раньше два быстрых клика
    # (или два устройства) читали один и тот же reserved_stock, оба вычитали
    # своё количество — резерв уходил в минус, а клиент получал лишние упаковки.
    # FOR UPDATE сериализует перенос: второй запрос дождётся первого.
    item = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product), joinedload(SupplyItem.supply))
        .filter(SupplyItem.id == preorder.supply_item_id)
        .with_for_update()
        .first()
    )
    if not item or not item.is_active:
        db.rollback()
        return False, "Товар ещё не активирован в каталоге"

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)

    reserved = item.reserved_stock or 0
    if reserved > 0:
        # Сначала тратим СВОЙ резерв (он уже выделен под этот предзаказ)
        want_packs = min(reserved, preorder.quantity)
    else:
        # Резерва нет (например, его уже сняли) — берём из свободного остатка
        want_packs = min(preorder.quantity, item.available_stock)

    if want_packs <= 0:
        db.rollback()
        return False, "Товар закончился"
    if want_packs < min_packs:
        db.rollback()
        return False, (
            f"Доступно только {want_packs} упак., а минимальный заказ — "
            f"{min_packs} упак. Свяжитесь с менеджером."
        )

    item.reserved_stock = max(0, reserved - want_packs)
    db.commit()

    try:
        from ..templating import _invalidate_preorder_cache
        _invalidate_preorder_cache(user.id)
    except Exception:
        pass

    return True, {
        "supply_item_id": item.id,
        "product_id": product.id,
        "name": product.name,
        "unit": product.unit or "упаковка",
        "package_size": pack,
        "price": item.price,
        "image_url": product.image_url or "",
        "quantity": want_packs,
        "from_preorder": True,
        "location": item.supply.location if item.supply else "—",
    }