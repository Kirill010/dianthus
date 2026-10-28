# tests/test_1c_formats.py
"""Формат обмена с 1С — ровно по образцам владельца (товар, заказ, контрагенты).

Образцы вставлены сюда ДОСЛОВНО. Если что-то в обмене изменится, этот файл упадёт раньше,
чем программист 1С получит неожиданный ответ. У каждой сущности есть id:
  контрагент — название, ИНН, id, email, адрес, телефон;
  товар      — наименование, артикул, штрихкод, id;
  заказ      — дата, номер, id на сайте."""
import copy
import json
from datetime import datetime

from app.models import Color, Height, Product, User
from app.services import api_keys, upload_service
from tests.helpers import make_order, make_user, seed_item
from tests.test_1c_photos import picture

# ───────────── образцы владельца (без изменений) ─────────────

OWNER_PRODUCT_SYNC = json.loads(
    '{"products":[{"id":"00000012345","sku":"ROSA-FREEDOM-60","barcode":"4600000000001",'
    '"name":"Роза Freedom 60 см","description":"Классическая красная роза","kind":"Роза",'
    '"color":"Красный","height":60,"country":"Эквадор","unit":"упаковка","package_size":25,'
    '"min_quantity":1,"price_per_stem":42.5,"stock_packs":120,"photos":[]}],"full":false}')

OWNER_ORDERS_EXPORT = json.loads(
    '{"count":1,"has_more":false,"last_id":57,"orders":[{"id":57,"number":"САЙТ-57",'
    '"date":"2026-10-27T09:31:00","status":"Новый","comment":"Позвонить за час",'
    '"desired_date":"2026-10-30T00:00:00","delivery_method":"delivery",'
    '"delivery_address":"Москва, ул. Цветочная, 1","subtotal":2125.0,"discount_percent":0,'
    '"total":2125.0,"counterparty":{"id":8,"external_id":"000001234","name":"ООО Ромашка",'
    '"inn":"7701234567","email":"shop@test.ru","address":"Москва, ул. Цветочная, 1",'
    '"phone":"+79001234567"},"items":[{"product_id":15,"external_id":"00000012345",'
    '"sku":"ROSA-FREEDOM-60","barcode":"4600000000001","name":"Роза Freedom 60 см",'
    '"quantity_packs":2,"package_size":25,"stems":50,"price_per_stem":42.5,"sum":2125.0}]}]}')

OWNER_CLIENTS_SYNC = json.loads(
    '{"clients":[{"email":"shop@test.ru","external_id":"000001234","discount_percent":5},'
    '{"inn":"7701234567","discount_percent":7.5}]}')


