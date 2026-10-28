# tests/test_supply_sync.py
"""Работа по поставкам (инвойсам из 1С): приём, повторная отправка, оприходование, остатки по
партиям, предзаказы, заказ с привязкой к поставке; подсказки по названию товара.

Цена и количество в обмене одинаковы везде: price — за штуку, quantity — в штуках."""
from datetime import datetime, timedelta

import pytest

from app.config import config
from app.models import Notification, OrderItem, Preorder, Product, Supply, SupplyItem
from app.services import api_keys
from app.services.preorder_service import add_preorder_to_db
from app.services.product_guess import guess_category, guess_country, guess_length_cm
from app.services.supply_sync import close_stale_supplies
from app.services.taxonomy import set_taxonomy
from tests.helpers import csrf_from, login, make_order, make_user, seed_item


def key_header(db):
    _, raw = api_keys.create_key(db, "1С инвойсы", "")
    return {"X-API-Key": raw}


def line(name="Роза Фридом 60", **over):
    base = {"id": f"G-{name}", "name": name, "package_size": 25,
            "price": 42.5, "quantity": 3000}
    base.update(over)
    return base


def invoice(number="00000622", date="2026-10-29", status="in_transit", items=None, **over):
    base = {"id": number, "number": number, "date": date, "country": "Эквадор",
            "supplier": "ECUANROS S.A", "status": status,
            "items": [line()] if items is None else items}
    base.update(over)
    return base


def post(web, headers, supplies, **params):
    query = "".join(f"&{k}={v}" for k, v in params.items())
    return web.post("/api/1c/supplies/sync?x=1" + query, json={"supplies": supplies}, headers=headers)


def item_of(db, ext="00000622", name=None):
    """Позиция поставки с этим id инвойса (одна, либо по названию товара)."""
    db.expire_all()
    q = db.query(SupplyItem).join(Supply).join(Product).filter(Supply.external_id == ext)
    return (q.filter(Product.name == name) if name else q).one()


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []
    monkeypatch.setattr("app.services.notifier.send_batch_emails",
                        lambda emails: sent.extend(emails) or len(emails))
    return sent


# ───────────────────────── приём инвойса ─────────────────────────

def test_invoice_becomes_in_transit_supply(web, mem_db):
    h = key_header(mem_db)
    r = post(web, h, [invoice()])
    assert r.status_code == 200, r.text
    data = r.json()
    assert (data["created"], data["lines"], data["products_created"], data["errors_total"]) == (1, 1, 1, 0)

    mem_db.expire_all()
    supply = mem_db.query(Supply).one()
    assert supply.external_id == "00000622" and supply.is_from_1c and supply.number == "00000622"
    assert supply.status == "В пути" and supply.country == "Эквадор"
    assert supply.arrival_date == datetime(2026, 10, 29)
    assert supply.label == "№ 00000622 от 29.10.2026"
    assert "ECUANROS S.A" in supply.notes and "00000622" in supply.notes
    item = supply.items[0]
    assert item.stock == 3000 // 25 and item.price == 42.5 and item.is_active is False
    product = item.product
    # рост и вид поняли по названию, страна — со страны инвойса
    assert (product.height_cm, product.kind_name, product.country_name) == (60, "Роза", "Эквадор")
    assert data["dictionaries_created"] == {}                    # всё это есть в стартовом наборе


def test_number_is_kept_apart_from_the_1c_id(web, mem_db):
    """id — идентификатор документа в 1С (может быть GUID), number — номер для людей."""
    h = key_header(mem_db)
    post(web, h, [invoice("00000622", id="3f9a0c1e-0000-4000-8000-000000000622")])
    mem_db.expire_all()
    supply = mem_db.query(Supply).one()
    assert supply.external_id.startswith("3f9a0c1e") and supply.number == "00000622"
    assert supply.invoice_no == "00000622" and supply.label == "№ 00000622 от 29.10.2026"


