# tests/test_1c_exchange.py
"""Обмен с 1С: устойчивость загрузки, подтверждение заказов, статусы, скидки."""
import base64

from app.models import Notification, Product, Supply, SupplyItem
from tests.helpers import make_order, make_user, seed_item

AUTH = {"Authorization": "Basic " + base64.b64encode(b"1c_dianthus:test-1c-pass").decode()}


def sync(web, products, **kw):
    body = {"products": products}
    body.update(kw.pop("body", {}))
    return web.post("/api/1c/products/sync", json=body, headers=AUTH, **kw)


def row(i=1, packs=10, **over):
    """Строка товара «без поставки»: price — за штуку, quantity — в штуках (packs упаковок по 25)."""
    base = {"id": f"G-{i}", "sku": f"SKU-{i}", "name": f"Роза {i}", "package_size": 25,
            "price": 40.0, "quantity": packs * 25, "kind": "Роза"}
    base.update(over)
    return base


# ═══════════════════ УСТОЙЧИВОСТЬ ЗАГРУЗКИ КАТАЛОГА ═══════════════════

def test_bad_rows_are_reported_and_good_rows_are_loaded(web, mem_db):
    """Было: на PostgreSQL одна «мусорная» строка роняла весь обмен (500)."""
    r = sync(web, [
        row(1),
        row(2, price="abc"),                           # не число
        row(3, price=-5),                              # отрицательная цена
        row(4, name=""),                               # нет названия
        row(5, quantity=10 ** 12),                     # нереальный остаток
        row(6, package_size=-3),                       # недопустимая упаковка → ошибка строки
        row(7),
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["processed"] == 2 and data["created"] == 2
    assert data["errors_total"] == 5
    assert {p.name for p in mem_db.query(Product).all()} == {"Роза 1", "Роза 7"}


def test_text_is_clamped_to_column_sizes_and_nul_removed(web, mem_db):
    r = sync(web, [row(1, name="Н" * 500, sku="S" * 300, barcode="1" * 200, country="Э" * 200,
                       description="D" * 9000, kind="К" * 300)])
    assert r.json()["created"] == 1, r.text
    p = mem_db.query(Product).one()
    assert len(p.name) == 200 and len(p.sku) == 100 and len(p.barcode) == 64
    assert len(p.country_name) == 100 and len(p.kind_name) == 100
    assert len(p.description) == 5000

    r = sync(web, [row(2, name="Тест" + chr(0) + "овый")])
    assert mem_db.query(Product).filter(Product.name == "Тестовый").count() == 1


def test_numbers_may_come_as_strings_and_stems_convert_to_packs(web, mem_db):
    r = sync(web, [
        row(1, price="42,50", quantity="300"),             # 300 шт = 12 упаковок
        {"id": "G-2", "name": "Гвоздика", "package_size": 25,
         "price": 30, "quantity": 130},                    # 130 шт = 5 упаковок + 5 шт
    ])
    assert r.json()["processed"] == 2, r.text
    items = {i.product.name: i for i in mem_db.query(SupplyItem).all()}
    assert items["Роза 1"].price == 42.5 and items["Роза 1"].stock == 12
    assert items["Гвоздика"].stock == 5                    # неполную упаковку не продаём


def test_package_size_zero_falls_back_to_one(web, mem_db):
    sync(web, [row(1, package_size=0, min_quantity=0)])
    p = mem_db.query(Product).one()
    assert p.package_size == 1 and p.min_quantity == 1


def test_same_name_different_1c_ids_are_not_merged(web, mem_db):
    """Два разных товара 1С с одинаковым названием не должны склеиться."""
    r = sync(web, [row(1, name="Роза Фридом", sku="A"), row(2, name="Роза Фридом", sku="B")])
    assert r.json()["created"] == 2
    assert mem_db.query(Product).count() == 2


def test_manual_product_is_adopted_by_name(web, mem_db):
    """Товар, заведённый вручную (без id из 1С), «усыновляется» по названию."""
    mem_db.add(Product(name="Роза Ручная", package_size=25))
    mem_db.commit()
    r = sync(web, [row(1, name="Роза Ручная")])
    assert r.json()["created"] == 0 and r.json()["updated"] == 1
    assert mem_db.query(Product).one().external_id == "G-1"


def test_legacy_service_supply_is_reused_not_duplicated(web, mem_db):
    """Базы до появления колонки is_service: служебная партия — обычная."""
    legacy = Supply(country="1С", status="Ожидается", is_service=False,
                    notes="Авто-синхронизация из 1С")
    mem_db.add(legacy)
    mem_db.commit()
    r = sync(web, [row(1)])
    assert r.json()["supply_id"] == legacy.id
    mem_db.refresh(legacy)
    assert legacy.is_service is True
    assert mem_db.query(Supply).count() == 1


def test_photos_are_filtered_without_dns_lookups(web, mem_db, monkeypatch):
    """Было: на каждый URL фото делался блокирующий DNS-запрос."""
    import socket

    def no_dns(*a, **k):
        raise AssertionError("DNS не должен вызываться")
    monkeypatch.setattr(socket, "gethostbyname", no_dns)

    sync(web, [row(1, photos=[
        "https://cdn.example.com/a.jpg",
        "http://127.0.0.1/secret.png",
        "http://192.168.0.5/x.png",
        "http://localhost/y.png",
        "https://user:pw@evil.example/z.png",
        "javascript:alert(1)",
        "/static/uploads/ok.webp",
    ])])
    assert mem_db.query(Product).one().all_photos == [
        "https://cdn.example.com/a.jpg", "/static/uploads/ok.webp"]


# ═══════════════════ ПОЛНЫЙ СНИМОК ОСТАТКОВ ═══════════════════

def test_full_snapshot_zeroes_items_missing_from_upload(web, mem_db):
    sync(web, [row(1), row(2)])
    r = sync(web, [row(1, packs=4)], body={"full": True})
    assert r.status_code == 200 and r.json()["deactivated"] == 1, r.text
    items = {i.product.name: i for i in mem_db.query(SupplyItem).all()}
    assert items["Роза 1"].stock == 4 and items["Роза 1"].is_active
    assert items["Роза 2"].stock == 0 and not items["Роза 2"].is_active


def test_partial_upload_does_not_touch_other_items(web, mem_db):
    sync(web, [row(1), row(2)])
    sync(web, [row(1, packs=4)])                            # без full
    items = {i.product.name: i for i in mem_db.query(SupplyItem).all()}
    assert items["Роза 2"].stock == 10 and items["Роза 2"].is_active


def test_full_snapshot_keeps_item_whose_row_was_broken(web, mem_db):
    """Битая строка при полном снимке не должна обнулить сам товар."""
    sync(web, [row(1), row(2)])
    r = sync(web, [row(1), row(2, price="oops")], body={"full": True})
    assert r.json()["errors_total"] == 1 and r.json()["deactivated"] == 0
    assert mem_db.query(SupplyItem).filter(SupplyItem.stock == 10).count() == 2


def test_empty_full_snapshot_is_rejected(web, mem_db):
    sync(web, [row(1)])
    r = sync(web, [], body={"full": True})
    assert r.status_code == 400
    assert mem_db.query(SupplyItem).one().stock == 10


# ═══════════════════ ЗАКАЗЫ: ЗАБРАЛИ / ПОДТВЕРДИЛИ ═══════════════════

def _order(web, mem_db, packs=3, stock=10):
    client = make_user(mem_db, "c@test.ru")
    item = seed_item(mem_db, stock=stock)
    return client, item, make_order(mem_db, client, item, packs=packs)


def test_orders_export_new_and_ack(web, mem_db):
    _, _, order = _order(web, mem_db)
    first = web.get("/api/1c/orders/export?new=1", headers=AUTH).json()
    assert first["count"] == 1 and first["orders"][0]["number"] == f"САЙТ-{order.id}"
    assert first["orders"][0]["id"] == order.id                     # id заказа на сайте

    # пока 1С не подтвердила — заказ приходит снова (ничего не теряется)
    assert web.get("/api/1c/orders/export?new=1", headers=AUTH).json()["count"] == 1

    ack = web.post("/api/1c/orders/ack", headers=AUTH, json={
        "orders": [{"number": f"САЙТ-{order.id}", "onec_number": "00000000123"}]})
    assert ack.json()["acked"] == 1

    assert web.get("/api/1c/orders/export?new=1", headers=AUTH).json()["count"] == 0
    assert web.get("/api/1c/orders/export", headers=AUTH).json()["count"] == 1   # весь список по-прежнему виден
    mem_db.refresh(order)
    assert order.exported_at is not None and order.onec_number == "00000000123"


def test_ack_accepts_plain_numbers_and_reports_unknown(web, mem_db):
    _, _, order = _order(web, mem_db)
    r = web.post("/api/1c/orders/ack", headers=AUTH,
                 json={"numbers": [f"САЙТ-{order.id}", "САЙТ-9999", "мусор"]})
    data = r.json()
    assert data["acked"] == 1 and len(data["errors"]) == 2


def test_orders_export_time_fields_and_since_with_z(web, mem_db):
    _, _, order = _order(web, mem_db)
    o = web.get("/api/1c/orders/export", headers=AUTH).json()["orders"][0]
    # date — время магазина (Москва, UTC+3) БЕЗ смещения, как его читает 1С
    from datetime import timedelta
    assert o["date"] == (order.created_at + timedelta(hours=3)).isoformat(timespec="seconds")
    assert "+" not in o["date"] and not o["date"].endswith("Z")
    ok = web.get("/api/1c/orders/export?since=2000-01-01T00:00:00Z", headers=AUTH)
    assert ok.status_code == 200 and ok.json()["count"] == 1
    future = web.get("/api/1c/orders/export?since=2999-01-01T00:00:00%2B03:00", headers=AUTH)
    assert future.json()["count"] == 0


def test_orders_export_after_id_cursor(web, mem_db):
    client, item, first = _order(web, mem_db, packs=1)
    second = make_order(mem_db, client, item, packs=1)
    data = web.get(f"/api/1c/orders/export?after_id={first.id}", headers=AUTH).json()
    assert [o["id"] for o in data["orders"]] == [second.id]
    assert data["last_id"] == second.id


# ═══════════════════ ЗАКАЗЫ: СТАТУСЫ ИЗ 1С ═══════════════════

def test_1c_can_change_status_and_cancel_returns_stock(web, mem_db):
    client, item, order = _order(web, mem_db, packs=3, stock=10)      # остаток 7
    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "Подтверждён", "onec_number": "A-1"}]})
    assert r.json()["updated"] == 1, r.text
    mem_db.refresh(order)
    assert order.status == "Подтверждён" and order.onec_number == "A-1"
    assert order.exported_at is not None

    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "Отменён"}]})
    assert r.json()["updated"] == 1
    mem_db.refresh(item); mem_db.refresh(order)
    assert order.status == "Отменён" and item.stock == 10

    # клиент получил уведомления в кабинете
    texts = [n.text for n in mem_db.query(Notification).filter(
        Notification.user_id == client.id)]
    assert len(texts) == 2 and any("отменён" in t.lower() for t in texts)


