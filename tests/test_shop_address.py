# tests/test_shop_address.py
"""Фактический адрес магазина вносится в админке («Настройки магазина») и попадает на главную,
в «Контакты» и в самовывоз; юридический адрес — в политику и «Контакты». Название организации
в готовом письме программисту 1С не получает лишнюю пару кавычек."""
from app.config import config
from app.services import onec_brief
from tests.helpers import csrf_from, login, make_user, seed_item
from tests.test_shop_flow import add_to_cart, shop_login


def _save_settings(web, token, **over):
    data = {"csrf_token": token, "payment_terms": "Предоплата 100%", "preorder_lead_days": "Эквадор=10",
            "shop_inn": "", "shop_ogrn": "", "shop_legal_address": "", "shop_address": "",
            "shop_email": "", "shop_pd_responsible": "", "pd_processors": ""}
    data.update(over)
    return web.post("/admin/settings", data=data, follow_redirects=False)


def _admin(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")


def test_settings_page_has_both_address_fields(web, mem_db):
    _admin(web, mem_db)
    page = web.get("/admin/settings").text
    assert 'name="shop_legal_address"' in page and 'name="shop_address"' in page
    assert "Фактический адрес" in page and "Юридический адрес" in page


def test_actual_address_saved_in_admin_reaches_public_pages_and_pickup(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "SHOP_ADDRESS", "")
    monkeypatch.setattr(config, "SHOP_LEGAL_ADDRESS", "")
    _admin(web, mem_db)
    token = csrf_from(web.get("/admin/settings").text)
    r = _save_settings(web, token, shop_address="г. Москва, Вешняковская ул., 39Г",
                       shop_legal_address="г. Москва, ул. Юридическая, 1, офис 5")
    assert r.status_code == 303
    web.cookies.clear()
    assert "Вешняковская ул., 39Г" in web.get("/").text
    contacts = web.get("/contacts").text
    assert "Вешняковская ул., 39Г" in contacts and "ул. Юридическая, 1" in contacts
    assert "Фактический адрес" in contacts and "Юридический адрес" in contacts
    assert "ул. Юридическая, 1" in web.get("/privacy").text         # юридический адрес — в политику
    web.cookies.clear()
    shop_login(web, mem_db)
    add_to_cart(web, seed_item(mem_db, stock=9), 25)
    assert "Самовывоз — г. Москва, Вешняковская ул., 39Г" in web.get("/checkout").text


def test_without_saved_address_the_env_value_is_used(web, monkeypatch):
    monkeypatch.setattr(config, "SHOP_ADDRESS", "Москва, Тестовая, 1")
    monkeypatch.setattr(config, "SHOP_LEGAL_ADDRESS", "")
    assert "Тестовая, 1" in web.get("/").text
    contacts = web.get("/contacts").text
    assert "Тестовая, 1" in contacts
    assert "Юридический адрес" not in contacts      # совпадает с фактическим — второй раз не показываем


def test_legal_address_falls_back_to_actual_one(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "SHOP_ADDRESS", "")
    monkeypatch.setattr(config, "SHOP_LEGAL_ADDRESS", "")
    _admin(web, mem_db)
    token = csrf_from(web.get("/admin/settings").text)
    _save_settings(web, token, shop_address="г. Москва, Вешняковская ул., 39Г")
    from app.services import legal
    assert legal.legal_address(mem_db) == "г. Москва, Вешняковская ул., 39Г"


def test_programmer_message_does_not_double_the_quotes(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "SHOP_NAME", "ООО «Диантус»")
    text = onec_brief.message_for_programmer()
    assert "сайтом ООО «Диантус» (оптовые цветы)" in text
    assert "««" not in text and "»»" not in text
    _admin(web, mem_db)
    page = web.get("/admin/integration").text                      # то же письмо на странице «Интеграция»
    assert "««" not in page and "»»" not in page
    monkeypatch.setattr(config, "SHOP_NAME", "Диантус")
    assert "сайтом «Диантус» (оптовые цветы)" in onec_brief.message_for_programmer()
