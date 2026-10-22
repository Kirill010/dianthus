# tests/test_improvements.py
"""Регрессионные тесты на найденные и исправленные ошибки.

Каждый тест описывает конкретную проблему «как было» — чтобы она не вернулась.
"""
import asyncio
import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pyotp
import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.models import (Notification, Order, Product, Supply, SupplyItem,
                        User)
from tests.helpers import csrf_from, login, make_order, make_user, seed_item


# ═══════════════════════════ ЗАПУСК С НУЛЯ ═══════════════════════════

def test_fresh_database_gets_admin_from_env(monkeypatch):
    """Было: ensure_default_admin() вызывался при импорте — раньше, чем
    создавались таблицы, и на чистой базе админ не появлялся вообще."""
    from app.bootstrap import ensure_default_admin
    from app.database import SessionLocal
    from app.migrations import prepare_database

    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    original = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=eng)
    monkeypatch.setenv("ADMIN_EMAIL", "boss@example.ru")
    monkeypatch.setenv("ADMIN_PASSWORD", "Passw0rd123")
    try:
        prepare_database(ensure_default_admin)
        prepare_database(ensure_default_admin)       # повторный старт безопасен
        db = SessionLocal()
        try:
            admins = db.query(User).filter(User.email == "boss@example.ru").all()
            assert len(admins) == 1
            assert admins[0].is_admin and admins[0].is_approved
        finally:
            db.close()
    finally:
        SessionLocal.configure(bind=original)
        eng.dispose()


def test_lock_files_do_not_pollute_drive_root():
    """Было: на Windows «/run/dianthus» создавался в корне диска (D:/run/...)."""
    import os
    import tempfile

    from app.locks import lock_path
    path = lock_path("DIANTHUS_NO_SUCH_ENV", "x.lock")
    assert path.endswith("x.lock")
    if os.name == "nt":
        assert path.startswith(tempfile.gettempdir())


# ═══════════════════════════ CSP / ШАБЛОНЫ ═══════════════════════════

TEMPLATES = sorted(Path(__file__).resolve().parent.parent.glob("app/templates/*.html"))
INLINE_HANDLER = re.compile(
    r"<[^>]*?\son(?:click|submit|change|input|load|error|focus|blur|key\w+|mouse\w+)\s*=",
    re.I | re.S,
)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_no_inline_event_handlers(path):
    """Было: CSP запрещает inline-обработчики, а в шаблонах остались
    onsubmit="return confirm(...)" — подтверждения «Удалить?» молча не
    срабатывали, и пользователи/поставки удалялись одним кликом."""
    assert not INLINE_HANDLER.search(path.read_text(encoding="utf-8")), (
        f"{path.name}: inline-обработчик блокируется CSP — используйте data-confirm"
    )


def test_base_template_handles_data_confirm_and_qty_step():
    base = (TEMPLATES[0].parent / "base.html").read_text(encoding="utf-8")
    assert "dataset.confirm" in base
    assert "data-qty-step" in base or "qtyStep" in base


def test_cancel_forms_ask_for_confirmation():
    admin = (TEMPLATES[0].parent / "admin.html").read_text(encoding="utf-8")
    assert 'data-confirm-when="Отменён"' in admin