def test_1c_status_errors_are_per_order(web, mem_db):
    _, _, order = _order(web, mem_db)
    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": "САЙТ-9999", "status": "Выполнен"},
        {"number": f"САЙТ-{order.id}", "status": "Не такой статус"},
        {"number": f"САЙТ-{order.id}"},
        {"number": f"САЙТ-{order.id}", "status": "Новый"},       # уже «Новый»
    ]})
    data = r.json()
    assert data["updated"] == 0 and data["unchanged"] == 1
    assert len(data["errors"]) == 3


def test_1c_endpoints_require_auth(web):
    for method, url in (("post", "/api/1c/orders/status"), ("post", "/api/1c/orders/ack"),
                        ("post", "/api/1c/clients/sync"), ("get", "/api/1c/clients/export")):
        r = getattr(web, method)(url, **({"json": {}} if method == "post" else {}))
        assert r.status_code == 401, url


# ═══════════════════ КЛИЕНТЫ: СКИДКИ ═══════════════════

def test_clients_sync_updates_only_discount(web, mem_db):
    a = make_user(mem_db, "a@test.ru", inn="7701234567")
    b = make_user(mem_db, "b@test.ru", inn="7707654321")
    r = web.post("/api/1c/clients/sync", headers=AUTH, json={"clients": [
        {"email": "a@test.ru", "discount_percent": 5},
        {"inn": "7707654321", "discount_percent": "7,5"},
        {"email": "nobody@test.ru", "discount_percent": 3},
        {"email": "a@test.ru", "discount_percent": 150},
        {"discount_percent": 3},
    ]})
    data = r.json()
    assert data["updated"] == 2 and len(data["errors"]) == 3, r.text
    mem_db.refresh(a); mem_db.refresh(b)
    assert a.discount_percent == 5 and b.discount_percent == 7.5
    assert a.is_approved and not a.is_admin                  # права не тронуты

    again = web.post("/api/1c/clients/sync", headers=AUTH, json={"clients": [
        {"email": "a@test.ru", "discount_percent": 5}]}).json()
    assert again["updated"] == 0 and again["unchanged"] == 1