def test_resending_the_same_invoice_does_not_duplicate(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(items=[line("Роза А 60"), line("Роза Б 70")])])
    again = post(web, h, [invoice(items=[line("Роза А 60", quantity=5000)])]).json()
    assert again["created"] == 0 and again["updated"] == 1
    mem_db.expire_all()
    assert mem_db.query(Supply).count() == 1
    items = mem_db.query(SupplyItem).all()
    assert len(items) == 1 and items[0].product.name == "Роза А 60" and items[0].stock == 200   # «Роза Б» убрали
    assert mem_db.query(Product).count() == 2                   # сам товар остался в справочнике


def test_in_transit_invoice_dry_run_saves_nothing(web, mem_db):
    h = key_header(mem_db)
    data = post(web, h, [invoice()], dry_run=1).json()
    assert data["dry_run"] is True and data["created"] == 1 and data["products_created"] == 1
    mem_db.expire_all()
    assert mem_db.query(Supply).count() == 0 and mem_db.query(Product).count() == 0


def test_bad_invoices_and_lines_are_reported_but_good_ones_load(web, mem_db):
    h = key_header(mem_db)
    r = post(web, h, [
        invoice("A-1"),
        {"number": "", "date": "2026-10-29"},                        # нет id
        invoice("A-3", status="странный"),                           # неизвестный статус
        invoice("A-4", date=""),                                     # нет даты прибытия
        invoice("A-5", items=[line("Роза Х 60", price=0),            # нет цены нигде
                              line("Роза Я 60", quantity=10),        # меньше пачки
                              line("Роза Э 60")]),
        "не объект",
    ])
    data = r.json()
    assert r.status_code == 200
    assert data["created"] == 2                                       # A-1 и A-5
    assert data["errors_total"] == 4, data["errors"]                  # id, статус, дата, цена
    assert len(data["warnings"]) == 1 and "меньше одной упаковки" in data["warnings"][0]
    mem_db.expire_all()
    a5 = mem_db.query(Supply).filter_by(external_id="A-5").one()
    assert [i.product.name for i in a5.items] == ["Роза Э 60"]


def test_line_without_quantity_or_with_old_names_is_a_line_error(web, mem_db):
    h = key_header(mem_db)
    no_qty = {"id": "G-1", "name": "Роза Ч 60", "package_size": 25, "price": 40}
    old = {"id": "G-2", "name": "Роза Ш 60", "package_size": 25, "price_per_stem": 40,
           "quantity_stems": 100}
    data = post(web, h, [invoice(items=[no_qty, old, line("Роза Э 60")])]).json()
    assert data["lines"] == 1 and data["errors_total"] == 2, data["errors"]
    assert "quantity" in data["errors"][0]
    assert "price_per_stem" in data["errors"][1] and "price" in data["errors"][1]


def test_missing_selling_price_falls_back_to_known_site_price(web, mem_db):
    seed_item(mem_db, name="Роза Фридом 60", price=44.0, service=True)
    h = key_header(mem_db)
    data = post(web, h, [invoice(items=[line(price=0)])]).json()
    assert data["errors_total"] == 0 and data["lines"] == 1
    assert item_of(mem_db).price == 44.0


def test_empty_list_and_bad_body_are_safe(web, mem_db):
    h = key_header(mem_db)
    transit_id = post(web, h, [invoice()]).status_code
    assert transit_id == 200
    empty = post(web, h, []).json()
    assert empty["processed"] == 0
    mem_db.expire_all()
    assert mem_db.query(Supply).one().status == "В пути"              # пустой список ничего не закрыл
    assert web.post("/api/1c/supplies/sync", json={"supplies": "x"}, headers=h).status_code == 400
    assert web.post("/api/1c/supplies/sync", json={"supplies": []}).status_code == 401


def test_supplies_sync_refreshes_the_prices_updated_strip(web, mem_db):
    """«Цены и остатки обновлены …» считается и по инвойсам — это основной путь цен и остатков."""
    from app.services import exchange_log
    h = key_header(mem_db)
    post(web, h, [invoice()], dry_run=1)
    mem_db.expire_all()
    assert exchange_log.last_catalog_update(mem_db) is None            # проверка не считается
    post(web, h, [invoice()])
    mem_db.expire_all()
    assert exchange_log.last_catalog_update(mem_db).endpoint == "supplies/sync"