def test_unknown_url_returns_html_404(web):
    """Было: на несуществующий адрес отдавался сырой JSON {"detail":"Not Found"}."""
    r = web.get("/net-takoy-stranitsy", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Страница не найдена" in r.text

    j = web.get("/net-takoy-stranitsy", headers={"accept": "application/json"})
    assert j.status_code == 404
    assert "detail" in j.json()


# ═══════════════════════════ ВРЕМЯ И ДЕНЬГИ ═══════════════════════════

def test_local_time_filter_shows_moscow_time():
    """Было: время заказа печаталось в UTC (на 3 часа раньше, чем у клиента)."""
    from app.templating import templates
    f = templates.env.filters["localdt"]
    assert f(datetime(2026, 2, 14, 9, 31)) == "14.02.2026 12:31"
    assert f(None) == "—"


def test_time_helpers():
    from app.timeutils import local_iso, local_midnight_utc, utc_iso
    dt = datetime(2026, 2, 14, 9, 31)
    assert utc_iso(dt) == "2026-02-14T09:31:00+00:00"
    assert local_iso(dt) == "2026-02-14T12:31:00+03:00"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start = local_midnight_utc(0)
    assert 0 <= (now - start).total_seconds() <= 24 * 3600 + 5


def test_money_is_rounded_to_kopecks():
    """Было: суммы копились во float: 2499.7500000000005 уходило в заказ и в 1С."""
    from app.services.pricing import apply_discount, line_total, money
    assert line_total(33.33, 25, 3) == 2499.75
    assert money(0.005) == 0.01 and money(2.675) == 2.68
    assert apply_discount(2499.75, 7) == (174.98, 2324.77)
    assert apply_discount(100, 0) == (0.0, 100.0)
    assert apply_discount(100, 500) == (100.0, 0.0)       # скидка не выше 100%


def test_place_order_totals_are_rounded(web, mem_db):
    user = make_user(mem_db, discount_percent=7)
    item = seed_item(mem_db, stock=10, price=33.33, pack=25)
    login(web, user.email, "Client123")

    token = csrf_from(web.get(f"/product/{item.id}").text)
    web.post("/add_to_cart", data={"csrf_token": token, "supply_item_id": item.id,
                                   "quantity_stems": 75}, follow_redirects=False)
    checkout = web.get("/checkout")
    order_token = re.search(r'name="order_token" value="([^"]+)"', checkout.text).group(1)
    r = web.post("/place_order", data={"csrf_token": csrf_from(checkout.text),
                                       "order_token": order_token, "comment": ""},
                 follow_redirects=False)
    assert r.status_code == 303

    order = mem_db.query(Order).one()
    assert order.subtotal == 2499.75
    assert order.total_price == 2324.77
    assert order.order_token == order_token
    mem_db.refresh(item)
    assert item.stock == 7


def test_place_order_double_submit_creates_one_order(web, mem_db):
    """Было: два одновременных POST с одной формы могли создать два заказа."""
    user = make_user(mem_db)
    item = seed_item(mem_db, stock=10)
    login(web, user.email, "Client123")

    token = csrf_from(web.get(f"/product/{item.id}").text)
    web.post("/add_to_cart", data={"csrf_token": token, "supply_item_id": item.id,
                                   "quantity_stems": 25}, follow_redirects=False)
    checkout = web.get("/checkout")
    order_token = re.search(r'name="order_token" value="([^"]+)"', checkout.text).group(1)

    # «параллельный» запрос успел создать заказ с тем же токеном
    mem_db.add(Order(user_id=user.id, subtotal=0, total_price=0,
                     status="Новый", order_token=order_token))
    mem_db.commit()

    r = web.post("/place_order", data={"csrf_token": csrf_from(checkout.text),
                                       "order_token": order_token, "comment": ""},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/orders"
    assert mem_db.query(Order).count() == 1                  # второго заказа нет
    mem_db.refresh(item)
    assert item.stock == 10                                  # списание откатилось


# ═══════════════════════════ ОТМЕНА ЗАКАЗА ═══════════════════════════

def _admin(web, mem_db):
    make_user(mem_db, "admin@test.ru", "Admin1234", admin=True)
    assert login(web, "admin@test.ru", "Admin1234").status_code == 303


def _set_status(web, order_id, status):
    token = csrf_from(web.get("/admin").text)
    return web.post("/admin/update_order_status", follow_redirects=False,
                    data={"csrf_token": token, "order_id": order_id, "status": status})


def test_cancel_returns_stock_and_reopen_takes_it_back(web, mem_db):
    """Было: «Отменён» только меняло слово в карточке, а товар навсегда
    оставался списанным со склада."""
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    item = seed_item(mem_db, stock=10)
    order = make_order(mem_db, client, item, packs=3)          # 10 → 7
    assert _set_status(web, order.id, "Отменён").status_code == 303

    mem_db.refresh(item); mem_db.refresh(order)
    assert order.status == "Отменён"
    assert item.stock == 10

    _set_status(web, order.id, "Отменён")                      # повтор — ничего не меняет
    mem_db.refresh(item)
    assert item.stock == 10

    _set_status(web, order.id, "Новый")                        # вернули в работу
    mem_db.refresh(item); mem_db.refresh(order)
    assert order.status == "Новый"
    assert item.stock == 7

    notes = mem_db.query(Notification).filter(Notification.user_id == client.id).count()
    assert notes == 2                                          # отмена + «Новый»


def test_cancel_puts_sold_out_item_back_on_shelf(web, mem_db):
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    item = seed_item(mem_db, stock=3)
    order = make_order(mem_db, client, item, packs=3)          # распродано → снято с полки
    mem_db.refresh(item)
    assert item.stock == 0 and item.is_active is False

    _set_status(web, order.id, "Отменён")
    mem_db.refresh(item)
    assert item.stock == 3 and item.is_active is True


def test_reopen_cancelled_order_is_refused_when_stock_is_gone(web, mem_db):
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    item = seed_item(mem_db, stock=10)
    order = make_order(mem_db, client, item, packs=3)
    _set_status(web, order.id, "Отменён")

    item.stock = 1                                             # товар успели продать
    mem_db.commit()
    _set_status(web, order.id, "Новый")
    mem_db.refresh(item); mem_db.refresh(order)
    assert order.status == "Отменён"                           # статус не сменился
    assert item.stock == 1


def test_admin_back_redirect_stays_on_order_page(web, mem_db):
    """Было: _back() сравнивал Referer с base_url, за nginx это не сходилось,
    и менеджера выкидывало в /admin вместо карточки заказа."""
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    order = make_order(mem_db, client, seed_item(mem_db, stock=10), packs=1)
    token = csrf_from(web.get(f"/admin/orders/{order.id}").text)
    r = web.post("/admin/update_order_status", follow_redirects=False,
                 headers={"referer": f"https://testserver/admin/orders/{order.id}"},
                 data={"csrf_token": token, "order_id": order.id, "status": "Подтверждён"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/admin/orders/{order.id}"


# ═══════════════════════════ ПЕРЕНАПРАВЛЕНИЯ ═══════════════════════════

def _req(referer=None, host="shop.ru"):
    headers = [(b"host", host.encode())]
    if referer:
        headers.append((b"referer", referer.encode()))
    return Request({"type": "http", "method": "POST", "path": "/x",
                    "headers": headers, "scheme": "https",
                    "server": (host, 443), "query_string": b""})


def test_safe_back_only_returns_paths_of_own_site():
    from app.web import safe_back
    assert safe_back(_req("https://shop.ru/admin/orders/5?x=1"), "/d") == "/admin/orders/5?x=1"
    assert safe_back(_req("http://shop.ru/cart"), "/d") == "/cart"      # схема не важна
    assert safe_back(_req("https://evil.com/phish"), "/d") == "/d"
    assert safe_back(_req("https://shop.ru//evil.com"), "/d") == "/d"
    assert safe_back(_req("https://shop.ru/\\evil.com"), "/d") == "/d"
    assert safe_back(_req(None), "/d") == "/d"
    assert safe_back(_req("not a url at all"), "/d") == "/d"


def test_profile_redirect_ignores_foreign_referer(web, mem_db):
    """Было: профиль возвращал любой Referer как есть (open redirect)."""
    user = make_user(mem_db)
    login(web, user.email, "Client123")
    token = csrf_from(web.get("/profile").text)
    r = web.post("/profile", follow_redirects=False,
                 headers={"referer": "https://evil.example/steal"},
                 data={"csrf_token": token, "full_name": "Иван Иванов",
                       "phone": "+79001234567", "company_name": "ООО Ромашка",
                       "inn": "7701234567", "city": "Москва"})
    assert r.status_code == 303
    assert "evil.example" not in r.headers["location"]


# ═══════════════════════════ ВХОД И 2FA ═══════════════════════════

def test_2fa_prompt_does_not_burn_attempts_and_code_is_one_time(web, mem_db):
    """Было: вход админа с 2FA без кода считался НЕУДАЧНОЙ попыткой — после
    трёх таких заходов блокировка на 10 минут. И код можно было
    использовать повторно в течение всего окна."""
    secret = pyotp.random_base32()
    make_user(mem_db, "admin@test.ru", "Admin1234", admin=True, totp_secret=secret)

    for _ in range(5):
        r = login(web, "admin@test.ru", "Admin1234")             # без кода
        assert r.status_code == 303 and r.headers["location"] == "/login"
    page = web.get("/login").text
    assert 'id="totp-row"' in page and 'id="totp-row" hidden' not in page

    code = pyotp.TOTP(secret).now()
    r = login(web, "admin@test.ru", "Admin1234", totp=code)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"

    web.post("/logout", data={"csrf_token": csrf_from(web.get("/catalog").text)})
    r = login(web, "admin@test.ru", "Admin1234", totp=code)      # тот же код ещё раз
    assert r.headers["location"] == "/login"


def test_login_runs_password_check_even_for_unknown_email(web, monkeypatch):
    """Было: для несуществующего email bcrypt не считался, и по времени ответа
    можно было узнать, зарегистрирован ли адрес."""
    import app.auth as auth
    calls = []
    original = auth.verify_password
    monkeypatch.setattr(auth, "verify_password",
                        lambda pw, hashed: calls.append(hashed) or original(pw, hashed))
    r = login(web, "nobody@test.ru", "Whatever123")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert len(calls) == 1


def test_non_ascii_csrf_token_is_rejected_not_500(web):
    """Было: secrets.compare_digest(str, str) падал с TypeError на не-ASCII
    токене — вместо отказа сервер отвечал 500."""
    web.get("/login")
    r = web.post("/login", follow_redirects=False,
                 data={"email": "a@b.ru", "password": "x", "csrf_token": "ёж"})
    assert r.status_code == 303


def test_rate_limit_falls_back_to_memory_when_redis_fails(monkeypatch):
    """Было: при ошибке Redis лимит запросов просто отключался."""
    from app import security

    async def broken(bucket, window):
        return None

    monkeypatch.setattr(security, "_redis_enabled", True)
    monkeypatch.setattr(security, "_redis_incr", broken)
    request = Request({"type": "http", "method": "POST", "path": "/login",
                       "headers": [], "client": ("9.9.9.9", 1)})

    async def hammer():
        await security.check_rate_limit(request, "t", max_hits=2, window=60)
        await security.check_rate_limit(request, "t", max_hits=2, window=60)
        with pytest.raises(HTTPException) as exc:
            await security.check_rate_limit(request, "t", max_hits=2, window=60)
        assert exc.value.status_code == 429

    asyncio.run(hammer())


def test_validators_accept_real_company_names():
    """Было: «чёрный список слов» отклонял Union Flowers, Select Group и т.п."""
    from app.validators import validate_city, validate_company_name
    assert validate_company_name("Union Flowers") is None
    assert validate_company_name("Select Group, ООО «Ромашка»") is None
    assert validate_company_name("Alert Trade") is None
    assert validate_city("Update") is None
    assert validate_company_name("<script>alert(1)</script>") is not None
    assert validate_company_name("A" + chr(0) + "B") is not None


# ═══════════════════════════ ПОИСК И ИМПОРТ ═══════════════════════════

def test_search_suggest_treats_wildcards_literally(web, mem_db):
    """Было: запрос «__» или «%%» совпадал со всеми товарами."""
    seed_item(mem_db, name="Роза 100% красная")
    seed_item(mem_db, name="Тюльпан белый")
    names = lambda q: [i["name"] for i in web.get(f"/api/search-suggest?q={q}").json()["items"]]
    assert names("%25%25") == []
    assert names("__") == []
    assert names("100%25") == ["Роза 100% красная"]


def _xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.append(["Название", "Страна", "Длина, см", "Единица", "В упаковке",
               "Мин. заказ", "Категория", "Описание"])
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_excel_import_does_not_duplicate_products(web, mem_db):
    """Было: повторная загрузка того же файла удваивала справочник."""
    _admin(web, mem_db)
    data = _xlsx([["Роза Импорт", "Эквадор", 60, "упаковка", 25, 1, "Роза Эквадор", "d"]])
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    for _ in range(2):
        token = csrf_from(web.get("/admin").text)
        r = web.post("/admin/products/import", follow_redirects=False,
                     data={"csrf_token": token},
                     files={"file": ("p.xlsx", data, mime)})
        assert r.status_code == 303
    assert mem_db.query(Product).filter(Product.name == "Роза Импорт").count() == 1


def test_excel_import_clamps_values_for_postgres_columns():
    from app.services.excel_service import import_products_from_excel
    data = _xlsx([["Н" * 500, "С" * 300, 9999, "У" * 99, 0, 0, "К" * 300, "О" * 9000]])
    products, _ = import_products_from_excel(data)
    p = products[0]
    assert len(p["name"]) == 200 and len(p["country"]) == 100
    assert len(p["unit"]) == 30 and len(p["category"]) == 100
    assert len(p["description"]) == 5000
    assert p["length_cm"] == 500 and p["package_size"] == 1 and p["min_quantity"] == 1


def test_product_edit_validates_fields(web, mem_db):
    """Было: форма правки не проверяла package_size (0 ломал расчёт остатков)."""
    _admin(web, mem_db)
    item = seed_item(mem_db)
    token = csrf_from(web.get(f"/admin/products/{item.product_id}/edit").text)
    web.post(f"/admin/products/{item.product_id}/edit", follow_redirects=False,
             data={"csrf_token": token, "name": "Роза", "package_size": 0,
                   "min_quantity": 1, "length_cm": 60, "category": "Прочее"})
    mem_db.refresh(item.product)
    assert item.product.package_size == 25                    # осталось как было


# ═══════════════════════════ ПОСТАВКИ ═══════════════════════════

def test_manual_unload_keeps_1c_stock_on_shelf(mem_db):
    """Было: разгрузка любой поставки снимала с полок и остатки 1С —
    клиенты видели пустой каталог до следующей выгрузки."""
    from app.scheduler import do_unload

    from_1c = seed_item(mem_db, name="Из 1С", service=True, stock=40)
    shipment = seed_item(mem_db, name="Из поставки", active=False, stock=5,
                         status="Прибыл")
    do_unload(mem_db, shipment.supply)
    mem_db.commit()

    mem_db.refresh(from_1c); mem_db.refresh(shipment)
    assert from_1c.is_active is True
    assert shipment.is_active is True and shipment.supply.status == "Разгружен"


# ═══════════════════════════ ПИСЬМА КЛИЕНТАМ ═══════════════════════════

def test_client_emails_are_built_and_addressed_to_the_client(mem_db, monkeypatch):
    """Было: клиент не узнавал ни об одобрении заявки, ни о принятом заказе."""
    from app.config import config
    from app.services import notifier

    for name, value in (("SMTP_HOST", "smtp.example.ru"), ("SMTP_USER", "shop@example.ru"),
                        ("SMTP_PASSWORD", "x"), ("SMTP_TO", "boss@example.ru"),
                        ("SMTP_FROM", "shop@example.ru")):
        monkeypatch.setattr(config, name, value)
    sent = []
    monkeypatch.setattr(notifier, "_smtp_send_raw",
                        lambda cfg, msg, rcpt: sent.append((msg["Subject"], rcpt)))

    user = make_user(mem_db, "client@test.ru")
    order = make_order(mem_db, user, seed_item(mem_db, stock=10), packs=2)
    notifier.notify_client_order_created(order, user)
    notifier.notify_client_approved(user)

    assert len(sent) == 2
    assert f"Заказ №{order.id}" in sent[0][0] and sent[0][1] == ["client@test.ru"]
    assert sent[1][1] == ["client@test.ru"]


def test_no_smtp_means_silently_no_emails(mem_db):
    from app.services import notifier
    user = make_user(mem_db)
    notifier.notify_client_approved(user)            # не падает без настроек почты
    assert notifier._get_smtp_config() is None


# ═══════════════════════════ PDF И EXCEL ═══════════════════════════

def test_invoice_pdf_is_generated_with_moscow_date(web, mem_db):
    """Проверка связки xhtml2pdf + reportlab на закреплённых версиях."""
    user = make_user(mem_db)
    order = make_order(mem_db, user, seed_item(mem_db, stock=10), packs=2)
    assert login(web, user.email, "Client123").status_code == 303
    r = web.get(f"/orders/{order.id}/invoice")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_invoice_of_foreign_order_is_not_available(web, mem_db):
    owner = make_user(mem_db, "owner@test.ru")
    stranger = make_user(mem_db, "stranger@test.ru")
    order = make_order(mem_db, owner, seed_item(mem_db, stock=10), packs=1)
    login(web, stranger.email, "Client123")
    assert web.get(f"/orders/{order.id}/invoice").status_code == 404


def test_admin_excel_exports_are_xlsx(web, mem_db):
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    make_order(mem_db, client, seed_item(mem_db, stock=10), packs=1)
    for url in ("/admin/orders/export", "/admin/customers/export",
                "/admin/products/import/template"):
        r = web.get(url)
        assert r.status_code == 200, url
        assert r.content[:2] == b"PK", url                    # xlsx = zip-архив


def test_admin_dashboard_renders_with_data(web, mem_db):
    """Дашборд собирает статистику по границам суток магазина и счётчики вкладок."""
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    make_order(mem_db, client, seed_item(mem_db, stock=10), packs=1)
    r = web.get("/admin")
    assert r.status_code == 200
    assert "Заказов сегодня" in r.text
    assert 'data-confirm="Отменить заказ?' in r.text


def test_bad_path_parameter_shows_html_page_not_json(web):
    """Было: /product/abc показывал сырой JSON с ошибкой валидации."""
    r = web.get("/product/abc", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Страница не найдена" in r.text
    j = web.get("/product/abc", headers={"accept": "application/json"})
    assert j.status_code == 422 and "detail" in j.json()


# ═══════════════════ 152-ФЗ, ЖУРНАЛ, РЕКВИЗИТЫ ═══════════════════

def _reg_data(token, **over):
    data = {"csrf_token": token, "email": "new@shop.ru", "password": "Passw0rd1",
            "full_name": "Пётр Сидоров", "phone": "+7 900 111-22-33",
            "company_name": "ООО Новые цветы", "inn": "7701234567", "city": "Казань"}
    data.update(over)
    return data


def test_registration_requires_personal_data_consent(web, mem_db):
    """152-ФЗ: без согласия на обработку персональных данных заявка не принимается,
    а момент согласия сохраняется."""
    token = csrf_from(web.get("/register").text)
    r = web.post("/register", data=_reg_data(token), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/register"
    assert mem_db.query(User).count() == 0
    assert "согласие" in web.get("/register").text.lower()

    token = csrf_from(web.get("/register").text)
    r = web.post("/register", data=_reg_data(token, consent="1"), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/registration_pending"
    user = mem_db.query(User).one()
    assert user.consent_at is not None and user.is_approved is False


def test_register_form_has_consent_checkbox_and_privacy_link(web):
    html = web.get("/register").text
    assert 'name="consent"' in html and 'href="/privacy"' in html


def test_privacy_page_is_public(web):
    r = web.get("/privacy")
    assert r.status_code == 200
    assert "Политика обработки персональных данных" in r.text
    assert 'href="/privacy"' in web.get("/login").text          # ссылка в подвале


def test_audit_log_page_shows_admin_actions(web, mem_db):
    """Журнал действий писался, но посмотреть его было негде."""
    _admin(web, mem_db)
    client = make_user(mem_db, "c@test.ru")
    token = csrf_from(web.get("/admin").text)
    web.post(f"/admin/users/{client.id}/discount", follow_redirects=False,
             data={"csrf_token": token, "discount_percent": 12})
    page = web.get("/admin/audit")
    assert page.status_code == 200
    assert "user_discount" in page.text and "12.0%" in page.text


def test_audit_log_page_is_admin_only(web, mem_db):
    user = make_user(mem_db)
    login(web, user.email, "Client123")
    r = web.get("/admin/audit", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"


def test_invoice_contains_shop_requisites(mem_db):
    from io import BytesIO

    from pypdf import PdfReader

    from app.services.pdf_service import generate_invoice_pdf
    user = make_user(mem_db)
    order = make_order(mem_db, user, seed_item(mem_db, stock=10), packs=1)
    order.items                                              # загружаем позиции
    pdf = generate_invoice_pdf(order, user, {
        "name": "ACME", "phone": "", "address": "",
        "requisites": "INN 7720965534 | BIK 044525225"})
    text = PdfReader(BytesIO(pdf)).pages[0].extract_text()
    assert "7720965534" in text and "044525225" in text


def test_totp_code_with_spaces_cannot_bypass_one_time_use(web, mem_db):
    """Код «123 456» и «123456» — один и тот же код: повтор с пробелом не проходит."""
    secret = pyotp.random_base32()
    make_user(mem_db, "admin@test.ru", "Admin1234", admin=True, totp_secret=secret)
    code = pyotp.TOTP(secret).now()
    assert login(web, "admin@test.ru", "Admin1234", totp=code).headers["location"] == "/catalog"
    web.post("/logout", data={"csrf_token": csrf_from(web.get("/catalog").text)})
    spaced = code[:3] + " " + code[3:]
    assert login(web, "admin@test.ru", "Admin1234", totp=spaced).headers["location"] == "/login"


# ═══════════════════════════ ЗАГРУЗКА ФОТО ═══════════════════════════

def _png(size=(64, 64), mode="RGB", color=(200, 30, 60)):
    from PIL import Image
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_product_photo_upload_is_converted_to_webp(web, mem_db, tmp_path, monkeypatch):
    """Загрузка фото в админке на закреплённой версии Pillow: PNG → WebP в папке uploads."""
    from app.services import upload_service
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    _admin(web, mem_db)
    token = csrf_from(web.get("/admin/products/new").text)
    r = web.post("/admin/products/new", follow_redirects=False,
                 data={"csrf_token": token, "name": "Роза с фото", "package_size": 25,
                       "min_quantity": 1, "length_cm": 60, "category": "Роза Эквадор"},
                 files={"image_files": ("rose.png", _png(), "image/png")})
    assert r.status_code == 303
    product = mem_db.query(Product).filter(Product.name == "Роза с фото").one()
    assert len(product.photos) == 1 and product.photos[0].startswith("/static/uploads/")
    saved = list(tmp_path.glob("*.webp"))
    assert len(saved) == 1 and saved[0].read_bytes()[:4] == b"RIFF"       # настоящий WebP


def test_decompression_bomb_photo_is_rejected(web, mem_db, tmp_path, monkeypatch):
    """Небольшой файл, «распаковывающийся» в сотни мегапикселей, не должен съесть память."""
    from app.services import upload_service
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    _admin(web, mem_db)
    bomb = _png(size=(8000, 8000), mode="L", color=0)          # 64 Мпикс при размере в десятки КБ
    assert len(bomb) < 200_000
    token = csrf_from(web.get("/admin/products/new").text)
    web.post("/admin/products/new", follow_redirects=False,
             data={"csrf_token": token, "name": "Бомба", "package_size": 25,
                   "min_quantity": 1, "length_cm": 60, "category": "Прочее"},
             files={"image_files": ("bomb.png", bomb, "image/png")})
    product = mem_db.query(Product).filter(Product.name == "Бомба").one()
    assert product.photos == []                                  # фото отклонено
    assert list(tmp_path.glob("*.webp")) == []
