# tests/test_dry_run.py
"""«Проверка без сохранения» (dry_run): ответ тот же, что у настоящей загрузки,
но в базе ничего не меняется. Нужна, чтобы владелец видел последствия до загрузки."""
from app.models import AuditLog, Product, Supply, SupplyItem
from app.services import api_keys
from tests.helpers import csrf_from, login, make_user, seed_item

CSV_UTF8 = ("Артикул;Наименование;Цена;Остаток;В пач.;Страна\n"
            "ROSA-1;Роза Freedom 60 см;42,5;3000;25;Эквадор\n"
            "ROSA-2;Роза Explorer 60 см;38;500;25;Колумбия\n")


def key_header(db):
    _, raw = api_keys.create_key(db, "Проверка", "")
    return {"X-API-Key": raw}


def rows():
    return [
        {"id": "G-1", "sku": "S-1", "name": "Роза 1", "package_size": 25,
         "price_per_stem": 40, "stock_packs": 10},
        {"id": "G-2", "sku": "S-2", "name": "Роза 2", "package_size": 25,
         "price_per_stem": "abc", "stock_packs": 10},            # плохая строка
    ]


def test_products_sync_dry_run_reports_but_saves_nothing(web, mem_db):
    h = key_header(mem_db)
    r = web.post("/api/1c/products/sync?dry_run=1", json={"products": rows()}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["dry_run"] is True
    assert data["created"] == 1 and data["processed"] == 1
    assert data["errors_total"] == 1 and "abc" in data["errors"][0]
    assert data["supply_id"] is None
    mem_db.expire_all()
    assert mem_db.query(Product).count() == 0
    assert mem_db.query(SupplyItem).count() == 0
    assert mem_db.query(Supply).count() == 0                       # служебную партию тоже откатили

    # настоящая загрузка после проверки даёт ровно то, что обещала проверка
    real = web.post("/api/1c/products/sync", json={"products": rows()}, headers=h).json()
    assert real["dry_run"] is False
    assert (real["created"], real["processed"], real["errors_total"]) == (1, 1, 1)
    mem_db.expire_all()
    assert mem_db.query(Product).count() == 1


def test_dry_run_full_snapshot_shows_what_would_be_hidden(web, mem_db):
    """Самый опасный случай — «полный снимок»: проверка должна показать, сколько
    товаров снимется с полки, и ничего не снять."""
    keep = seed_item(mem_db, name="Роза Старая", stock=7, service=True, external_id="OLD-1")
    h = key_header(mem_db)
    r = web.post("/api/1c/products/sync?dry_run=1&full=1", headers=h,
                 json={"products": [{"id": "NEW-1", "name": "Роза Новая", "package_size": 25,
                                     "price_per_stem": 40, "stock_packs": 5}]})
    data = r.json()
    assert data["dry_run"] is True and data["deactivated"] == 1 and data["created"] == 1
    mem_db.expire_all()
    item = mem_db.get(SupplyItem, keep.id)
    assert item.stock == 7 and item.is_active                      # не тронуто
    assert mem_db.query(Product).filter_by(name="Роза Новая").count() == 0


def test_dry_run_is_written_to_exchange_log_with_label(web, mem_db):
    from app.models import ExchangeLog
    h = key_header(mem_db)
    web.post("/api/1c/products/sync?dry_run=1", json={"products": rows()[:1]}, headers=h)
    log = mem_db.query(ExchangeLog).filter_by(endpoint="products/sync").one()
    assert "ПРОВЕРКА" in (log.summary or "")


def test_clients_sync_dry_run_changes_no_discount(web, mem_db):
    user = make_user(mem_db, "a@test.ru")
    h = key_header(mem_db)
    body = {"clients": [{"email": "a@test.ru", "discount_percent": 12},
                        {"email": "ghost@test.ru", "discount_percent": 3}]}
    data = web.post("/api/1c/clients/sync?dry_run=1", json=body, headers=h).json()
    assert data["dry_run"] is True and data["updated"] == 1 and len(data["errors"]) == 1
    mem_db.expire_all()
    assert mem_db.get(type(user), user.id).discount_percent == 0
    assert mem_db.query(AuditLog).filter_by(action="user_discount_1c").count() == 0

    real = web.post("/api/1c/clients/sync", json=body, headers=h).json()
    assert real["dry_run"] is False and real["updated"] == 1
    mem_db.expire_all()
    assert mem_db.get(type(user), user.id).discount_percent == 12


def test_admin_file_import_dry_run(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin/integration").text)
    r = web.post("/admin/integration/import",
                 data={"csrf_token": token, "dry_run": "1"},
                 files={"file": ("price.csv", CSV_UTF8.encode("cp1251"))},
                 follow_redirects=False)
    assert r.status_code == 303
    assert mem_db.query(Product).count() == 0                      # ничего не сохранено
    page = web.get("/admin/integration").text
    assert "ничего не сохранено" in page and "Это была только проверка" in page
    assert mem_db.query(AuditLog).filter_by(action="catalog_file_import").count() == 0

    # тот же файл без галочки — загружается по-настоящему
    token = csrf_from(web.get("/admin/integration").text)
    web.post("/admin/integration/import", data={"csrf_token": token},
             files={"file": ("price.csv", CSV_UTF8.encode("cp1251"))}, follow_redirects=False)
    assert mem_db.query(Product).count() == 2
    assert mem_db.query(AuditLog).filter_by(action="catalog_file_import").count() == 1


def test_import_page_has_the_dry_run_checkbox(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    html = web.get("/admin/integration").text
    assert 'name="dry_run"' in html and "Только проверить" in html
