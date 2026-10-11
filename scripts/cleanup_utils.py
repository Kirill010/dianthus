# Общие утилиты для скриптов очистки.

# Главная задача — возврат остатков на склад перед удалением заказов.

import sys
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy.orm import Session

from app.models import OrderItem, SupplyItem


def return_stock_for_order_items(
    db: Session, items: Iterable[OrderItem]
) -> dict:
    """
    Возвращает остатки на склад для переданных позиций заказов.

    Возвращает словарь-отчёт:
        {
            "returned_packs": int,       # сколько упаковок вернули
            "supply_items_touched": int, # сколько позиций склада затронули
            "without_supply_link": int,  # позиции без связи со складом
            "supply_item_missing": int,  # позиции, где сама позиция склада удалена
        }
    """
    # Считаем, сколько упаковок надо вернуть на каждую позицию склада
    to_return: dict[int, int] = {}
    without_link = 0

    for item in items:
        if item.supply_item_id is None:
            without_link += 1
            continue
        packs = item.quantity or 0
        if packs <= 0:
            continue
        to_return[item.supply_item_id] = (
            to_return.get(item.supply_item_id, 0) + packs
        )

    # Возвращаем остатки
    returned = 0
    touched = 0
    missing = 0

    for supply_item_id, packs in to_return.items():
        si = db.query(SupplyItem).filter(
            SupplyItem.id == supply_item_id
        ).first()
        if si is None:
            missing += 1
            continue
        si.stock = (si.stock or 0) + packs
        returned += packs
        touched += 1

    return {
        "returned_packs": returned,
        "supply_items_touched": touched,
        "without_supply_link": without_link,
        "supply_item_missing": missing,
    }


def print_stock_report(report: dict) -> None:
    # Красиво печатает отчёт о возврате остатков.
    print(f"   ✅ Возвращено на склад:  {report['returned_packs']} упаковок")
    print(f"   ✅ Затронуто позиций:    {report['supply_items_touched']}")
    if report["without_supply_link"]:
        print(f"   ⚠️  Без связи со складом: {report['without_supply_link']} "
              f"(эти остатки не вернуть — supply_item был удалён)")
    if report["supply_item_missing"]:
        print(f"   ⚠️  Позиция склада удалена: {report['supply_item_missing']} "
              f"(вернуть некуда)")