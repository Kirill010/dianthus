# tests/test_public.py
"""
Публичная витрина: лендинг, каталог и товар без цен для анонимов,
SEO-маршруты, и проверка, что авторизованный видит цены.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Product, Supply, SupplyItem, User
from app.services.passwords import hash_password
from app.services.taxonomy import set_taxonomy


@pytest.fixture(scope="function")
def test_db():
    # StaticPool: одно соединение на все потоки — иначе in-memory SQLite
    # виден только в потоке создания, а потоки приложения получают пустую БД.
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


@pytest.fixture
def seeded(test_db):
    """Один активный товар с ценой + одобренный клиент."""
    product = Product(
        name="Роза Публичная", package_size=25, min_quantity=1,
        image_url="/static/logo-icon.png",
    )
    set_taxonomy(test_db, product, kind="Роза", height=60, country="Эквадор")
    supply = Supply(country="Эквадор", status="Разгружен")
    user = User(
        email="client@test.ru",
        hashed_password=hash_password("Client123"),
        full_name="Клиент Тестов",
        company_name="ООО Клиент",
        is_approved=True,
    )
    test_db.add_all([product, supply, user])
    test_db.flush()
    item = SupplyItem(
        supply_id=supply.id, product_id=product.id,
        price=123.45, stock=40, is_active=True,
    )
    test_db.add(item)
    test_db.commit()
    return {"item_id": item.id, "price": "123.45", "user_email": user.email}


def _login(client) -> None:
    """Вход с валидным CSRF: токен берём из HTML страницы входа."""
    r = client.get("/login")
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text)
    assert m, "CSRF-токен не найден на странице входа"
    resp = client.post("/login", data={
        "email": "client@test.ru",
        "password": "Client123",
        "csrf_token": m.group(1),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/catalog"


# ── Анонимный доступ ────────────────────────────────────────

def test_anon_landing_ok(client, seeded):
    r = client.get("/")
    assert r.status_code == 200
    assert "Стать оптовиком" in r.text
    # Лендинг не должен светить цены
    assert seeded["price"] not in r.text
    assert "₽" not in r.text


def test_anon_catalog_ok_no_prices(client, seeded):
    r = client.get("/catalog")
    assert r.status_code == 200
    assert "Роза Публичная" in r.text
    # Ни цены, ни кнопок корзины
    assert seeded["price"] not in r.text
    assert "₽" not in r.text
    assert "add_to_cart" not in r.text
    assert "после регистрации" in r.text


def test_anon_product_ok_no_price(client, seeded):
    r = client.get(f"/product/{seeded['item_id']}")
    assert r.status_code == 200
    assert "Роза Публичная" in r.text
    assert seeded["price"] not in r.text
    assert "₽" not in r.text
    assert "add_to_cart" not in r.text


def test_anon_contacts_ok(client):
    r = client.get("/contacts")
    assert r.status_code == 200
    assert "Контакты" in r.text


def test_anon_register_page_ok(client):
    r = client.get("/register")
    assert r.status_code == 200
    assert "company_name" in r.text


def test_anon_cart_still_protected(client):
    r = client.get("/cart", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# ── Авторизованный видит цены ───────────────────────────────

def test_logged_in_sees_prices(client, seeded):
    _login(client)
    r = client.get("/catalog")
    assert r.status_code == 200
    assert seeded["price"] in r.text
    assert "add_to_cart" in r.text


def test_logged_in_product_has_price(client, seeded):
    _login(client)
    r = client.get(f"/product/{seeded['item_id']}")
    assert r.status_code == 200
    assert seeded["price"] in r.text


# ── SEO ─────────────────────────────────────────────────────

def test_sitemap_lists_public_pages(client, seeded):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.headers["content-type"]
    assert "/catalog" in r.text
    assert f"/product/{seeded['item_id']}" in r.text


def test_search_suggest_public(client, seeded):
    r = client.get("/api/search-suggest?q=Публич")
    assert r.status_code == 200
    items = r.json()["items"]
    assert any("Публичная" in i["name"] for i in items)
