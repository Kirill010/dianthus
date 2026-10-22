# app/services/orders.py
"""Смена статуса заказа — одна логика для админки и обмена с 1С.

Главное, чего раньше не было: отмена заказа не возвращала товар на склад.
Остаток списывался при оформлении и «сгорал» навсегда — до ближайшей
выгрузки остатков из 1С. Теперь:

  * переход В «Отменён»  → упаковки возвращаются в SupplyItem.stock;
  * переход ИЗ «Отменён» → упаковки списываются снова (если их хватает,
    иначе статус не меняется и менеджер видит причину);
  * строки склада блокируются FOR UPDATE в едином порядке (по id), а сам
    заказ — на время смены статуса: два одновременных нажатия «Отменить»
    не вернут остаток дважды.
"""
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, selectinload

from ..models import ORDER_STATUSES, Notification, Order, SupplyItem

logger = logging.getLogger(__name__)

CANCELLED = "Отменён"

STATUS_TEXT = {
    "Подтверждён": "Ваш заказ подтверждён",
    "В работе": "Заказ собирается на складе",
    "Отправлен": "Заказ отправлен в доставку",
    "Выполнен": "Заказ выполнен. Спасибо за покупку!",
    CANCELLED: "Заказ отменён. Свяжитесь с менеджером.",
}


@dataclass
class StatusChange:
    old: str
    new: str
    changed: bool = False       # статус действительно сменился
    stock_note: str = ""        # что сделали со складом (для сообщения менеджеру)
    error: str = ""             # причина отказа (тогда ничего не менялось)


def _lock_supply_items(db: Session, order: Order) -> dict[int, SupplyItem]:
    ids = sorted({i.supply_item_id for i in order.items if i.supply_item_id})
    if not ids:
        return {}
    rows = (
        db.query(SupplyItem)
        .filter(SupplyItem.id.in_(ids))
        .order_by(SupplyItem.id)          # единый порядок блокировок
        .with_for_update()
        .all()
    )
    return {r.id: r for r in rows}


def _put_back_on_shelf(db: Session, si: SupplyItem) -> None:
    """Вернуть позицию в каталог, если её сняла сама система при распродаже.

    place_order выключает позицию, когда остаток дошёл до нуля. После
    возврата товара её нужно включить — но НЕ ту партию, которую заменила
    более свежая (иначе в каталоге появятся дубли одного товара).
    """
    if si.is_active or si.available_stock <= 0:
        return
    supply = si.supply
    if supply is None:
        return
    if supply.is_service:                    # партия 1С: is_active = (остаток > 0)
        si.is_active = True
        return
    if supply.status != "Разгружен":         # ещё не поступила — не трогаем
        return
    newer = (
        db.query(SupplyItem.id)
        .filter(SupplyItem.product_id == si.product_id,
                SupplyItem.is_active.is_(True),
                SupplyItem.id != si.id)
        .first()
    )
    if newer is None:
        si.is_active = True


def _return_stock(db: Session, order: Order) -> str:
    items = _lock_supply_items(db, order)
    returned = lost = 0
    for oi in order.items:
        si = items.get(oi.supply_item_id) if oi.supply_item_id else None
        qty = oi.quantity or 0
        if si is None:
            lost += qty                      # позицию склада уже удалили
            continue
        si.stock = (si.stock or 0) + qty
        returned += qty
    for si in items.values():
        _put_back_on_shelf(db, si)
    note = f"на склад возвращено {returned} упак."
    if lost:
        note += f" (ещё {lost} упак. вернуть некуда — позиция склада удалена)"
    return note


def _take_stock(db: Session, order: Order) -> tuple[str, str]:
    """Списать товар заказа заново. Возвращает (заметка, ошибка)."""
    items = _lock_supply_items(db, order)
    need: dict[int, int] = {}
    names: dict[int, str] = {}
    for oi in order.items:
        if oi.supply_item_id in items:
            need[oi.supply_item_id] = need.get(oi.supply_item_id, 0) + (oi.quantity or 0)
            names[oi.supply_item_id] = oi.product_name

    for sid, qty in need.items():
        have = items[sid].available_stock
        if have < qty:
            return "", (
                f"Нельзя вернуть заказ в работу: «{names[sid]}» — нужно "
                f"{qty} упак., а на складе доступно {have}."
            )
    taken = 0
    for sid, qty in need.items():
        si = items[sid]
        si.stock = (si.stock or 0) - qty
        taken += qty
        if si.available_stock <= 0:
            si.is_active = False
    return f"со склада снова списано {taken} упак.", ""


def apply_status_change(db: Session, order: Order, new_status: str) -> StatusChange:
    """Меняет статус и согласует склад. БЕЗ commit — это делает вызывающий код.

    Заказ (order.items должны быть доступны) вызывающий код должен получить
    запросом с .with_for_update(), чтобы смены статуса шли по очереди.
    """
    result = StatusChange(old=order.status or "", new=new_status)
    if new_status not in ORDER_STATUSES:
        result.error = "Неизвестный статус заказа"
        return result
    if order.status == new_status:
        return result                        # ничего не делаем

    was_cancelled = order.status == CANCELLED
    will_cancel = new_status == CANCELLED

    if will_cancel and not was_cancelled:
        result.stock_note = _return_stock(db, order)
    elif was_cancelled and not will_cancel:
        result.stock_note, result.error = _take_stock(db, order)
        if result.error:
            return result

    order.status = new_status
    result.changed = True
    db.flush()
    return result


def load_order_for_update(db: Session, order_id: int) -> Order | None:
    return (
        db.query(Order)
        .options(selectinload(Order.items))
        .filter(Order.id == order_id)
        .with_for_update(of=Order)
        .first()
    )


def notify_client_of_status(db: Session, order: Order, new_status: str,
                            background_tasks=None) -> None:
    """Уведомление в личном кабинете + письмо клиенту (в фоне). Делает commit."""
    if not order.user_id:
        return
    text = STATUS_TEXT.get(new_status, f"Статус заказа изменён на «{new_status}»")
    db.add(Notification(
        user_id=order.user_id,
        order_id=order.id,
        text=f"Заказ №{order.id}: {text}",
    ))
    db.commit()

    if background_tasks is None:
        return
    from .notifier import notify_client_status_changed
    loaded = (
        db.query(Order)
        .options(selectinload(Order.user), selectinload(Order.items))
        .filter(Order.id == order.id)
        .first()
    )
    if loaded:
        background_tasks.add_task(notify_client_status_changed, loaded, new_status)