def test_the_same_product_twice_in_one_invoice_is_summed(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500), line(quantity=250)])])
    assert item_of(mem_db).stock == 30                                 # 750 шт / 25


# ───────────────────────── витрина и предзаказ ─────────────────────────

def test_in_transit_items_are_shown_and_can_be_preordered(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice()])
    user = make_user(mem_db, "buyer@test.ru", password="Buyer1234")
    login(web, "buyer@test.ru", "Buyer1234")
    page = web.get("/catalog").text
    assert "Роза Фридом 60" in page and "29.10.2026" in page          # блок «Товары в пути»
    assert "Поставка:" in page and "№ 00000622" in page

    mem_db.expire_all()
    item = mem_db.query(SupplyItem).one()
    assert add_preorder_to_db(mem_db, user.id, item.id, 4) is True
    assert mem_db.query(Preorder).one().quantity == 4


def test_each_received_line_is_its_own_card_with_the_supply_number(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("00000622", status="received", items=[line(quantity=500)]),
                  invoice("00000700", date="2026-11-05", status="received",
                          items=[line(quantity=250, price=47)])])
    make_user(mem_db, "buyer@test.ru", password="Buyer1234")
    login(web, "buyer@test.ru", "Buyer1234")
    page = web.get("/catalog").text
    assert page.count("Роза Фридом 60") >= 2                           # одна роза — две партии, две карточки
    assert "№ 00000622 от 29.10.2026" in page and "№ 00000700 от 05.11.2026" in page
    one = item_of(mem_db, "00000700")
    detail = web.get(f"/product/{one.id}").text
    assert "Поставка:" in detail and "№ 00000700 от 05.11.2026" in detail


# ───────────────────────── товар приехал ─────────────────────────

def make_preorder(web, mem_db, h, *, qty=5, name="Роза Фридом 60"):
    """Инвойс в пути + клиент с предзаказом на его строку."""
    post(web, h, [invoice(items=[line(name)])])
    user = make_user(mem_db, "buyer@test.ru")
    transit = item_of(mem_db)
    assert add_preorder_to_db(mem_db, user.id, transit.id, qty)
    return user, transit


def test_received_invoice_puts_the_line_on_the_shelf_and_fulfils_the_preorder(web, mem_db, sent_emails):
    h = key_header(mem_db)
    user, transit = make_preorder(web, mem_db, h, qty=5)

    data = post(web, h, [invoice(status="received", items=[line(quantity=2950)])]).json()
    assert data["received"] == 1 and data["preorders_fulfilled"] == 1

    mem_db.expire_all()
    supply = mem_db.query(Supply).filter_by(external_id="00000622").one()
    assert supply.status == "Разгружен"
    item = supply.items[0]
    assert item.id == transit.id                                       # та же позиция, а не новая
    assert item.is_active and item.stock == 118 and item.price == 42.5   # 2950 шт / 25 — остаток партии
    assert item.reserved_stock == 5 and item.available_stock == 113
    pre = mem_db.query(Preorder).one()
    assert pre.is_fulfilled and pre.quantity == 5 and pre.supply_item_id == item.id
    assert "Предзаказ поступил" in mem_db.query(Notification).filter_by(user_id=user.id).one().text
    assert [m["to"] for m in sent_emails] == ["buyer@test.ru"]

    # повторная отправка того же состояния ничего не меняет
    again = post(web, h, [invoice(status="received", items=[line(quantity=2950)])]).json()
    assert again["received"] == 0 and again["preorders_fulfilled"] == 0
    assert len(sent_emails) == 1
    assert item_of(mem_db).reserved_stock == 5


def test_partial_stock_gives_partial_preorder(web, mem_db, sent_emails):
    h = key_header(mem_db)
    make_preorder(web, mem_db, h, qty=8)
    post(web, h, [invoice(status="received", items=[line(quantity=75)])])        # приехало 3 упаковки
    mem_db.expire_all()
    pre = mem_db.query(Preorder).one()
    assert pre.is_fulfilled and pre.quantity == 3
    assert "из 8" in mem_db.query(Notification).one().text


