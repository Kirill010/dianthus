# tests/test_1c_integration.py
"""Обмен с 1С: приём каталога с id, выгрузка каталога и выгрузка заказов.

Запуск:  pytest tests/test_1c_integration.py -v
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.main import app
from app.models import (Color, Country, Height, Kind, Order, OrderItem, Product,
                        SupplyItem, User)
from app.services.passwords import hash_password

# Логин/пароль заданы в tests/conftest.py (INTEGRATION_USER / INTEGRATION_PASSWORD)
AUTH = {
    "Authorization": "Basic "
    + base64.b64encode(b"1c_dianthus:test-1c-pass").decode()
}

# Товар «без поставки» в формате обмена: price — за штуку, quantity — в штуках (3000 шт = 120 упаковок).
# "id" здесь — идентификатор товара В 1С; он сохраняется в Product.external_id.
PRODUCTS_PAYLOAD = {
    "products": [
        {
            "id": "00000012345",
            "sku": "ROSA-FREEDOM-60",
            "barcode": "4600000000001",
            "name": "Роза Freedom 60 см",
            "description": "Классическая красная роза",
            "kind": "Роза",
            "color": "Красный",
            "height": 60,
            "country": "Эквадор",
            "unit": "упаковка",
            "package_size": 25,
            "min_quantity": 25,
            "price": 42.50,
            "quantity": 3000,
            "photos": ["https://dianthus-opt.ru/static/uploads/abc.webp"],
        }
    ],
    "full": False,
}


@pytest.fixture(scope="function")
def test_db():
    """SQLite in-memory для каждого теста.

    StaticPool ОБЯЗАТЕЛЕН: без него пул выдаёт каждому потоку своё
    соединение, а у :memory:-SQLite каждое соединение — отдельная пустая
    БД. TestClient выполняет приложение в другом потоке, и без StaticPool
    запросы падали бы с «no such table: supplies».
    """
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
    assert product.external_id == "00000012345"        # id из 1С сохранён
    assert product.sku == "ROSA-FREEDOM-60"
    assert product.barcode == "4600000000001"
    assert product.package_size == 25 and product.unit == "упаковка"
    assert product.min_quantity == 1                   # 25 шт при упаковке 25 = одна упаковка
    # вид, цвет, рост и страна — записи отдельных справочников (создались сами)
    assert (product.kind_name, product.color_name, product.height_cm, product.country_name) == (
        "Роза", "Красный", 60, "Эквадор")
    assert data["dictionaries_created"] == {}          # эти значения есть в стартовом наборе

    item = test_db.query(SupplyItem).one()
    assert item.price == 42.50                 # цена за ШТ
    assert item.stock == 120                   # остаток в УПАКОВКАХ
    assert item.is_active is True
    assert item.supply.is_service is True      # служебная поставка 1С


def test_sync_creates_missing_dictionary_values_once(client, test_db):
    """Значения, которых в справочниках ещё нет, создаются сами (и сообщаются в ответе) — без дублей."""
    novel = {"products": [{**PRODUCTS_PAYLOAD["products"][0], "kind": "Эустома", "color": "Малиновый",
                           "height": 65, "country": "Мексика"}]}
    first = client.post("/api/1c/products/sync", json=novel, headers=AUTH).json()
    assert first["dictionaries_created"] == {"kind": ["Эустома"], "color": ["Малиновый"],
                                             "height": ["65 см"], "country": ["Мексика"]}
    again = client.post("/api/1c/products/sync", json=novel, headers=AUTH).json()
    assert again["dictionaries_created"] == {}                     # повторная выгрузка ничего не плодит
    assert test_db.query(Kind).filter(Kind.name == "Эустома").count() == 1
    assert test_db.query(Height).filter(Height.cm == 65).count() == 1


def test_sync_is_idempotent_by_external_id(client, test_db):
    """Повторная выгрузка того же товара не создаёт дубль."""
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)
    sizes = [test_db.query(m).count() for m in (Kind, Color, Height, Country)]

    changed = {
        "products": [
            {
                **PRODUCTS_PAYLOAD["products"][0],
                "name": "Роза Freedom 60 см (новая упаковка)",
                "price": 45.0,
                "quantity": 2000,                          # 80 упаковок
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
    assert r.json()["dictionaries_created"] == {}                  # справочники не плодятся
    assert [test_db.query(m).count() for m in (Kind, Color, Height, Country)] == sizes

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

    # справочник товара: цен и остатков тут нет — они живут в поставках
    assert set(p) == {
        "id", "external_id", "sku", "barcode", "name", "description", "kind", "kind_id",
        "color", "color_id", "height", "height_id", "country", "country_id", "unit",
        "package_size", "min_quantity", "photos",
    }
    assert p["id"] == product.id               # id товара на сайте
    assert p["external_id"] == "00000012345"   # id товара в 1С
    assert p["sku"] == "ROSA-FREEDOM-60"
    assert p["barcode"] == "4600000000001"
    assert p["name"] == "Роза Freedom 60 см"
    assert p["package_size"] == 25
    assert p["min_quantity"] == 25             # в штуках, как и присылала 1С
    assert (p["kind"], p["color"], p["height"], p["country"]) == ("Роза", "Красный", 60, "Эквадор")
    # рядом с названиями — id записей справочников на сайте
    assert (p["kind_id"], p["color_id"], p["height_id"], p["country_id"]) == (
        product.kind_id, product.color_id, product.height_id, product.country_id)
    assert p["photos"] == ["https://dianthus-opt.ru/static/uploads/abc.webp"]


def test_products_export_lists_every_product_even_when_nothing_is_on_the_shelf(client, test_db):
    client.post("/api/1c/products/sync", json=PRODUCTS_PAYLOAD, headers=AUTH)
    test_db.query(SupplyItem).update({SupplyItem.is_active: False})
    test_db.commit()

    # справочник не зависит от полки: товар на месте и без остатка
    assert client.get("/api/1c/products/export", headers=AUTH).json()["count"] == 1


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
    # контрагент: id (на сайте), id в 1С, название, ИНН, email, адрес, телефон
    assert o["counterparty"] == {
        "id": user.id, "external_id": "", "name": "ООО Ромашка", "inn": "7701234567",
        "email": "shop@test.ru", "address": "", "phone": "+79001234567",
    }

    line = o["items"][0]
    assert line["product_id"] == product.id
    assert line["external_id"] == "00000012345"
    assert line["sku"] == "ROSA-FREEDOM-60"
    assert line["barcode"] == "4600000000001"
    assert line["package_size"] == 25
    assert line["price"] == 42.5
    assert line["quantity"] == 50                      # в штуках: 2 упаковки по 25
    assert line["sum"] == 2125.0
    assert o["sum"] == 2125.0                          # сумма заказа до скидки
    # остаток «без поставки» — поставка без id в 1С
    assert line["supply"] == {"id": item.supply_id, "external_id": "", "number": "", "date": ""}


def test_orders_export_rejects_unknown_status(client):
    r = client.get("/api/1c/orders/export?status=Несуществующий", headers=AUTH)
    assert r.status_code == 400
