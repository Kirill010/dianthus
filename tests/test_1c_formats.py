# tests/test_1c_formats.py
"""Формат обмена с 1С: одни и те же короткие названия (price, quantity, sum, total) везде.

Примеры берутся из app/content/1c_formats.json — это тот самый файл, который получает программист
(и на сайте он открыт по адресу /1c-docs.json). Здесь каждый пример прогоняется через настоящий API:
запрос должен приниматься, а ответ — совпадать с описанным по ключам и типам. Если что-то в обмене
изменится, а файл останется прежним, этот модуль упадёт раньше, чем программист 1С получит
неожиданный ответ."""
import copy
from datetime import datetime

from app.models import Color, Height, Product, Supply, SupplyItem, User
from app.services import api_keys, onec_brief, upload_service
from tests.helpers import make_order, make_user, seed_item
from tests.test_1c_photos import picture

OLD_NAMES = {"price_per_stem", "stock_packs", "stock_stems", "quantity_packs", "quantity_stems",
             "subtotal", "stems", "line_sum", "price_per_piece", "pieces", "pieces_per_pack",
             "packs"}


def example(endpoint: str, part: str):
    """Пример из файла форматов (каждый раз свежая копия)."""
    return copy.deepcopy(onec_brief.formats()["endpoints"][endpoint][part])


def same_shape(real, doc, path="$"):
    """Те же ключи и типы значений, что в описании. Числа — просто «число»;
    null в описании — «что угодно», пустой список — «любой список»."""
    if doc is None:
        return
    if isinstance(doc, dict):
        assert isinstance(real, dict), f"{path}: ожидался объект, пришло {real!r}"
        assert set(real) == set(doc), f"{path}: ключи {sorted(real)} ≠ описанным {sorted(doc)}"
        for key, value in doc.items():
            same_shape(real[key], value, f"{path}.{key}")
    elif isinstance(doc, list):
        assert isinstance(real, list), f"{path}: ожидался список, пришло {real!r}"
        if doc and real:
            same_shape(real[0], doc[0], f"{path}[0]")
    elif isinstance(doc, bool):
        assert isinstance(real, bool), f"{path}: ожидалось true/false, пришло {real!r}"
    elif isinstance(doc, (int, float)):
        assert isinstance(real, (int, float)) and not isinstance(real, bool), \
            f"{path}: ожидалось число, пришло {real!r}"
    else:
        assert isinstance(real, str), f"{path}: ожидалась строка, пришло {real!r}"


def all_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, inner in value.items():
            found.add(key)
            all_keys(inner, found)
    elif isinstance(value, list):
        for inner in value:
            all_keys(inner, found)
    return found


def key_header(db):
    _, raw = api_keys.create_key(db, "Формат", "")
    return {"X-API-Key": raw}


def send_story(web, mem_db):
    """Инвойс 00000624 из описания: сначала в пути (все реквизиты строк), потом оприходован."""
    h = key_header(mem_db)
    assert web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request"),
                    headers=h).status_code == 200
    assert web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request_received"),
                    headers=h).status_code == 200
    item = (mem_db.query(SupplyItem).join(Product)
            .filter(Product.external_id == "00000012345").one())
    return h, item


# ───────────── единый словарь названий ─────────────

def test_the_documented_examples_use_only_the_short_unified_names():
    """price, quantity, sum, total — одинаково во всём файле; прежних названий там нет."""
    keys = all_keys(onec_brief.formats()["endpoints"])
    assert not keys & OLD_NAMES, sorted(keys & OLD_NAMES)
    assert {"price", "quantity", "sum", "total"} <= keys


def test_every_documented_endpoint_has_a_title_a_url_and_an_example():
    for name, spec in onec_brief.formats()["endpoints"].items():
        assert spec["title"] and spec["url"].startswith("{{URL}}/api/1c/"), name
        assert spec["method"] in ("GET", "POST") and spec["direction"], name
        assert "request" in spec or "response" in spec, name


# ───────────── инвойсы: 1С → сайт ─────────────