def test_clients_sync_ambiguous_inn_and_admin_protected(web, mem_db):
    make_user(mem_db, "a@test.ru", inn="7701234567")
    make_user(mem_db, "b@test.ru", inn="7701234567")
    admin = make_user(mem_db, "boss@test.ru", admin=True, inn="7799999999")
    r = web.post("/api/1c/clients/sync", headers=AUTH, json={"clients": [
        {"inn": "7701234567", "discount_percent": 5},
        {"email": "boss@test.ru", "discount_percent": 5},
    ]})
    assert r.json()["updated"] == 0 and len(r.json()["errors"]) == 2
    mem_db.refresh(admin)
    assert admin.discount_percent == 0


def test_clients_export_pagination(web, mem_db):
    for i in range(3):
        make_user(mem_db, f"u{i}@test.ru", inn=f"77012345{i:02d}")
    make_user(mem_db, "admin@test.ru", admin=True)
    page = web.get("/api/1c/clients/export?limit=2", headers=AUTH).json()
    assert page["count"] == 2 and page["has_more"] is True
    rest = web.get(f"/api/1c/clients/export?after_id={page['last_id']}", headers=AUTH).json()
    assert rest["count"] == 1                                # администратор не выгружается
    assert "email" in rest["clients"][0]


# ═══════════════════ ЗАЩИТА ОТ ПЕРЕПРОДАЖИ ═══════════════════

