# tests/test_password_eye.py
"""«Глаз» у полей пароля и скрипт смены логина (email)."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import AuditLog, User
from scripts.change_email import change_email
from tests.helpers import csrf_from, login, make_user

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def _password_inputs(html: str):
    return re.findall(r"<input\b[^>]*\btype=\"password\"[^>]*>", html)


@pytest.mark.parametrize("path, count", [("/login", 1), ("/register", 1)])
def test_public_forms_have_an_eye_next_to_every_password_field(web, path, count):
    html = web.get(path).text
    assert len(_password_inputs(html)) == count
    groups = re.findall(r'<div class="input-group">\s*<input\b[^>]*type="password"[^>]*>\s*'
                        r'<button type="button" class="btn btn-outline-secondary pw-toggle" data-pw-toggle '
                        r'aria-pressed="false" aria-label="Показать пароль"', html)
    assert len(groups) == count, "каждое поле пароля должно лежать в input-group вместе с кнопкой-глазом"


def test_profile_and_reset_pages_have_an_eye_for_every_password_field():
    # профиль и сброс пароля требуют входа/токена — проверяем сами шаблоны: каждому полю пароля
    # (type="password") должна соответствовать кнопка из _pw_toggle.html
    for name in ("profile.html", "reset.html", "login.html", "register.html"):
        src = (TEMPLATES / name).read_text(encoding="utf-8")
        fields = len(re.findall(r'type="password"', src))
        toggles = len(re.findall(r'{% include "_pw_toggle.html" %}', src))
        assert fields > 0 and fields == toggles, f"{name}: полей {fields}, глаз {toggles}"


def test_eye_button_has_accessible_name_and_script_handles_it(web):
    toggle = (TEMPLATES / "_pw_toggle.html").read_text(encoding="utf-8")
    assert 'aria-label="Показать пароль"' in toggle and 'type="button"' in toggle
    page = web.get("/login").text
    # логика лежит под nonce (CSP запрещает inline-скрипты без него) и возвращает type="password" при отправке
    block = re.search(r"<script nonce=\"[^\"]+\">\s*/\* ─── Подтверждения[\s\S]*?</script>", page)
    assert block and "data-pw-toggle" in block.group(0)
    assert "'submit', hideAll" in block.group(0) and "pagehide" in block.group(0)


# ───────────── смена логина ─────────────

def test_change_email_moves_login_and_keeps_password(mem_db, web):
    make_user(mem_db, email="old@mail.ru", password="Pass12345", admin=True, full_name="Владелец")
    assert change_email("OLD@mail.ru", " New@Ya.ru ", db=mem_db) == 0
    mem_db.expire_all()
    assert mem_db.query(User).filter(User.email == "old@mail.ru").first() is None
    user = mem_db.query(User).filter(User.email == "new@ya.ru").first()
    assert user and user.is_admin
    log = mem_db.query(AuditLog).filter(AuditLog.action == "email_change").one()
    assert "old@mail.ru -> new@ya.ru" in log.details
    # пароль прежний: вход по новому адресу проходит, по старому — нет (возврат на /login)
    r = login(web, "new@ya.ru", "Pass12345")
    assert r.status_code == 303 and not r.headers["location"].startswith("/login")
    with TestClient(app) as fresh:
        r_old = login(fresh, "old@mail.ru", "Pass12345")
        assert r_old.status_code == 303 and r_old.headers["location"].startswith("/login")


@pytest.mark.parametrize("old, new, code", [
    ("", "a@b.ru", 2),
    ("a@b.ru", "", 2),
    ("a@b.ru", "не адрес", 2),
    ("a@b.ru", "A@B.ru", 2),
    ("нет@такого.ru", "new@ya.ru", 1),
])
def test_change_email_rejects_bad_input(mem_db, old, new, code):
    make_user(mem_db, email="a@b.ru")
    assert change_email(old, new, db=mem_db) == code
    assert mem_db.query(User).filter(User.email == "a@b.ru").first() is not None


def test_change_email_refuses_a_taken_address(mem_db):
    make_user(mem_db, email="one@x.ru")
    make_user(mem_db, email="two@x.ru")
    assert change_email("one@x.ru", "two@x.ru", db=mem_db) == 1
    assert {u.email for u in mem_db.query(User).all()} == {"one@x.ru", "two@x.ru"}
