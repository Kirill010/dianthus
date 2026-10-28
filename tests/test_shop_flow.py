# tests/test_shop_flow.py
"""Покупательский путь: корзина, оформление, заказы, карточка товара (в т. ч.
предзаказ), «Сроки заказа», рекламации."""
import io
import re
from datetime import datetime, timedelta

from PIL import Image

from app.models import (Claim, ClaimPhoto, Notification, Order, Preorder,
                        Product, Supply, SupplyItem)
from app.services import preorder_service
from app.timeutils import shop_tz
from tests.helpers import csrf_from, login, make_order, make_user, seed_item


def shop_login(web, mem_db, **kw):
    user = make_user(mem_db, **kw)
    login(web, user.email, "Client123")
    return user


def add_to_cart(web, item, stems):
    token = csrf_from(web.get("/catalog").text)
    return web.post("/add_to_cart", data={"csrf_token": token, "supply_item_id": item.id,
                                          "quantity_stems": stems}, follow_redirects=False)


def cart_session(web):
    """Корзина из страницы: [(ключ, количество)]."""
    html = web.get("/cart").text
    keys = re.findall(r'name="item_key" value="([^"]+)"', html)
    qty = re.findall(r'name="quantity_packs" value="(\d+)"', html)
    return list(zip(dict.fromkeys(keys), qty)), html


# ───────────────────────── корзина ─────────────────────────

def test_cart_quantity_can_be_changed_in_the_cart(web, mem_db):
    shop_login(web, mem_db)
    item = seed_item(mem_db, stock=10, pack=25)
    add_to_cart(web, item, 25)
    (key, qty), = cart_session(web)[0]
    assert qty == "1"
    token = csrf_from(web.get("/cart").text)
    web.post("/cart/update", data={"csrf_token": token, "item_key": key, "quantity_packs": "4"})
    assert cart_session(web)[0][0][1] == "4"
    # больше остатка → исправляется до остатка, с пояснением
    r = web.post("/cart/update", data={"csrf_token": token, "item_key": key, "quantity_packs": "99"},
                 follow_redirects=True)
    assert cart_session(web)[0][0][1] == "10" and "на складе только 10" in r.text
    # ноль убирает позицию
    web.post("/cart/update", data={"csrf_token": token, "item_key": key, "quantity_packs": "0"})
    assert cart_session(web)[0] == []


def test_cart_update_respects_minimum_order(web, mem_db):
    shop_login(web, mem_db)
    item = seed_item(mem_db, stock=20, pack=10)
    item.product.min_quantity = 3
    mem_db.commit()
    add_to_cart(web, item, 30)
    (key, _), = cart_session(web)[0]
    token = csrf_from(web.get("/cart").text)
    r = web.post("/cart/update", data={"csrf_token": token, "item_key": key, "quantity_packs": "1"},
                 follow_redirects=True)
    assert cart_session(web)[0][0][1] == "3" and "минимум 3 упак." in r.text


def test_cart_update_unknown_key_and_sold_out_item(web, mem_db):
    shop_login(web, mem_db)
    item = seed_item(mem_db, stock=5)
    add_to_cart(web, item, 25)
    (key, _), = cart_session(web)[0]
    token = csrf_from(web.get("/cart").text)
    r = web.post("/cart/update", data={"csrf_token": token, "item_key": "999:0", "quantity_packs": "2"},
                 follow_redirects=True)
    assert "уже убрана" in r.text
    item.stock = 0
    mem_db.commit()
    r = web.post("/cart/update", data={"csrf_token": token, "item_key": key, "quantity_packs": "2"},
                 follow_redirects=True)
    assert "закончился" in r.text and cart_session(web)[0] == []


def test_remove_by_key_is_not_fooled_by_stale_index(web, mem_db):
    shop_login(web, mem_db)
    a, b = seed_item(mem_db, name="Роза А", stock=9), seed_item(mem_db, name="Роза Б", stock=9)
    add_to_cart(web, a, 25)
    add_to_cart(web, b, 25)
    keys = [k for k, _ in cart_session(web)[0]]
    assert len(keys) == 2
    token = csrf_from(web.get("/cart").text)
    # «вторая вкладка» уже удалила первую позицию; наш запрос всё равно про «Роза Б»
    web.post("/remove_from_cart", data={"csrf_token": token, "item_key": keys[0]})
    web.post("/remove_from_cart", data={"csrf_token": token, "item_key": keys[1], "item_index": "0"})
    assert cart_session(web)[0] == []


