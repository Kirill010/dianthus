"""Полная очистка каталога. История заказов НЕ удаляется."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import (OrderItem, Product, Supply,  # noqa: E402
                        SupplyItem)


def clear_catalog() -> None:
    if input("⚠️  Удалить все товары и поставки? [y/N]: ").lower() != "y":
        print("Отменено.")
        return
    db = SessionLocal()
    try:
        n = db.query(OrderItem).filter(
            OrderItem.supply_item_id.isnot(None)
        ).update({OrderItem.supply_item_id: None}, synchronize_session=False)
        p = db.query(OrderItem).filter(
            OrderItem.product_id.isnot(None)
        ).update({OrderItem.product_id: None}, synchronize_session=False)
        print(f"🔗 Обнулено: {n} supply_item + {p} product")
        print(f"🗑  Партий: {db.query(SupplyItem).delete(synchronize_session=False)}")
        print(f"🗑  Поставок: {db.query(Supply).delete(synchronize_session=False)}")
        print(f"🗑  Товаров: {db.query(Product).delete(synchronize_session=False)}")
        db.commit()
        print("\n✅ Каталог очищен.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_catalog()