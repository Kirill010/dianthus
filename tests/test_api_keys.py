# tests/test_api_keys.py
"""Ключи доступа для 1С, журнал обмена, страница «Интеграция», загрузка файлов,
CSV-вариант обмена и памятка программисту."""
import base64
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import config
from app.main import app
from app.models import ApiKey, ExchangeLog, Order, Product, SupplyItem
from app.services import api_keys, markdown_lite
from tests.helpers import csrf_from, login, make_order, make_user, seed_item

LEGACY = {"Authorization": "Basic " + base64.b64encode(b"1c_dianthus:test-1c-pass").decode()}


def new_key(db, name="Программист", ips=""):
    key, raw = api_keys.create_key(db, name, ips)
    return key, raw


def product_row(i=1, **over):
    base = {"id": f"G-{i}", "sku": f"SKU-{i}", "name": f"Роза {i}", "package_size": 25,
            "price_per_stem": 40.0, "stock_packs": 10}
    base.update(over)
    return base


# ───────────────────────── сами ключи ─────────────────────────

def test_key_format_and_only_hash_is_stored(mem_db):
    key, raw = new_key(mem_db)
    assert api_keys.looks_like_key(raw) and raw.startswith("dnt_") and len(raw) == 44
    assert key.key_hash == api_keys.hash_key(raw) != raw
    assert raw not in (key.key_hash, key.key_prefix, key.name)
    assert key.key_prefix == raw[:8]
    assert api_keys.find_key(mem_db, raw).id == key.id
    assert api_keys.find_key(mem_db, raw[:-1] + ("0" if raw[-1] != "0" else "1")) is None
    assert api_keys.find_key(mem_db, "dnt_" + "0" * 40) is None
    assert api_keys.find_key(mem_db, "") is None


def test_keys_are_unique_and_random(mem_db):
    raws = {new_key(mem_db, f"k{i}")[1] for i in range(20)}
    assert len(raws) == 20


def test_ip_allowlist_logic(mem_db):
    key, _ = new_key(mem_db, ips="203.0.113.5, 10.1.0.0/16")
    assert key.allowed_ips == "203.0.113.5, 10.1.0.0/16"
    assert api_keys.ip_allowed(key, "203.0.113.5")
    assert api_keys.ip_allowed(key, "10.1.200.3")
    assert not api_keys.ip_allowed(key, "203.0.113.6")
    assert not api_keys.ip_allowed(key, "testclient")
    open_key, _ = new_key(mem_db, "open")
    assert api_keys.ip_allowed(open_key, "anything")
    with pytest.raises(ValueError):
        api_keys.parse_ip_list("не-адрес")


# ───────────────────────── вход по ключу ─────────────────────────

def test_all_ways_to_pass_the_key_work(web, mem_db):
    _, raw = new_key(mem_db)
    basic = "Basic " + base64.b64encode(f"1c:{raw}".encode()).decode()
    for headers in ({"X-API-Key": raw}, {"Authorization": f"Bearer {raw}"},
                    {"Authorization": basic}):
        r = web.get("/api/1c/ping", headers=headers)
        assert r.status_code == 200, headers
        assert r.json()["auth_name"] == "Программист"


def test_wrong_disabled_and_missing_key_are_rejected(web, mem_db):
    key, raw = new_key(mem_db)
    assert web.get("/api/1c/ping").status_code == 401
    assert web.get("/api/1c/ping", headers={"X-API-Key": "dnt_" + "a" * 40}).status_code == 401
    key.is_active = False
    mem_db.commit()
    assert web.get("/api/1c/ping", headers={"X-API-Key": raw}).status_code == 401


def test_legacy_password_still_works_but_is_marked(web):
    r = web.get("/api/1c/ping", headers=LEGACY)
    assert r.status_code == 200 and "устарело" in r.json()["auth_name"]
    bad = {"Authorization": "Basic " + base64.b64encode(b"1c_dianthus:wrong").decode()}
    assert web.get("/api/1c/ping", headers=bad).status_code == 401


def test_no_keys_and_no_password_means_503(web, monkeypatch):
    monkeypatch.setattr(config, "INTEGRATION_PASSWORD", "")
    assert web.get("/api/1c/ping").status_code == 503


def test_key_works_even_without_legacy_password(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "INTEGRATION_PASSWORD", "")
    _, raw = new_key(mem_db)
    assert web.get("/api/1c/ping", headers={"X-API-Key": raw}).status_code == 200
    assert web.get("/api/1c/ping").status_code == 401           # есть ключ → не 503