def test_remove_from_cart_still_accepts_old_index_forms(web, mem_db):
    shop_login(web, mem_db)
    add_to_cart(web, seed_item(mem_db, stock=9), 25)
    token = csrf_from(web.get("/cart").text)
    web.post("/remove_from_cart", data={"csrf_token": token, "item_index": "0"})
    assert cart_session(web)[0] == []


# ───────────────────────── оформление ─────────────────────────

def place(web, **extra):
    page = web.get("/checkout")
    data = {"csrf_token": csrf_from(page.text),
            "order_token": re.search(r'name="order_token" value="([^"]+)"', page.text).group(1)}
    data.update(extra)
    return web.post("/place_order", data=data, follow_redirects=False)


def test_checkout_page_shows_new_fields_and_payment_terms(web, mem_db):
    shop_login(web, mem_db)
    add_to_cart(web, seed_item(mem_db, stock=9), 25)
    page = web.get("/checkout").text
    for needle in ('name="desired_date"', 'name="delivery_method"', 'name="delivery_address"',
                   "Предоплата 100%", 'href="/deadlines"'):
        assert needle in page, needle


def test_order_saves_desired_date_and_delivery(web, mem_db):
    shop_login(web, mem_db)
    add_to_cart(web, seed_item(mem_db, stock=9), 25)
    day = (datetime.now(shop_tz()).date() + timedelta(days=12)).isoformat()
    r = place(web, desired_date=day, delivery_method="delivery",
              delivery_address="  Москва,   ул. Цветочная, 5  ", comment="Позвонить")
    assert r.status_code == 303 and r.headers["location"] == "/orders"
    mem_db.expire_all()
    order = mem_db.query(Order).one()
    assert order.desired_date.date().isoformat() == day
    assert order.delivery_method == "delivery" and order.delivery_address == "Москва, ул. Цветочная, 5"
    page = web.get("/orders").text
    assert "Нужно к:" in page and "Москва, ул. Цветочная, 5" in page


def test_pickup_forgets_address_and_fields_are_optional(web, mem_db):
    shop_login(web, mem_db)
    add_to_cart(web, seed_item(mem_db, stock=9), 25)
    place(web, delivery_method="pickup", delivery_address="не нужен")
    order = mem_db.query(Order).one()
    assert order.delivery_method == "pickup" and order.delivery_address == "" and order.desired_date is None


def test_bad_checkout_extras_keep_the_cart_and_stock(web, mem_db):
    shop_login(web, mem_db)
    item = seed_item(mem_db, stock=9)
    add_to_cart(web, item, 25)
    today = datetime.now(shop_tz()).date()
    for extra, message in (
        ({"desired_date": (today - timedelta(days=1)).isoformat()}, "в прошлом"),
        ({"desired_date": "32.13.2026"}, "указана неверно"),
        ({"desired_date": (today + timedelta(days=900)).isoformat()}, "слишком далеко"),
        ({"delivery_method": "delivery", "delivery_address": ""}, "укажите адрес"),
    ):
        r = place(web, comment="сохрани меня", **extra)
        assert r.status_code == 303 and r.headers["location"] == "/checkout"
        page = web.get("/checkout").text
        assert message in page and "сохрани меня" in page, message
    assert mem_db.query(Order).count() == 0
    mem_db.expire_all()
    assert mem_db.get(SupplyItem, item.id).stock == 9
    assert cart_session(web)[0]


# ───────────────────────── заказы клиента ─────────────────────────

def test_orders_page_shows_items_steps_and_claim_button(web, mem_db):
    user = shop_login(web, mem_db)
    item = seed_item(mem_db, name="Роза Эксплорер", stock=30, pack=25)
    done = make_order(mem_db, user, item, packs=2, status="Выполнен")
    new = make_order(mem_db, user, item, packs=1, status="Новый")
    page = web.get("/orders").text
    assert "Роза Эксплорер" in page and "2 упак. × 25 шт" in page
    assert page.count('class="order-steps"') == 2
    assert f"/claims/new?order_id={done.id}" in page
    assert f"/claims/new?order_id={new.id}" not in page          # на новый заказ жаловаться рано


# ───────────────────────── карточка товара / предзаказ ─────────────────────────

def make_incoming(mem_db, status="В пути", service=False):
    item = seed_item(mem_db, name="Роза Прибудет", stock=40, active=False, status=status,
                     service=service)
    item.product.description = "Нежный розовый оттенок"
    item.supply.arrival_date = datetime.now() + timedelta(days=5)
    mem_db.commit()
    return item


