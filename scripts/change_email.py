# Сменить email (он же логин для входа) у пользователя — например, у администратора.
# Использование (на сервере): docker compose exec -T app python scripts/change_email.py СТАРЫЙ НОВЫЙ
#
# ВАЖНО для администратора: в боевом .env есть ADMIN_EMAIL. При каждом запуске сайт создаёт
# администратора с этим адресом, если такого пользователя нет. Поэтому после смены логина
# в .env нужно заменить ADMIN_EMAIL на новый адрес (и убрать ADMIN_PASSWORD), иначе при первом же
# перезапуске появится второй администратор со старым адресом. Скрипт напоминает об этом.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models
from app.validators import validate_email


def change_email(old: str, new: str, db=None) -> int:
    old = (old or "").strip().lower()
    new = (new or "").strip().lower()
    if not old or not new:
        print("❌ Укажите старый и новый email")
        return 2
    problem = validate_email(new)
    if problem:
        print(f"❌ Новый адрес не подходит: {problem}")
        return 2
    if old == new:
        print("❌ Старый и новый адрес одинаковые — менять нечего")
        return 2

    own_session = db is None
    if own_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.email == old).first()
        if not user:
            print(f"❌ Пользователь {old} не найден")
            return 1
        if db.query(models.User).filter(models.User.email == new).first():
            print(f"❌ Адрес {new} уже занят другим пользователем")
            return 1
        user.email = new
        db.add(models.AuditLog(
            user_id=user.id, action="email_change", entity="user", entity_id=user.id,
            details=f"{old} -> {new} (scripts/change_email.py)"[:2000], ip="",
        ))
        db.commit()
        print(f"✅ {user.full_name}: вход теперь по адресу {new} (пароль прежний)")
        if user.is_admin:
            print("⚠️  Дальше в .env на сервере: ADMIN_EMAIL=" + new +
                  " и удалите строку ADMIN_PASSWORD (иначе после перезапуска сайт создаст "
                  "второго администратора со старым адресом).")
        return 0
    except Exception as e:                      # noqa: BLE001
        db.rollback()
        print(f"❌ Ошибка БД: {e}")
        return 1
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Использование: python scripts/change_email.py СТАРЫЙ_EMAIL НОВЫЙ_EMAIL")
        sys.exit(2)
    sys.exit(change_email(sys.argv[1], sys.argv[2]))
