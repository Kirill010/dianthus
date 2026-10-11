# Очистка клиентов (не админов) с ВОЗВРАТОМ остатков их заказов.

# Порядок действий:
#   1. Находим всех клиентов (is_admin = False)
#   2. Для их заказов возвращаем остатки на склад
#   3. Удаляем уведомления клиентов
#   4. Удаляем позиции их заказов
#   5. Удаляем их заказы
#   6. Удаляем самих клиентов

# Что НЕ трогает:
#   - Админов (is_admin = True)
#   - Товары справочника
#   - Поставки

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal
from app.models import (Notification, Order, OrderItem, User)
from cleanup_utils import print_stock_report, return_stock_for_order_items


def clear_clients_return_stock(keep_admins: bool = True) -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   🔄 Очистка КЛИЕНТОВ + ВОЗВРАТ остатков их заказов       ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Остатки из заказов клиентов вернутся на склад")
    print("  ✅ Клиенты, их заказы и уведомления будут удалены")
    print()
    print("Что НЕ трогается:")
    print("  ✓ Админы")
    print("  ✓ Товары в справочнике")
    print("  ✓ Поставки")
    print()

    db = SessionLocal()
    try:
        q = db.query(User)
        if keep_admins:
            q = q.filter(User.is_admin == False)
        clients = q.all()

        if not clients:
            print("ℹ️  Клиентов нет — нечего очищать.")
            return

        client_ids = [u.id for u in clients]
        print(f"🔍 Найдено клиентов: {len(client_ids)}")

        # Заказы этих клиентов
        orders = (db.query(Order)
                  .filter(Order.user_id.in_(client_ids))
                  .all())
        order_ids = [o.id for o in orders]
        print(f"📦 Заказов у них: {len(order_ids)}")

        # Позиции этих заказов
        items = []
        if order_ids:
            items = (db.query(OrderItem)
                     .filter(OrderItem.order_id.in_(order_ids))
                     .all())
        print(f"📋 Позиций в заказах: {len(items)}")
        print()

        # Предварительный расчёт возврата
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

        # ── 1. Возвращаем остатки ──
        print("\n⏳ Возвращаем остатки на склад…")
        report = return_stock_for_order_items(db, items)
        print_stock_report(report)

        # ── 2. Удаляем уведомления ──
        print("\n⏳ Удаляем уведомления клиентов…")
        n_notif = (db.query(Notification)
                   .filter(Notification.user_id.in_(client_ids))
                   .delete(synchronize_session=False))
        print(f"   ✅ Удалено уведомлений: {n_notif}")

        # ── 3. Удаляем позиции заказов ──
        print("\n⏳ Удаляем позиции заказов…")
        n_items = 0
        if order_ids:
            n_items = (db.query(OrderItem)
                       .filter(OrderItem.order_id.in_(order_ids))
                       .delete(synchronize_session=False))
        print(f"   ✅ Удалено позиций: {n_items}")

        # ── 4. Удаляем заказы ──
        print("\n⏳ Удаляем заказы клиентов…")
        n_orders = 0
        if order_ids:
            n_orders = (db.query(Order)
                        .filter(Order.id.in_(order_ids))
                        .delete(synchronize_session=False))
        print(f"   ✅ Удалено заказов: {n_orders}")

        # ── 5. Удаляем самих клиентов ──
        print("\n⏳ Удаляем клиентов…")
        n_users = (db.query(User)
                   .filter(User.id.in_(client_ids))
                   .delete(synchronize_session=False))
        print(f"   ✅ Удалено клиентов: {n_users}")

        db.commit()

        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║   ✅ ГОТОВО!                                             ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()
        print(f"  • Возвращено на склад:  {report['returned_packs']} упаковок")
        print(f"  • Удалено клиентов:     {n_users}")
        print(f"  • Удалено заказов:      {n_orders}")
        print(f"  • Удалено позиций:      {n_items}")
        print(f"  • Удалено уведомлений:  {n_notif}")
        print()
        print("  Админы, товары, поставки — на месте.")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        print("   Изменения откачены. Ничего не удалено.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_clients_return_stock()