def test_reserve_unexported_orders_when_enabled(web, mem_db, monkeypatch):
    """Опция: свежий заказ, которого 1С ещё не видела, вычитается из остатка 1С —
    иначе выгрузка вернула бы проданные упаковки на полку."""
    from app.config import config
    sync(web, [row(1, packs=10)])
    item = mem_db.query(SupplyItem).one()
    client = make_user(mem_db, "c@test.ru")
    order = make_order(mem_db, client, item, packs=3)         # остаток на сайте: 7

    # по умолчанию (опция выключена) выгрузка 1С затирает остаток как есть
    sync(web, [row(1, packs=10)])
    mem_db.refresh(item)
    assert item.stock == 10

    monkeypatch.setattr(config, "ONEC_RESERVE_UNEXPORTED", True)
    sync(web, [row(1, packs=10)])
    mem_db.refresh(item)
    assert item.stock == 7                                   # 10 − 3 неподтверждённых

    web.post("/api/1c/orders/ack", headers=AUTH, json={"numbers": [f"САЙТ-{order.id}"]})
    sync(web, [row(1, packs=7)])                              # теперь 1С учла заказ сама
    mem_db.refresh(item)
    assert item.stock == 7


def test_sync_warns_when_product_is_on_shelf_from_both_1c_and_manual_supply(web, mem_db):
    """Переход на 1С: ручная партия ещё на полке — товар виден в каталоге дважды."""
    seed_item(mem_db, name="Роза Дубль", stock=5)                # ручная поставка, активна
    r = sync(web, [row(1, name="Роза Дубль")])
    assert r.json()["warnings"] and "дважды" in r.json()["warnings"][0]

    clean = sync(web, [row(2, name="Роза Другая")])
    assert clean.json()["warnings"] == [] or "Роза Дубль" not in clean.json()["warnings"][0]


# ═══════════════════ СТАТУСЫ: ПРИВЫЧНЫЕ НАЗВАНИЯ И КОММЕНТАРИЙ ═══════════════════

