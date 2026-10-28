# tests/test_filters_admin.py
"""Страница «Фильтры каталога» (только владелец) и формы товара, которые берут значения оттуда;
адрес контрагента в регистрации, профиле и карточке клиента."""
import re

from app.models import AuditLog, Color, Country, Height, Kind, Product, User
from tests.helpers import csrf_from, login, make_user, seed_item


def as_admin(web, db):
    make_user(db, "boss@test.ru", "Admin1234", admin=True)
    assert login(web, "boss@test.ru", "Admin1234").status_code == 303


def post(web, url, **data):
    data["csrf_token"] = csrf_from(web.get("/admin/filters").text)
    return web.post(url, data=data, follow_redirects=False)


def flash(web):
    return web.get("/admin/filters").text


# ───────────── доступ ─────────────

def test_filters_page_is_only_for_the_owner(web, mem_db):
    assert web.get("/admin/filters", follow_redirects=False).status_code == 303          # аноним
    make_user(mem_db, "m@test.ru", "Manager123", is_manager=True)
    login(web, "m@test.ru", "Manager123")
    assert web.get("/admin/filters", follow_redirects=False).headers["location"] == "/catalog"
    web.cookies.clear()
    as_admin(web, mem_db)
    page = web.get("/admin/filters")
    assert page.status_code == 200
    for title in ("Вид цветка", "Цвет", "Рост, см", "Страна"):
        assert title in page.text
    assert "Роза" in page.text and "Эквадор" in page.text                                # стартовый набор


def test_filters_changes_need_the_csrf_token(web, mem_db):
    as_admin(web, mem_db)
    before = mem_db.query(Kind).count()
    r = web.post("/admin/filters/kind/add", data={"name": "Лилия"}, follow_redirects=False)
    assert r.status_code == 303 and mem_db.query(Kind).count() == before


# ───────────── добавить ─────────────

def test_add_values_to_every_filter(web, mem_db):
    as_admin(web, mem_db)
    post(web, "/admin/filters/kind/add", name="лилия")
    post(web, "/admin/filters/color/add", name="малиновый", swatch="#dc143c")
    post(web, "/admin/filters/height/add", name="65")
    post(web, "/admin/filters/country/add", name="Мексика")
    mem_db.expire_all()
    assert mem_db.query(Kind).filter(Kind.name == "Лилия").count() == 1                   # с большой буквы
    color = mem_db.query(Color).filter(Color.name == "Малиновый").one()
    assert color.hex == "#dc143c"
    assert mem_db.query(Height).filter(Height.cm == 65).count() == 1
    assert mem_db.query(Country).filter(Country.name == "Мексика").count() == 1
    assert {r.action for r in mem_db.query(AuditLog).all()} >= {"filter_add"}


def test_duplicates_in_any_spelling_are_not_added(web, mem_db):
    as_admin(web, mem_db)
    before = [mem_db.query(m).count() for m in (Kind, Color, Height, Country)]
    post(web, "/admin/filters/kind/add", name="  роза  ")
    post(web, "/admin/filters/color/add", name="БЕЛЫЙ")
    post(web, "/admin/filters/height/add", name="60 см")
    post(web, "/admin/filters/country/add", name="эквадор")
    assert [mem_db.query(m).count() for m in (Kind, Color, Height, Country)] == before
    assert "уже есть в списке" in flash(web)


def test_bad_values_are_refused_with_a_message(web, mem_db):
    as_admin(web, mem_db)
    before = mem_db.query(Height).count()
    post(web, "/admin/filters/height/add", name="очень высокий")
    assert "Рост" in flash(web) and mem_db.query(Height).count() == before
    post(web, "/admin/filters/height/add", name="9999")
    assert mem_db.query(Height).count() == before
    post(web, "/admin/filters/kind/add", name="   ")
    assert mem_db.query(Kind).filter(Kind.name == "").count() == 0
    assert post(web, "/admin/filters/nonsense/add", name="x").status_code == 404


# ───────────── переименовать ─────────────

def test_rename_keeps_the_id_and_the_products(web, mem_db):
    as_admin(web, mem_db)
    item = seed_item(mem_db, kind="Роза", color="Красный")
    mem_db.commit()
    color = mem_db.query(Color).filter(Color.name == "Красный").one()
    post(web, f"/admin/filters/color/{color.id}/edit", name="Алый", swatch="#ff2400")
    mem_db.expire_all()
    product = mem_db.get(Product, item.product_id)
    assert product.color_name == "Алый" and product.color_id == color.id
    assert mem_db.get(Color, color.id).hex == "#ff2400"


