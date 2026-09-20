"""ПОЛНАЯ очистка сайта с возвратом остатков.

Порядок:
  1. Возвращаем остатки из всех заказов
  2. Удаляем заказы, позиции, уведомления
  3. Удаляем клиентов (не админов)
  4. Удаляем товары и партии
  5. Удаляем поставки

Остаются: только админы.

Запуск:  python clear_all_safe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import (Notification, Order, OrderItem, Product,  # noqa: E402
                        Supply, SupplyItem, User)
from cleanup_utils import print_stock_report, return_stock_for_order_items  # noqa: E402


def clear_all_safe() -> None:
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   💥 ПОЛНАЯ ОЧИСТКА (с возвратом остатков)                ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("Что будет сделано:")
    print("  ✅ Остатки из заказов вернутся на склад")
    print("  ✅ Заказы, позиции, уведомления удалены")
    print("  ✅ Клиенты (не админы) удалены")
    print("  ✅ Товары и партии удалены")
    print("  ✅ Поставки удалены")
    print()
    print("Что ОСТАНЕТСЯ:")
    print("  ✓ Только админы")
    print()
    print("⚠️  ЭТО НЕ ОТКАТИТЬ!")
    print()

    if input("Точно продолжить? Введи YES: ").strip() != "YES":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # ── 1. Возврат остатков ──
        print("\n⏳ Возвращаем остатки на склад…")
        items = db.query(OrderItem).all()
        report = return_stock_for_order_items(db, items)
        print_stock_report(report)

        # ── 2. Удаляем уведомления ──
        print("\n⏳ Удаляем уведомления…")
        n_notif = db.query(Notification).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_notif}")

        # ── 3. Удаляем позиции заказов ──
        print("\n⏳ Удаляем позиции заказов…")
        n_items = db.query(OrderItem).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_items}")

        # ── 4. Удаляем заказы ──
        print("\n⏳ Удаляем заказы…")
        n_orders = db.query(Order).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_orders}")

        # ── 5. Удаляем клиентов ──
        print("\n⏳ Удаляем клиентов (не админов)…")
        n_clients = (db.query(User)
                     .filter(User.is_admin == False)  # noqa: E712
                     .delete(synchronize_session=False))
        print(f"   ✅ Удалено: {n_clients}")

        # ── 6. Удаляем партии ──
        print("\n⏳ Удаляем партии…")
        n_si = db.query(SupplyItem).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_si}")

        # ── 7. Удаляем товары ──
        print("\n⏳ Удаляем товары…")
        n_p = db.query(Product).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_p}")

        # ── 8. Удаляем поставки ──
        print("\n⏳ Удаляем поставки…")
        n_s = db.query(Supply).delete(synchronize_session=False)
        print(f"   ✅ Удалено: {n_s}")

        db.commit()

        print()
        print("╔══════════════════════════════════════════════════════════╗")
        print("║   ✅ ГОТОВО! Сайт пустой, админы на месте                 ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print()
        print(f"  • Возвращено на склад:  {report['returned_packs']} упаковок")
        print(f"  • Удалено заказов:      {n_orders}")
        print(f"  • Удалено клиентов:     {n_clients}")
        print(f"  • Удалено товаров:      {n_p}")
        print(f"  • Удалено поставок:     {n_s}")

    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        print("   Изменения откачены. Ничего не удалено.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_all_safe()