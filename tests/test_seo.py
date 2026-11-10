# tests/test_seo.py
"""SEO: страницы-разделы, главная позиция товара (canonical и sitemap), noindex для служебных страниц,
уникальные og-теги, разметка организации и хлебных крошек, часы работы для schema.org."""
import json
import re

from app.models import Product, Supply, SupplyItem
from app.services import seo, seo_pages
from tests.helpers import seed_item


def _meta(html: str, name: str) -> str:
    m = re.search(r'<meta (?:name|property)="%s" content="([^"]*)"' % re.escape(name), html)
    return m.group(1) if m else ""


def _canonical(html: str) -> str:
    m = re.search(r'<link rel="canonical" href="([^"]*)"', html)
    return m.group(1) if m else ""


def _second_item_of_same_product(db, item, stock=7):
    """Ещё одна позиция того же товара (приехала другой поставкой)."""
    supply = Supply(country="Эквадор", status="Разгружен")
    db.add(supply)
    db.flush()
    second = SupplyItem(supply_id=supply.id, product_id=item.product_id, price=item.price, stock=stock, is_active=True)
    db.add(second)
    db.commit()
    return second


# ───────────── schema_hours ─────────────

def test_schema_hours_parses_common_forms():
    assert seo.schema_hours("Пн–Вс 09:00–21:00") == "Mo-Su 09:00-21:00"
    assert seo.schema_hours("Пн-Пт, с 9:30 до 18:00") == ""        # нет двоеточий-диапазона: не гадаем
    assert seo.schema_hours("Пн-Пт 9:30-18:00") == "Mo-Fr 09:30-18:00"
    assert seo.schema_hours("") == ""
    assert seo.schema_hours("круглосуточно") == ""


# ───────────── страницы-разделы ─────────────

def test_section_page_lists_stock_and_is_indexable(web, mem_db):
    seed_item(mem_db, name="Мондиал 60 Tessa", country="Эквадор", height=60, pack=25, stock=4)
    seed_item(mem_db, name="Кустовая роза 60 SIAN", country="Кения", height=60, pack=10, stock=3)
    seed_item(mem_db, name="Алтай", kind="Хризантема", country="Голландия", height=None, pack=5, stock=2)

    r = web.get("/optom/rozy")
    assert r.status_code == 200
    html = r.text
    assert "<h1" in html and "Розы оптом в Москве" in html
    assert "Мондиал 60 Tessa" in html and "Кустовая роза 60 SIAN" in html
    assert "Алтай" not in html                                      # другой вид
    assert "2 позиции" in html and "Эквадор и Кения" in html or "Кения и Эквадор" in html
    assert _meta(html, "robots") == "index, follow"
    assert _canonical(html).endswith("/optom/rozy")
    assert "после регистрации" in html
    # хлебные крошки: видимые и в разметке
    assert 'aria-label="Хлебные крошки"' in html
    ld = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)]
    crumbs = next(d for d in ld if d.get("@type") == "BreadcrumbList")
    assert [i["position"] for i in crumbs["itemListElement"]] == [1, 2, 3]


def test_section_filters_by_country_and_name(web, mem_db):
    seed_item(mem_db, name="Мондиал 60 Tessa", country="Эквадор")
    seed_item(mem_db, name="Кустовая роза 60 SIAN", country="Кения", pack=10)
    seed_item(mem_db, name="Роза микс 40 Кения Ultraflo", country="Кения", pack=10)
    assert "Мондиал 60 Tessa" in web.get("/optom/ekvadorskie-rozy").text
    kenya = web.get("/optom/kenijskie-rozy").text
    assert "Кустовая роза 60 SIAN" in kenya and "Мондиал" not in kenya
    bush = web.get("/optom/kustovye-rozy").text
    assert "Кустовая роза 60 SIAN" in bush and "Роза микс 40 Кения Ultraflo" not in bush


def test_empty_section_is_noindex_but_unknown_slug_is_404(web, mem_db):
    r = web.get("/optom/hrizantemy")
    assert r.status_code == 200
    assert _meta(r.text, "robots") == "noindex, follow"
    assert "нет товара в наличии" in r.text
    assert web.get("/optom/net-takogo", headers={"accept": "text/html"}).status_code == 404


def test_section_ignores_sold_out_and_in_transit_items(web, mem_db):
    seed_item(mem_db, name="Роза Есть", stock=5)
    seed_item(mem_db, name="Роза Нет", stock=0, active=False)
    seed_item(mem_db, name="Роза В Пути", stock=8, active=False, status="В пути")
    html = web.get("/optom/rozy").text
    assert "Роза Есть" in html and "Роза Нет" not in html and "Роза В Пути" not in html


def test_section_description_matches_real_numbers_and_has_no_invented_claims(web, mem_db):
    seed_item(mem_db, name="Роза Раз", country="Эквадор")
    html = web.get("/optom/rozy").text
    description = _meta(html, "description")
    assert "1 позиция в наличии" in description and "Эквадор" in description
    for banned in ("напрямую от производителя", "без посредников", "цены импортёра"):
        assert banned not in html


