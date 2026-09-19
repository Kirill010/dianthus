"""Удаляет всех клиентов (не админов), их заказы и уведомления.
Админы сохраняются. Каталог/товары/поставки НЕ трогаются."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import Notification, Order, OrderItem, User  # noqa: E402


def clear_clients(keep_admins: bool = True) -> None:
    print("⚠️  Это удалит ВСЕХ клиентов (не админов), их заказы и уведомления.")
    print("   Каталог, товары, поставки — НЕ трогаются.")
    if input("Продолжить? [y/N]: ").lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # Находим всех клиентов (не админов)
        q = db.query(User)
        if keep_admins:
            q = q.filter(User.is_admin == False)  # noqa: E712
        clients = q.all()

        if not clients:
            print("ℹ️  Клиентов нет — нечего удалять.")
            return

        client_ids = [u.id for u in clients]
        print(f"\n🔍 Найдено клиентов: {len(client_ids)}")

        # 1. Удаляем уведомления
        n_notif = (db.query(Notification)
                   .filter(Notification.user_id.in_(client_ids))
                   .delete(synchronize_session=False))
        print(f"🗑  Уведомлений: {n_notif}")

        # 2. Находим заказы клиентов
        order_ids = [o.id for o in
                     db.query(Order.id)
                     .filter(Order.user_id.in_(client_ids))
                     .all()]

        # 3. Удаляем позиции заказов
        n_items = 0
        if order_ids:
            n_items = (db.query(OrderItem)
                       .filter(OrderItem.order_id.in_(order_ids))
                       .delete(synchronize_session=False))
        print(f"🗑  Позиций заказов: {n_items}")

        # 4. Удаляем заказы
        n_orders = 0
        if order_ids:
            n_orders = (db.query(Order)
                        .filter(Order.id.in_(order_ids))
                        .delete(synchronize_session=False))
        print(f"🗑  Заказов: {n_orders}")

        # 5. Удаляем самих клиентов
        n_users = (db.query(User)
                   .filter(User.id.in_(client_ids))
                   .delete(synchronize_session=False))
        print(f"🗑  Клиентов: {n_users}")

        db.commit()
        print("\n✅ Клиенты сброшены.")
        print("   Админы, каталог, поставки — на месте.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_clients()