def test_incoming_product_page_opens_and_offers_preorder(web, mem_db):
    """Регрессия: ссылка из блока «Товары в пути» вела на 404."""
    shop_login(web, mem_db)
    item = make_incoming(mem_db)
    r = web.get(f"/product/{item.id}")
    assert r.status_code == 200
    assert 'action="/add_to_preorder"' in r.text and "В пути" in r.text
    assert "Нежный розовый оттенок" in r.text and "/deadlines" in r.text
    assert 'action="/add_to_cart"' not in r.text


def test_incoming_product_page_for_guest_hides_price(web, mem_db):
    item = make_incoming(mem_db)
    r = web.get(f"/product/{item.id}")
    assert r.status_code == 200 and "после регистрации" in r.text
    assert 'action="/add_to_preorder"' not in r.text and "₽ / шт" not in r.text


def test_inactive_items_that_are_not_incoming_stay_404(web, mem_db):
    unloaded = seed_item(mem_db, name="Разгружен", stock=3, active=False, status="Разгружен")
    service = make_incoming(mem_db, service=True)
    assert web.get(f"/product/{unloaded.id}").status_code == 404
    assert web.get(f"/product/{service.id}").status_code == 404
    assert web.get("/product/99999").status_code == 404


def test_product_page_shows_description_and_pack_price(web, mem_db):
    shop_login(web, mem_db)
    item = seed_item(mem_db, name="Роза Тест", stock=5, price=40.0, pack=25)
    item.product.description = "Описание товара"
    mem_db.commit()
    page = web.get(f"/product/{item.id}").text
    assert "Описание товара" in page and "1000 ₽" in page          # упаковка 25 × 40
    assert 'id="line-total"' in page


def test_preorder_quantity_is_capped(web, mem_db):
    user = shop_login(web, mem_db)
    item = make_incoming(mem_db)
    token = csrf_from(web.get("/catalog").text)
    web.post("/add_to_preorder", data={"csrf_token": token, "supply_item_id": item.id,
                                       "quantity_stems": str(10 ** 12)})
    web.post("/add_to_preorder", data={"csrf_token": token, "supply_item_id": item.id,
                                       "quantity_stems": "5000"})
    mem_db.expire_all()
    assert mem_db.query(Preorder).one().quantity == preorder_service.MAX_PREORDER_PACKS
    assert str(preorder_service.MAX_PREORDER_PACKS * 25) in web.get("/catalog").text


# ───────────────────────── каталог ─────────────────────────

def test_catalog_links_encode_query_text(web, mem_db):
    shop_login(web, mem_db)
    seed_item(mem_db, name="Роза & Пион", stock=5)
    from urllib.parse import unquote
    from app.models import Kind
    kind_id = mem_db.query(Kind).filter(Kind.name == "Роза").one().id
    page = web.get("/catalog", params={"q": "Роза & Пион", "kind": kind_id}).text
    link = re.search(r'href="/catalog\?q=([^"]*)"', page)           # кнопка вида «Роза» (снять выбор)
    assert link is not None
    assert "&" not in link.group(1) and unquote(link.group(1)) == "Роза & Пион"
    reset = re.search(r'href="/catalog\?kind=([^"]*)"', page)        # крестик «сбросить поиск»
    assert reset is not None and unquote(reset.group(1)) == str(kind_id)


def test_empty_catalog_messages_differ_for_search_and_no_stock(web, mem_db):
    shop_login(web, mem_db)
    assert "Сейчас нет товаров в наличии" in web.get("/catalog").text
    assert "По вашему запросу товаров нет" in web.get("/catalog", params={"q": "нет такого"}).text


# ───────────────────────── сроки заказа ─────────────────────────

def test_deadlines_page_is_public_and_calculates(web):
    page = web.get("/deadlines")
    assert page.status_code == 200 and "Сколько дней нужно" in page.text
    assert "8 марта" in page.text or "Ближайшие праздники" in page.text
    target = (datetime.now(shop_tz()).date() + timedelta(days=60)).isoformat()
    result = web.get("/deadlines", params={"date": target}).text
    assert "Нужно заказать до" in result and "Успеваете" in result
    soon = (datetime.now(shop_tz()).date() + timedelta(days=1)).isoformat()
    assert "Срок прошёл" in web.get("/deadlines", params={"date": soon}).text