def test_supplies_sample_is_accepted_and_goes_on_the_shelf_by_supply(web, mem_db):
    h = key_header(mem_db)
    r = web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request"), headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == example("supplies_sync", "response")
    mem_db.expire_all()
    supply = mem_db.query(Supply).one()
    assert (supply.external_id, supply.number, supply.status) == ("00000624", "00000624", "В пути")
    assert supply.label.startswith("№ 00000624")           # дату «в пути» показываем, пока она не прошла
    lines = {i.product.external_id: i for i in supply.items}
    assert {k: (v.stock, v.price, v.is_active) for k, v in lines.items()} == {
        "00000012345": (120, 42.5, False),             # 3000 шт / 25 = 120 упаковок; в пути — только предзаказ
        "00000012346": (100, 48.0, False),
    }
    product = lines["00000012345"].product
    assert (product.sku, product.barcode, product.package_size) == ("ROSA-FREEDOM-60", "4600000000001", 25)
    assert (product.kind_name, product.color_name, product.height_cm, product.country_name) == (
        "Роза", "Красный", 60, "Эквадор")

    again = web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request"), headers=h).json()
    assert (again["created"], again["updated"], again["errors"]) == (0, 1, [])      # не дубль
    assert mem_db.query(Supply).count() == 1 and mem_db.query(SupplyItem).count() == 2


def test_received_invoice_puts_the_remaining_quantity_on_the_shelf(web, mem_db):
    h = key_header(mem_db)
    web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request"), headers=h)
    r = web.post("/api/1c/supplies/sync", json=example("supplies_sync", "request_received"), headers=h)
    same_shape(r.json(), example("supplies_sync", "response"))
    assert (r.json()["created"], r.json()["updated"], r.json()["received"]) == (0, 1, 1)
    mem_db.expire_all()
    assert mem_db.query(Supply).one().status == "Разгружен"
    lines = {i.product.external_id: i for i in mem_db.query(SupplyItem).all()}
    assert (lines["00000012345"].stock, lines["00000012345"].is_active) == (118, True)   # 2950 шт / 25
    assert (lines["00000012346"].stock, lines["00000012346"].is_active) == (100, True)
    assert lines["00000012345"].product.sku == "ROSA-FREEDOM-60"       # реквизиты первой отправки не потеряны


def test_every_documented_supplies_request_is_valid_as_a_dry_run(web, mem_db):
    h = key_header(mem_db)
    for part in ("request", "request_received"):
        r = web.post("/api/1c/supplies/sync?dry_run=1", json=example("supplies_sync", part), headers=h)
        assert r.status_code == 200 and r.json()["dry_run"] is True and r.json()["errors"] == [], r.text
    assert mem_db.query(Supply).count() == 0


# ───────────── товары: 1С → сайт ─────────────

def test_products_sample_is_accepted_as_is(web, mem_db):
    h = key_header(mem_db)
    r = web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h)
    assert r.status_code == 200, r.text
    same_shape(r.json(), example("products_sync", "response"))
    assert (r.json()["created"], r.json()["errors"]) == (1, [])
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert (p.external_id, p.sku, p.barcode) == ("00000012345", "ROSA-FREEDOM-60", "4600000000001")
    assert p.name == "Роза Freedom 60 см" and p.description == "Классическая красная роза"
    assert (p.kind_name, p.color_name, p.height_cm, p.country_name) == ("Роза", "Красный", 60, "Эквадор")
    assert (p.unit, p.package_size, p.min_quantity) == ("упаковка", 25, 1)     # 25 шт = 1 упаковка
    assert mem_db.query(SupplyItem).count() == 0         # карточка без price и quantity на полку не попадает

    again = web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h).json()
    assert (again["created"], again["updated"]) == (0, 1)               # тот же товар, не дубль


def test_product_with_price_and_quantity_is_sold_without_a_supply(web, mem_db):
    h = key_header(mem_db)
    body = example("products_sync", "request")
    body["products"][0].update(price=42.5, quantity=3000)
    assert web.post("/api/1c/products/sync", json=body, headers=h).json()["errors"] == []
    mem_db.expire_all()
    item = mem_db.query(SupplyItem).one()
    assert (item.price, item.stock, item.is_active) == (42.5, 120, True)
    assert item.supply.is_service and item.supply.label == ""

    # заказ такого товара уходит в 1С с пустой поставкой
    user = make_user(mem_db, "shop@test.ru")
    make_order(mem_db, user, item, packs=2)
    line = web.get("/api/1c/orders/export", headers=h).json()["orders"][0]["items"][0]
    assert line["supply"]["external_id"] == "" and line["supply"]["number"] == ""


