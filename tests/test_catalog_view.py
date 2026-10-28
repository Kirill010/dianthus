# tests/test_catalog_view.py
"""Каталог: режим «Прайс», счётчики категорий, цена со скидкой клиента, строка
«обновлено», сортировка; установка на телефон (сервис-воркер, манифест, баннер)."""
import json
import re
import struct
from datetime import datetime, timedelta, timezone

from app.models import Color, Country, ExchangeLog, Height, Kind
from app.services import exchange_log
from app.timeutils import fmt_when
from tests.helpers import login, make_user, seed_item


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def client_session(web, db, **extra):
    make_user(db, **extra)
    r = login(web, "client@test.ru", "Client123")
    assert r.status_code == 303, r.text


def chip(html: str, name: str):
    """Число на кнопке фильтра (или None, если кнопки нет)."""
    m = re.search(r">\s*" + re.escape(name) + r'\s*<span class="category-btn__n">(\d+)</span>', html)
    return int(m.group(1)) if m else None


def ident(db, model, name):
    """id записи справочника по названию (для рост — по сантиметрам)."""
    column = model.cm if model is Height else model.name
    return db.query(model).filter(column == name).one().id


def log_row(db, endpoint="products/sync", ok=True, summary="товаров: 5, новых: 1", **kw):
    db.add(ExchangeLog(endpoint=endpoint, ok=ok, status_code=200 if ok else 401,
                       summary=summary, created_at=kw.pop("created_at", _now()), **kw))
    db.commit()


# ───────────── режим «Прайс» ─────────────

def test_list_view_groups_by_kind_and_shows_client_price(web, mem_db):
    a = seed_item(mem_db, name="Роза Фридом", price=50.0)
    seed_item(mem_db, name="Гвоздика Микс", price=20.0, kind="Гвоздика")
    # порядок групп задаётся в «Фильтрах каталога» (sort_order), а не алфавитом
    mem_db.query(Kind).filter(Kind.name == "Роза").one().sort_order = 1
    mem_db.query(Kind).filter(Kind.name == "Гвоздика").one().sort_order = 2
    mem_db.commit()
    client_session(web, mem_db, discount_percent=10)

    html = web.get("/catalog?view=list").text
    assert html.count('class="price-row ') == 2
    assert 'class="price-list__group">Гвоздика<' in html
    assert 'class="price-list__group">Роза<' in html
    assert html.index("Роза Фридом") < html.index("Гвоздика Микс")
    assert "Цена, ₽/шт" in html
    # цена со скидкой 10%: 50 → 45, старая зачёркнута; итог считает корзина
    assert '<s class="price-old">50</s> 45 ₽' in html
    assert "с вашей скидкой −10%" in html
    assert 'action="/add_to_cart"' in html and f'value="{a.id}"' in html


def test_cards_view_shows_discount_note_only_when_client_has_discount(web, mem_db):
    seed_item(mem_db, name="Роза Фридом", price=50.0)
    client_session(web, mem_db, discount_percent=7.5)
    html = web.get("/catalog?view=cards").text
    assert "product-card-v2" in html and "price-row" not in html
    assert "с вашей скидкой −7.5%" in html
    assert '<s class="price-old">50</s> 46.25 ₽' in html


def test_no_discount_no_strikethrough(web, mem_db):
    seed_item(mem_db, price=50.0)
    client_session(web, mem_db)                                      # скидка 0
    for view in ("cards", "list"):
        html = web.get(f"/catalog?view={view}").text
        assert "price-old" not in html.split("</style>")[-1].split("<main", 1)[-1]
        assert "с вашей скидкой" not in html


def test_view_choice_is_remembered_in_cookie(web, mem_db):
    seed_item(mem_db)
    client_session(web, mem_db)
    r = web.get("/catalog?view=list")
    assert "catalog_view=list" in r.headers.get("set-cookie", "")
    assert "price-list" in web.get("/catalog").text                  # запомнилось
    web.get("/catalog?view=cards")
    assert "price-list" not in web.get("/catalog").text.split("<main", 1)[-1]
    # мусор в параметре игнорируется и cookie не портит
    r = web.get("/catalog?view=%3Cscript%3E")
    assert "set-cookie" not in r.headers or "catalog_view" not in r.headers["set-cookie"]
    assert r.status_code == 200


def test_public_list_view_hides_prices_and_cart(web, mem_db):
    seed_item(mem_db, name="Роза Секретная", price=77.0)
    html = web.get("/catalog?view=list").text
    assert "Роза Секретная" in html
    assert "после регистрации" in html
    assert "77" not in html.split("<main", 1)[-1].split("price-list__head", 1)[-1].split("</ul>")[0]
    assert 'action="/add_to_cart"' not in html