def test_deadlines_page_handles_garbage_dates(web):
    for bad in ("abc", "2000-01-01", "2999-01-01", "<script>"):
        r = web.get("/deadlines", params={"date": bad})
        assert r.status_code == 200 and "<script>alert" not in r.text
    assert "Эта дата уже прошла" in web.get("/deadlines", params={"date": "2000-01-01"}).text


def test_deadlines_uses_admin_settings(web, mem_db):
    from app.services.site_settings import set_setting
    set_setting(mem_db, "preorder_lead_days", "Эквадор=21, Россия=1")
    target = (datetime.now(shop_tz()).date() + timedelta(days=60)).isoformat()
    page = web.get("/deadlines", params={"date": target}).text
    assert "за 21 дн." in page and "за 1 дн." in page and "за 10 дн." not in page


# ───────────────────────── рекламации ─────────────────────────

def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (30, 30), "red").save(buf, "PNG")
    return buf.getvalue()


def submit_claim(web, order_id=0, **over):
    token = csrf_from(web.get("/claims/new").text)
    data = {"csrf_token": token, "order_id": order_id, "product_name": "Роза Эксплорер",
            "affected_qty": 30, "total_qty": 100, "unit": "шт",
            "received_at": datetime.now(shop_tz()).date().isoformat(),
            "description": "Бутоны почернели, стебли вялые", "expectation": "Замена"}
    data.update(over)
    files = over.pop("files", None) if "files" in over else None
    return web.post("/claims", data=data, files=files or None, follow_redirects=False)


def test_client_files_claim_with_photos(web, mem_db, tmp_path, monkeypatch):
    from app.services import upload_service
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    user = shop_login(web, mem_db)
    order = make_order(mem_db, user, seed_item(mem_db, name="Роза Эксплорер", stock=30),
                       packs=2, status="Выполнен")
    token = csrf_from(web.get(f"/claims/new?order_id={order.id}").text)
    r = web.post("/claims", data={
        "csrf_token": token, "order_id": order.id, "product_name": "Роза Эксплорер",
        "affected_qty": 30, "total_qty": 100, "unit": "шт",
        "received_at": datetime.now(shop_tz()).date().isoformat(),
        "description": "Бутоны почернели, стебли вялые", "expectation": "Замена",
    }, files=[("photos", ("a.png", png_bytes(), "image/png")),
              ("photos", ("b.png", png_bytes(), "image/png"))], follow_redirects=False)
    assert r.status_code == 303
    claim = mem_db.query(Claim).one()
    assert r.headers["location"] == f"/claims/{claim.id}"
    assert claim.number == f"РЕК-{claim.id:04d}" and claim.status == "Новая"
    assert claim.order_id == order.id and claim.user_id == user.id and not claim.is_late
    assert mem_db.query(ClaimPhoto).count() == 2 and len(list(tmp_path.iterdir())) == 2
    page = web.get(f"/claims/{claim.id}").text
    assert claim.number in page and "Бутоны почернели" in page
    assert claim.number in web.get("/claims").text and claim.number in web.get("/orders").text


def test_claim_validation_errors_keep_the_text(web, mem_db):
    shop_login(web, mem_db)
    r = submit_claim(web, description="коротко", affected_qty=50, total_qty=10)
    assert r.status_code == 303 and r.headers["location"].startswith("/claims/new")
    page = web.get(r.headers["location"]).text
    assert "Опишите, что случилось" in page and "больше, чем получено" in page
    assert "коротко" in page and mem_db.query(Claim).count() == 0


def test_claim_older_than_a_day_is_flagged_for_owner(web, mem_db):
    shop_login(web, mem_db)
    old = (datetime.now(shop_tz()).date() - timedelta(days=3)).isoformat()
    submit_claim(web, received_at=old)
    assert mem_db.query(Claim).one().is_late is True


def test_client_cannot_open_foreign_claims_or_foreign_orders(web, mem_db):
    owner = make_user(mem_db, email="own@test.ru")
    claim = Claim(user_id=owner.id, product_name="Чужая", description="секретно секретно")
    other_order = make_order(mem_db, owner, seed_item(mem_db, stock=9), packs=1)
    mem_db.add(claim)
    mem_db.commit()
    shop_login(web, mem_db, email="me@test.ru")
    assert web.get(f"/claims/{claim.id}").status_code == 404
    assert "секретно" not in web.get("/claims").text
    r = submit_claim(web, order_id=other_order.id)
    assert r.headers["location"].startswith("/claims/new") and mem_db.query(Claim).count() == 1


def test_claims_pages_require_login(web):
    for url in ("/claims", "/claims/new", "/claims/1"):
        r = web.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", url
    assert web.get("/admin/claims", follow_redirects=False).headers["location"] == "/login"