def test_rename_to_an_existing_name_is_refused(web, mem_db):
    as_admin(web, mem_db)
    white = mem_db.query(Color).filter(Color.name == "Белый").one()
    post(web, f"/admin/filters/color/{white.id}/edit", name="красный", swatch="#ffffff")
    mem_db.expire_all()
    assert mem_db.get(Color, white.id).name == "Белый"
    assert "уже есть в списке" in flash(web)
    h60 = mem_db.query(Height).filter(Height.cm == 60).one()
    post(web, f"/admin/filters/height/{h60.id}/edit", name="70")
    mem_db.expire_all()
    assert mem_db.get(Height, h60.id).cm == 60


def test_picker_default_white_does_not_paint_a_value_that_had_no_circle(web, mem_db):
    """Палитра в форме всегда присылает цвет; «Микс» без кружка при переименовании не должен стать белым."""
    as_admin(web, mem_db)
    mix = mem_db.query(Color).filter(Color.name == "Микс").one()
    assert not mix.hex
    post(web, f"/admin/filters/color/{mix.id}/edit", name="Микс (разный)", swatch="#ffffff")
    mem_db.expire_all()
    assert mem_db.get(Color, mix.id).hex in ("", None)


def test_edit_of_a_missing_row_is_a_message_not_an_error(web, mem_db):
    as_admin(web, mem_db)
    assert post(web, "/admin/filters/kind/99999/edit", name="x").status_code == 303
    assert post(web, "/admin/filters/kind/99999/delete").status_code == 303


# ───────────── удалить ─────────────

def test_only_unused_values_can_be_deleted(web, mem_db):
    as_admin(web, mem_db)
    item = seed_item(mem_db, kind="Роза", country="Эквадор")
    rose = mem_db.query(Kind).filter(Kind.name == "Роза").one()
    post(web, f"/admin/filters/kind/{rose.id}/delete")
    assert mem_db.query(Kind).filter(Kind.id == rose.id).count() == 1                    # используется
    assert "нельзя удалить" in flash(web)

    unused = mem_db.query(Country).filter(Country.name == "Израиль").one()
    post(web, f"/admin/filters/country/{unused.id}/delete")
    mem_db.expire_all()
    assert mem_db.query(Country).filter(Country.name == "Израиль").count() == 0
    assert mem_db.get(Product, item.product_id).country_name == "Эквадор"
    assert "filter_delete" in {r.action for r in mem_db.query(AuditLog).all()}


def test_page_shows_ids_and_how_many_products_use_each_value(web, mem_db):
    as_admin(web, mem_db)
    seed_item(mem_db, name="А", kind="Роза")
    seed_item(mem_db, name="Б", kind="Роза")
    rose = mem_db.query(Kind).filter(Kind.name == "Роза").one()
    html = web.get("/admin/filters").text
    row = re.search(r"<tr>\s*<td class=\"text-muted small\">%d</td>.*?</tr>" % rose.id, html, re.S).group(0)
    assert 'value="Роза"' in row and re.search(r'text-end small">2</td>', row)


# ───────────── форма товара ─────────────

def product_form_data(db, **over):
    data = {"name": "Роза Форма", "sku": "FORM-1", "barcode": "4600000000100",
            "description": "d", "unit": "упаковка", "package_size": 25, "min_quantity": 1,
            "kind_id": db.query(Kind).filter(Kind.name == "Роза").one().id,
            "color_id": db.query(Color).filter(Color.name == "Белый").one().id,
            "height_id": db.query(Height).filter(Height.cm == 70).one().id,
            "country_id": db.query(Country).filter(Country.name == "Колумбия").one().id}
    data.update(over)
    return data


def test_product_form_offers_every_value_and_saves_the_choice(web, mem_db):
    as_admin(web, mem_db)
    page = web.get("/admin/products/new")
    for name in ("kind_id", "color_id", "height_id", "country_id", "sku", "barcode"):
        assert f'name="{name}"' in page.text
    r = web.post("/admin/products/new", follow_redirects=False,
                 data={"csrf_token": csrf_from(page.text), **product_form_data(mem_db)})
    assert r.status_code == 303
    p = mem_db.query(Product).filter(Product.name == "Роза Форма").one()
    assert (p.sku, p.barcode) == ("FORM-1", "4600000000100")
    assert (p.kind_name, p.color_name, p.height_cm, p.country_name) == ("Роза", "Белый", 70, "Колумбия")


