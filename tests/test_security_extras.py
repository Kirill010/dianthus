# tests/test_security_extras.py
"""
Тесты второго раунда доработок безопасности:
CSP nonce, open redirect в Referer, Excel-инъекции, полный цикл 2FA.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import User
from app.auth import hash_password


# ── Фикстуры (как в test_critical.py) ──

@pytest.fixture(scope="function")
def test_db():
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            yield test_db
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _csrf_from(page_html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', page_html)
    assert m, "CSRF-токен не найден в HTML"
    return m.group(1)


def _make_admin(test_db, totp_secret: str = "") -> User:
    admin = User(
        email="admin@test.ru",
        hashed_password=hash_password("Admin1234"),
        full_name="Админ",
        company_name="Диантус",
        is_admin=True,
        is_approved=True,
        totp_secret=totp_secret,
    )
    test_db.add(admin)
    test_db.commit()
    return admin


def _login_admin(client) -> None:
    page = client.get("/login")
    resp = client.post("/login", data={
        "email": "admin@test.ru", "password": "Admin1234",
        "csrf_token": _csrf_from(page.text),
    }, follow_redirects=False)
    assert resp.status_code == 303


# ── CSP nonce ──

def test_csp_nonce_header_and_template_match(client):
    """CSP содержит nonce, 'unsafe-inline' убран из script-src,
    и inline-скрипт в HTML использует тот же nonce."""
    resp = client.get("/login")
    assert resp.status_code == 200

    csp = resp.headers.get("content-security-policy", "")
    assert "script-src" in csp
    script_src = next(
        d for d in csp.split(";") if d.strip().startswith("script-src")
    )
    assert "'unsafe-inline'" not in script_src

    m = re.search(r"script-src [^;]*'nonce-([^']+)'", csp)
    assert m, "nonce не найден в script-src"
    nonce = m.group(1)

    # Inline-скрипты на странице подписаны тем же nonce
    assert f'<script nonce="{nonce}">' in resp.text

    # Соседний запрос — другой nonce (nonce одноразовый на запрос)
    resp2 = client.get("/login")
    csp2 = resp2.headers.get("content-security-policy", "")
    m2 = re.search(r"'nonce-([^']+)'", csp2)
    assert m2 and m2.group(1) != nonce


def test_security_headers_present(client):
    resp = client.get("/login")
    h = resp.headers
    assert h.get("x-frame-options") == "DENY"
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("cross-origin-opener-policy") == "same-origin"
    assert h.get("cross-origin-resource-policy") == "same-origin"


# ── Open redirect через Referer ──

def test_cart_referer_external_host_ignored(client, test_db):
    """POST с Referer на чужой домен → редирект на fallback, а не на чужой сайт."""
    user = User(
        email="u@test.ru", hashed_password=hash_password("Test1234"),
        full_name="Тест", company_name="ООО Тест", is_approved=True,
    )
    test_db.add(user)
    test_db.commit()

    page = client.get("/login")
    csrf = _csrf_from(page.text)
    client.post("/login", data={
        "email": "u@test.ru", "password": "Test1234",
        "csrf_token": csrf,
    }, follow_redirects=False)

    resp = client.post(
        "/clear_cart",
        data={"csrf_token": csrf},
        headers={"referer": "https://evil.example.com/phishing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "evil.example.com" not in location
    assert location.startswith("/") or "evil" not in location


# ── Excel-инъекции ──

def test_excel_formula_sanitized():
    from app.services.excel_service import _cell, _row

    assert _cell("=1+1") == "'=1+1"
    assert _cell("+SUM(A1)") == "'+SUM(A1)"
    assert _cell("-10") == "'-10"
    assert _cell("@evil") == "'@evil"
    assert _cell("\t=1") == "'\t=1"
    assert _cell("Обычный текст") == "Обычный текст"
    assert _cell("") == ""
    assert _cell(None) is None
    assert _cell(123) == 123
    assert _row(["=cmd", "текст", 5]) == ["'=cmd", "текст", 5]


# ── Полный цикл 2FA ──

def test_totp_full_setup_flow(client, test_db):
    """Админ включает 2FA через профиль: старт → QR/секрет → подтверждение кода."""
    import pyotp

    admin = _make_admin(test_db)
    assert not admin.totp_secret

    _login_admin(client)

    profile = client.get("/profile")
    assert profile.status_code == 200
    assert 'id="2fa"' in profile.text

    # Старт: генерирует секрет в сессии
    resp = client.post("/profile/2fa/start", data={
        "csrf_token": _csrf_from(profile.text),
    }, follow_redirects=False)
    assert resp.status_code == 303

    # Страница показывает QR и секрет
    profile = client.get("/profile")
    assert "data:image/png;base64" in profile.text
    m = re.search(r'break-all;">\s*([A-Z2-7=]{16,})\s*<', profile.text)
    assert m, "секрет не показан на странице"
    secret = m.group(1).strip()

    # Неверный код → ошибка, секрет НЕ сохраняется
    resp = client.post("/profile/2fa/confirm", data={
        "csrf_token": _csrf_from(profile.text), "code": "000000",
    }, follow_redirects=True)
    assert "Неверный код" in resp.text or "неверный" in resp.text.lower()
    test_db.refresh(admin)
    assert not admin.totp_secret

    # Верный код → секрет сохраняется
    profile = client.get("/profile")
    resp = client.post("/profile/2fa/confirm", data={
        "csrf_token": _csrf_from(profile.text),
        "code": pyotp.TOTP(secret).now(),
    }, follow_redirects=True)
    test_db.refresh(admin)
    assert admin.totp_secret == secret

    # Страница теперь показывает состояние «включено»
    profile = client.get("/profile")
    assert "2fa/disable" in profile.text


def test_totp_confirm_requires_admin(client, test_db):
    """Обычный пользователь не может вызвать 2FA-эндпоинты (403/редирект)."""
    user = User(
        email="plain@test.ru", hashed_password=hash_password("Test1234"),
        full_name="П", company_name="ООО", is_approved=True,
    )
    test_db.add(user)
    test_db.commit()

    page = client.get("/login")
    client.post("/login", data={
        "email": "plain@test.ru", "password": "Test1234",
        "csrf_token": _csrf_from(page.text),
    }, follow_redirects=False)

    profile = client.get("/profile")
    resp = client.post("/profile/2fa/start", data={
        "csrf_token": _csrf_from(profile.text),
    }, follow_redirects=False)
    assert resp.status_code in (303, 403)
    test_db.refresh(user)
    assert not user.totp_secret
