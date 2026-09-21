# Удаляет ВСЕ товары из справочника.

# Важно: удаляет и партии товаров в поставках (т.к. FK).
# История заказов СОХРАНЯЕТСЯ — в OrderItem остаётся снимок product_name и price.
# Поставки (сами рейсы), клиенты, сами заказы — НЕ трогаются.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal
from app.models import OrderItem, Product, SupplyItem


def clear_products() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   🗑  Очистка СПРАВОЧНИКА ТОВАРОВ                          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Все товары из справочника удалены")
    print("  ✅ Все партии этих товаров в поставках удалены")
    print("  ✅ Позиции заказов отвязаны (история сохранится)")
    print()
    print("Что НЕ трогается:")
    print("  ✓ Поставки (сами рейсы)")
    print("  ✓ Заказы (как факт)")
    print("  ✓ Клиенты")
    print()
    print("⚠️  ВНИМАНИЕ: партии удаляются вместе с остатками — "
          "это не откатить.")
    print()

    if input("Продолжить? [y/N]: ").strip().lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # 1. Обнуляем FK в позициях заказов (сохраняем историю)
        n_si = (db.query(OrderItem)
                .filter(OrderItem.supply_item_id.isnot(None))
                .update({OrderItem.supply_item_id: None},
                        synchronize_session=False))
        n_p = (db.query(OrderItem)
               .filter(OrderItem.product_id.isnot(None))
               .update({OrderItem.product_id: None},
                       synchronize_session=False))
        print(f"🔗 Обнулено FK в позициях заказов: "
              f"supply_item={n_si}, product={n_p}")

        # 2. Удаляем партии
        n_items = db.query(SupplyItem).delete(synchronize_session=False)
        print(f"🗑  Партий в поставках: {n_items}")

        # 3. Удаляем товары
        n_products = db.query(Product).delete(synchronize_session=False)
        print(f"🗑  Товаров: {n_products}")

        db.commit()
        print("\n✅ Справочник очищен.")
        print("   Поставки, клиенты, заказы — на месте.")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_products()