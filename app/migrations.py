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
        "sku":          "VARCHAR(100) DEFAULT ''",
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
        "is_active":      "BOOLEAN DEFAULT FALSE",
        # ✅ НОВОЕ ПОЛЕ
        "reserved_stock": "INTEGER DEFAULT 0 NOT NULL",
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
    "preorders": {
        "product_id":     "INTEGER",
        "supply_item_id": "INTEGER",
        "is_fulfilled":   "BOOLEAN DEFAULT FALSE",
        "quantity":       "INTEGER DEFAULT 0",
    },
}

EXPECTED_INDEXES: dict[str, list[tuple[str, str]]] = {
    "products": [
        ("ix_products_category", "category"),
        ("ix_products_sku", "sku"),
    ],
}


def _column_ddl(table: str, column: str, sqlite_ddl: str) -> str:
    dialect = engine.dialect.name
    if table == "products" and column == "photos":
        if dialect == "postgresql":
            return "JSONB DEFAULT '[]'::jsonb"
        return "TEXT DEFAULT '[]'"
    return sqlite_ddl


def ensure_notifications_table() -> None:
    try:
        insp = inspect(engine)
        if "notifications" not in insp.get_table_names():
            from .models import Notification
            Notification.__table__.create(bind=engine, checkfirst=True)
            logger.info("🔧 Создана таблица notifications")
    except Exception as e:
        logger.error("Не удалось создать таблицу notifications: %s", e)


# ═══════════════════════════════════════════════════════════
# ОДНОРАЗОВЫЕ BACKFILL-функции
# ═══════════════════════════════════════════════════════════

def _backfill_once(flag: str) -> bool:
    """
    Проверяет, выполнен ли бэкфилл с данным флагом.
    Использует таблицу _migrations (создаётся при первом вызове).
    """
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS _migrations (
                    name VARCHAR(100) PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """))
            row = conn.execute(text(
                "SELECT 1 FROM _migrations WHERE name = :n"
            ), {"n": flag}).first()
            if row:
                return False  # уже применён
            conn.execute(text(
                "INSERT INTO _migrations (name) VALUES (:n)"
            ), {"n": flag})
        return True
    except Exception as e:
        logger.warning("_backfill_once(%s): %s", flag, e)
        return False


def _backfill_order_item_pack_sizes() -> None:
    """Одноразовый бэкфилл: восстанавливает package_size у старых заказов."""
    if not _backfill_once("order_item_pack_sizes_v1"):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE order_items
                SET package_size = COALESCE(
                    (SELECT p.package_size FROM products p
                     WHERE p.id = order_items.product_id),
                    1
                )
                WHERE package_size = 1
            """))
        logger.info("✅ Backfill order_items.package_size выполнен")
    except Exception as e:
        logger.warning("Backfill order_items.package_size: %s", e)


def _backfill_product_photos() -> None:
    if not _backfill_once("product_photos_v1"):
        return
    try:
        dialect = engine.dialect.name
        with engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text("""
                    UPDATE products
                    SET photos = to_jsonb(ARRAY[image_url])
                    WHERE (photos IS NULL
                           OR photos::text = '[]'
                           OR photos::text = 'null')
                      AND image_url IS NOT NULL
                      AND image_url != ''
                """))
            else:
                conn.execute(text("""
                    UPDATE products
                    SET photos = '["' || image_url || '"]'
                    WHERE (photos IS NULL OR photos = '[]' OR photos = '')
                      AND image_url IS NOT NULL AND image_url != ''
                """))
        logger.info("✅ Backfill products.photos выполнен")
    except Exception as e:
        logger.warning("Backfill products.photos: %s", e)


def _backfill_order_subtotals() -> None:
    if not _backfill_once("order_subtotals_v1"):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE orders SET subtotal = total_price "
                "WHERE subtotal = 0 AND total_price > 0"
            ))
            conn.execute(text(
                "UPDATE orders SET total_price = subtotal "
                "WHERE (total_price IS NULL OR total_price = 0) "
                "AND subtotal > 0"
            ))
        logger.info("✅ Backfill orders.subtotal/total_price выполнен")
    except Exception as e:
        logger.warning("Backfill orders: %s", e)


# ═══════════════════════════════════════════════════════════
# ОСНОВНАЯ ФУНКЦИЯ АВТО-МИГРАЦИИ
# ═══════════════════════════════════════════════════════════

def auto_migrate() -> None:
    try:
        insp = inspect(engine)
    except Exception as e:
        logger.error("Не удалось получить схему БД: %s", e)
        return

    tables = set(insp.get_table_names())
    added = 0
    dialect = engine.dialect.name
    logger.info("🔍 Диалект БД: %s", dialect)

    with engine.begin() as conn:
        for table, columns in EXPECTED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, sqlite_ddl in columns.items():
                if col in existing:
                    continue
                ddl = _column_ddl(table, col, sqlite_ddl)
                try:
                    conn.execute(text(
                        f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'
                    ))
                    logger.info("🔧 Добавлена колонка %s.%s (%s)",
                                table, col, ddl)
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

    # Одноразовые бэкфиллы
    _backfill_order_item_pack_sizes()
    _backfill_product_photos()
    _backfill_order_subtotals()

    if added:
        logger.info("✅ Авто-миграция: +%d изменений", added)
    else:
        logger.info("✅ Схема БД актуальна")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    auto_migrate()