def test_staff_answers_claim_and_client_is_notified(web, mem_db):
    client = make_user(mem_db, email="c@test.ru")
    claim = Claim(user_id=client.id, product_name="Роза", description="Вялые стебли",
                  affected_qty=5, total_qty=50)
    mem_db.add(claim)
    mem_db.commit()
    make_user(mem_db, email="m@test.ru", password="Manager123", is_manager=True)
    login(web, "m@test.ru", "Manager123")
    page = web.get(f"/admin/claims/{claim.id}")
    assert page.status_code == 200 and "Вялые стебли" in page.text and "Правила ответа" in page.text
    token = csrf_from(page.text)
    r = web.post(f"/admin/claims/{claim.id}", data={
        "csrf_token": token, "status": "В работе", "urgency": "высокая",
        "staff_comment": "Предположение: долго стояли без воды", "owner_decision": "ХАКЕР",
        "reply_to_client": "Передали руководителю, ответим сегодня"}, follow_redirects=False)
    assert r.status_code == 303
    mem_db.expire_all()
    c = mem_db.get(Claim, claim.id)
    assert c.status == "В работе" and c.urgency == "высокая" and c.resolved_at is None
    assert c.owner_decision == ""                       # менеджер решение владельца не пишет
    note = mem_db.query(Notification).filter_by(user_id=client.id).one()
    assert "РЕК-" in note.text and "Передали руководителю" in note.text

    # владелец принимает решение и закрывает
    web.cookies.clear()
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get(f"/admin/claims/{claim.id}").text)
    web.post(f"/admin/claims/{claim.id}", data={
        "csrf_token": token, "status": "Решена", "urgency": "обычная",
        "owner_decision": "Скидка 10% на следующий заказ", "reply_to_client": "Предлагаем скидку 10%"})
    mem_db.expire_all()
    c = mem_db.get(Claim, claim.id)
    assert c.status == "Решена" and c.resolved_at is not None and "10%" in c.owner_decision
    web.cookies.clear()
    login(web, "c@test.ru", "Client123")
    mine = web.get(f"/claims/{claim.id}").text
    assert "Предлагаем скидку 10%" in mine
    assert "Предположение" not in mine and "Скидка 10% на следующий заказ" not in mine   # внутреннее скрыто


def test_regular_client_cannot_use_staff_claim_pages(web, mem_db):
    shop_login(web, mem_db)
    for url in ("/admin/claims", "/admin/claims/1"):
        r = web.get(url, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/catalog"


def test_unreadable_claim_photo_is_reported_not_silently_dropped(web, mem_db, tmp_path, monkeypatch):
    from app.services import upload_service
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    shop_login(web, mem_db)
    token = csrf_from(web.get("/claims/new").text)
    r = web.post("/claims", data={
        "csrf_token": token, "order_id": 0, "product_name": "Роза", "affected_qty": 1, "total_qty": 5,
        "unit": "шт", "description": "Вялые стебли, бутоны не раскрылись",
    }, files=[("photos", ("bad.png", "это не картинка".encode("utf-8"), "image/png")),
              ("photos", ("ok.png", png_bytes(), "image/png"))], follow_redirects=True)
    assert "Не удалось загрузить фото" in r.text and "bad.png" in r.text
    assert mem_db.query(ClaimPhoto).count() == 1                    # нормальное фото сохранилось


def test_order_emails_mention_desired_date_and_delivery(mem_db, monkeypatch):
    from app.services import notifier
    sent = []
    monkeypatch.setattr(notifier, "send_email", lambda subject, html: sent.append(html))
    monkeypatch.setattr(notifier, "send_email_to",
                        lambda to, subject, body_html, text=None: sent.append(body_html))
    buyer = make_user(mem_db, "b@test.ru")
    order = make_order(mem_db, buyer, seed_item(mem_db, stock=9), packs=1)
    order.desired_date = datetime(2026, 11, 4)
    order.delivery_method, order.delivery_address = "delivery", "Москва, <b>Цветочная</b> 5"
    mem_db.commit()
    notifier.notify_admin_new_order(order, buyer)
    notifier.notify_client_order_created(order, buyer)
    assert len(sent) == 2
    for html in sent:
        assert "04.11.2026" in html and "Доставка:" in html
        assert "&lt;b&gt;Цветочная&lt;/b&gt;" in html and "<b>Цветочная</b>" not in html   # адрес экранирован