def test_preorder_waits_until_the_line_has_stock(web, mem_db, sent_emails):
    h = key_header(mem_db)
    make_preorder(web, mem_db, h, qty=5)
    first = post(web, h, [invoice(status="received", items=[line(quantity=0)])]).json()
    assert first["received"] == 1 and first["preorders_fulfilled"] == 0
    mem_db.expire_all()
    assert not mem_db.query(Preorder).one().is_fulfilled
    assert not item_of(mem_db).is_active                               # остаток 0 — на полке нет

    # остаток появился — следующий обмен выполняет предзаказ
    second = post(web, h, [invoice(status="received", items=[line(quantity=3000)])]).json()
    assert second["preorders_fulfilled"] == 1
    mem_db.expire_all()
    assert mem_db.query(Preorder).one().is_fulfilled
    assert item_of(mem_db).is_active and item_of(mem_db).reserved_stock == 5
    assert len(sent_emails) == 1


def test_preorder_falls_back_to_the_stock_without_supply(web, mem_db, sent_emails):
    """Партия распродана, но такой же товар лежит «без поставки» — предзаказ берётся оттуда."""
    h = key_header(mem_db)
    live = seed_item(mem_db, name="Роза Фридом 60", stock=10, price=42.5, service=True)
    make_preorder(web, mem_db, h, qty=5)
    data = post(web, h, [invoice(status="received", items=[line(quantity=0)])]).json()
    assert data["preorders_fulfilled"] == 1
    mem_db.expire_all()
    pre = mem_db.query(Preorder).one()
    assert pre.is_fulfilled and pre.supply_item_id == live.id
    assert mem_db.get(SupplyItem, live.id).reserved_stock == 5


def test_status_only_message_keeps_the_lines_and_an_empty_list_clears_them(web, mem_db, sent_emails):
    """Без items меняется только статус: оприходованный инвойс выкладывает то, что в нём было.
    items: [] — другое дело: «в партии ничего не осталось»."""
    h = key_header(mem_db)
    _, transit = make_preorder(web, mem_db, h, qty=5)
    data = post(web, h, [{"id": "00000622", "status": "received"}]).json()
    assert data["received"] == 1 and data["preorders_fulfilled"] == 1 and data["errors"] == []
    item = item_of(mem_db)
    assert item.id == transit.id and item.is_active and item.stock == 120
    assert item.reserved_stock == 5 and item.price == 42.5

    post(web, h, [invoice(status="received", items=[])])
    item = item_of(mem_db)                                       # на строку ссылается предзаказ — не удаляем
    assert item.stock == 0 and not item.is_active


def test_received_or_cancelled_for_unknown_invoice_is_skipped(web, mem_db):
    h = key_header(mem_db)
    data = post(web, h, [{"id": "NOPE", "status": "received"}, {"id": "NOPE2", "status": "cancelled"}]).json()
    assert data["skipped"] == 2 and data["errors_total"] == 0 and data["created"] == 0
    mem_db.expire_all()
    assert mem_db.query(Supply).count() == 0                          # пустых поставок не заводим


def test_a_new_invoice_whose_lines_are_all_bad_is_not_created(web, mem_db):
    h = key_header(mem_db)
    data = post(web, h, [invoice("BAD-1", items=[line(price="abc")]),
                         invoice("OK-1")]).json()
    assert (data["created"], data["skipped"], data["errors_total"]) == (1, 1, 1)
    mem_db.expire_all()
    assert [s.external_id for s in mem_db.query(Supply).all()] == ["OK-1"]


def test_cancelled_invoice_is_removed_or_closed_if_clients_wait(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("INV-1"), invoice("INV-2", items=[line("Роза Ж 60")])])
    user = make_user(mem_db, "buyer@test.ru")
    waiting_item = item_of(mem_db, "INV-2")
    add_preorder_to_db(mem_db, user.id, waiting_item.id, 2)

    data = post(web, h, [{"id": "INV-1", "status": "cancelled"}, {"id": "INV-2", "status": "cancelled"}]).json()
    assert data["cancelled"] == 2 and len(data["warnings"]) == 1 and "предзаказ" in data["warnings"][0]
    mem_db.expire_all()
    assert mem_db.query(Supply).filter_by(external_id="INV-1").count() == 0          # удалён
    closed = mem_db.query(Supply).filter_by(external_id="INV-2").one()
    assert closed.status == "Разгружен" and "отменён в 1С" in closed.notes             # клиенты ждут — сохранён


