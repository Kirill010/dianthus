"""Удаляет ВСЕ товары из справочника (вкладка «Справочник»).
Партии в поставках, привязанные к этим товарам, тоже удаляются.
История заказов СОХРАНЯЕТСЯ — в OrderItem остаётся снимок product_name и price.
Поставки, клиенты, сами заказы — НЕ трогаются.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import OrderItem, Product, SupplyItem  # noqa: E402


def clear_products() -> None:
    print("⚠️  Это удалит ВСЕ товары из справочника.")
    print("   Партии этих товаров в поставках тоже будут удалены.")
    print("   История заказов СОХРАНИТСЯ (product_name остаётся как текст).")
    print("   Поставки (сами рейсы), клиенты и заказы — НЕ трогаются.")
    if input("Продолжить? [y/N]: ").lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # 1. Обнуляем FK в позициях заказов, чтобы не потерять историю
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

        # 2. Удаляем партии товаров
        n_items = db.query(SupplyItem).delete(synchronize_session=False)
        print(f"🗑  Партий в поставках: {n_items}")

        # 3. Удаляем сами товары
        n_products = db.query(Product).delete(synchronize_session=False)
        print(f"🗑  Товаров: {n_products}")

        db.commit()
        print("\n✅ Справочник товаров очищен.")
        print("   Поставки, клиенты, заказы — на месте.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_products()