# tests/test_supply_sync.py
"""Инвойсы из 1С → «товары в пути»: приём, повторная отправка, приём товара,
предзаказы, защита от двойной разгрузки; подсказки по названию товара."""
from datetime import datetime, timedelta

import pytest

from app.models import Notification, Preorder, Product, Supply, SupplyItem
from app.services import api_keys
from app.services.preorder_service import add_preorder_to_db
from app.services.product_guess import guess_category, guess_country, guess_length_cm
from app.services.supply_sync import close_stale_supplies
from app.services.taxonomy import set_taxonomy
from tests.helpers import csrf_from, login, make_user, seed_item


def key_header(db):
    _, raw = api_keys.create_key(db, "1С инвойсы", "")
    return {"X-API-Key": raw}


def line(name="Роза Фридом 60", **over):
    base = {"id": f"G-{name}", "name": name, "package_size": 25,
            "price_per_stem": 42.5, "quantity_stems": 3000}
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
    assert supply.external_id == "00000622" and supply.is_from_1c
    assert supply.status == "В пути" and supply.country == "Эквадор"
    assert supply.arrival_date == datetime(2026, 10, 29)
    assert "ECUANROS S.A" in supply.notes and "00000622" in supply.notes
    item = supply.items[0]
    assert item.stock == 3000 // 25 and item.price == 42.5 and item.is_active is False
    product = item.product
    # рост и вид поняли по названию, страна — со страны инвойса
    assert (product.height_cm, product.kind_name, product.country_name) == (60, "Роза", "Эквадор")
    assert data["dictionaries_created"] == {}                    # всё это есть в стартовом наборе


