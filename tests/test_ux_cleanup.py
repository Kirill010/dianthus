# tests/test_ux_cleanup.py
"""Меньше шума для клиентов и сотрудников: подпись партии датой, а не номером документа 1С; один раз «после
регистрации» вместо двухсот пятидесяти; длинные фильтры под «Ещё N»; вход без приветственной плашки;
главная админки показывает только дела, требующие действия, и следующий шаг заказа — одной кнопкой."""
from app.models import Claim, Kind, Supply
from tests.helpers import csrf_from, login, make_order, make_user, seed_item
from tests.test_supply_sync import invoice, item_of, key_header, line, post, soon


def _received(web, mem_db):
    post(web, key_header(mem_db), [invoice("00000624", date="2026-10-29", status="received",
                                           items=[line(quantity=500)])])
    return item_of(mem_db)


def _admin(web, mem_db):
    make_user(mem_db, "boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")


# ───────────────────────── подпись партии ─────────────────────────

def test_client_label_is_a_short_date_without_the_1c_number(web, mem_db):
    _received(web, mem_db)
    supply = mem_db.query(Supply).one()
    assert supply.arrival_short.startswith("29 октября")
    assert supply.client_label.startswith("привоз 29 октября")
    assert "00000624" not in supply.client_label                 # номер документа 1С клиенту ничего не говорит
    assert supply.label.startswith("№ 00000624")                 # полный номер — для сотрудников и истории заказов


def test_in_transit_label_says_arrival_and_is_silent_when_the_date_has_passed(web, mem_db):
    h = key_header(mem_db)
    post(web, h, [invoice("00000625", date=soon(5).isoformat())])
    post(web, h, [invoice("00000626", date="2020-01-15")])
    mem_db.expire_all()
    ahead = mem_db.query(Supply).filter(Supply.external_id == "00000625").one()
    late = mem_db.query(Supply).filter(Supply.external_id == "00000626").one()
    assert ahead.client_label.startswith("прибытие ")
    assert late.client_label == "" and late.arrival_short == ""   # машина задерживается — прошедшую дату не показываем


def test_manual_supply_has_no_client_label(web, mem_db):
    item = seed_item(mem_db)
    assert item.supply.client_label == "" and item.supply.arrival_short == ""


def test_catalog_shows_the_batch_date_and_keeps_the_number_in_the_tooltip(web, mem_db):
    _received(web, mem_db)
    make_user(mem_db, "buyer@test.ru", password="Buyer1234")
    login(web, "buyer@test.ru", "Buyer1234")
    rows = web.get("/catalog?view=list").text
    assert "привоз 29 октября" in rows
    assert "· поставка № 00000624" not in rows                   # в строке номера нет…
    assert 'title="Поставка № 00000624 от 29.10.2026"' in rows   # …он лежит во всплывающей подсказке
    cards = web.get("/catalog?view=cards").text
    assert "Привоз:" in cards and "29 октября" in cards


# ───────────────────────── витрина ─────────────────────────

def test_public_price_list_does_not_repeat_the_registration_phrase_in_every_row(web, mem_db):
    for i in range(5):
        seed_item(mem_db, name=f"Роза {i}")
    html = web.get("/catalog?view=list").text
    assert html.count("Оптовые цены и заказ — после регистрации") == 1
    assert '<i class="fas fa-lock me-1"></i>после регистрации' not in html
    assert html.count('<span class="visually-hidden">Цена после регистрации</span>') == 5   # читалки экрана слышат


def test_long_filter_list_collapses_under_more_and_short_one_does_not(web, mem_db):
    for i in range(3):
        seed_item(mem_db, name=f"Короткий {i}", kind=f"Малый вид {i}")
    assert "filter-more" not in web.get("/catalog").text
    for i in range(10):
        seed_item(mem_db, name=f"Длинный {i}", kind=f"Вид {i}")
    html = web.get("/catalog").text
    assert 'class="filter-more"' in html and "Ещё 5" in html     # 13 видов: 8 на виду, 5 под «Ещё»


def test_selected_value_inside_the_collapsed_part_keeps_it_open(web, mem_db):
    for i in range(10):
        seed_item(mem_db, name=f"Товар {i}", kind=f"Вид {i}")
    ids = [k.id for k in mem_db.query(Kind).all()]
    html = web.get("/catalog?" + "&".join(f"kind={i}" for i in ids)).text
    assert 'class="filter-more" open' in html