def test_list_view_has_bigger_pages_than_cards(web, mem_db):
    for i in range(60):
        seed_item(mem_db, name=f"Роза {i:02d}")
    client_session(web, mem_db)
    first = web.get("/catalog?view=list").text
    second = web.get("/catalog?view=list&page=2").text
    assert first.count('class="price-row ') == 50
    assert second.count('class="price-row ') == 10
    assert web.get("/catalog?view=cards").text.count('class="product-card-v2 ') == 12


# ───────────── фильтры: вид, цвет, рост, страна ─────────────

def seed_filter_catalog(db):
    """Три товара: у каждого свой вид, цвет, рост и страна."""
    seed_item(db, name="Роза Красная", kind="Роза", color="Красный", height=60, country="Эквадор")
    seed_item(db, name="Роза Белая", kind="Роза", color="Белый", height=70, country="Колумбия")
    seed_item(db, name="Гвоздика Микс", kind="Гвоздика", color="Красный", height=60, country="Колумбия")


def test_filter_chips_show_counts_that_match_the_result(web, mem_db):
    seed_filter_catalog(mem_db)
    html = web.get("/catalog").text
    assert (chip(html, "Роза"), chip(html, "Гвоздика")) == (2, 1)
    assert (chip(html, "Красный"), chip(html, "Белый")) == (2, 1)
    assert (chip(html, "60 см"), chip(html, "70 см")) == (2, 1)
    assert (chip(html, "Эквадор"), chip(html, "Колумбия")) == (1, 2)
    assert "Сбросить фильтры" not in html                           # ничего не выбрано
    # число на кнопке = сколько товаров покажет страница
    assert "2 товаров" in web.get(f"/catalog?kind={ident(mem_db, Kind, 'Роза')}").text


def test_several_values_of_one_filter_and_filters_of_different_kinds_combine(web, mem_db):
    seed_filter_catalog(mem_db)
    red, white = ident(mem_db, Color, "Красный"), ident(mem_db, Color, "Белый")
    rose = ident(mem_db, Kind, "Роза")

    both = web.get(f"/catalog?color={red}&color={white}").text          # «или» внутри одного фильтра
    assert "3 товаров" in both and "Сбросить фильтры" in both
    red_only = web.get(f"/catalog?color={red}").text
    assert "Роза Красная" in red_only and "Гвоздика Микс" in red_only and "Роза Белая" not in red_only
    # «и» между разными фильтрами
    assert "1 товаров" in web.get(f"/catalog?color={red}&kind={rose}").text
    # счёт у каждого значения считается при ДРУГИХ выбранных фильтрах, но без своего собственного
    html = web.get(f"/catalog?color={red}").text
    assert chip(html, "Красный") == 2 and chip(html, "Белый") == 1
    assert chip(html, "Роза") == 1 and chip(html, "Гвоздика") == 1
    # ссылка на выбранное значение его снимает, а на другое — добавляет
    assert f'href="/catalog?color={red}&amp;color={white}"' in html or f'href="/catalog?color={red}&color={white}"' in html
    assert 'href="/catalog"' in html


def test_filter_by_height_and_country_and_search_by_their_names(web, mem_db):
    seed_filter_catalog(mem_db)
    h70, columbia = ident(mem_db, Height, 70), ident(mem_db, Country, "Колумбия")
    page = web.get(f"/catalog?height={h70}").text
    assert "Роза Белая" in page and "Роза Красная" not in page
    page = web.get(f"/catalog?country={columbia}").text
    assert "Роза Белая" in page and "Гвоздика Микс" in page and "Роза Красная" not in page
    # поиск понимает названия из справочников и рост числом
    assert "Роза Белая" in web.get("/catalog?q=Колумбия").text
    assert "Роза Белая" in web.get("/catalog?q=70").text and "Роза Красная" not in web.get("/catalog?q=70").text
    assert "Гвоздика Микс" in web.get("/catalog?q=Красный").text and "Роза Белая" not in web.get("/catalog?q=Красный").text


def test_search_finds_product_by_sku_and_barcode(web, mem_db):
    seed_item(mem_db, name="Роза Артикульная", sku="ROSA-FREEDOM-60", barcode="4600000000001")
    assert "Роза Артикульная" in web.get("/catalog?q=ROSA-FREEDOM").text
    assert "Роза Артикульная" in web.get("/catalog?q=4600000000001").text
    assert "Роза Артикульная" not in web.get("/catalog?q=4600000000999").text


def test_unknown_or_garbage_filter_values_do_not_break_the_page(web, mem_db):
    seed_filter_catalog(mem_db)
    for query in ("kind=999999", "color=abc", "height=-5", "country=", "kind=1&kind=1&kind=1",
                  "color=" + "9" * 40, "category=Нет такого"):
        assert web.get("/catalog?" + query).status_code == 200, query


