# app/services/preorder_service.py
import logging
from sqlalchemy.orm import Session, joinedload
from ..models import Preorder, SupplyItem

logger = logging.getLogger(__name__)


def add_preorder_to_db(
    db: Session, user_id: int, supply_item_id: int, quantity_packs: int
) -> bool:
    """Добавляет или увеличивает предзаказ. True при успехе."""
    item = db.query(SupplyItem).filter(SupplyItem.id == supply_item_id).first()
    if not item or item.is_active:
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


def get_user_preorders(db: Session, user_id: int) -> list:
    """Активные предзаказы для корзины."""
    rows = (
        db.query(Preorder)
        .filter(
            Preorder.user_id == user_id,
            Preorder.is_fulfilled.is_(False),
        )
        .all()
    )
    result = []
    for p in rows:
        item = p.supply_item or (
            db.query(SupplyItem)
            .options(joinedload(SupplyItem.product))
            .filter(
                SupplyItem.product_id == p.product_id,
                SupplyItem.is_active.is_(False),
            )
            .order_by(SupplyItem.id.desc())
            .first()
        )
        if not item:
            continue
        product = item.product
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
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
        })
    return result


def get_all_user_preorders(db: Session, user_id: int) -> list:
    """ВСЕ предзаказы пользователя (включая выполненные)."""
    rows = (
        db.query(Preorder)
        .filter(Preorder.user_id == user_id)
        .order_by(Preorder.created_at.desc())
        .all()
    )
    result = []
    for p in rows:
        item = p.supply_item or (
            db.query(SupplyItem)
            .options(joinedload(SupplyItem.product))
            .filter(SupplyItem.product_id == p.product_id)
            .order_by(SupplyItem.id.desc())
            .first()
        )
        if not item:
            continue
        product = item.product
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
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
        })
    return result


def preorder_count_db(db: Session, user_id: int) -> int:
    """Количество упаковок в активных предзаказах."""
    rows = (
        db.query(Preorder)
        .filter(
            Preorder.user_id == user_id,
            Preorder.is_fulfilled.is_(False),
        )
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