def test_every_section_page_renders(web, mem_db):
    seed_item(mem_db, name="Роза Тест")
    for section in seo_pages.SECTIONS:
        r = web.get(f"/optom/{section.slug}")
        assert r.status_code == 200, section.slug
        assert section.h1 in r.text


# ───────────── главная позиция товара ─────────────

def test_second_supply_of_a_product_points_canonical_to_the_newest_item(web, mem_db):
    first = seed_item(mem_db, name="Кустовая роза 60 SIAN")
    second = _second_item_of_same_product(mem_db, first)
    assert second.id > first.id
    html_first = web.get(f"/product/{first.id}").text
    html_second = web.get(f"/product/{second.id}").text
    assert _canonical(html_first).endswith(f"/product/{second.id}")
    assert _canonical(html_second).endswith(f"/product/{second.id}")


def test_sitemap_has_one_url_per_product_and_the_sections_with_stock(web, mem_db):
    first = seed_item(mem_db, name="Кустовая роза 60 SIAN")
    second = _second_item_of_same_product(mem_db, first)
    other = seed_item(mem_db, name="Мондиал 60 Tessa")
    body = web.get("/sitemap.xml").text
    urls = re.findall(r"<loc>([^<]+)</loc>", body)
    assert len(urls) == len(set(urls))
    assert any(u.endswith(f"/product/{second.id}") for u in urls)
    assert not any(u.endswith(f"/product/{first.id}") for u in urls)      # дубль не попадает в карту
    assert any(u.endswith(f"/product/{other.id}") for u in urls)
    assert any(u.endswith("/optom/rozy") for u in urls)
    assert not any(u.endswith("/optom/hrizantemy") for u in urls)         # пустой раздел в карту не идёт


def test_in_transit_item_is_its_own_canonical(web, mem_db):
    item = seed_item(mem_db, name="Роза В Пути", stock=8, active=False, status="В пути")
    assert _canonical(web.get(f"/product/{item.id}").text).endswith(f"/product/{item.id}")


# ───────────── noindex, og, разметка ─────────────

def test_service_pages_are_noindex_and_public_pages_are_indexable(web):
    for path in ("/login", "/forgot"):
        r = web.get(path)
        assert r.status_code == 200, path
        assert _meta(r.text, "robots") == "noindex, follow", path
    for path in ("/", "/catalog", "/contacts", "/register", "/deadlines"):
        assert _meta(web.get(path).text, "robots") == "index, follow", path


def test_open_graph_follows_the_page_title_and_description(web, mem_db):
    item = seed_item(mem_db, name="Мондиал 60 Tessa", country="Эквадор")
    html = web.get(f"/product/{item.id}").text
    assert _meta(html, "og:title") == "Мондиал 60 Tessa — оптом · Диантус"
    assert _meta(html, "og:description") == _meta(html, "description")
    assert "из Эквадора" in _meta(html, "og:description")
    landing = web.get("/").text
    assert _meta(landing, "og:title") != _meta(html, "og:title")


def test_business_markup_on_contacts_and_landing(web):
    for path in ("/contacts", "/"):
        html = web.get(path).text
        blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)]
        biz = next(d for d in blocks if d.get("@type") == "Florist")
        assert biz["name"] and biz["url"].startswith("http")
        assert biz["contactPoint"][0]["contactType"] == "sales"


# ───────────── аналитика: только по настройке и после согласия ─────────────

def test_no_analytics_and_no_cookie_banner_by_default(web):
    r = web.get("/")
    assert "mc.yandex.ru" not in r.text and 'id="cookie-notice"' not in r.text
    assert "mc.yandex.ru" not in r.headers["content-security-policy"]


def test_metrika_is_wired_only_when_the_counter_id_is_set(web, monkeypatch):
    from app.config import config
    monkeypatch.setattr(config, "YANDEX_METRIKA_ID", "12345678")
    r = web.get("/")
    csp = r.headers["content-security-policy"]
    assert 'id="cookie-notice"' in r.text and "Согласен" in r.text and "Отказаться" in r.text
    assert "12345678" in r.text
    # счётчик не вшит в страницу: скрипт грузится из кода только после согласия
    assert "mc.yandex.ru/metrika/tag.js" in r.text and 'src="https://mc.yandex.ru' not in r.text
    assert "script-src 'self' 'nonce-" in csp and "https://mc.yandex.ru" in csp.split("script-src", 1)[1].split(";")[0]
    assert "wss://mc.yandex.ru" in csp.split("connect-src", 1)[1].split(";")[0]


def test_footer_links_to_sections_on_every_page(web):
    html = web.get("/deadlines").text
    assert 'href="/optom/rozy"' in html and "Цветы оптом" in html
    assert 'href="/optom/rozy"' in web.get("/").text
