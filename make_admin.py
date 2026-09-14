"""
Выдать права администратора по email.

Использование: python make_admin.py EMAIL
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal   # noqa: E402
from app import models                  # noqa: E402


def make_admin(email: str) -> int:
    """Возвращает 0 при успехе, 1 при ошибке."""
    email = (email or "").strip().lower()
    if not email:
        print("❌ Не указан email")
        return 1

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(
            models.User.email == email).first()
        if not user:
            print(f"❌ Пользователь {email} не найден")
            return 1
        user.is_admin = True
        user.is_approved = True
        db.commit()
        print(f"✅ {user.full_name} ({email}) теперь админ")
        return 0
    except Exception as e:
        db.rollback()
        print(f"❌ Ошибка БД: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python make_admin.py EMAIL")
        sys.exit(1)
    sys.exit(make_admin(sys.argv[1]))