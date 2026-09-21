# Лёгкая авто-миграция: добавляет недостающие колонки, индексы и таблицы.
import logging

from sqlalchemy import inspect, text

from .database import engine

logger = logging.getLogger(__name__)

EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "products": {
        "name":         "VARCHAR(200)",
        "description":  "TEXT DEFAULT ''",
        "country":      "VARCHAR(100) DEFAULT ''",
        "length_cm":    "INTEGER DEFAULT 0",
        "unit":         "VARCHAR(30) DEFAULT 'упаковка'",
        "package_size": "INTEGER DEFAULT 1",
        "min_quantity": "INTEGER DEFAULT 1",
        "image_url":    "VARCHAR(500) DEFAULT ''",
        "category":     "VARCHAR(100) DEFAULT 'Прочее'",
    },
    "users": {
        "phone":            "VARCHAR(30) DEFAULT ''",
        "company_name":     "VARCHAR(200) DEFAULT ''",
        "inn":              "VARCHAR(12) DEFAULT ''",
        "city":             "VARCHAR(100) DEFAULT ''",
        "is_admin":         "BOOLEAN DEFAULT FALSE",
        "is_approved":      "BOOLEAN DEFAULT FALSE",
        "discount_percent": "FLOAT DEFAULT 0",       
    },
    "supplies": {
        "status": "VARCHAR(30) DEFAULT 'Ожидается'",
        "notes":  "TEXT DEFAULT ''",
    },
    "supply_items": {
        "is_active": "BOOLEAN DEFAULT FALSE",
    },
    "orders": {
        "status":           "VARCHAR(30) DEFAULT 'Новый'",
        "comment":          "TEXT DEFAULT ''",
        "subtotal":         "FLOAT DEFAULT 0",       
        "discount_percent": "FLOAT DEFAULT 0",       
    },
    "order_items": {
        "package_size": "INTEGER DEFAULT 1",         
    },
}

EXPECTED_INDEXES: dict[str, list[tuple[str, str]]] = {
    "products": [("ix_products_category", "category")],
}


def ensure_notifications_table() -> None:
    try:
        insp = inspect(engine)
        if "notifications" not in insp.get_table_names():
            from .models import Notification
            Notification.__table__.create(bind=engine, checkfirst=True)
            logger.info("🔧 Создана таблица notifications")
    except Exception as e:
        logger.error("Не удалось создать таблицу notifications: %s", e)


def _backfill_order_subtotals() -> None:
    # Заполняет subtotal для старых заказов (subtotal = total_price).
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE orders SET subtotal = total_price "
                "WHERE subtotal = 0 AND total_price > 0"
            ))
    except Exception as e:
        logger.warning("Backfill orders.subtotal: %s", e)


def _backfill_order_item_pack_sizes() -> None:
    # Заполняет package_size в order_items для старых заказов.
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE order_items oi
                SET package_size = COALESCE(p.package_size, 1)
                FROM products p
                WHERE oi.product_id = p.id AND oi.package_size = 1
            """))
    except Exception as e:
        logger.warning("Backfill order_items.package_size: %s", e)


def auto_migrate() -> None:
    try:
        insp = inspect(engine)
    except Exception as e:
        logger.error("Не удалось получить схему БД: %s", e)
        return
    tables = set(insp.get_table_names())
    added = 0
    with engine.begin() as conn:
        for table, columns in EXPECTED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in columns.items():
                if col in existing:
                    continue
                try:
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'
                    ))
                    logger.info("🔧 Добавлена колонка %s.%s", table, col)
                    added += 1
                except Exception as e:
                    logger.warning("Не удалось добавить %s.%s: %s",
                                   table, col, e)
        for table, indexes in EXPECTED_INDEXES.items():
            if table not in tables:
                continue
            existing_idx = {i["name"] for i in insp.get_indexes(table)}
            for idx_name, column in indexes:
                if idx_name in existing_idx:
                    continue
                try:
                    conn.execute(text(
                        f"CREATE INDEX IF NOT EXISTS {idx_name} "
                        f"ON {table} ({column})"
                    ))
                    logger.info("🔧 Создан индекс %s", idx_name)
                    added += 1
                except Exception as e:
                    logger.warning("Индекс %s: %s", idx_name, e)

    _backfill_order_subtotals()
    _backfill_order_item_pack_sizes()

    if added:
        logger.info("✅ Авто-миграция: +%d изменений", added)
    else:
        logger.info("✅ Схема БД актуальна")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    auto_migrate()