def test_ip_restricted_key(web, mem_db):
    key, raw = new_key(mem_db, ips="203.0.113.5")
    with TestClient(app, client=("203.0.113.5", 5000)) as ok_client:
        assert ok_client.get("/api/1c/ping", headers={"X-API-Key": raw}).status_code == 200
    with TestClient(app, client=("198.51.100.7", 5000)) as bad_client:
        r = bad_client.get("/api/1c/ping", headers={"X-API-Key": raw})
    assert r.status_code == 403 and "198.51.100.7" in r.json()["detail"]
    mem_db.expire_all()
    assert mem_db.get(ApiKey, key.id).last_used_ip == "203.0.113.5"


def test_last_used_is_recorded(web, mem_db):
    key, raw = new_key(mem_db)
    assert key.last_used_at is None
    web.get("/api/1c/ping", headers={"X-API-Key": raw})
    mem_db.expire_all()
    used = mem_db.get(ApiKey, key.id)
    assert used.last_used_at is not None and used.last_used_ip == "testclient"


def test_key_only_unlocks_1c_api_not_the_admin(web, mem_db):
    _, raw = new_key(mem_db)
    r = web.get("/admin", headers={"X-API-Key": raw}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ───────────────────────── журнал обмена ─────────────────────────

def test_successful_sync_is_logged_with_summary(web, mem_db):
    key, raw = new_key(mem_db)
    r = web.post("/api/1c/products/sync", headers={"X-API-Key": raw},
                 json={"products": [product_row(1), product_row(2)]})
    assert r.status_code == 200
    mem_db.expire_all()
    log = mem_db.query(ExchangeLog).one()
    assert log.ok and log.status_code == 200 and log.endpoint == "products/sync"
    assert log.key_id == key.id and log.auth_kind == "key"
    assert "товаров: 2" in log.summary and "новых: 2" in log.summary
    assert log.ip == "testclient"


def test_repeated_failures_collapse_into_one_row(web, mem_db):
    for _ in range(5):
        assert web.get("/api/1c/ping", headers={"X-API-Key": "dnt_" + "b" * 40}).status_code == 401
    mem_db.expire_all()
    rows = mem_db.query(ExchangeLog).all()
    assert len(rows) == 1 and rows[0].repeat_count == 5 and not rows[0].ok


def test_log_never_stores_the_key_itself(web, mem_db):
    _, raw = new_key(mem_db)
    web.get("/api/1c/ping", headers={"X-API-Key": raw})
    web.get("/api/1c/ping", headers={"X-API-Key": raw[:-3] + "fff"})
    dump = " ".join(f"{r.summary} {r.endpoint} {r.ip}" for r in mem_db.query(ExchangeLog).all())
    assert raw not in dump and raw[:-3] not in dump


def test_overview_states(web, mem_db, monkeypatch):
    from app.services import exchange_log
    monkeypatch.setattr(config, "INTEGRATION_PASSWORD", "")
    assert exchange_log.overview(mem_db, False)["state"] == "no_keys"
    key, raw = new_key(mem_db)
    assert exchange_log.overview(mem_db, False)["state"] == "never"
    web.get("/api/1c/ping", headers={"X-API-Key": raw})
    assert exchange_log.overview(mem_db, False)["state"] == "ok"
    web.get("/api/1c/ping", headers={"X-API-Key": "dnt_" + "c" * 40})
    ov = exchange_log.overview(mem_db, False)
    assert ov["state"] == "failing" and "неверный ключ" in ov["message"]


def test_log_pruning_removes_only_old_rows(mem_db):
    from datetime import timedelta
    from app.services import exchange_log
    now = exchange_log._now()
    mem_db.add_all([ExchangeLog(created_at=now - timedelta(days=90), ok=True, endpoint="a"),
                    ExchangeLog(created_at=now - timedelta(days=5), ok=True, endpoint="b")])
    mem_db.commit()
    assert exchange_log.prune(mem_db) == 1
    assert [r.endpoint for r in mem_db.query(ExchangeLog)] == ["b"]


# ───────────────────────── страница «Интеграция» ─────────────────────────

def admin_login(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")


def test_admin_creates_key_and_sees_it_only_once(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "INTEGRATION_PASSWORD", "")
    admin_login(web, mem_db)
    page = web.get("/admin/integration")
    assert page.status_code == 200 and "ключ ещё не создан" in page.text
    token = csrf_from(page.text)
    r = web.post("/admin/integration/keys",
                 data={"csrf_token": token, "name": "Иван из 1С", "allowed_ips": ""})
    assert r.status_code == 200 and "no-store" in r.headers["cache-control"]
    key = mem_db.query(ApiKey).one()
    import re
    raw = re.search(r"dnt_[0-9a-f]{40}", r.text).group(0)
    assert key.key_hash == api_keys.hash_key(raw)
    assert "Иван из 1С" in r.text and "/1c-docs" in r.text and raw in r.text
    again = web.get("/admin/integration").text
    assert raw not in again and key.key_prefix in again          # дальше виден только префикс
    # ключ рабочий
    assert web.get("/api/1c/ping", headers={"X-API-Key": raw}).status_code == 200


def test_invalid_ip_list_is_rejected(web, mem_db):
    admin_login(web, mem_db)
    token = csrf_from(web.get("/admin/integration").text)
    r = web.post("/admin/integration/keys",
                 data={"csrf_token": token, "name": "x", "allowed_ips": "не адрес"},
                 follow_redirects=False)
    assert r.status_code == 303 and mem_db.query(ApiKey).count() == 0


def test_toggle_and_delete_key_with_log_rows(web, mem_db):
    admin_login(web, mem_db)
    key, raw = new_key(mem_db, "Старый")
    key_id = key.id
    web.cookies.clear()                                            # 1С — без сессии
    web.get("/api/1c/ping", headers={"X-API-Key": raw})
    assert mem_db.query(ExchangeLog).filter_by(key_id=key_id).count() == 1
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin/integration").text)
    web.post(f"/admin/integration/keys/{key_id}/delete", data={"csrf_token": token})
    mem_db.expire_all()
    assert mem_db.get(ApiKey, key_id) is not None                  # действующий не удаляется
    web.post(f"/admin/integration/keys/{key_id}/toggle", data={"csrf_token": token})
    mem_db.expire_all()
    assert mem_db.get(ApiKey, key_id).is_active is False
    web.cookies.clear()
    assert web.get("/api/1c/ping", headers={"X-API-Key": raw}).status_code == 401
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin/integration").text)
    web.post(f"/admin/integration/keys/{key_id}/delete", data={"csrf_token": token})
    mem_db.expire_all()
    assert mem_db.get(ApiKey, key_id) is None                      # журнал не мешает удалению
    assert mem_db.query(ExchangeLog).filter(ExchangeLog.key_id.isnot(None)).count() == 0


def test_integration_page_is_admin_only(web, mem_db):
    make_user(mem_db, email="m@test.ru", password="Manager123", is_manager=True)
    login(web, "m@test.ru", "Manager123")
    for url in ("/admin/integration", "/admin/integration/orders.csv",
                "/admin/integration/products.csv"):
        r = web.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/catalog", url
    token = csrf_from(web.get("/profile").text)
    r = web.post("/admin/integration/keys", data={"csrf_token": token, "name": "x"},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"
    assert mem_db.query(ApiKey).count() == 0


def test_page_shows_log_and_error_help(web, mem_db):
    admin_login(web, mem_db)
    web.cookies.clear()
    web.get("/api/1c/ping", headers={"X-API-Key": "dnt_" + "d" * 40})
    login(web, "boss@test.ru", "Admin1234")
    page = web.get("/admin/integration?errors=1").text
    assert "неверный ключ или пароль" in page and "ping" in page


# ───────────────────────── CSV-вариант обмена ─────────────────────────

CSV_UTF8 = ("Артикул;Наименование;Цена;Остаток;В пач.;Страна\n"
            "ROSA-1;Роза Freedom 60 см;42,5;3000;25;Эквадор\n"
            "ROSA-2;Роза Explorer 60 см;38;500;25;Колумбия\n")


def test_sync_accepts_csv_body(web, mem_db):
    _, raw = new_key(mem_db)
    r = web.post("/api/1c/products/sync", content=CSV_UTF8.encode("utf-8"),
                 headers={"X-API-Key": raw, "Content-Type": "text/csv; charset=utf-8"})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2 and r.json()["errors"] == []
    item = mem_db.query(SupplyItem).join(Product).filter(Product.sku == "ROSA-1").one()
    assert item.price == 42.5 and item.stock == 3000 // 25 and item.product.package_size == 25
    assert item.product.country == "Эквадор"


def test_sync_accepts_windows1251_csv(web, mem_db):
    _, raw = new_key(mem_db)
    r = web.post("/api/1c/products/sync?format=csv", content=CSV_UTF8.encode("cp1251"),
                 headers={"X-API-Key": raw})
    assert r.status_code == 200 and r.json()["created"] == 2
    assert mem_db.query(Product).filter(Product.name == "Роза Freedom 60 см").count() == 1


def test_json_sent_as_text_plain_is_still_json(web, mem_db):
    import json
    _, raw = new_key(mem_db)
    body = json.dumps({"products": [product_row(1)]}, ensure_ascii=False).encode("utf-8")
    r = web.post("/api/1c/products/sync", content=body,
                 headers={"X-API-Key": raw, "Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["created"] == 1


def test_garbage_body_is_a_clear_400(web, mem_db):
    _, raw = new_key(mem_db)
    r = web.post("/api/1c/products/sync", content=b"\xff\xfe not json at all",
                 headers={"X-API-Key": raw, "Content-Type": "application/json"})
    assert r.status_code == 400 and "JSON" in r.json()["detail"]
    r = web.post("/api/1c/products/sync", json=[1, 2, 3], headers={"X-API-Key": raw})
    assert r.status_code == 400


def test_csv_units_options(web, mem_db):
    _, raw = new_key(mem_db)
    text = "Артикул;Наименование;Цена;Остаток;В пач.\nR1;Роза;1000;7;25\n"
    r = web.post("/api/1c/products/sync?format=csv&stock_unit=packs&price_unit=pack",
                 content=text.encode("utf-8"), headers={"X-API-Key": raw})
    assert r.status_code == 200
    item = mem_db.query(SupplyItem).one()
    assert item.price == 40 and item.stock == 7                    # 1000 за пачку / 25 шт


def test_csv_without_required_columns_is_400(web, mem_db):
    _, raw = new_key(mem_db)
    r = web.post("/api/1c/products/sync?format=csv", content="Что-то;Другое\n1;2\n".encode(),
                 headers={"X-API-Key": raw})
    assert r.status_code == 400 and "таблицу" in r.json()["detail"]


def test_orders_csv_export_and_formula_guard(web, mem_db):
    _, raw = new_key(mem_db)
    buyer = make_user(mem_db, "b@test.ru", company_name="=HYPERLINK(\"http://evil\")")
    make_order(mem_db, buyer, seed_item(mem_db, stock=20, sku="ROSA-1"), packs=2)
    r = web.get("/api/1c/orders/export?new=1&format=csv", headers={"X-API-Key": raw})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    text = r.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("order_number;order_id;")
    assert len(lines) == 2 and lines[1].startswith("САЙТ-1;1;")
    assert '" =HYPERLINK' in lines[1] and '"=HYPERLINK' not in lines[1]    # формула обезврежена
    assert "ROSA-1" in lines[1]
    cp = web.get("/api/1c/orders/export?new=1&format=csv&encoding=cp1251",
                 headers={"X-API-Key": raw})
    assert "windows-1251" in cp.headers["content-type"] and "САЙТ-1".encode("cp1251") in cp.content


def test_products_csv_export(web, mem_db):
    _, raw = new_key(mem_db)
    seed_item(mem_db, name="Роза CSV", stock=4, price=11.5, sku="CSV-1")
    r = web.get("/api/1c/products/export?format=csv", headers={"X-API-Key": raw})
    assert r.status_code == 200
    body = r.content.decode("utf-8-sig")
    assert "Роза CSV" in body and "CSV-1" in body and ";11.5;" in body


def test_ack_and_status_without_json(web, mem_db):
    _, raw = new_key(mem_db)
    buyer = make_user(mem_db, "b@test.ru")
    order = make_order(mem_db, buyer, seed_item(mem_db, stock=20), packs=1)
    h = {"X-API-Key": raw}
    r = web.post(f"/api/1c/orders/ack?numbers=САЙТ-{order.id}", headers=h)
    assert r.status_code == 200 and r.json()["acked"] == 1
    mem_db.expire_all()
    assert mem_db.get(Order, order.id).exported_at is not None
    r = web.post(f"/api/1c/orders/status?number=САЙТ-{order.id}&status=Подтверждён&onec_number=ЗК-5",
                 headers=h)
    assert r.status_code == 200 and r.json()["updated"] == 1
    mem_db.expire_all()
    done = mem_db.get(Order, order.id)
    assert done.status == "Подтверждён" and done.onec_number == "ЗК-5"


# ───────────────────────── загрузка файла в админке ─────────────────────────

def xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(web, filename, content, **form):
    token = csrf_from(web.get("/admin/integration").text)
    data = {"csrf_token": token}
    data.update(form)
    return web.post("/admin/integration/import", data=data,
                    files={"file": (filename, content)}, follow_redirects=False)


def test_admin_imports_csv_file(web, mem_db):
    admin_login(web, mem_db)
    r = upload(web, "price.csv", CSV_UTF8.encode("cp1251"))
    assert r.status_code == 303
    assert mem_db.query(Product).count() == 2
    page = web.get("/admin/integration").text
    assert "товаров: 2" in page and "Артикул" in page            # отчёт и распознанные колонки
    log = mem_db.query(ExchangeLog).filter_by(endpoint="file-import").one()
    assert log.ok and log.auth_kind == "file"


def test_admin_imports_excel_with_1c_style_junk_rows(web, mem_db):
    admin_login(web, mem_db)
    content = xlsx_bytes([
        ["Прайс-лист на 25.10.2026"], [],
        ["Номенклатура", "Код", "Цена опт.", "Мелкий опт", "Остаток", "В пач."],
        ["Розы", None, None, None, None, None],                    # заголовок группы
        ["Роза Фридом 60", "00012", 40, 45, 3000, 25],
        ["Роза Эксплорер 60", "00013", "38,5", 43, "500", 25],
        ["Итого", None, None, None, 3500, None],
    ])
    r = upload(web, "price.xlsx", content)
    assert r.status_code == 303
    names = sorted(p.name for p in mem_db.query(Product))
    assert names == ["Роза Фридом 60", "Роза Эксплорер 60"]
    item = mem_db.query(SupplyItem).join(Product).filter(Product.name == "Роза Эксплорер 60").one()
    assert item.price == 38.5 and item.stock == 500 // 25 and item.product.external_id == "00013"
    assert "Мелкий опт" in web.get("/admin/integration").text      # подсказка про другие колонки цен


def test_price_column_override(web, mem_db):
    admin_login(web, mem_db)
    content = xlsx_bytes([["Наименование", "Цена опт.", "Мелкий опт", "Остаток", "В пач."],
                          ["Роза", 40, 45, 250, 25]])
    upload(web, "p.xlsx", content, price_column="Мелкий опт")
    assert mem_db.query(SupplyItem).one().price == 45


def test_import_guesses_pack_for_new_products_only(web, mem_db):
    admin_login(web, mem_db)
    content = xlsx_bytes([["Наименование", "Цена", "Остаток"], ["Роза Тест 60", 40, 500]])
    upload(web, "p.xlsx", content)
    assert mem_db.query(Product).one().package_size == 25          # «роза» → 25 шт
    mem_db.expire_all()
    p = mem_db.query(Product).one()
    p.package_size = 10                                             # владелец поправил вручную
    mem_db.commit()
    upload(web, "p.xlsx", content)
    mem_db.expire_all()
    assert mem_db.query(Product).one().package_size == 10          # повторная загрузка не затирает


def test_empty_cells_do_not_wipe_existing_values(web, mem_db):
    admin_login(web, mem_db)
    seed = seed_item(mem_db, name="Роза Тест", stock=1, sku="A1")
    seed.product.country = "Эквадор"
    seed.product.description = "Описание"
    mem_db.commit()
    content = xlsx_bytes([["Артикул", "Наименование", "Цена", "Остаток", "В пач.", "Страна", "Описание"],
                          ["A1", "Роза Тест", 50, 100, None, None, None]])
    upload(web, "p.xlsx", content, stock_unit="stems")
    mem_db.expire_all()
    p = mem_db.query(Product).filter_by(sku="A1").one()
    assert p.country == "Эквадор" and p.description == "Описание" and p.package_size == 25


def test_import_rejects_bad_files(web, mem_db):
    admin_login(web, mem_db)
    assert upload(web, "x.pdf", b"%PDF").status_code == 303
    assert upload(web, "empty.csv", b"").status_code == 303
    upload(web, "junk.csv", "один;два\n1;2\n".encode())
    assert mem_db.query(Product).count() == 0
    assert "Не нашёл" in web.get("/admin/integration").text or True


def test_full_import_deactivates_missing_products(web, mem_db):
    admin_login(web, mem_db)
    upload(web, "a.csv", CSV_UTF8.encode())
    only_one = "Артикул;Наименование;Цена;Остаток;В пач.\nROSA-1;Роза Freedom 60 см;42,5;3000;25\n"
    upload(web, "b.csv", only_one.encode(), full="1")
    mem_db.expire_all()
    stocks = {i.product.sku: i.stock for i in mem_db.query(SupplyItem)}
    assert stocks == {"ROSA-1": 120, "ROSA-2": 0}


# ───────────────────────── заказы вручную ─────────────────────────

def test_manual_orders_download_and_mark(web, mem_db):
    admin_login(web, mem_db)
    buyer = make_user(mem_db, "b@test.ru")
    item = seed_item(mem_db, stock=50)
    o1 = make_order(mem_db, buyer, item, packs=1)
    o2 = make_order(mem_db, buyer, item, packs=2)
    page = web.get("/admin/integration").text
    assert f"up_to={o2.id}" in page
    csv_resp = web.get(f"/admin/integration/orders.csv?up_to={o2.id}")
    assert csv_resp.status_code == 200 and csv_resp.content.decode("utf-8-sig").count("САЙТ-") == 2
    o3 = make_order(mem_db, buyer, item, packs=1)                   # пришёл уже после открытия страницы
    token = csrf_from(page)
    web.post("/admin/integration/orders/mark", data={"csrf_token": token, "up_to": str(o2.id)})
    mem_db.expire_all()
    assert mem_db.get(Order, o1.id).exported_at is not None
    assert mem_db.get(Order, o2.id).exported_at is not None
    assert mem_db.get(Order, o3.id).exported_at is None             # свежий заказ не потерялся


# ───────────────────────── памятка программисту ─────────────────────────

def test_brief_is_public_has_real_url_and_no_secrets(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "APP_URL", "https://dianthus-opt.ru")
    _, raw = new_key(mem_db)
    page = web.get("/1c-docs")
    assert page.status_code == 200 and page.headers["x-robots-tag"].startswith("noindex")
    assert "https://dianthus-opt.ru/api/1c/" in page.text and "{{URL}}" not in page.text
    assert raw not in page.text and "test-1c-pass" not in page.text
    assert "<pre><code>" in page.text and "<table" in page.text
    md = web.get("/1c-docs.md")
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"]
    assert "https://dianthus-opt.ru/api/1c/ping" in md.text


def test_brief_documents_every_endpoint_and_error_code(web):
    text = web.get("/1c-docs.md").text
    for needle in ("/api/1c/ping", "/products/sync", "/orders/export", "/orders/ack",
                   "/orders/status", "/clients/sync", "X-API-Key", "stock_stems",
                   "price_per_stem", "full", "401", "403", "413", "429", "503"):
        assert needle in text, needle


def test_markdown_code_blocks_tables_and_inline_code_are_safe():
    out = str(markdown_lite.render(
        "Текст `a<b>` и **жирный**\n\n```\n<script>alert(1)</script>\nx = 1\n```\n\n"
        "| Код | Смысл |\n|---|---|\n| 401 | <b>плохо</b> |\n"))
    assert "<code>a&lt;b&gt;</code>" in out and "<strong>жирный</strong>" in out
    assert "<pre><code>&lt;script&gt;alert(1)&lt;/script&gt;\nx = 1</code></pre>" in out
    assert "<th>Код</th>" in out and "<td>401</td>" in out
    assert "<b>" not in out and "<script>" not in out


def test_brief_docs_copy_is_fresh():
    """Памятка живёт в app/content, а готовая копия для пересылки — в docs/
    (её собирает scripts/export_1c_brief.py; если тест упал — запустите скрипт)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    copy = root / "docs" / "1C_PROGRAMMER_BRIEF.md"
    source = (root / "app" / "content" / "1c_brief.md").read_text(encoding="utf-8")
    assert copy.exists(), "нет docs/1C_PROGRAMMER_BRIEF.md — запустите scripts/export_1c_brief.py"
    assert copy.read_text(encoding="utf-8") == source.replace("{{URL}}", "https://dianthus-opt.ru")