def test_old_text_links_with_category_and_country_names_still_work(web, mem_db):
    seed_filter_catalog(mem_db)
    page = web.get("/catalog?category=Гвоздика").text
    assert "Гвоздика Микс" in page and "Роза Красная" not in page
    page = web.get("/catalog?country=Колумбия").text
    assert "Роза Белая" in page and "Роза Красная" not in page


def test_filter_panel_is_hidden_when_there_are_no_products(web, mem_db):
    html = web.get("/catalog").text
    assert 'id="catalogFilters"' not in html


def test_color_chip_shows_its_circle(web, mem_db):
    seed_item(mem_db, name="Роза Красная", color="Красный")
    mem_db.query(Color).filter(Color.name == "Красный").one().hex = "#c0392b"
    mem_db.commit()
    html = web.get("/catalog").text
    assert 'class="color-dot " style="background: #c0392b"' in html


# ───────────── счётчики видов и уровней ─────────────

def test_kind_counts_follow_search_and_hide_empty(web, mem_db):
    seed_item(mem_db, name="Роза Красная")
    seed_item(mem_db, name="Гвоздика Микс", kind="Гвоздика")
    html = web.get("/catalog?q=Роза").text
    assert chip(html, "Роза") == 1
    assert chip(html, "Гвоздика") is None                           # по запросу пусто — кнопки нет
    # выбранный вид остаётся на месте, даже если в нём ничего не нашлось
    html = web.get(f"/catalog?q=Роза&kind={ident(mem_db, Kind, 'Гвоздика')}").text
    assert chip(html, "Гвоздика") == 0
    assert "Ничего не найдено" in html


def test_kind_from_1c_outside_the_default_list_gets_its_own_chip(web, mem_db):
    seed_item(mem_db, name="Эустома Белая", kind="Эустома")
    html = web.get("/catalog").text
    assert chip(html, "Эустома") == 1
    assert "Эустома Белая" in web.get(f"/catalog?kind={ident(mem_db, Kind, 'Эустома')}").text


def test_level_counts_follow_the_search(web, mem_db):
    seed_item(mem_db, name="Роза Красная", stock=100, pack=25)
    seed_item(mem_db, name="Гвоздика Микс", stock=100, pack=25)
    assert "В наличии (2)" in web.get("/catalog").text
    assert "В наличии (1)" in web.get("/catalog?q=Гвоздика").text


# ───────────── сортировка ─────────────

def test_sort_select_orders_items_and_is_kept_in_links(web, mem_db):
    seed_item(mem_db, name="Роза Дорогая", price=90.0)
    seed_item(mem_db, name="Роза Дешёвая", price=10.0)
    asc = web.get("/catalog?sort=price_asc").text
    desc = web.get("/catalog?sort=price_desc").text
    assert asc.index("Роза Дешёвая") < asc.index("Роза Дорогая")
    assert desc.index("Роза Дорогая") < desc.index("Роза Дешёвая")
    assert 'id="sortSelect"' in asc
    assert re.search(r'value="/catalog\?sort=price_asc" selected', asc)
    assert 'name="sort" value="price_asc"' in asc                    # поиск не сбрасывает сортировку
    assert "sort=price_asc" in asc.split("category-scroll", 1)[1]    # а кнопки категорий — тем более
    assert web.get("/catalog?sort=bogus").status_code == 200


def test_list_view_default_sort_is_called_by_groups(web, mem_db):
    seed_item(mem_db)
    assert "По группам" in web.get("/catalog?view=list").text
    assert "Сначала новые" in web.get("/catalog?view=cards").text


# ───────────── «Цены и остатки обновлены …» ─────────────

def test_freshness_strip_follows_the_last_successful_catalog_load(web, mem_db):
    seed_item(mem_db)
    client_session(web, mem_db)
    assert "обновлены" not in web.get("/catalog").text               # обмена ещё не было

    log_row(mem_db, "products/sync", ok=False, summary="")           # неудачная попытка
    log_row(mem_db, "products/sync", summary=f"{exchange_log.DRY_RUN_MARK} — товаров: 5")
    log_row(mem_db, "orders/export", summary="заказов: 3")           # не каталог
    assert "обновлены" not in web.get("/catalog").text

    log_row(mem_db, "file-import", summary="файл «прайс.xlsx»: товаров: 9",
            created_at=_now() - timedelta(days=1))
    html = web.get("/catalog").text
    assert "Цены и остатки обновлены вчера в" in html

    log_row(mem_db, "products/sync")
    assert "Цены и остатки обновлены сегодня в" in web.get("/catalog").text