def test_login_does_not_greet_with_a_banner(web, mem_db):
    make_user(mem_db, "buyer@test.ru", password="Buyer1234")
    assert login(web, "buyer@test.ru", "Buyer1234").status_code == 303
    assert "Добро пожаловать" not in web.get("/catalog").text


# ───────────────────────── админка ─────────────────────────

def test_dashboard_with_nothing_to_do_says_so_and_shows_no_empty_tiles(web, mem_db):
    _admin(web, mem_db)
    page = web.get("/admin").text
    assert "Что сделать сегодня" in page and "На сегодня всё сделано" in page
    assert "todo-tile" not in page                               # нулевые плитки не показываем
    assert "рекламаций в работе" not in page and "запросов на удаление данных" not in page


def test_dashboard_shows_tiles_only_for_open_tasks(web, mem_db):
    buyer = make_user(mem_db, "b@test.ru")
    make_user(mem_db, "pending@test.ru", approved=False)
    make_order(mem_db, buyer, seed_item(mem_db, stock=20), packs=1)
    mem_db.add(Claim(user_id=buyer.id, product_name="Роза", description="плохо плохо плохо"))
    mem_db.commit()
    _admin(web, mem_db)
    page = web.get("/admin").text
    for text in ("новых заказов — подтвердить", "заявок клиентов — одобрить", "рекламаций в работе"):
        assert text in page, text
    assert "На сегодня всё сделано" not in page
    assert "срочных" not in page                                 # срочных заказов нет — и плитки нет


def test_dashboard_numbers_of_the_day_are_one_line_and_money_is_for_the_owner(web, mem_db):
    _admin(web, mem_db)
    page = web.get("/admin").text
    assert "dash-stats" in page and "Выручка за день" in page and "Выручка за неделю" in page


def test_order_row_has_the_next_step_as_one_button(web, mem_db):
    buyer = make_user(mem_db, "b@test.ru")
    order = make_order(mem_db, buyer, seed_item(mem_db, stock=20), packs=1)
    _admin(web, mem_db)
    page = web.get("/admin").text
    assert "Подтвердить" in page                                 # у нового заказа следующий шаг — подтвердить
    r = web.post("/admin/update_order_status", follow_redirects=False,
                 data={"csrf_token": csrf_from(page), "order_id": order.id, "status": "Подтверждён"})
    assert r.status_code == 303
    page = web.get("/admin").text
    assert "Подтвердить" not in page and "В работу" in page      # теперь предлагается «В работу»


def test_finished_order_has_no_next_step_button(web, mem_db):
    buyer = make_user(mem_db, "b@test.ru")
    make_order(mem_db, buyer, seed_item(mem_db, stock=20), packs=1, status="Выполнен")
    _admin(web, mem_db)
    page = web.get("/admin").text
    assert "Перевести заказ №" not in page                       # подсказка есть только у кнопки следующего шага


def test_admin_word_is_in_the_menu_for_staff_only(web, mem_db):
    make_user(mem_db, "c@test.ru")
    login(web, "c@test.ru", "Client123")
    assert "app-nav__link--admin" not in web.get("/catalog").text
    web.cookies.clear()
    _admin(web, mem_db)
    assert "app-nav__link--admin" in web.get("/catalog").text


def test_more_menu_stays_next_to_its_button_on_wide_screens(web, mem_db):
    """Баг 11.11: у ряда вкладок был overflow, и меню «Ещё» «приклеивалось» к краю окна — на широком экране оно
    раскрывалось в двух тысячах пикселей от кнопки. Кнопка идёт сразу за вкладками, а меню — под ней."""
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text(encoding="utf-8")
    assert ".admin-tabs .dropdown { position: static" not in css
    tabs_rules = [line for line in css.splitlines() if line.startswith(".admin-tabs {")]
    assert tabs_rules and all("overflow" not in rule for rule in tabs_rules)
    _admin(web, mem_db)
    page = web.get("/admin").text
    assert '<li class="nav-item dropdown">' in page and "dropdown ms-auto" not in page
    assert 'class="dropdown-menu dropdown-menu-end"' in page


def test_manager_menu_is_one_row_with_more_and_no_owner_links(web, mem_db):
    make_user(mem_db, "m@test.ru", password="Manager123", is_manager=True)
    login(web, "m@test.ru", "Manager123")
    page = web.get("/admin").text
    assert "Ещё" in page and "/admin/claims" in page and "/admin/picking" in page
    assert "/admin/settings" not in page and "/admin/integration" not in page
