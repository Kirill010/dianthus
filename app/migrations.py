# app/migrations.py
"""Лёгкая авто-миграция.

ИСПРАВЛЕНИЯ:
  * файловый lock — миграцию выполняет ТОЛЬКО один воркер
  * движок берётся через current_bind() — актуальный bind SessionLocal
    (в тестах подменяется через SessionLocal.configure(bind=...))
  * engine.begin() автоматически откатывает транзакцию (фикс InFailedSqlTransaction)
  * ON CONFLICT DO NOTHING в _backfill_once (защита от гонки)
  * перед созданием каждого индекса — перечитываем состояние БД
"""
import logging

from sqlalchemy import inspect, text

from .database import Base, current_bind
from .locks import file_lock, lock_path

logger = logging.getLogger(__name__)

MIGRATION_LOCK = lock_path("MIGRATION_LOCK", "migration.lock")


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
        "external_id":  "VARCHAR(64) DEFAULT ''",
        # Для PostgreSQL _column_ddl() заменит тип на JSONB.
        "photos":       "TEXT DEFAULT '[]'",
    },
    "users": {
        "phone":            "VARCHAR(30) DEFAULT ''",
        "company_name":     "VARCHAR(200) DEFAULT ''",
        "inn":              "VARCHAR(12) DEFAULT ''",
        "city":             "VARCHAR(100) DEFAULT ''",
        "is_admin":         "BOOLEAN DEFAULT FALSE",
        "is_approved":      "BOOLEAN DEFAULT FALSE",
        "discount_percent": "FLOAT DEFAULT 0",
        "totp_secret":     "VARCHAR(64) DEFAULT ''",
        "consent_at":      "TIMESTAMP",
    },
    "supplies": {
        "status":     "VARCHAR(30) DEFAULT 'Ожидается'",
        "notes":      "TEXT DEFAULT ''",
        "is_service": "BOOLEAN DEFAULT FALSE NOT NULL",
        "city":       "VARCHAR(100) DEFAULT ''",
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
        # Обмен с 1С: когда заказ забрала 1С и под каким номером он там заведён
        "exported_at":      "TIMESTAMP",
        "onec_number":      "VARCHAR(64) DEFAULT ''",
        "order_token":      "VARCHAR(64)",
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

# (имя индекса, колонки) или (имя, колонки, "unique")
EXPECTED_INDEXES: dict[str, list[tuple]] = {
    "products": [
        ("ix_products_category", "category"),
        ("ix_products_sku", "sku"),
        ("ix_products_country", "country"),
        ("ix_products_external_id", "external_id"),
    ],
    "orders": [
        ("ix_orders_created_at", "created_at"),
        ("ix_orders_status", "status"),
        ("ix_orders_user_created", "user_id, created_at"),
        ("ix_orders_exported_at", "exported_at"),
        # NULL-ы в уникальном индексе допустимы (старые заказы без токена)
        ("uq_orders_order_token", "order_token", "unique"),
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
    "audit_log": [
        ("ix_audit_log_created", "created_at"),
        ("ix_audit_log_user_action", "user_id, action"),
    ],
}


def _column_ddl(table: str, column: str, sqlite_ddl: str) -> str:
    engine = current_bind()  # актуальный bind (см. app/database.py)
    dialect = engine.dialect.name
    if table == "products" and column == "photos":
        if dialect == "postgresql":
            return "JSONB DEFAULT '[]'::jsonb"
        return "TEXT DEFAULT '[]'"
    return sqlite_ddl


# Блокировка миграции — в app/locks.py (работает и на Linux, и на Windows).
# Миграцию выполняет один воркер, остальные ждут освобождения файла.


def ensure_notifications_table() -> None:
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
        insp = inspect(engine)
        if "notifications" not in insp.get_table_names():
            from .models import Notification
            engine = current_bind()  # актуальный bind (см. app/database.py)
            Notification.__table__.create(bind=engine, checkfirst=True)
            logger.info("🔧 Создана таблица notifications")
    except Exception as e:
        logger.error("Не удалось создать таблицу notifications: %s", e)


def _exec_one(sql: str, label: str) -> bool:
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
        with engine.begin() as conn:
            conn.execute(text(sql))
        logger.info("🔧 %s", label)
        return True
    except Exception as e:
        msg = str(e).split("\n")[0][:200]
        logger.debug("Авто-миграция: %s — %s", label, msg)
        return False


def _backfill_once(flag: str) -> bool:
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
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
            conn.execute(text("""
                INSERT INTO _migrations (name) VALUES (:n)
                ON CONFLICT (name) DO NOTHING
            """), {"n": flag})
        return True
    except Exception as e:
        logger.warning("_backfill_once(%s): %s", flag, e)
        return False


def _backfill_order_item_pack_sizes() -> None:
    if not _backfill_once("order_item_pack_sizes_v1"):
        return
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
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
        engine = current_bind()  # актуальный bind (см. app/database.py)
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


def _backfill_product_external_ids() -> None:
    """Старым товарам в external_id кладём sku.

    Так у уже загруженного каталога появляется идентификатор для обмена,
    и первый же обмен с 1С не создаёт дубли по названию.
    """
    if not _backfill_once("product_external_id_v1"):
        return
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE products SET external_id = sku "
                "WHERE (external_id IS NULL OR external_id = '') "
                "AND sku IS NOT NULL AND sku != ''"
            ))
        logger.info("✅ Backfill products.external_id выполнен")
    except Exception as e:
        logger.warning("Backfill products.external_id: %s", e)


