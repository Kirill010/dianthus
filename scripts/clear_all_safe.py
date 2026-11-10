# Очистка торговых данных: заказы, клиенты, товары и поставки.
# Сохраняются администраторы, справочники, ключи, настройки, юридические
# документы и журналы; пользовательские ссылки в сохранённых записях обнуляются.

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))          # scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # корень проекта (пакет app)
import _guard  # noqa: E402,F401 — в ENV=prod отказывается работать без явного подтверждения

from app.database import SessionLocal
from app.models import (Notification, Order, OrderItem, Product,
                        Supply, SupplyItem, User)
from cleanup_utils import (detach_orders_from_claims, drop_preorders,
                           print_stock_report, purge_users,
                           return_stock_for_order_items)


def clear_all_data(db) -> dict:
    """Очищает торговые данные в одной транзакции и возвращает счётчики."""
    try:
        items = (db.query(OrderItem).join(Order)
                 .filter(Order.status != "Отменён").all())
        stock = return_stock_for_order_items(db, items)
        notifications = db.query(Notification).delete(synchronize_session=False)
        detach_orders_from_claims(db)
        preorders = drop_preorders(db)
        order_items = db.query(OrderItem).delete(synchronize_session=False)
        orders = db.query(Order).delete(synchronize_session=False)

        client_ids = [row[0] for row in db.query(User.id).filter(User.is_admin.is_(False)).all()]
        purge_users(db, client_ids)
        db.flush()
        clients = (db.query(User).filter(User.is_admin.is_(False))
                   .delete(synchronize_session=False))

        supply_items = db.query(SupplyItem).delete(synchronize_session=False)
        products = db.query(Product).delete(synchronize_session=False)
        supplies = db.query(Supply).delete(synchronize_session=False)
        db.commit()
        return {
            "stock": stock,
            "notifications": notifications,
            "preorders": preorders,
            "order_items": order_items,
            "orders": orders,
            "clients": clients,
            "supply_items": supply_items,
            "products": products,
            "supplies": supplies,
        }
    except Exception:
        db.rollback()
        raise


def clear_all_safe() -> None:
    print("ПОЛНАЯ ОЧИСТКА ТОРГОВЫХ ДАННЫХ")
    print("Будут удалены: заказы, предзаказы, рекламации клиентов, уведомления,")
    print("клиенты, товары и поставки.")
    print("Сохранятся: администраторы, справочники, ключи, настройки,")
    print("юридические документы и журналы (без ссылок на удалённых клиентов).")
    print("ЭТО НЕ ОТКАТИТЬ. Сначала сделайте резервную копию: bash scripts/backup.sh")
    print()

    if input("Точно продолжить? Введите YES: ").strip() != "YES":
        print("Отменено.")
        return

    db = SessionLocal()
    try:
        result = clear_all_data(db)
        print_stock_report(result["stock"])
        print("Готово. Торговые данные удалены, администраторы сохранены.")
        for key, label in (
                ("orders", "заказов"), ("clients", "клиентов"),
                ("products", "товаров"), ("supplies", "поставок")):
            print(f"  Удалено {label}: {result[key]}")
    except Exception as e:
        print(f"Ошибка: {e}")
        print("Изменения откачены. Ничего не удалено.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_all_safe()
