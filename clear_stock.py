"""Снимает ВСЕ активные позиции с полок (вкладка «На складе»).
У всех SupplyItem ставится is_active=False и stock=0.
Товары, поставки, заказы и клиенты — НЕ удаляются.
После этого каталог у клиентов будет пустым.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import SupplyItem  # noqa: E402


def clear_stock() -> None:
    print("⚠️  Снять ВСЕ товары с полок (обнулить активные позиции).")
    print("   Товары, поставки, заказы и клиенты — НЕ удаляются.")
    print("   Каталог у клиентов станет пустым.")
    if input("Продолжить? [y/N]: ").lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        n = (db.query(SupplyItem)
             .filter(SupplyItem.is_active == True)  # noqa: E712
             .update(
                 {SupplyItem.is_active: False, SupplyItem.stock: 0},
                 synchronize_session=False,
             ))
        db.commit()
        print(f"🗑  Снято с полок: {n} позиций.")
        print("\n✅ Каталог пуст. Товары и поставки на месте.")
        print("   Чтобы вернуть товары в каталог — разгрузите нужную поставку.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_stock()