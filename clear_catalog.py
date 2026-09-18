"""
Полная очистка каталога: товары + поставки + партии.
История заказов НЕ удаляется — только обнуляются ссылки на товары.

Запуск:
    python clear_catalog.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import (OrderItem, Product, Supply,  # noqa: E402
                        SupplyItem)


def clear_catalog() -> None:
    confirm = input(
        "⚠️  Удалить ВСЕ товары, поставки и партии?\n"
        "   История заказов останется, но товары в ней станут «архивными».\n"
        "   Продолжить? [y/N]: "
    )
    if confirm.lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # 1. Обнуляем FK в OrderItem, чтобы не нарушать целостность
        n_items = db.query(OrderItem).filter(
            OrderItem.supply_item_id.isnot(None)
        ).update({OrderItem.supply_item_id: None},
                 synchronize_session=False)
        n_prod = db.query(OrderItem).filter(
            OrderItem.product_id.isnot(None)
        ).update({OrderItem.product_id: None},
                 synchronize_session=False)
        print(f"🔗 Обнулено ссылок в заказах: "
              f"{n_items} supply_item + {n_prod} product")

        # 2. Удаляем партии поставок
        n_si = db.query(SupplyItem).delete(synchronize_session=False)
        print(f"🗑  Удалено партий (SupplyItem): {n_si}")

        # 3. Удаляем поставки
        n_sup = db.query(Supply).delete(synchronize_session=False)
        print(f"🗑  Удалено поставок (Supply): {n_sup}")

        # 4. Удаляем товары из справочника
        n_p = db.query(Product).delete(synchronize_session=False)
        print(f"🗑  Удалено товаров (Product): {n_p}")

        db.commit()
        print("\n✅ Каталог полностью очищен. БД готова к новым данным.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_catalog()