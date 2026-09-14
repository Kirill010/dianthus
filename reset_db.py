"""
Полный сброс БД — удалить и создать таблицы заново.

⚠️  УДАЛИТ ВСЕ ДАННЫЕ!
Запуск: python reset_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import Base, engine   # noqa: E402
from app import models                  # noqa: E402, F401 — регистрирует модели


def reset() -> None:
    confirm = input("⚠️  Удалить ВСЕ данные? [y/N]: ")
    if confirm.lower() != "y":
        print("Отменено.")
        return
    print("🗑  Удаляем таблицы...")
    Base.metadata.drop_all(bind=engine)
    print("🏗  Создаём заново...")
    Base.metadata.create_all(bind=engine)
    print("✅ Готово.")


if __name__ == "__main__":
    reset()