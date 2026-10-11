# Сброс счётчиков ID во всех таблицах. Данные НЕ удаляются.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import inspect, text
from app.database import engine

TABLES = ["users", "products", "supplies",
          "supply_items", "orders", "order_items", "notifications"]


def _reset_postgres(conn, table: str) -> None:
    seq = conn.execute(text(
        f"SELECT pg_get_serial_sequence('{table}', 'id')"
    )).scalar()
    if not seq:
        print(f"   ⚠️  {table}: sequence не найден")
        return
    max_id = conn.execute(text(
        f"SELECT COALESCE(MAX(id), 0) FROM {table}"
    )).scalar()
    conn.execute(text(f"SELECT setval('{seq}', {max_id + 1}, false)"))
    print(f"   ✅ {table:15s} → следующий ID = {max_id + 1}")


def _reset_sqlite(conn, table: str) -> None:
    max_id = conn.execute(text(
        f"SELECT COALESCE(MAX(id), 0) FROM {table}"
    )).scalar()
    has_seq = conn.execute(text(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='sqlite_sequence'"
    )).scalar()
    if not has_seq:
        print(f"   ℹ️  {table:15s} → следующий ID = {max_id + 1}")
        return
    exists = conn.execute(text(
        "SELECT name FROM sqlite_sequence WHERE name = :n"
    ), {"n": table}).scalar()
    if exists:
        if max_id == 0:
            conn.execute(text(
                "DELETE FROM sqlite_sequence WHERE name = :n"
            ), {"n": table})
        else:
            conn.execute(text(
                "UPDATE sqlite_sequence SET seq = :s WHERE name = :n"
            ), {"s": max_id, "n": table})
    print(f"   ✅ {table:15s} → следующий ID = {max_id + 1}")


def reset_ids() -> None:
    if input("⚠️  Сбросить счётчики ID? [y/N]: ").lower() != "y":
        print("Отменено.")
        return
    is_pg = engine.dialect.name == "postgresql"
    print(f"\n🔧 Диалект: {'PostgreSQL' if is_pg else 'SQLite'}\n")
    existing = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        for t in TABLES:
            if t not in existing:
                print(f"   ⏭  {t:15s} → нет таблицы")
                continue
            _reset_postgres(conn, t) if is_pg else _reset_sqlite(conn, t)
    print("\n✅ Готово. Перезапусти: sudo systemctl restart dianthus")


if __name__ == "__main__":
    reset_ids()