# tests/test_smoke.py
"""Smoke-тесты для критических путей приложения."""


def test_health_endpoint(client):
    """Health должен отвечать JSON-ом с полем db."""
    r = client.get("/health")
    assert r.status_code in (200, 503), r.text
    data = r.json()
    assert "status" in data
    assert "db" in data
    assert isinstance(data["db"], str)


def test_login_page_renders(client):
    """Страница входа должна отдавать 200 и содержать CSRF-токен."""
    r = client.get("/login")
    assert r.status_code == 200
    assert "Вход" in r.text
    assert "csrf_token" in r.text


def test_root_redirects_to_login_for_anonymous(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_catalog_requires_auth(client):
    r = client.get("/catalog", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_cart_requires_auth(client):
    r = client.get("/cart", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_admin_requires_auth(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_register_without_csrf_rejected(client):
    """POST /register без CSRF-токена должен быть отклонён."""
    r = client.post("/register", data={
        "email": "test@test.com",
        "password": "password123",
        "full_name": "Test User",
        "phone": "+79001234567",
        "company_name": "Test LLC",
        "inn": "1234567890",
        "city": "Moscow",
    }, follow_redirects=False)
    # CSRF-ошибка → либо редирект, либо 400/403
    assert r.status_code in (303, 400, 403)


def test_1c_ping_unauthorized(client):
    """1С ping без авторизации должен вернуть 401 или 503."""
    r = client.get("/api/1c/ping")
    assert r.status_code in (401, 503)


def test_security_headers_present(client):
    """Middleware должен добавлять security-заголовки."""
    r = client.get("/login")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "Permissions-Policy" in r.headers


def test_registration_pending_page(client):
    r = client.get("/registration_pending")
    assert r.status_code == 200