"""Удаляет ВСЕ поставки и их позиции.

Товары справочника, клиенты и заказы — НЕ трогаются.

Запуск:  python clear_supplies.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import OrderItem, Supply, SupplyItem  # noqa: E402


def clear_supplies() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   🗑  Очистка ПОСТАВОК                                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Все поставки удалены")
    print("  ✅ Все партии (позиции) удалены")
    print("  ✅ Позиции заказов отвязаны от supply_item")
    print()
    print("Что НЕ трогается:")
    print("  ✓ Товары справочника")
    print("  ✓ Заказы")
    print("  ✓ Клиенты")
    print()

    if input("Продолжить? [y/N]: ").strip().lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        n_ref = (db.query(OrderItem)
                 .filter(OrderItem.supply_item_id.isnot(None))
                 .update({OrderItem.supply_item_id: None},
                         synchronize_session=False))
        print(f"🔗 Обнулено supply_item в позициях заказов: {n_ref}")

        n_items = db.query(SupplyItem).delete(synchronize_session=False)
        print(f"🗑  Партий: {n_items}")

        n_supplies = db.query(Supply).delete(synchronize_session=False)
        print(f"🗑  Поставок: {n_supplies}")

        db.commit()
        print("\n✅ Поставки очищены.")
        print("   Товары, клиенты, заказы — на месте.")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_supplies()