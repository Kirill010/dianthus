# app/services/preorder_service.py
import logging
from sqlalchemy.orm import Session
from ..models import Preorder, SupplyItem

logger = logging.getLogger(__name__)


def add_preorder_to_db(
    db: Session, user_id: int, supply_item_id: int, quantity_packs: int
) -> bool:
    """Добавляет или увеличивает предзаказ в БД. Возвращает True при успехе."""
    item = db.query(SupplyItem).filter(SupplyItem.id == supply_item_id).first()
    if not item or item.is_active:
        return False  # товар уже на складе — это не предзаказ

    # Ищем существующий НЕВЫПОЛНЕННЫЙ предзаказ на этот товар у этого юзера
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

    db.commit()
    return True


def get_user_preorders(db: Session, user_id: int) -> list:
    """Возвращает список НЕВЫПОЛНЕННЫХ предзаказов для отображения в корзине."""
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
            .filter(SupplyItem.product_id == p.product_id,
                    SupplyItem.is_active.is_(False))
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
            "unit": product.unit,
            "package_size": max(1, product.package_size or 1),
            "price": item.price,
            "image_url": product.image_url,
            "quantity": p.quantity,
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
        })
    return result


def preorder_count_db(db: Session, user_id: int) -> int:
    """Количество упаковок в предзаказах (для бейджа в шапке)."""
    rows = (
        db.query(Preorder)
        .filter(Preorder.user_id == user_id,
                Preorder.is_fulfilled.is_(False))
        .all()
    )
    return sum(p.quantity for p in rows)


def remove_preorder_db(db: Session, user_id: int, preorder_id: int) -> bool:
    """Удаляет предзаказ из БД."""
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