# ───────────────────────── остатки по партиям ─────────────────────────

def test_each_sync_sets_the_remaining_stock_of_the_batch(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    assert (item_of(mem_db).stock, item_of(mem_db).is_active) == (20, True)
    post(web, h, [invoice(status="received", items=[line(quantity=425)])])          # 1С: ушло 3 упаковки
    assert (item_of(mem_db).stock, item_of(mem_db).is_active) == (17, True)
    post(web, h, [invoice(status="received", items=[line(quantity=0)])])           # распродано
    assert (item_of(mem_db).stock, item_of(mem_db).is_active) == (0, False)
    post(web, h, [invoice(status="received", items=[line(quantity=100)])])         # докинули
    assert (item_of(mem_db).stock, item_of(mem_db).is_active) == (4, True)


def test_unexported_orders_are_subtracted_when_the_option_is_on(web, mem_db, monkeypatch):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    item = item_of(mem_db)
    buyer = make_user(mem_db, "b@test.ru")
    order = make_order(mem_db, buyer, item, packs=3)                # на сайте осталось 17

    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    assert item_of(mem_db).stock == 20                              # опция выключена: остаток как у 1С

    monkeypatch.setattr(config, "ONEC_RESERVE_UNEXPORTED", True)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    assert item_of(mem_db).stock == 17                              # 20 − 3 заказа, которых 1С ещё не видела

    web.post("/api/1c/orders/ack", headers=h, json={"numbers": [f"САЙТ-{order.id}"]})
    post(web, h, [invoice(status="received", items=[line(quantity=425)])])         # теперь 1С учла заказ
    assert item_of(mem_db).stock == 17


def test_line_with_orders_is_kept_but_taken_off_the_shelf_when_it_leaves_the_invoice(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line("Роза А 60", quantity=500),
                                                    line("Роза Б 60", quantity=500)])])
    sold = item_of(mem_db, name="Роза А 60")
    buyer = make_user(mem_db, "b@test.ru")
    make_order(mem_db, buyer, sold, packs=2)

    post(web, h, [invoice(status="received", items=[line("Роза Б 60", quantity=500)])])
    mem_db.expire_all()
    kept = mem_db.get(SupplyItem, sold.id)
    assert kept is not None and kept.stock == 0 and not kept.is_active   # заказ ссылается — не удаляем
    assert mem_db.query(OrderItem).one().supply_item_id == sold.id
    assert item_of(mem_db, name="Роза Б 60").is_active


def test_a_broken_row_does_not_change_the_existing_line(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line("Роза А 60", quantity=500),
                                                    line("Роза Б 60", quantity=500)])])
    data = post(web, h, [invoice(status="received", items=[
        line("Роза А 60", quantity=250), line("Роза Б 60", price="abc", quantity=0)])]).json()
    assert data["errors_total"] == 1
    assert item_of(mem_db, name="Роза А 60").stock == 10
    assert item_of(mem_db, name="Роза Б 60").stock == 20              # битая строка позицию не тронула


def test_order_from_a_received_line_goes_to_1c_with_its_supply_and_cancel_returns_it(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    item = item_of(mem_db)
    buyer = make_user(mem_db, "b@test.ru")
    order = make_order(mem_db, buyer, item, packs=20)                   # раскупили всю партию
    mem_db.refresh(item)
    assert item.stock == 0 and not item.is_active

    sent = web.get("/api/1c/orders/export?new=1", headers=h).json()["orders"][0]["items"][0]
    assert sent["supply"] == {"id": item.supply_id, "external_id": "00000622",
                              "number": "00000622", "date": "2026-10-29"}
    assert (sent["price"], sent["quantity"], sent["sum"]) == (42.5, 500, 21250.0)

    # отмена из 1С возвращает товар в ту же поставку и снова выкладывает его на полку
    r = web.post("/api/1c/orders/status", headers=h, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "Отменён"}]})
    assert r.json()["updated"] == 1, r.text
    mem_db.expire_all()
    item = mem_db.get(SupplyItem, item.id)
    assert item.stock == 20 and item.is_active


