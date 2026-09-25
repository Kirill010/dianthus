# app/migrations.py (полный код)
"""Лёгкая авто-миграция: добавляет недостающие колонки, индексы и таблицы."""
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
        "notes": "TEXT DEFAULT ''",
        "is_service": "BOOLEAN DEFAULT FALSE NOT NULL",
    },
    "supply_items": {
        "is_active":      "BOOLEAN DEFAULT FALSE",
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

# ✅ РАСШИРЕННЫЙ набор индексов для критичных полей
EXPECTED_INDEXES: dict[str, list[tuple[str, str]]] = {
    "products": [
        ("ix_products_category", "category"),
        ("ix_products_sku", "sku"),
        ("ix_products_country", "country"),
    ],
    "orders": [
        ("ix_orders_created_at", "created_at"),
        ("ix_orders_status", "status"),
        ("ix_orders_user_created", "user_id, created_at"),
    ],
    "supply_items": [
        ("ix_supply_items_supply_product", "supply_id, product_id"),
        ("ix_supply_items_active", "is_active"),
        ("ix_supply_items_active_stock", "is_active, stock"),
    ],
    "users": [
        ("ix_users_email", "email"),
        ("ix_users_created_at", "created_at"),
        ("ix_users_approved", "is_approved"),
    ],
    "notifications": [
        ("ix_notifications_user_unread", "user_id, is_read"),
        ("ix_notifications_created", "created_at"),
    ],
    "preorders": [
        ("ix_preorders_user_fulfilled", "user_id, is_fulfilled"),
        ("ix_preorders_product", "product_id"),
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
    """Проверяет, выполнен ли бэкфилл с данным флагом."""
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
                return False
            conn.execute(text(
                "INSERT INTO _migrations (name) VALUES (:n)"
            ), {"n": flag})
        return True
    except Exception as e:
        logger.warning("_backfill_once(%s): %s", flag, e)
        return False


def _backfill_order_item_pack_sizes() -> None:
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

def _exec_one(sql: str, label: str) -> bool:
    """
    fix #68: выполняет ОДИН DDL/DML в ОТДЕЛЬНОЙ транзакции.
    
    Безопасно при `--workers 2`: если другой воркер уже создал этот
    индекс/колонку — получим ошибку и просто пропустим её.
    НЕ портит соседние операции (в PostgreSQL ошибка в одной
    транзакции отменяет всю транзакцию до конца).
    """
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("🔧 %s", label)
        return True
    except Exception as e:
        # Не логируем весь трейсбек — это ожидаемая гонка воркеров
        msg = str(e).split("\n")[0][:200]
        logger.debug("Авто-миграция: %s — %s", label, msg)
        return False


def auto_migrate() -> None:
    try:
        insp = inspect(engine)
    except Exception as e:
        logger.error("Не удалось получить схему БД: %s", e)
        return

    tables = set(insp.get_table_names())
    dialect = engine.dialect.name
    logger.info("🔍 Диалект БД: %s", dialect)

    added = 0

    # ── 1. Колонки ──
    for table, columns in EXPECTED_COLUMNS.items():
        if table not in tables:
            continue
        try:
            existing = {c["name"] for c in insp.get_columns(table)}
        except Exception as e:
            logger.warning("Не удалось прочитать колонки %s: %s", table, e)
            continue

        for col, sqlite_ddl in columns.items():
            if col in existing:
                continue
            ddl = _column_ddl(table, col, sqlite_ddl)
            sql = f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'
            if _exec_one(sql, f"Добавлена колонка {table}.{col}"):
                added += 1

    # ── 2. Индексы (fix #68: КАЖДЫЙ в своей транзакции) ──
    for table, indexes in EXPECTED_INDEXES.items():
        if table not in tables:
            continue
        try:
            existing_idx = {i["name"] for i in insp.get_indexes(table)}
        except Exception as e:
            logger.warning("Не удалось прочитать индексы %s: %s", table, e)
            continue

        for idx_name, columns in indexes:
            if idx_name in existing_idx:
                continue
            sql = (
                f"CREATE INDEX IF NOT EXISTS {idx_name} "
                f"ON {table} ({columns})"
            )
            if _exec_one(sql, f"Создан индекс {idx_name} ON {table}"):
                added += 1

    # ── 3. Одноразовые бэкфиллы ──
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