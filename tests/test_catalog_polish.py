# tests/test_catalog_polish.py
"""Каталог без фото: прайс-лист по умолчанию, узкие карточки, подпись «за шт.», фильтр из одного значения
не показывается; клиентам не показывается название поставщика вместо страны."""
from app.models import Product, Supply
from tests.helpers import login, make_user, seed_item


def _with_photo(db, item):
    item.product.image_url = "/static/uploads/x.webp"
    db.commit()


# ───────────── вид по умолчанию ─────────────

def test_without_photos_the_price_list_is_the_default(web, mem_db):
    seed_item(mem_db, name="Роза А")
    seed_item(mem_db, name="Роза Б")
    html = web.get("/catalog").text
    assert 'class="price-row ' in html and 'class="product-card-v2 ' not in html


def test_with_photos_cards_are_the_default(web, mem_db):
    items = [seed_item(mem_db, name=f"Роза {i}") for i in range(4)]
    for it in items[:3]:
        _with_photo(mem_db, it)                                   # у 3 из 4 есть фото
    html = web.get("/catalog").text
    assert 'class="product-card-v2 ' in html and 'class="price-row ' not in html


def test_visitor_choice_beats_the_automatic_default(web, mem_db):
    seed_item(mem_db, name="Роза А")
    assert 'class="product-card-v2 ' in web.get("/catalog?view=cards").text       # явный выбор
    assert 'class="product-card-v2 ' in web.get("/catalog").text                  # и запомнился в cookie
    web.cookies.clear()
    assert 'class="price-row ' in web.get("/catalog").text                        # без выбора — прайс


def test_empty_catalog_defaults_to_cards(web):
    assert "price-row" not in web.get("/catalog").text.split("<main", 1)[-1]


# ───────────── карточки: нет фото, цена за штуку ─────────────

def test_card_without_photo_is_compact_and_price_is_marked_per_piece(web, mem_db):
    seed_item(mem_db, name="Роза Без Фото", price=87.5)
    with_photo = seed_item(mem_db, name="Роза С Фото", price=60.0)
    _with_photo(mem_db, with_photo)
    make_user(mem_db)
    login(web, "client@test.ru", "Client123")
    html = web.get("/catalog?view=cards").text
    cards = html.split('<div class="product-card-v2 ')[1:]
    no_photo = next(c for c in cards if "Роза Без Фото" in c)
    photo = next(c for c in cards if "Роза С Фото" in c)
    assert "product-card-v2--nophoto" in no_photo.split(">", 1)[0]
    assert "product-card-v2--nophoto" not in photo.split(">", 1)[0]
    assert "87.50 ₽" in no_photo and 'class="price-unit">за шт.<' in no_photo
    assert 'class="price-unit">за шт.<' in photo


def test_guest_sees_no_price_label(web, mem_db):
    seed_item(mem_db, price=87.5)
    html = web.get("/catalog?view=cards").text
    assert "Цена после регистрации" in html and "price-unit" not in html.split("<main", 1)[-1]


# ───────────── фильтр из одного значения ─────────────

def _group(title):
    return f'<summary class="filter-group__title">{title}'


def test_single_option_filter_group_is_hidden_until_there_is_a_choice(web, mem_db):
    seed_item(mem_db, name="Роза Красная", kind="Роза", color="Красный", country="Эквадор")
    seed_item(mem_db, name="Гвоздика", kind="Гвоздика", color=None, country="Эквадор")
    html = web.get("/catalog").text
    assert _group("Вид") in html                                   # два вида — выбор есть
    assert _group("Цвет") not in html and _group("Страна") not in html     # одно значение — не показываем
    seed_item(mem_db, name="Роза Белая", kind="Роза", color="Белый", country="Колумбия")
    html = web.get("/catalog").text
    assert _group("Цвет") in html and _group("Страна") in html     # появился выбор — группы вернулись


def test_selected_single_option_stays_visible_so_it_can_be_unticked(web, mem_db):
    from app.models import Color
    seed_item(mem_db, name="Роза Красная", kind="Роза", color="Красный")
    cid = mem_db.query(Color).filter(Color.name == "Красный").one().id
    html = web.get(f"/catalog?color={cid}").text
    assert _group("Цвет") in html                                  # выбранное значение можно снять


# ───────────── поставщик вместо страны ─────────────

def test_supplier_name_is_not_shown_to_clients_instead_of_country(mem_db):
    from_1c = Supply(country="РЫНОК", status="Разгружен", external_id="X-1", number="1",
                     notes="Инвойс 1С № 1, поставщик РЫНОК")
    fallback = Supply(country="Поставка из 1С", status="Разгружен", external_id="X-2", number="2",
                      notes="Инвойс 1С № 2")
    with_country = Supply(country="Эквадор", city="Кито", status="Разгружен", external_id="X-3", number="3",
                          notes="Инвойс 1С № 3, поставщик ECUANROS S.A.")
    manual = Supply(country="Россия", status="Разгружен")                  # заведена вручную
    assert from_1c.location == "" and fallback.location == ""
    assert with_country.location == "Эквадор, Кито"
    assert manual.location == "Россия"
    assert from_1c.country == "РЫНОК"                                      # сотрудникам в админке он виден


def test_admin_still_sees_the_supplier_in_the_supplies_table(web, mem_db):
    seed_item(mem_db, name="Горшок")
    s = mem_db.query(Supply).first()
    s.country, s.external_id, s.number = "РЫНОК", "A-1", "1"
    s.notes = "Инвойс 1С № 1, поставщик РЫНОК"
    mem_db.commit()
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    assert "РЫНОК" in web.get("/admin").text