def test_supply_is_shown_on_the_order_pages(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    item = item_of(mem_db)
    buyer = make_user(mem_db, "b@test.ru", password="Buyer1234")
    order = make_order(mem_db, buyer, item, packs=2)
    label = "№ 00000622 от 29.10.2026"

    login(web, "b@test.ru", "Buyer1234")
    assert label in web.get("/orders").text                            # клиент видит, из какой поставки

    make_user(mem_db, "boss@test.ru", password="Admin1234", admin=True)
    web.cookies.clear()
    login(web, "boss@test.ru", "Admin1234")
    assert label in web.get(f"/admin/orders/{order.id}").text
    assert label in web.get(f"/admin/orders/{order.id}/print").text


# ───────────────────────── полный список ─────────────────────────

def test_full_list_closes_1c_supplies_that_are_no_longer_sent(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("A-1"),                                                    # в пути
                  invoice("B-2", status="received", items=[line("Роза Б 60", quantity=500)]),
                  invoice("C-3", status="received", items=[line("Роза В 60", quantity=0)])])
    manual = seed_item(mem_db, name="Ручная роза", stock=5)                         # ручная поставка
    service = seed_item(mem_db, name="Служебная роза", stock=5, service=True)

    dry = post(web, h, [invoice("A-1")], full=1, dry_run=1).json()
    assert dry["closed"] == 1                                                        # только B-2: C-3 уже пуста
    assert item_of(mem_db, "B-2").is_active                                          # проверка ничего не меняет

    data = post(web, h, [invoice("A-1")], full=1).json()
    assert data["closed"] == 1 and data["errors"] == []
    mem_db.expire_all()
    assert mem_db.query(Supply).filter_by(external_id="A-1").one().status == "В пути"
    closed = mem_db.query(Supply).filter_by(external_id="B-2").one()
    assert closed.status == "Разгружен" and not any(i.is_active for i in closed.items)
    assert mem_db.get(SupplyItem, manual.id).is_active and mem_db.get(SupplyItem, service.id).is_active

    # пустой «полный список» ничего не закрывает
    post(web, h, [invoice("B-2", status="received", items=[line("Роза Б 60", quantity=500)])])
    empty = web.post("/api/1c/supplies/sync?full=1", json={"supplies": []}, headers=h).json()
    assert empty["closed"] == 0 and empty["warnings"]
    assert item_of(mem_db, "B-2").is_active


# ───────────────────────── защита от двойной разгрузки ─────────────────────────

def test_1c_supply_is_never_unloaded_to_the_shelf(web, mem_db):
    from app.scheduler import _auto_unload_overdue_sync, do_unload

    h = key_header(mem_db)
    post(web, h, [invoice(date="2025-01-01")])                      # дата прибытия давно прошла
    _auto_unload_overdue_sync()
    mem_db.expire_all()
    supply = mem_db.query(Supply).one()
    assert supply.status == "В пути" and not supply.items[0].is_active
    assert do_unload(mem_db, supply) == 0


def test_unloading_a_manual_supply_leaves_1c_lines_on_the_shelf(web, mem_db):
    from app.scheduler import do_unload

    h = key_header(mem_db)
    post(web, h, [invoice(status="received", items=[line(quantity=500)])])
    manual = seed_item(mem_db, name="Ручная роза", stock=5, status="В пути", active=False).supply
    do_unload(mem_db, manual)
    mem_db.commit()
    assert item_of(mem_db).is_active                                 # партия 1С осталась на полке


