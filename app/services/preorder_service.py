# app/services/preorder_service.py
import logging
from sqlalchemy.orm import Session, joinedload

from ..models import Preorder, SupplyItem, Supply, User

logger = logging.getLogger(__name__)


def _calc_preorder_totals(preorder, item, product, user_discount: float):
    """
    Считает суммы для одного предзаказа с учётом скидки клиента.
    Возвращает словарь с subtotal / discount_percent / discount_amount / total.
    """
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
    """Добавляет или увеличивает предзаказ. True при успехе."""
    item = db.query(SupplyItem).filter(SupplyItem.id == supply_item_id).first()
    if not item or item.is_active:
        return False

    # Проверяем, что поставка действительно активна (Ожидается / В пути)
    if not item.supply or item.supply.status not in ("Ожидается", "В пути"):
        logger.warning(
            "Попытка предзаказа из неактивной поставки: supply_id=%s, status=%s",
            item.supply_id, item.supply.status if item.supply else "?",
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
        preorder.supply_item_id = item.id  # обновляем ссылку на актуальную позицию
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
    """Возвращает процент скидки пользователя (0, если нет)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return 0.0
    return float(user.discount_percent or 0)


def _resolve_item_for_preorder(db: Session, preorder: Preorder):
    """
    Возвращает SupplyItem для предзаказа.
    Если позиция ещё не активна — берём её же.
    Если позиция уже разгружена (is_active=True) — берём её же
    (в do_unload мы обновляем supply_item_id на актуальную).
    """
    if preorder.supply_item is not None:
        return preorder.supply_item

    # Фолбэк: ищем последнюю позицию по продукту
    return (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product), joinedload(SupplyItem.supply))
        .filter(SupplyItem.product_id == preorder.product_id)
        .order_by(SupplyItem.id.desc())
        .first()
    )


def get_user_preorders(db: Session, user_id: int) -> list:
    """Активные предзаказы для корзины (только is_fulfilled=False)."""
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
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
            **totals,
        })
    return result


def get_all_user_preorders(db: Session, user_id: int) -> list:
    """ВСЕ предзаказы пользователя (включая выполненные) — для /preorders."""
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
            "arrival_date": (
                item.supply.arrival_date.strftime("%d.%m.%Y")
                if item.supply and item.supply.arrival_date else "—"
            ),
            **totals,
        })
    return result


def preorder_count_db(db: Session, user_id: int) -> int:
    """Количество упаковок в активных предзаказах (для бейджа в шапке)."""
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


def move_fulfilled_preorder_to_cart(db, user, preorder_id: int) -> tuple[bool, str]:
    """
    Переносит ВЫПОЛНЕННЫЙ предзаказ в основную корзину пользователя.
    Возвращает (успех, сообщение).
    """
    preorder = (
        db.query(Preorder)
        .options(
            joinedload(Preorder.supply_item).joinedload(SupplyItem.product),
        )
        .filter(
            Preorder.id == preorder_id,
            Preorder.user_id == user.id,
            Preorder.is_fulfilled.is_(True),
        )
        .first()
    )
    if not preorder:
        return False, "Предзаказ не найден или ещё не поступил"

    item = preorder.supply_item
    if not item or not item.is_active:
        return False, "Товар ещё не активирован в каталоге"

    product = item.product
    pack = max(1, product.package_size or 1)
    min_packs = max(1, product.min_quantity or 1)
    want_packs = max(min_packs, min(preorder.quantity, item.stock or preorder.quantity))

    # Сессия недоступна в сервисе, поэтому вернём данные для добавления
    # (используется в роутере, где есть request.session)
    return True, {
        "supply_item_id": item.id,
        "product_id": product.id,
        "name": product.name,
        "unit": product.unit or "упаковка",
        "package_size": pack,
        "price": item.price,
        "image_url": product.image_url or "",
        "quantity": want_packs,
    }