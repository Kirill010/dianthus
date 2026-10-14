# Сменить пароль пользователя (например, администратора) по email.
# Использование: docker compose exec app python scripts/set_password.py EMAIL НОВЫЙ_ПАРОЛЬ
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal
from app import models
from app.services.passwords import hash_password


def set_password(email: str, new_password: str) -> int:
    email = (email or "").strip().lower()
    if not email or not new_password:
        print("❌ Укажите email и новый пароль")
        return 1
    if len(new_password) < 8 or len(new_password.encode("utf-8")) > 72:
        print("❌ Пароль должен быть не короче 8 символов (bcrypt: до 72 байт)")
        return 1
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == email).first()
        if not user:
            print(f"❌ Пользователь {email} не найден")
            return 1
        user.hashed_password = hash_password(new_password)
        db.commit()
        print(f"✅ Пароль для {user.full_name} ({email}) обновлён")
        return 0
    except Exception as e:
        db.rollback()
        print(f"❌ Ошибка БД: {e}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python scripts/set_password.py EMAIL ПАРОЛЬ")
        sys.exit(2)
    sys.exit(set_password(sys.argv[1], sys.argv[2]))