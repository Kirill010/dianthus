"""Полный сброс БД — удалить и создать таблицы заново. ⚠️ УДАЛИТ ВСЕ ДАННЫЕ!"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app import models                  # noqa: E402, F401


def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def reset_postgres() -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))


def reset_sqlite() -> None:
    Base.metadata.drop_all(bind=engine)


def reset() -> None:
    if input("⚠️  Удалить ВСЕ данные? [y/N]: ").lower() != "y":
        print("Отменено.")
        return
    print("🗑  Удаляем старую схему...")
    reset_postgres() if _is_postgres() else reset_sqlite()
    print("🏗  Создаём заново...")
    Base.metadata.create_all(bind=engine)
    print("✅ Готово.")


if __name__ == "__main__":
    reset()