def test_old_field_names_are_refused_with_a_hint(web, mem_db):
    h = key_header(mem_db)
    row = {"id": "X", "name": "Роза", "package_size": 25}
    r = web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**row, "price_per_stem": 40, "stock_packs": 5},
        {**row, "id": "Y", "name": "Гвоздика", "price": 40, "quantity_stems": 100},
    ]}).json()
    assert r["created"] == 0 and r["errors_total"] == 2
    assert "price_per_stem" in r["errors"][0] and "price" in r["errors"][0]
    assert "quantity_stems" in r["errors"][1] and "quantity" in r["errors"][1]

    s = web.post("/api/1c/supplies/sync", headers=h, json={"supplies": [
        {"id": "S-1", "date": "2026-10-29", "status": "in_transit", "items": [
            {**row, "price_per_stem": 40, "quantity_stems": 100}]}]}).json()
    assert s["lines"] == 0 and "price_per_stem" in s["errors"][0]
    assert mem_db.query(SupplyItem).count() == 0


def test_min_quantity_is_in_stems_and_rounded_up_to_whole_packs(web, mem_db):
    h = key_header(mem_db)
    base = {"price": 10, "quantity": 500, "package_size": 25}
    web.post("/api/1c/products/sync", headers=h, json={"products": [
        {**base, "id": "A", "name": "Мин 25", "min_quantity": 25},
        {**base, "id": "B", "name": "Мин 26", "min_quantity": 26},
        {**base, "id": "C", "name": "Мин 100", "min_quantity": 100}]})
    mem_db.expire_all()
    packs = {p.name: p.min_quantity for p in mem_db.query(Product).all()}
    assert packs == {"Мин 25": 1, "Мин 26": 2, "Мин 100": 4}
    exported = {p["name"]: p["min_quantity"]
                for p in web.get("/api/1c/products/export", headers=h).json()["products"]}
    assert exported == {"Мин 25": 25, "Мин 26": 50, "Мин 100": 100}     # снова в штуках


# ───────────── товары: сайт → 1С ─────────────

def test_products_export_has_the_same_fields_as_the_sync_plus_site_ids(web, mem_db):
    h = key_header(mem_db)
    web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h)
    exported = web.get("/api/1c/products/export", headers=h).json()
    same_shape(exported, example("products_export", "response"))
    assert exported["count"] == 1
    p = exported["products"][0]
    sent = example("products_sync", "request")["products"][0]
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
    assert set(p) - set(sent) == {"external_id", "kind_id", "color_id", "height_id", "country_id"}
    assert not set(p) & OLD_NAMES                              # цен и остатков в справочнике нет


def test_exported_product_can_be_sent_back_without_changes(web, mem_db):
    """Выгрузку можно поправить в 1С и отправить обратно: ничего не дублируется и не ломается."""
    h = key_header(mem_db)
    web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h)
    exported = web.get("/api/1c/products/export", headers=h).json()["products"]
    r = web.post("/api/1c/products/sync", json={"products": exported}, headers=h).json()
    assert (r["created"], r["updated"], r["errors"]) == (0, 1, [])
    assert mem_db.query(Product).count() == 1


def test_product_is_found_by_barcode_when_the_1c_id_is_new(web, mem_db):
    """Штрихкод — запасной ключ: товар заведён вручную (без id 1С), 1С присылает его со штрихкодом."""
    h = key_header(mem_db)
    seed_item(mem_db, name="Роза вручную", barcode="4600000000001")
    r = web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h).json()
    assert (r["created"], r["updated"]) == (0, 1)
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    assert p.external_id == "00000012345" and p.name == "Роза Freedom 60 см"


def test_unit_is_accepted_and_kept_when_not_sent(web, mem_db):
    h = key_header(mem_db)
    body = example("products_sync", "request")
    body["products"][0]["unit"] = "шт"
    web.post("/api/1c/products/sync", json=body, headers=h)
    del body["products"][0]["unit"]
    web.post("/api/1c/products/sync", json=body, headers=h)             # без unit — не трогаем
    mem_db.expire_all()
    assert mem_db.query(Product).one().unit == "шт"
    export = web.get("/api/1c/products/export", headers=h).json()["products"][0]
    assert export["unit"] == "шт"


# ───────────── заказы: сайт → 1С ─────────────