def test_status_synonyms_and_comment_from_1c(web, mem_db):
    """Образец программиста 1С: {"number": "САЙТ-57", "status": "Отгружен", "comment": "…"}."""
    _, _, order = _order(web, mem_db)
    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "Отгружен", "comment": "Передан в доставку"}]})
    assert r.json() == {"status": "ok", "updated": 1, "unchanged": 0, "errors": []}, r.text
    mem_db.refresh(order)
    assert order.status == "Отправлен"
    assert order.staff_note.startswith("1С") and "Передан в доставку" in order.staff_note

    # регистр и «ё» не важны
    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "ПОДТВЕРЖДЕН"}]})
    assert r.json()["updated"] == 1
    mem_db.refresh(order)
    assert order.status == "Подтверждён"


def test_unknown_status_error_lists_the_allowed_ones(web, mem_db):
    _, _, order = _order(web, mem_db)
    r = web.post("/api/1c/orders/status", headers=AUTH, json={"orders": [
        {"number": f"САЙТ-{order.id}", "status": "Улетел на Марс"}]})
    error = r.json()["errors"][0]
    assert "Улетел на Марс" in error and "Выполнен" in error and "Отменён" in error


# ═══════════════════ КОНТРАГЕНТЫ: ТЕЛО КАК В ВЫГРУЗКЕ ═══════════════════

def test_clients_sync_accepts_the_export_shape_by_site_id(web, mem_db):
    """Запись из clients/export можно отправить обратно, добавив external_id и скидку."""
    a = make_user(mem_db, "a@test.ru", inn="7701234567", address="Москва, ул. Цветочная, 1")
    exported = web.get("/api/1c/clients/export", headers=AUTH).json()
    client = exported["clients"][0]
    assert set(client) == {"id", "external_id", "name", "inn", "email", "address", "phone",
                           "discount_percent"}
    client.update(external_id="000001234", discount_percent=7.5)
    body = {"count": 1, "has_more": False, "last_id": a.id, "clients": [client]}   # как в образце
    r = web.post("/api/1c/clients/sync", headers=AUTH, json=body)
    assert r.json()["updated"] == 1 and r.json()["errors"] == [], r.text
    mem_db.refresh(a)
    assert a.external_id == "000001234" and a.discount_percent == 7.5
    assert a.address == "Москва, ул. Цветочная, 1"


def test_clients_sync_string_id_is_the_1c_code_and_conflicting_ids_are_errors(web, mem_db):
    a = make_user(mem_db, "a@test.ru", inn="7701234567")
    b = make_user(mem_db, "b@test.ru", inn="7707654321")
    a.external_id = "000001234"
    mem_db.commit()
    r = web.post("/api/1c/clients/sync", headers=AUTH, json={"clients": [
        # id строкой — код контрагента в 1С, а не номер клиента на сайте
        {"id": "000001234", "discount_percent": 3},
        # id одного клиента, email другого — не угадываем, чью скидку менять
        {"id": b.id, "email": "a@test.ru", "discount_percent": 9},
    ]})
    data = r.json()
    assert data["updated"] == 1 and len(data["errors"]) == 1, r.text
    mem_db.refresh(a)
    mem_db.refresh(b)
    assert a.discount_percent == 3 and b.discount_percent == 0


def test_clients_sync_fills_the_address_only_when_the_site_has_none(web, mem_db):
    empty = make_user(mem_db, "a@test.ru", inn="7701234567")
    filled = make_user(mem_db, "b@test.ru", inn="7707654321", address="Наш адрес")
    web.post("/api/1c/clients/sync", headers=AUTH, json={"clients": [
        {"id": empty.id, "address": "Адрес из 1С", "discount_percent": 1},
        {"id": filled.id, "address": "Адрес из 1С", "discount_percent": 1},
    ]})
    mem_db.refresh(empty)
    mem_db.refresh(filled)
    assert empty.address == "Адрес из 1С" and filled.address == "Наш адрес"


def test_json_body_with_utf8_bom_is_accepted(web, mem_db):
    """1С по умолчанию может дописать BOM в начало тела запроса."""
    import json
    import codecs
    body = codecs.BOM_UTF8 + json.dumps({"products": [row(1)]}, ensure_ascii=False).encode("utf-8")
    r = web.post("/api/1c/products/sync", content=body,
                 headers={**AUTH, "Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
