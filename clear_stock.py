"""Снимает ВСЕ активные позиции с полок.

У всех SupplyItem: is_active = False, stock = 0.
После этого каталог у клиентов пуст.

Заказы, поставки, справочник, клиенты — НЕ трогаются.
Чтобы вернуть товары — снова разгрузите нужную поставку.

Запуск:  python clear_stock.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import SupplyItem  # noqa: E402


def clear_stock() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   🧹 Снять ВСЁ с ПОЛОК                                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Все активные позиции сняты с полок (is_active=False)")
    print("  ✅ Остатки обнулены (stock=0)")
    print()
    print("Что НЕ трогается:")
    print("  ✓ Товары справочника")
    print("  ✓ Поставки (сами рейсы)")
    print("  ✓ Заказы")
    print("  ✓ Клиенты")
    print()
    print("ℹ️  Вернуть товары можно разгрузкой нужной поставки.")
    print()

    if input("Продолжить? [y/N]: ").strip().lower() != "y":
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

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_stock()