def test_resending_the_same_invoice_does_not_duplicate(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice(items=[line("Роза А 60"), line("Роза Б 70")])])
    again = post(web, h, [invoice(items=[line("Роза А 60", quantity_stems=5000)])]).json()
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
        invoice("A-5", items=[line("Роза Х 60", price_per_stem=0),   # нет цены нигде
                              line("Роза Я 60", quantity_stems=10),  # меньше пачки
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


def test_missing_selling_price_falls_back_to_known_site_price(web, mem_db):
    seed_item(mem_db, name="Роза Фридом 60", price=44.0, service=True)
    h = key_header(mem_db)
    data = post(web, h, [invoice(items=[line(price_per_stem=0)])]).json()
    assert data["errors_total"] == 0 and data["lines"] == 1
    mem_db.expire_all()
    transit = mem_db.query(SupplyItem).join(Supply).filter(Supply.external_id == "00000622").one()
    assert transit.price == 44.0


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


# ───────────────────────── витрина и предзаказ ─────────────────────────

def test_in_transit_items_are_shown_and_can_be_preordered(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice()])
    user = make_user(mem_db, "buyer@test.ru", password="Buyer1234")
    login(web, "buyer@test.ru", "Buyer1234")
    page = web.get("/catalog").text
    assert "Роза Фридом 60" in page and "29.10.2026" in page          # блок «Товары в пути»

    mem_db.expire_all()
    item = mem_db.query(SupplyItem).one()
    assert add_preorder_to_db(mem_db, user.id, item.id, 4) is True
    assert mem_db.query(Preorder).one().quantity == 4


# ───────────────────────── товар приехал ─────────────────────────

def make_preorder(web, mem_db, h, *, qty=5, live_stock=10, name="Роза Фридом 60"):
    """В пути + клиент с предзаказом + (опц.) такой же товар на полке из 1С."""
    live = seed_item(mem_db, name=name, stock=live_stock, price=42.5, service=True,
                     active=live_stock > 0) if live_stock is not None else None
    post(web, h, [invoice(items=[line(name)])])
    user = make_user(mem_db, "buyer@test.ru")
    mem_db.expire_all()
    transit = mem_db.query(SupplyItem).join(Supply).filter(Supply.external_id == "00000622").one()
    assert add_preorder_to_db(mem_db, user.id, transit.id, qty)
    return user, live


def test_received_invoice_closes_supply_and_reserves_goods_for_preorders(web, mem_db, sent_emails):
    h = key_header(mem_db)
    user, live = make_preorder(web, mem_db, h, qty=5, live_stock=10)

    data = post(web, h, [{"id": "00000622", "status": "received"}]).json()
    assert data["received"] == 1 and data["preorders_fulfilled"] == 1

    mem_db.expire_all()
    supply = mem_db.query(Supply).filter_by(external_id="00000622").one()
    assert supply.status == "Разгружен"
    assert all(not i.is_active for i in supply.items)                 # на полку НЕ выложена: она из остатков 1С
    pre = mem_db.query(Preorder).one()
    assert pre.is_fulfilled and pre.quantity == 5 and pre.supply_item_id == live.id
    assert mem_db.get(SupplyItem, live.id).reserved_stock == 5
    assert mem_db.get(SupplyItem, live.id).available_stock == 5
    assert "Предзаказ поступил" in mem_db.query(Notification).filter_by(user_id=user.id).one().text
    assert [m["to"] for m in sent_emails] == ["buyer@test.ru"]

    # повторная отправка того же статуса ничего не меняет
    again = post(web, h, [{"id": "00000622", "status": "received"}]).json()
    assert again["received"] == 0 and again["preorders_fulfilled"] == 0
    assert len(sent_emails) == 1


def test_partial_stock_gives_partial_preorder(web, mem_db, sent_emails):
    h = key_header(mem_db)
    _, live = make_preorder(web, mem_db, h, qty=8, live_stock=3)
    post(web, h, [{"id": "00000622", "status": "received"}])
    mem_db.expire_all()
    pre = mem_db.query(Preorder).one()
    assert pre.is_fulfilled and pre.quantity == 3
    assert "из 8" in mem_db.query(Notification).one().text


def test_preorder_waits_until_stock_arrives_from_1c(web, mem_db, sent_emails):
    h = key_header(mem_db)
    user, _ = make_preorder(web, mem_db, h, qty=5, live_stock=None)    # остатков на полке ещё нет
    first = post(web, h, [{"id": "00000622", "status": "received"}]).json()
    assert first["received"] == 1 and first["preorders_fulfilled"] == 0
    mem_db.expire_all()
    assert not mem_db.query(Preorder).one().is_fulfilled

    # остатки из 1С доехали позже — следующий обмен выполняет предзаказ
    seed_item(mem_db, name="Роза Фридом 60", stock=20, price=42.5, service=True)
    # (товар на полке — тот же, найденный по названию)
    product = mem_db.query(Product).filter_by(name="Роза Фридом 60").first()
    dup = mem_db.query(SupplyItem).join(Supply).filter(Supply.is_service.is_(True)).one()
    assert dup.product.name == "Роза Фридом 60"
    # привязываем остаток к исходному товару, как это делает products/sync
    dup.product_id = product.id
    mem_db.commit()
    second = post(web, h, [invoice("00000999", items=[line("Другая роза 50")])]).json()
    assert second["preorders_fulfilled"] == 1
    mem_db.expire_all()
    assert mem_db.query(Preorder).one().is_fulfilled
    assert len(sent_emails) == 1


def test_received_or_cancelled_for_unknown_invoice_is_skipped(web, mem_db):
    h = key_header(mem_db)
    data = post(web, h, [{"id": "NOPE", "status": "received"}, {"id": "NOPE2", "status": "cancelled"}]).json()
    assert data["skipped"] == 2 and data["errors_total"] == 0
    mem_db.expire_all()
    assert mem_db.query(Supply).count() == 0


def test_cancelled_invoice_is_removed_or_closed_if_clients_wait(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("INV-1"), invoice("INV-2", items=[line("Роза Ж 60")])])
    user = make_user(mem_db, "buyer@test.ru")
    mem_db.expire_all()
    waiting_item = (mem_db.query(SupplyItem).join(Supply).filter(Supply.external_id == "INV-2").one())
    add_preorder_to_db(mem_db, user.id, waiting_item.id, 2)

    data = post(web, h, [{"id": "INV-1", "status": "cancelled"}, {"id": "INV-2", "status": "cancelled"}]).json()
    assert data["cancelled"] == 2 and len(data["warnings"]) == 1 and "предзаказ" in data["warnings"][0]
    mem_db.expire_all()
    assert mem_db.query(Supply).filter_by(external_id="INV-1").count() == 0          # удалён
    closed = mem_db.query(Supply).filter_by(external_id="INV-2").one()
    assert closed.status == "Разгружен" and "отменён в 1С" in closed.notes             # клиенты ждут — сохранён


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
    assert "из 1С" in web.get("/admin").text                        # бейдж в списке поставок


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
        {"id": "N-1", "name": "Роза Кения 40с", "package_size": 25, "price_per_stem": 30, "stock_stems": 500},
        {"id": "N-2", "name": "Роза Кения 40", "package_size": 25, "price_per_stem": 30, "stock_stems": 500,
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
