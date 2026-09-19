"""Очистка заказов. Клиенты, каталог и поставки НЕ удаляются."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal  # noqa: E402
from app.models import Notification, Order, OrderItem  # noqa: E402


def clear_orders() -> None:
    print("⚠️  Это удалит ВСЕ заказы, их позиции и уведомления, связанные с заказами.")
    print("   Клиенты, каталог и поставки — НЕ трогаются.")
    if input("Продолжить? [y/N]: ").lower() != "y":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        # 1. Удаляем уведомления, связанные с заказами
        n_notif = (db.query(Notification)
                   .filter(Notification.order_id.isnot(None))
                   .delete(synchronize_session=False))
        print(f"🗑  Уведомлений, связанных с заказами: {n_notif}")

        # 2. Удаляем позиции заказов
        n_items = db.query(OrderItem).delete(synchronize_session=False)
        print(f"🗑  Позиций заказов: {n_items}")

        # 3. Удаляем сами заказы
        n_orders = db.query(Order).delete(synchronize_session=False)
        print(f"🗑  Заказов: {n_orders}")

        db.commit()
        print("\n✅ Заказы очищены.")
        print("   Клиенты, каталог, поставки — на месте.")
    except Exception as e:
        db.rollback()
        print(f"\n❌ Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_orders()