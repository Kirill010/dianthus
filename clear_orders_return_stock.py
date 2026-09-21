# Очистка ВСЕХ заказов с возвратом остатков на склад.

# Порядок действий:
#   1. Считаем, сколько упаковок надо вернуть (по каждой позиции заказа)
#   2. Возвращаем остатки в supply_items.stock
#   3. Удаляем уведомления, связанные с заказами
#   4. Удаляем позиции заказов (order_items)
#   5. Удаляем сами заказы (orders)

# Что НЕ трогает:
#   - Клиентов (users)
#   - Товары справочника (products)
#   - Поставки (supplies)

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal
from app.models import Notification, Order, OrderItem
from cleanup_utils import print_stock_report, return_stock_for_order_items


def clear_orders_return_stock() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   🔄 Очистка ЗАКАЗОВ + ВОЗВРАТ остатков на склад          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Остатки товаров вернутся на склад")
    print("  ✅ Заказы и их позиции будут удалены")
    print("  ✅ Уведомления о заказах будут удалены")
    print()
    print("Что НЕ трогается:")
    print("  ✓ Клиенты")
    print("  ✓ Товары в справочнике")
    print("  ✓ Поставки")
    print()

    db = SessionLocal()
    try:
        orders_count = db.query(Order).count()
        if orders_count == 0:
            print("ℹ️  Заказов нет — нечего очищать.")
            return

        items = db.query(OrderItem).all()
        print(f"📊 Найдено:")
        print(f"   • Заказов:          {orders_count}")
        print(f"   • Позиций в них:    {len(items)}")
        print()

        # Предварительный расчёт возврата (без изменения БД)
        preview = {}
        for it in items:
            if it.supply_item_id is None or (it.quantity or 0) <= 0:
                continue
            preview[it.supply_item_id] = (
                preview.get(it.supply_item_id, 0) + it.quantity
            )
        total_packs = sum(preview.values())
        print(f"🔄 Вернётся на склад: {total_packs} упаковок")
        print()

        if input("Продолжить? [y/N]: ").strip().lower() != "y":
            print("Отменено.")
            return

        # 1. Возвращаем остатки
        print("\n⏳ Возвращаем остатки на склад")
        report = return_stock_for_order_items(db, items)
        print_stock_report(report)

        # 2. Удаляем уведомления по заказам
        print("\n⏳ Удаляем уведомления о заказах...")
        n_notif = (db.query(Notification)
                   .filter(Notification.order_id.isnot(None))
                   .delete(synchronize_session=False))
        print(f"   ✅ Удалено уведомлений: {n_notif}")

        # 3. Удаляем позиции заказов
        print("\n⏳ Удаляем позиции заказов...")
        n_items = db.query(OrderItem).delete(synchronize_session=False)
        print(f"   ✅ Удалено позиций: {n_items}")

        # 4. Удаляем сами заказы
        print("\n⏳ Удаляем заказы...")
        n_orders = db.query(Order).delete(synchronize_session=False)
        print(f"   ✅ Удалено заказов: {n_orders}")

        db.commit()

        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║   ✅ ГОТОВО!                                             ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()
        print(f"  • Возвращено на склад:  {report['returned_packs']} упаковок")
        print(f"  • Удалено заказов:      {n_orders}")
        print(f"  • Удалено позиций:      {n_items}")
        print(f"  • Удалено уведомлений:  {n_notif}")
        print()
        print("Проверь в админке:")
        print("  → вкладка «Заказы» — должно быть 0")
        print("  → вкладка «На складе» — остатки вернулись")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        print("   Изменения откачены. Ничего не удалено.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_orders_return_stock()