def test_orders_export_matches_the_documented_sample_exactly(web, mem_db):
    h, item = send_story(web, mem_db)
    user = make_user(mem_db, "shop@test.ru", company_name="ООО Ромашка", phone="+79001234567",
                     inn="7701234567", address="Москва, ул. Цветочная, 1", external_id="000001234",
                     discount_percent=5)
    order = make_order(mem_db, user, item, packs=2)
    order.comment = "Позвонить за час"
    order.created_at = datetime(2026, 10, 27, 6, 31)                     # 09:31 по Москве
    order.desired_date = datetime(2026, 10, 30)
    order.delivery_method = "delivery"
    order.delivery_address = "Москва, ул. Цветочная, 1"
    order.discount_percent = 5
    order.total_price = 2018.75
    mem_db.commit()

    got = web.get("/api/1c/orders/export", headers=h).json()
    # в описании стоят свои номера; подставляем настоящие и сравниваем ВСЁ целиком
    expected = example("orders_export", "response")
    expected["last_id"] = order.id
    o = expected["orders"][0]
    o["id"], o["number"] = order.id, f"САЙТ-{order.id}"
    o["counterparty"]["id"] = user.id
    o["items"][0]["product_id"] = item.product_id
    o["items"][0]["supply"]["id"] = item.supply_id
    assert got == expected
    same_shape(got, example("orders_export", "response"))
    # единые названия: цена и количество в штуках, sum — до скидки, total — с ней
    line = got["orders"][0]["items"][0]
    assert (line["price"], line["quantity"], line["sum"]) == (42.5, 50, 2125.0)
    assert (got["orders"][0]["sum"], got["orders"][0]["total"]) == (2125.0, 2018.75)
    assert not all_keys(got) & OLD_NAMES


def test_order_without_optional_data_keeps_the_same_keys(web, mem_db):
    """Нет даты получения, адреса, id в 1С, поставки — ключи на месте, значения пустые."""
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru")
    make_order(mem_db, user, seed_item(mem_db, stock=10), packs=1)
    sample = example("orders_export", "response")["orders"][0]
    o = web.get("/api/1c/orders/export", headers=h).json()["orders"][0]
    assert set(o) == set(sample)
    assert set(o["counterparty"]) == set(sample["counterparty"])
    assert set(o["items"][0]) == set(sample["items"][0])
    assert set(o["items"][0]["supply"]) == set(sample["items"][0]["supply"])
    assert (o["desired_date"], o["delivery_method"], o["delivery_address"]) == ("", "", "")
    assert o["counterparty"]["external_id"] == "" and o["counterparty"]["address"] == ""
    assert o["items"][0]["external_id"] == "" and o["items"][0]["barcode"] == ""
    assert o["items"][0]["supply"] == {"id": o["items"][0]["supply"]["id"], "external_id": "",
                                       "number": "", "date": ""}      # ручная поставка: не из 1С


def test_orders_csv_has_one_line_per_item_with_the_same_ids(web, mem_db):
    h, item = send_story(web, mem_db)
    user = make_user(mem_db, "shop@test.ru", address="Москва", external_id="000001234")
    order = make_order(mem_db, user, item, packs=2)
    text = web.get("/api/1c/orders/export?format=csv", headers=h).content.decode("utf-8-sig")
    header, line = text.strip().splitlines()
    assert not set(header.split(";")) & OLD_NAMES
    cols = dict(zip(header.split(";"), line.split(";")))
    assert cols["order_number"] == f"САЙТ-{order.id}" and cols["order_id"] == str(order.id)
    assert cols["counterparty_id"] == str(user.id) and cols["counterparty_external_id"] == "000001234"
    assert cols["counterparty_address"] == "Москва" and cols["counterparty_inn"] == "7701234567"
    assert (cols["product_id"], cols["external_id"], cols["sku"], cols["barcode"]) == (
        str(item.product_id), "00000012345", "ROSA-FREEDOM-60", "4600000000001")
    assert (cols["supply_id"], cols["supply_external_id"], cols["supply_number"],
            cols["supply_date"]) == (str(item.supply_id), "00000624", "00000624", "2026-10-29")
    assert (cols["package_size"], cols["price"], cols["quantity"], cols["sum"]) == (
        "25", "42.5", "50", "2125.0")
    assert (cols["order_sum"], cols["order_total"]) == ("2125.0", "2125.0")