def test_product_edit_changes_and_clears_values(web, mem_db):
    as_admin(web, mem_db)
    item = seed_item(mem_db, color="Красный")
    page = web.get(f"/admin/products/{item.product_id}/edit")
    assert "Номер товара на сайте" in page.text and str(item.product_id) in page.text
    data = product_form_data(mem_db, name="Роза Тест", color_id=0, height_id=0, barcode="")
    web.post(f"/admin/products/{item.product_id}/edit", follow_redirects=False,
             data={"csrf_token": csrf_from(page.text), **data})
    mem_db.expire_all()
    p = mem_db.get(Product, item.product_id)
    assert (p.color_name, p.height_cm, p.country_name, p.kind_name) == ("", 0, "Колумбия", "Роза")


def test_product_form_rejects_a_duplicate_barcode_and_unknown_choices(web, mem_db):
    as_admin(web, mem_db)
    seed_item(mem_db, name="Занят", barcode="4600000000100")
    token = csrf_from(web.get("/admin/products/new").text)
    web.post("/admin/products/new", follow_redirects=False,
             data={"csrf_token": token, **product_form_data(mem_db)})
    assert mem_db.query(Product).filter(Product.name == "Роза Форма").count() == 0
    assert "уже указан у товара" in web.get("/admin").text
    web.post("/admin/products/new", follow_redirects=False,
             data={"csrf_token": token, **product_form_data(mem_db, barcode="", kind_id=99999)})
    assert mem_db.query(Product).filter(Product.name == "Роза Форма").count() == 0


def test_admin_product_table_shows_the_new_columns(web, mem_db):
    as_admin(web, mem_db)
    seed_item(mem_db, name="Роза Таблица", sku="TAB-1", barcode="4600000000200", color="Белый")
    html = web.get("/admin").text
    row = re.search(r"<tr data-search=\"[^\"]*роза таблица[^\"]*\">.*?</tr>", html, re.S | re.I).group(0)
    for text in ("арт. TAB-1", "ш/к 4600000000200", "Роза", "Белый", "60 см", "Эквадор"):
        assert text in row


# ───────────── адрес контрагента ─────────────

def register_data(**over):
    data = {"email": "new@test.ru", "password": "Secret123", "full_name": "Иван Иванов",
            "phone": "+79001234567", "company_name": "ООО Новая", "inn": "7701234567",
            "city": "Москва", "consent": "1"}
    data.update(over)
    return data


def test_registration_stores_the_optional_address(web, mem_db):
    token = csrf_from(web.get("/register").text)
    assert 'name="address"' in web.get("/register").text
    r = web.post("/register", follow_redirects=False,
                 data={"csrf_token": token, **register_data(address="Москва, ул. Цветочная, 1")})
    assert r.headers["location"] == "/registration_pending"
    assert mem_db.query(User).filter(User.email == "new@test.ru").one().address == "Москва, ул. Цветочная, 1"

    web.cookies.clear()
    token = csrf_from(web.get("/register").text)
    web.post("/register", follow_redirects=False,
             data={"csrf_token": token, **register_data(email="two@test.ru")})        # без адреса — тоже можно
    assert mem_db.query(User).filter(User.email == "two@test.ru").one().address == ""


def test_registration_refuses_a_too_long_address_and_keeps_the_form(web, mem_db):
    token = csrf_from(web.get("/register").text)
    r = web.post("/register", follow_redirects=False,
                 data={"csrf_token": token, **register_data(address="А" * 301)})
    assert r.headers["location"] == "/register"
    assert mem_db.query(User).count() == 0
    assert "Адрес слишком длинный" in web.get("/register").text


def test_profile_updates_the_address_and_staff_see_ids_and_address(web, mem_db):
    client = make_user(mem_db, "shop@test.ru", "Client123", external_id="000001234")
    login(web, "shop@test.ru", "Client123")
    page = web.get("/profile")
    assert 'name="address"' in page.text
    web.post("/profile", follow_redirects=False, data={
        "csrf_token": csrf_from(page.text), "full_name": "Иван Иванов", "phone": "+79001234567",
        "company_name": "ООО Ромашка", "inn": "7701234567", "city": "Москва",
        "address": "Москва, ул. Цветочная, 1"})
    mem_db.expire_all()
    assert mem_db.get(User, client.id).address == "Москва, ул. Цветочная, 1"

    web.cookies.clear()
    as_admin(web, mem_db)
    card = web.get(f"/admin/clients/{client.id}").text
    assert "Москва, ул. Цветочная, 1" in card and "id в 1С: 000001234" in card and f"{client.id}" in card


def test_personal_data_export_and_erasure_cover_the_address(mem_db):
    from app.services import legal
    client = make_user(mem_db, "shop@test.ru", address="Москва, ул. Цветочная, 1", external_id="000001234")
    assert legal.export_user_data(mem_db, client)["profile"]["address"] == "Москва, ул. Цветочная, 1"
    legal.anonymize_user(mem_db, client)
    mem_db.commit()
    assert (client.address, client.external_id) == ("", "")