def test_public_strip_does_not_mention_prices(web, mem_db):
    seed_item(mem_db)
    log_row(mem_db, "products/sync")
    html = web.get("/catalog").text
    assert "Остатки обновлены сегодня в" in html
    assert "Цены и остатки" not in html


def test_last_catalog_update_ignores_dry_runs_everywhere(mem_db):
    assert exchange_log.last_catalog_update(mem_db) is None
    log_row(mem_db, "products/sync", summary=f"{exchange_log.DRY_RUN_MARK} — товаров: 1")
    log_row(mem_db, "file-import", summary=f"файл «a.csv»: {exchange_log.DRY_RUN_MARK} — товаров: 1")
    assert exchange_log.last_catalog_update(mem_db) is None
    ov = exchange_log.overview(mem_db, legacy_password_set=False)
    assert ov["last_sync"] is None                                   # и на плашке «Интеграция» тоже
    log_row(mem_db, "products/sync", summary="товаров: 4")
    assert exchange_log.last_catalog_update(mem_db).summary == "товаров: 4"


def test_dry_run_through_the_api_does_not_refresh_the_strip(web, mem_db):
    from app.services import api_keys
    _, raw = api_keys.create_key(mem_db, "Проверка", "")
    body = {"products": [{"id": "G-1", "name": "Роза 1", "package_size": 25,
                          "price_per_stem": 40, "stock_packs": 10}]}
    r = web.post("/api/1c/products/sync?dry_run=1", json=body, headers={"X-API-Key": raw})
    assert r.status_code == 200
    mem_db.expire_all()
    assert exchange_log.last_catalog_update(mem_db) is None
    web.post("/api/1c/products/sync", json=body, headers={"X-API-Key": raw})
    mem_db.expire_all()
    assert exchange_log.last_catalog_update(mem_db) is not None


def test_fmt_when_today_yesterday_older():
    now = _now()
    assert re.fullmatch(r"сегодня в \d\d:\d\d", fmt_when(now))
    assert re.fullmatch(r"вчера в \d\d:\d\d", fmt_when(now - timedelta(days=1)))
    assert re.fullmatch(r"\d\d\.\d\d(\.\d{4})? в \d\d:\d\d", fmt_when(now - timedelta(days=5)))
    assert fmt_when(None) == "—"


# ───────────── установка на телефон ─────────────

def test_service_worker_is_served_from_the_site_root(web):
    r = web.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert "no-cache" in r.headers["cache-control"]
    assert "OFFLINE_URL" in r.text


def test_every_file_the_service_worker_precaches_exists(web):
    """cache.addAll падает целиком, если хоть один файл 404 — тогда воркер не установится."""
    js = web.get("/sw.js").text
    block = js.split("STATIC_ASSETS = [", 1)[1].split("];", 1)[0]
    paths = re.findall(r"'(/static/[^']+)'", block)
    offline = re.search(r"OFFLINE_URL = '([^']+)'", js).group(1)
    assert "OFFLINE_URL" in block and offline == "/static/offline.html"   # заглушка кэшируется при установке
    paths.append(offline)
    assert len(paths) >= 6
    for p in paths:
        assert web.get(p).status_code == 200, p


def test_manifest_icons_are_square_and_match_their_declared_size(web):
    manifest = json.loads(web.get("/static/manifest.json").text)
    assert manifest["start_url"] == "/catalog" and manifest["display"] == "standalone"
    sizes = set()
    for icon in manifest["icons"]:
        raw = web.get(icon["src"]).content
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", icon["src"]
        w, h = struct.unpack(">II", raw[16:24])
        assert f"{w}x{h}" == icon["sizes"] and w == h, icon
        sizes.add(w)
    assert {192, 512} <= sizes
    assert any(i.get("purpose") == "maskable" for i in manifest["icons"])


def test_install_banner_only_on_start_pages_and_old_registration_is_gone(web, mem_db):
    seed_item(mem_db)
    catalog = web.get("/catalog").text
    assert 'id="pwaBanner"' in catalog and "/static/pwa.js" in catalog
    assert 'id="pwaBanner"' not in web.get("/contacts").text
    assert "/static/sw.js" not in catalog                           # регистрация только корневая
    assert 'rel="apple-touch-icon"' in catalog
    js = web.get("/static/pwa.js").text
    assert "register('/sw.js'" in js and "'/static/'" in js         # и старую, из /static/, снимаем


def test_robots_txt_is_served_from_the_root_with_a_live_sitemap_link(web):
    r = web.get("/robots.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "Disallow: /admin" in r.text and "Allow: /catalog" in r.text
    assert r.text.count("Sitemap:") == 1
    assert r.text.strip().endswith("/sitemap.xml")
    assert "/static/robots.txt" not in r.text