def _backfill_order_subtotals() -> None:
    if not _backfill_once("order_subtotals_v1"):
        return
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
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


def _ensure_trigram_search_indexes(dialect: str) -> None:
    """PostgreSQL: триграммные индексы для поиска ILIKE '%текст%'.

    Обычный индекс для такого поиска бесполезен — запрос читает всю таблицу.
    С pg_trgm поиск по названию остаётся быстрым и при десятках тысяч товаров
    из 1С. Делается «по возможности»: нет прав на CREATE EXTENSION — просто
    пропускаем (поиск работает, только медленнее).
    """
    if dialect != "postgresql":
        return
    try:
        existing = {i["name"] for i in inspect(current_bind()).get_indexes("products")}
    except Exception:                                        # noqa: BLE001
        existing = set()
    if "ix_products_name_trgm" in existing:
        return
    if _exec_one("CREATE EXTENSION IF NOT EXISTS pg_trgm", "Включено расширение pg_trgm"):
        _exec_one(
            "CREATE INDEX IF NOT EXISTS ix_products_name_trgm "
            "ON products USING gin (name gin_trgm_ops)",
            "Создан триграммный индекс по названию товара",
        )


def _auto_migrate_inner() -> None:
    try:
        engine = current_bind()  # актуальный bind (см. app/database.py)
        insp = inspect(engine)
    except Exception as e:
        logger.error("Не удалось получить схему БД: %s", e)
        return

    tables = set(insp.get_table_names())
    engine = current_bind()  # актуальный bind (см. app/database.py)
    dialect = engine.dialect.name
    logger.info("🔍 Диалект БД: %s", dialect)

    added = 0

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

    for table, indexes in EXPECTED_INDEXES.items():
        if table not in tables:
            continue
        for idx_name, columns, *flags in indexes:
            unique = "UNIQUE " if "unique" in flags else ""
            try:
                engine = current_bind()  # актуальный bind (см. app/database.py)
                insp_local = inspect(engine)
                existing_idx = {
                    i["name"] for i in insp_local.get_indexes(table)
                }
            except Exception:
                existing_idx = set()
            if idx_name in existing_idx:
                continue
            sql = (
                f"CREATE {unique}INDEX IF NOT EXISTS {idx_name} "
                f"ON {table} ({columns})"
            )
            if _exec_one(sql, f"Создан индекс {idx_name} ON {table}"):
                added += 1

    _ensure_trigram_search_indexes(dialect)

    _backfill_order_item_pack_sizes()
    _backfill_product_photos()
    _backfill_product_external_ids()
    _backfill_order_subtotals()

    if added:
        logger.info("✅ Авто-миграция: +%d изменений", added)
    else:
        logger.info("✅ Схема БД актуальна")


def auto_migrate() -> None:
    with file_lock(MIGRATION_LOCK) as got_lock:
        if got_lock:
            logger.info("🔒 Lock миграции получен: %s", MIGRATION_LOCK)
        _auto_migrate_inner()


def prepare_database(*after) -> None:
    """Вся подготовка БД при старте — ОДНИМ куском под одной блокировкой.

    Порядок: создать таблицы → довести схему (колонки/индексы) → выполнить
    шаги `after` (например, создание админа из .env).

    Почему так. Раньше create_all() выполнялся ДО блокировки: при первом
    старте на пустой PostgreSQL два воркера одновременно делали CREATE TABLE
    и один из них падал. А ensure_default_admin() вызывался при импорте
    модуля — раньше, чем появлялись таблицы, поэтому на чистой базе админ
    не создавался вообще («no such table: users»).

    Блокировки нельзя вкладывать друг в друга (flock на двух дескрипторах
    одного файла блокирует сам себя), поэтому здесь — один with.
    """
    from . import models  # noqa: F401 — регистрация таблиц в Base.metadata

    with file_lock(MIGRATION_LOCK) as got_lock:
        if got_lock:
            logger.info("🔒 Lock миграции получен: %s", MIGRATION_LOCK)
        Base.metadata.create_all(bind=current_bind())
        _auto_migrate_inner()
        ensure_notifications_table()
        for step in after:
            step()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    auto_migrate()