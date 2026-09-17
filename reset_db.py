"""
Полный сброс БД — удалить и создать таблицы заново.
⚠️ УДАЛИТ ВСЕ ДАННЫЕ!

Работает и с PostgreSQL, и с SQLite.

Для PostgreSQL используется DROP SCHEMA public CASCADE — это надёжно
удаляет вообще всё, включая «осиротевшие» FK от старых версий схемы.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app import models                  # noqa: E402, F401


def _is_postgres() -> bool:
    return engine.dialect.name == "postgresql"


def reset_postgres() -> None:
    """Полный сброс схемы public для PostgreSQL."""
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        # Возвращаем права по умолчанию
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))


def reset_sqlite() -> None:
    """Для SQLite просто drop_all + create_all."""
    Base.metadata.drop_all(bind=engine)


def reset() -> None:
    confirm = input("⚠️  Удалить ВСЕ данные? [y/N]: ")
    if confirm.lower() != "y":
        print("Отменено.")
        return

    print("🗑  Удаляем старую схему...")
    if _is_postgres():
        reset_postgres()
    else:
        reset_sqlite()

    print("🏗  Создаём заново...")
    Base.metadata.create_all(bind=engine)
    print("✅ Готово. Схема БД создана с нуля.")


if __name__ == "__main__":
    reset()