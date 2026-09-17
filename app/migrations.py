"""
Лёгкая авто-миграция.

Что делает:
  • при старте приложения смотрит на реальную схему БД;
  • если в таблице не хватает колонки из models.py — добавляет её
    через ALTER TABLE ADD COLUMN (данные не трогаются).

Работает и с SQLite, и с PostgreSQL — синтаксис ADD COLUMN общий.
"""
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
        # ← вот из-за отсутствия этой колонки падал /catalog
        "category":     "VARCHAR(100) DEFAULT 'Прочее'",
    },
    "users": {
        "phone":        "VARCHAR(30) DEFAULT ''",
        "company_name": "VARCHAR(200) DEFAULT ''",
        "is_admin":     "BOOLEAN DEFAULT FALSE",
        "is_approved":  "BOOLEAN DEFAULT FALSE",
    },
    "supplies": {
        "status": "VARCHAR(30) DEFAULT 'Ожидается'",
        "notes":  "TEXT DEFAULT ''",
    },
    "supply_items": {
        "is_active": "BOOLEAN DEFAULT FALSE",
    },
    "orders": {
        "status":  "VARCHAR(30) DEFAULT 'Новый'",
        "comment": "TEXT DEFAULT ''",
    },
}


EXPECTED_INDEXES: dict[str, list[tuple[str, str]]] = {
    "products": [("ix_products_category", "category")],
}


def _safe_inspect():
    try:
        return inspect(engine)
    except Exception as e:
        logger.error("Не удалось получить схему БД: %s", e)
        return None


def auto_migrate() -> None:
    """Синхронизирует схему БД с моделями (добавляет недостающее)."""
    insp = _safe_inspect()
    if insp is None:
        return

    tables = set(insp.get_table_names())
    added_columns = 0
    added_indexes = 0

    with engine.begin() as conn:
        # 1) Добавляем недостающие колонки
        for table, columns in EXPECTED_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in columns.items():
                if col in existing:
                    continue
                sql = f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'
                try:
                    conn.execute(text(sql))
                    logger.info("🔧 Миграция: добавлена колонка %s.%s",
                                table, col)
                    added_columns += 1
                except Exception as e:
                    logger.warning("Не удалось добавить %s.%s: %s",
                                   table, col, e)

        # 2) Добавляем недостающие индексы
        for table, indexes in EXPECTED_INDEXES.items():
            if table not in tables:
                continue
            existing_idx = {i["name"] for i in insp.get_indexes(table)}
            for idx_name, column in indexes:
                if idx_name in existing_idx:
                    continue
                sql = (f"CREATE INDEX IF NOT EXISTS {idx_name} "
                       f"ON {table} ({column})")
                try:
                    conn.execute(text(sql))
                    logger.info("🔧 Миграция: создан индекс %s", idx_name)
                    added_indexes += 1
                except Exception as e:
                    logger.warning("Не удалось создать индекс %s: %s",
                                   idx_name, e)

    if added_columns or added_indexes:
        logger.info("✅ Авто-миграция: +%d колонок, +%d индексов",
                    added_columns, added_indexes)
    else:
        logger.info("✅ Схема БД актуальна (миграции не нужны)")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    auto_migrate()
    print("Готово. Проверьте БД.")