def shape(value):
    """Форма данных: какие ключи и какого типа значения (числа — просто «number»)."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return "string" if isinstance(value, str) else "null"


def key_header(db):
    _, raw = api_keys.create_key(db, "Формат", "")
    return {"X-API-Key": raw}


# ───────────── товары: 1С → сайт ─────────────

def test_owner_product_sample_is_accepted_as_is(web, mem_db):
    h = key_header(mem_db)
    r = web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["errors"]) == (1, [])
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert (p.external_id, p.sku, p.barcode) == ("00000012345", "ROSA-FREEDOM-60", "4600000000001")
    assert p.name == "Роза Freedom 60 см" and p.description == "Классическая красная роза"
    assert (p.kind_name, p.color_name, p.height_cm, p.country_name) == ("Роза", "Красный", 60, "Эквадор")
    assert (p.unit, p.package_size, p.min_quantity) == ("упаковка", 25, 1)
    item = p.supply_items[0]
    assert item.price == 42.5 and item.stock == 120

    again = web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h).json()
    assert (again["created"], again["updated"]) == (0, 1)               # тот же товар, не дубль


def test_product_is_found_by_barcode_when_the_1c_id_is_new(web, mem_db):
    """Штрихкод — запасной ключ: товар заведён вручную (без id 1С), 1С присылает его со штрихкодом."""
    h = key_header(mem_db)
    seed_item(mem_db, name="Роза вручную", barcode="4600000000001")
    r = web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h).json()
    assert (r["created"], r["updated"]) == (0, 1)
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert p.external_id == "00000012345" and p.name == "Роза Freedom 60 см"


def test_unit_is_accepted_and_kept_when_not_sent(web, mem_db):
    h = key_header(mem_db)
    body = copy.deepcopy(OWNER_PRODUCT_SYNC)
    body["products"][0]["unit"] = "шт"
    web.post("/api/1c/products/sync", json=body, headers=h)
    del body["products"][0]["unit"]
    web.post("/api/1c/products/sync", json=body, headers=h)             # без unit — не трогаем
    mem_db.expire_all()
    assert mem_db.query(Product).one().unit == "шт"
    export = web.get("/api/1c/products/export", headers=h).json()["products"][0]
    assert export["unit"] == "шт"


# ───────────── товары: сайт → 1С ─────────────

def test_products_export_has_the_same_fields_as_the_sync_plus_site_ids(web, mem_db):
    h = key_header(mem_db)
    web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h)
    exported = web.get("/api/1c/products/export", headers=h).json()
    assert set(exported) == {"count", "products"} and exported["count"] == 1
    p = exported["products"][0]
    sent = OWNER_PRODUCT_SYNC["products"][0]
    # всё, что 1С прислала, приходит обратно теми же ключами и значениями (id — теперь id на сайте)
    for key, value in sent.items():
        if key != "id":
            assert p[key] == value, key
    mem_db.expire_all()
    product = mem_db.query(Product).one()
    assert p["id"] == product.id and p["external_id"] == sent["id"]
    # справочники — названием и рядом id записи на сайте
    assert (p["kind_id"], p["color_id"], p["height_id"], p["country_id"]) == (
        product.kind_id, product.color_id, product.height_id, product.country_id)
    assert set(p) - set(sent) == {"external_id", "kind_id", "color_id", "height_id", "country_id",
                                  "reserved_packs", "available_packs"}


def test_exported_product_can_be_sent_back_without_changes(web, mem_db):
    """Выгрузку можно поправить в 1С и отправить обратно: ничего не дублируется и не ломается."""
    h = key_header(mem_db)
    web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h)
    exported = web.get("/api/1c/products/export", headers=h).json()["products"]
    r = web.post("/api/1c/products/sync", json={"products": exported}, headers=h).json()
    assert (r["created"], r["updated"], r["errors"]) == (0, 1, [])
    assert mem_db.query(Product).count() == 1


# ───────────── заказы: сайт → 1С ─────────────

def test_orders_export_matches_the_owner_sample_exactly(web, mem_db):
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru", company_name="ООО Ромашка", phone="+79001234567",
                     inn="7701234567", address="Москва, ул. Цветочная, 1", external_id="000001234")
    item = seed_item(mem_db, name="Роза Freedom 60 см", price=42.5, pack=25, stock=120,
                     sku="ROSA-FREEDOM-60", barcode="4600000000001", external_id="00000012345")
    order = make_order(mem_db, user, item, packs=2)
    order.comment = "Позвонить за час"
    order.created_at = datetime(2026, 10, 27, 6, 31)                     # 09:31 по Москве
    order.desired_date = datetime(2026, 10, 30)
    order.delivery_method = "delivery"
    order.delivery_address = "Москва, ул. Цветочная, 1"
    mem_db.commit()

    got = web.get("/api/1c/orders/export", headers=h).json()
    # в образце владельца стоят свои id; подставляем настоящие и сравниваем ВСЁ целиком
    expected = copy.deepcopy(OWNER_ORDERS_EXPORT)
    expected["last_id"] = order.id
    o = expected["orders"][0]
    o["id"], o["number"] = order.id, f"САЙТ-{order.id}"
    o["counterparty"]["id"] = user.id
    o["items"][0]["product_id"] = item.product_id
    assert got == expected
    assert shape(got) == shape(OWNER_ORDERS_EXPORT)


def test_order_without_optional_data_keeps_the_same_keys(web, mem_db):
    """Нет даты получения, адреса, id в 1С — ключи на месте, значения пустые (а не пропавшие)."""
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru")
    make_order(mem_db, user, seed_item(mem_db, stock=10), packs=1)
    o = web.get("/api/1c/orders/export", headers=h).json()["orders"][0]
    assert set(o) == set(OWNER_ORDERS_EXPORT["orders"][0])
    assert set(o["counterparty"]) == set(OWNER_ORDERS_EXPORT["orders"][0]["counterparty"])
    assert set(o["items"][0]) == set(OWNER_ORDERS_EXPORT["orders"][0]["items"][0])
    assert (o["desired_date"], o["delivery_method"], o["delivery_address"]) == ("", "", "")
    assert o["counterparty"]["external_id"] == "" and o["counterparty"]["address"] == ""
    assert o["items"][0]["external_id"] == "" and o["items"][0]["barcode"] == ""


def test_orders_csv_has_one_line_per_item_with_the_same_ids(web, mem_db):
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru", address="Москва", external_id="000001234")
    item = seed_item(mem_db, sku="ROSA-1", barcode="4600000000001", external_id="G-1")
    order = make_order(mem_db, user, item, packs=2)
    text = web.get("/api/1c/orders/export?format=csv", headers=h).content.decode("utf-8-sig")
    header, line = text.strip().splitlines()
    cols = dict(zip(header.split(";"), line.split(";")))
    assert cols["order_number"] == f"САЙТ-{order.id}" and cols["order_id"] == str(order.id)
    assert cols["counterparty_id"] == str(user.id) and cols["counterparty_external_id"] == "000001234"
    assert cols["counterparty_address"] == "Москва" and cols["counterparty_inn"] == "7701234567"
    assert (cols["product_id"], cols["external_id"], cols["sku"], cols["barcode"]) == (
        str(item.product_id), "G-1", "ROSA-1", "4600000000001")


# ───────────── контрагенты ─────────────

def test_clients_export_has_the_counterparty_fields(web, mem_db):
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru", address="Москва, ул. Цветочная, 1",
                     external_id="000001234", discount_percent=5)
    make_user(mem_db, "boss@test.ru", admin=True)                        # сотрудников в выгрузке нет
    data = web.get("/api/1c/clients/export", headers=h).json()
    assert set(data) == {"count", "has_more", "last_id", "clients"}
    assert data["clients"] == [{
        "id": user.id, "external_id": "000001234", "name": "ООО Ромашка", "inn": "7701234567",
        "email": "shop@test.ru", "address": "Москва, ул. Цветочная, 1", "phone": "+79001234567",
        "discount_percent": 5,
    }]
    # то же описание контрагента, что и внутри заказа
    order_cp = OWNER_ORDERS_EXPORT["orders"][0]["counterparty"]
    assert set(data["clients"][0]) == set(order_cp) | {"discount_percent"}


def test_owner_clients_sample_is_accepted_as_is(web, mem_db):
    h = key_header(mem_db)
    make_user(mem_db, "shop@test.ru")
    r = web.post("/api/1c/clients/sync", json=OWNER_CLIENTS_SYNC, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["updated"], r.json()["errors"]) == (2, [])
    mem_db.expire_all()
    user = mem_db.query(User).filter(User.email == "shop@test.ru").one()
    assert user.external_id == "000001234" and user.discount_percent == 7.5


def test_client_is_found_by_email_then_by_1c_id_then_by_inn(web, mem_db):
    h = key_header(mem_db)
    linked = make_user(mem_db, "a@test.ru", inn="7701111111", external_id="C-1")
    by_inn = make_user(mem_db, "b@test.ru", inn="7702222222")
    r = web.post("/api/1c/clients/sync", headers=h, json={"clients": [
        {"email": "unknown@test.ru", "external_id": "C-1", "discount_percent": 3},    # email не нашёлся → по id 1С
        {"email": "nobody@test.ru", "inn": "7702222222", "external_id": "C-2", "discount_percent": 4},
    ]})
    assert (r.json()["updated"], r.json()["errors"]) == (2, [])
    mem_db.expire_all()
    assert mem_db.get(User, linked.id).discount_percent == 3
    assert (mem_db.get(User, by_inn.id).discount_percent, mem_db.get(User, by_inn.id).external_id) == (4, "C-2")


def test_clients_sync_refuses_ambiguous_and_staff_matches(web, mem_db):
    h = key_header(mem_db)
    make_user(mem_db, "a@test.ru", inn="7703333333")
    make_user(mem_db, "b@test.ru", inn="7703333333")
    make_user(mem_db, "boss@test.ru", admin=True)
    r = web.post("/api/1c/clients/sync", headers=h, json={"clients": [
        {"inn": "7703333333", "discount_percent": 5},            # два клиента с таким ИНН
        {"email": "boss@test.ru", "discount_percent": 5},        # владелец
        {"email": "a@test.ru", "discount_percent": 150},         # скидка вне 0…100
    ]}).json()
    assert r["updated"] == 0 and len(r["errors"]) == 3


# ───────────── справочники ─────────────

def test_dictionaries_endpoint_lists_the_four_tables_with_ids(web, mem_db):
    h = key_header(mem_db)
    data = web.get("/api/1c/dictionaries", headers=h).json()
    assert set(data) == {"kinds", "colors", "heights", "countries"}
    assert {"Роза", "Хризантема"} <= {k["name"] for k in data["kinds"]}
    assert set(data["kinds"][0]) == {"id", "external_id", "name"}
    assert set(data["colors"][0]) == {"id", "external_id", "name", "hex"}
    assert set(data["heights"][0]) == {"id", "external_id", "name", "cm"}
    assert [r["cm"] for r in data["heights"]] == sorted(r["cm"] for r in data["heights"])
    assert web.get("/api/1c/dictionaries").status_code == 401           # без ключа закрыто
    assert web.post("/api/1c/dictionaries", headers=h, json={}).status_code == 405   # только чтение


def test_a_value_can_be_sent_by_site_id_or_by_1c_id_instead_of_the_name(web, mem_db):
    h = key_header(mem_db)
    data = web.get("/api/1c/dictionaries", headers=h).json()
    rose = next(k["id"] for k in data["kinds"] if k["name"] == "Роза")
    row = {"name": "Товар", "price_per_stem": 10, "stock_packs": 5, "package_size": 10}

    # 1) id на сайте
    web.post("/api/1c/products/sync", headers=h, json={"products": [{**row, "id": "X-1", "kind_id": rose}]})
    # 2) название + id в 1С: сайт запоминает соответствие
    web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**row, "id": "X-2", "name": "Товар 2", "color": "Красный", "color_external_id": "COL-7"}]})
    # 3) потом достаточно одного id в 1С
    web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**row, "id": "X-3", "name": "Товар 3", "color_external_id": "COL-7"}]})
    mem_db.expire_all()
    got = {p.external_id: p for p in mem_db.query(Product).all()}
    assert got["X-1"].kind_name == "Роза"
    assert got["X-2"].color_name == got["X-3"].color_name == "Красный"
    assert mem_db.query(Color).filter(Color.external_id == "COL-7").count() == 1

    bad = web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**row, "id": "X-4", "name": "Товар 4", "kind_id": 99999}]}).json()
    assert bad["created"] == 0 and "нет записи" in bad["errors"][0]       # понятная ошибка, а не сбой


def test_bad_dictionary_values_are_row_errors_not_a_failed_exchange(web, mem_db):
    h = key_header(mem_db)
    row = {"price_per_stem": 10, "stock_packs": 5, "package_size": 10}
    r = web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**row, "id": "A", "name": "Нормальный", "height": 60},
        {**row, "id": "B", "name": "Рост текстом", "height": "высокий"},
        {**row, "id": "C", "name": "Рост 999", "height": 999},
        {**row, "id": "D", "name": "Рост «60 см»", "height": "60 см"},
    ]}).json()
    assert r["created"] == 2 and r["errors_total"] == 2
    assert {p.name for p in mem_db.query(Product).all()} == {"Нормальный", "Рост «60 см»"}
    assert mem_db.query(Height).filter(Height.cm == 60).count() == 1       # «60» и «60 см» — одна запись


def test_empty_value_clears_it_but_missing_key_keeps_it(web, mem_db):
    h = key_header(mem_db)
    web.post("/api/1c/products/sync", json=OWNER_PRODUCT_SYNC, headers=h)
    body = copy.deepcopy(OWNER_PRODUCT_SYNC)
    for key in ("kind", "color", "height", "country"):
        del body["products"][0][key]
    web.post("/api/1c/products/sync", json=body, headers=h)               # ключей нет → всё осталось
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert (p.kind_name, p.color_name, p.height_cm, p.country_name) == ("Роза", "Красный", 60, "Эквадор")
    body["products"][0]["color"] = ""                                      # прислали пустым → убрать цвет
    body["products"][0]["height"] = 0
    web.post("/api/1c/products/sync", json=body, headers=h)
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert (p.kind_name, p.color_name, p.height_cm, p.country_name) == ("Роза", "", 0, "Эквадор")


# ───────────── фото по штрихкоду ─────────────

def test_photo_can_be_attached_by_barcode(web, mem_db, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    h = key_header(mem_db)
    item = seed_item(mem_db, barcode="4600000000001", external_id="G-9")
    r = web.post("/api/1c/products/photo", params={"barcode": "4600000000001"},
                 content=picture(), headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["product"]["barcode"] == "4600000000001"
    mem_db.expire_all()
    assert len(mem_db.get(Product, item.product_id).photos) == 1
    assert web.post("/api/1c/products/photo", params={"barcode": "000"}, content=picture(),
                    headers=h).status_code == 404