def test_documented_ack_and_status_requests_work(web, mem_db):
    h, item = send_story(web, mem_db)
    user = make_user(mem_db, "shop@test.ru")
    order = make_order(mem_db, user, item, packs=1)

    ack = example("orders_ack", "request")
    ack["orders"][0]["number"] = f"САЙТ-{order.id}"
    r = web.post("/api/1c/orders/ack", json=ack, headers=h)
    assert r.json() == example("orders_ack", "response")
    mem_db.refresh(order)
    assert order.onec_number == "ЗК00000123"

    status = example("orders_status", "request")
    status["orders"][0]["number"] = f"САЙТ-{order.id}"
    r = web.post("/api/1c/orders/status", json=status, headers=h)
    assert r.json() == example("orders_status", "response")
    mem_db.refresh(order)
    assert order.status == "Отправлен" and "Передан в доставку" in order.staff_note


# ───────────── контрагенты ─────────────

def test_clients_export_has_the_counterparty_fields(web, mem_db):
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru", address="Москва, ул. Цветочная, 1",
                     external_id="000001234", discount_percent=5)
    make_user(mem_db, "boss@test.ru", admin=True)                        # сотрудников в выгрузке нет
    data = web.get("/api/1c/clients/export", headers=h).json()
    same_shape(data, example("clients_export", "response"))
    assert data["clients"] == [{
        "id": user.id, "external_id": "000001234", "name": "ООО Ромашка", "inn": "7701234567",
        "email": "shop@test.ru", "address": "Москва, ул. Цветочная, 1", "phone": "+79001234567",
        "discount_percent": 5,
    }]
    # то же описание контрагента, что и внутри заказа
    order_cp = example("orders_export", "response")["orders"][0]["counterparty"]
    assert set(data["clients"][0]) == set(order_cp) | {"discount_percent"}


def test_documented_clients_sync_request_is_accepted_as_is(web, mem_db):
    h = key_header(mem_db)
    user = make_user(mem_db, "shop@test.ru")
    body = example("clients_sync", "request")
    body["clients"][0]["id"] = user.id                                   # в описании свой номер
    r = web.post("/api/1c/clients/sync", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == example("clients_sync", "response")
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
    same_shape(data, example("dictionaries", "response"))
    assert {"Роза", "Хризантема"} <= {k["name"] for k in data["kinds"]}
    assert [r["cm"] for r in data["heights"]] == sorted(r["cm"] for r in data["heights"])
    assert web.get("/api/1c/dictionaries").status_code == 401           # без ключа закрыто
    assert web.post("/api/1c/dictionaries", headers=h, json={}).status_code == 405   # только чтение


def test_a_value_can_be_sent_by_site_id_or_by_1c_id_instead_of_the_name(web, mem_db):
    h = key_header(mem_db)
    data = web.get("/api/1c/dictionaries", headers=h).json()
    rose = next(k["id"] for k in data["kinds"] if k["name"] == "Роза")
    row = {"name": "Товар", "price": 10, "quantity": 50, "package_size": 10}

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
    row = {"price": 10, "quantity": 50, "package_size": 10}
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
    web.post("/api/1c/products/sync", json=example("products_sync", "request"), headers=h)
    body = example("products_sync", "request")
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


# ───────────── служебное и фото ─────────────

def test_ping_has_the_documented_shape(web, mem_db):
    h = key_header(mem_db)
    r = web.get("/api/1c/ping", headers=h)
    same_shape(r.json(), example("ping", "response"))
    assert r.json()["status"] == "ok" and r.json()["auth"] is True


def test_photo_can_be_attached_by_barcode_and_the_reply_has_the_documented_shape(
        web, mem_db, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    h = key_header(mem_db)
    item = seed_item(mem_db, barcode="4600000000001", external_id="G-9")
    r = web.post("/api/1c/products/photo", params={"barcode": "4600000000001"},
                 content=picture(), headers=h)
    assert r.status_code == 200, r.text
    same_shape(r.json(), example("products_photo", "response"))
    assert r.json()["product"]["barcode"] == "4600000000001"
    mem_db.expire_all()
    assert len(mem_db.get(Product, item.product_id).photos) == 1
    assert web.post("/api/1c/products/photo", params={"barcode": "000"}, content=picture(),
                    headers=h).status_code == 404
