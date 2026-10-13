# tests/test_1c_integration.py
"""Обмен с 1С: приём каталога с id, выгрузка каталога и выгрузка заказов.

Запуск:  pytest tests/test_1c_integration.py -v
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Order, OrderItem, Product, Supply, SupplyItem, User
from app.services.passwords import hash_password

# Логин/пароль заданы в tests/conftest.py (INTEGRATION_USER / INTEGRATION_PASSWORD)
AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(b"1c_dianthus:test-1c-pass").decode()
}

# Ровно тот формат, который согласован с 1С.
# "id" здесь — идентификатор товара В 1С; он сохраняется в Product.external_id.
PRODUCTS_PAYLOAD = {
    "products": [
        {
            "id": 1,
            "sku": "ROSA-FREEDOM-60",
            "name": "Роза Freedom 60 см",
            "description": "Классическая красная роза",
            "country": "Эквадор",
            "length_cm": 60,
            "package_size": 25,
            "min_quantity": 1,
            "category": "Роза Эквадор",
            "price_per_stem": 42.50,
            "stock_packs": 120,
            "photos": ["https://dianthus-opt.ru/static/uploads/abc.webp"],
        }
    ]
}


@pytest.fixture(scope="function")
def test_db():
    """SQLite in-memory для каждого теста."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
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
    """Клиент с подменённой БД.

    Исправление: раньше делался app.dependency_overrides[get_db], но
    роутер 1С использует get_db из app.database — FastAPI сравнивает
    зависимости по объекту функции, и подмена не срабатывала: запросы
    шли в глобальную SQLite-БД без нужных таблиц. Теперь на время теста
    перепривязывается сам SessionLocal (его использует get_db), а после
    теста восстановление через try/finally — даже при исключении.
    """
    from app.database import SessionLocal

    original_bind = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=test_db.get_bind())
    try:
        with TestClient(app) as c:
            yield c
    finally:
        SessionLocal.configure(bind=original_bind)


def _seed_user(db) -> User:
    user = User(
        email="shop@test.ru",
        hashed_password=hash_password("Test1234"),
        full_name="Иван Иванов",
        company_name="ООО Ромашка",
        phone="+79001234567",
        inn="7701234567",
        city="Москва",
        is_approved=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── ПРИЁМ КАТАЛОГА ИЗ 1С ─────────────────────────────────────────

def test_sync_requires_auth(client):
    r = client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD)
    assert r.status_code == 401


def test_sync_creates_product_with_external_id(client, test_db):
    r = client.post("/api/1c/products/sync",
                    json=PRODUCTS_PAYLOAD, headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["created"] == 1
    assert data["errors"] == []

    product = test_db.query(Product).one()
    assert product.name == "Роза Freedom 60 см"
    assert product.external_id == "1"          # id из 1С сохранён
    assert product.sku == "ROSA-FREEDOM-60"
    assert product.package_size == 25
    assert product.length_cm == 60

    item = test_db.query(SupplyItem).one()
    assert item.price == 42.50                 # цена за ШТ
    assert item.stock == 120                   # остаток в УПАКОВКАХ
    assert item.is_active is True
    assert item.supply.is_service is True      # служебная поставка 1С


def test_sync_is_idempotent_by_external_id(client, test_db):
    """Повторная выгрузка того же товара не создаёт дубль."""
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)

    changed = {
        "products": [
            {
                **PRODUCTS_PAYLOAD["products"][0],
                "name": "Роза Freedom 60 см (новая упаковка)",
                "price_per_stem": 45.0,
                "stock_packs": 80,
            }
        ]
    }
    r = client.post("/api/1c/products/sync", json=changed, headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 0
    assert r.json()["updated"] == 1

    assert test_db.query(Product).count() == 1
    product = test_db.query(Product).one()
    assert product.name == "Роза Freedom 60 см (новая упаковка)"

    item = test_db.query(SupplyItem).one()
    assert item.price == 45.0
    assert item.stock == 80


# ── ВЫГРУЗКА КАТАЛОГА В 1С ───────────────────────────────────────

def test_products_export_has_agreed_format(client, test_db):
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)

    r = client.get("/api/1c/products/export", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1

    p = data["products"][0]
    product = test_db.query(Product).one()

    # обязательные поля согласованного формата
    assert set(p) >= {
        "id", "external_id", "sku", "name", "description", "country",
        "length_cm", "package_size", "min_quantity", "category",
        "price_per_stem", "stock_packs", "photos",
    }
    assert p["id"] == product.id               # id товара на сайте
    assert p["external_id"] == "1"             # id товара в 1С
    assert p["sku"] == "ROSA-FREEDOM-60"
    assert p["name"] == "Роза Freedom 60 см"
    assert p["price_per_stem"] == 42.50
    assert p["stock_packs"] == 120
    assert p["package_size"] == 25
    assert p["min_quantity"] == 1
    assert p["category"] == "Роза Эквадор"
    assert p["photos"] == ["https://dianthus-opt.ru/static/uploads/abc.webp"]


def test_products_export_all_includes_out_of_stock(client, test_db):
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)
    test_db.query(SupplyItem).update({SupplyItem.is_active: False})
    test_db.commit()

    # на полке ничего нет
    assert client.get("/api/1c/products/export", headers=AUTH).json()["count"] == 0
    # но весь справочник доступен по ?all=1
    assert client.get(
        "/api/1c/products/export?all=1", headers=AUTH
    ).json()["count"] == 1


# ── ВЫГРУЗКА ЗАКАЗОВ В 1С ────────────────────────────────────────

def test_orders_export(client, test_db):
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)
    user = _seed_user(test_db)
    item = test_db.query(SupplyItem).one()
    product = item.product

    order = Order(
        user_id=user.id, subtotal=2125.0, discount_percent=0,
        total_price=2125.0, status="Новый", comment="Позвонить за час",
    )
    test_db.add(order)
    test_db.flush()
    test_db.add(OrderItem(
        order_id=order.id, product_id=product.id, supply_item_id=item.id,
        product_name=product.name, unit="упаковка", price=42.50,
        package_size=25, quantity=2,
    ))
    test_db.commit()

    r = client.get("/api/1c/orders/export", headers=AUTH)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 1

    o = data["orders"][0]
    assert o["id"] == order.id
    assert o["number"] == f"САЙТ-{order.id}"
    assert o["status"] == "Новый"
    assert o["total"] == 2125.0
    assert o["comment"] == "Позвонить за час"
    assert o["buyer"]["company"] == "ООО Ромашка"
    assert o["buyer"]["inn"] == "7701234567"

    line = o["items"][0]
    assert line["sku"] == "ROSA-FREEDOM-60"
    assert line["quantity_packs"] == 2
    assert line["package_size"] == 25
    assert line["stems"] == 50
    assert line["sum"] == 2125.0


def test_orders_export_rejects_unknown_status(client):
    r = client.get("/api/1c/orders/export?status=Несуществующий", headers=AUTH)
    assert r.status_code == 400