def test_admin_cannot_unload_1c_supply_by_hand(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice()])
    make_user(mem_db, "boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    mem_db.expire_all()
    supply = mem_db.query(Supply).one()
    page = web.get(f"/admin/supplies/{supply.id}/edit").text
    assert "Эту поставку ведёт 1С" in page and "/unload" not in page
    token = csrf_from(page)
    r = web.post(f"/admin/supplies/{supply.id}/unload", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    mem_db.expire_all()
    assert mem_db.get(Supply, supply.id).status == "В пути"
    assert not mem_db.query(SupplyItem).one().is_active
    assert "из 1С · № 00000622" in web.get("/admin").text           # бейдж в списке поставок


# ───────────────────────── забытые инвойсы ─────────────────────────

def test_forgotten_1c_supplies_are_closed_after_three_weeks(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("OLD"), invoice("FRESH", items=[line("Роза Н 60")])])
    mem_db.expire_all()
    old = mem_db.query(Supply).filter_by(external_id="OLD").one()
    old.external_synced_at = datetime.utcnow() - timedelta(days=30)
    manual = Supply(country="Россия", status="В пути")              # ручная поставка не трогается
    mem_db.add(manual)
    mem_db.commit()
    assert close_stale_supplies(mem_db) == 1
    mem_db.expire_all()
    assert mem_db.query(Supply).filter_by(external_id="OLD").one().status == "Разгружен"
    assert mem_db.query(Supply).filter_by(external_id="FRESH").one().status == "В пути"
    assert mem_db.get(Supply, manual.id).status == "В пути"


# ───────────────────────── догадки по названию товара ─────────────────────────

@pytest.mark.parametrize("name,length", [
    ("Роза Фридом 60", 60), ("Роза Кения 40с", 40), ("Гвоздика Мондиал 70 см", 70),
    ("Роза Эсперансе 50 Tessa", 50), ("Роза 60 см 25 шт", 60), ("Роза x25 Эксплорер", 0),
    ("Гипсофила Миллион звёзд", 0), ("Роза 50/60", 0), ("Роза 200", 0), ("", 0),
])
def test_guess_length(name, length):
    assert guess_length_cm(name) == length


@pytest.mark.parametrize("name,country", [
    ("Роза Кения 40с", "Кения"), ("Роза Эквадор Фридом 60", "Эквадор"),
    ("Гвоздика колумбийская", "Колумбия"), ("Тюльпан Голландия 40", "Голландия"),
    ("Нидерландские тюльпаны", "Голландия"), ("Роза Фридом", ""),
])
def test_guess_country(name, country):
    assert guess_country(name) == country


@pytest.mark.parametrize("name,category", [
    ("Роза Эквадор Фридом 60", "Роза"), ("Роза Кения 40", "Роза"),
    ("Роза кустовая Баблс", "Роза кустовая"), ("Хризантема одноголовая", "Хризантема"),
    ("Гвоздика Мондиал 70", "Гвоздика"), ("Гипсофила", ""),
])
def test_guess_category(name, category):
    assert guess_category(name) == category


def test_sync_fills_only_empty_fields_from_name(web, mem_db):
    h = key_header(mem_db)
    body = {"products": [
        {"id": "N-1", "name": "Роза Кения 40с", "package_size": 25, "price": 30, "quantity": 500},
        {"id": "N-2", "name": "Роза Кения 40", "package_size": 25, "price": 30, "quantity": 500,
         "country": "Эквадор", "height": 70, "kind": "Свой раздел"},      # 1С прислала сама — не трогаем
    ]}
    assert web.post("/api/1c/products/sync", json=body, headers=h).status_code == 200
    mem_db.expire_all()
    a = mem_db.query(Product).filter_by(external_id="N-1").one()
    b = mem_db.query(Product).filter_by(external_id="N-2").one()
    assert (a.country_name, a.height_cm, a.kind_name) == ("Кения", 40, "Роза")
    assert (b.country_name, b.height_cm, b.kind_name) == ("Эквадор", 70, "Свой раздел")

    # менеджер поправил руками — повторная выгрузка без этих полей их не меняет
    set_taxonomy(mem_db, a, country="Колумбия", height=55)
    mem_db.commit()
    web.post("/api/1c/products/sync", json=body, headers=h)
    mem_db.expire_all()
    a = mem_db.query(Product).filter_by(external_id="N-1").one()
    assert (a.country_name, a.height_cm) == ("Колумбия", 55)
