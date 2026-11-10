# tests/test_design_shell.py
"""Оболочка сайта: меню в шапке на компьютере и нижняя панель на телефоне, подвал, свои шрифты и лёгкие
картинки, фильтры каталога слева (на телефоне — выезжающая панель), доступность (ссылка «к содержимому»,
подписи полей). Эти проверки не дают вернуться к тому, что было: шрифтов без файлов, логотипа на 1 МБ,
иконок вместо слов в меню."""
import re
from pathlib import Path

from app.models import Kind
from tests.helpers import login, make_user, seed_item

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"


def client_page(web, db, url="/catalog", **extra):
    make_user(db, **extra)
    assert login(web, "client@test.ru", "Client123").status_code == 303
    return web.get(url).text


# ───────────────────────── доступность ─────────────────────────

def test_every_page_has_a_skip_link_and_a_target_for_it(web):
    for url in ("/", "/catalog", "/login", "/register", "/privacy"):
        html = web.get(url).text
        assert 'class="skip-link" href="#main"' in html, url
        assert re.search(r'<main id="main"', html), url


def test_login_page_has_a_heading_and_visible_labels(web):
    html = web.get("/login").text
    assert "<h1" in html
    for field in ("login-email", "login-password"):
        assert f'for="{field}"' in html and f'id="{field}"' in html, field


def test_quantity_fields_in_cards_have_labels(web, mem_db):
    seed_item(mem_db, name="Роза Красная", stock=40)
    html = re.sub(r"<script.*?</script>", "", client_page(web, mem_db), flags=re.S)      # поля в тексте скриптов не считаем
    fields = re.findall(r"<input[^>]*>", html)
    numbers = [f for f in fields if 'type="number"' in f]
    assert numbers, "в каталоге нет ни одного поля количества"
    assert all("aria-label=" in f for f in numbers), numbers


# ───────────────────────── шапка, меню, подвал ─────────────────────────

def test_logged_in_header_has_a_text_menu_and_the_phone_keeps_its_bottom_bar(web, mem_db):
    html = client_page(web, mem_db, "/orders")
    header = html.split('<header class="app-header')[1].split("</header>")[0]
    for url in ("/catalog", "/orders", "/preorders", "/deadlines", "/contacts"):
        assert f'href="{url}"' in header, url
    nav = header.split('class="app-nav"')[1].split("</nav>")[0]
    assert re.search(r'href="/orders" class="app-nav__link active"', nav)       # страница, на которой мы
    assert 'class="bottom-nav"' in html                                         # телефону — нижняя панель


def test_public_header_footer_and_documents_links(web):
    html = web.get("/").text
    footer = html.split('class="app-footer"')[1]
    for url in ("/terms", "/privacy", "/consent", "/marketing-consent", "/contacts", "/deadlines"):
        assert f'href="{url}"' in footer, url
    assert 'aria-label="Диантус — оптовый магазин цветов, главная"' in html


# ───────────────────────── шрифты и картинки ─────────────────────────

def test_site_fonts_are_served_by_the_site_itself(web):
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*{[^}]*}", css)
    assert len(faces) == 4 and all("font-display: swap" in f for f in faces)
    files = re.findall(r"url\('(/static/fonts/[^']+)'\)", css)
    assert files
    for url in files:
        assert (ROOT / "app" / url.lstrip("/")).is_file(), url
        assert web.get(url).status_code == 200, url
    assert "fonts.googleapis" not in css and "fonts.gstatic" not in css       # посетитель не ходит на чужие серверы
    for licence in ("OFL-Montserrat.txt", "OFL-PlayfairDisplay.txt"):
        assert (STATIC / "fonts" / licence).is_file(), licence
    html = web.get("/").text
    assert 'rel="preload" href="/static/fonts/montserrat-cyrillic-wght-normal.woff2"' in html


def test_pictures_used_on_every_page_are_light():
    """Раньше на странице входа грузился логотип на 1 МБ, а в шапке каждой страницы — иконка на 113 КБ."""
    refs = set()
    for name in ("base.html", "landing.html", "login.html"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        refs |= set(re.findall(r"/static/(?!vendor/)([\w./-]+\.(?:png|webp|jpg|ico))", text))
    assert {"brand-mark.webp", "emblem.webp", "og-image.jpg"} <= refs
    for ref in sorted(refs):
        path = STATIC / ref
        assert path.is_file(), ref
        assert path.stat().st_size <= 150 * 1024, f"{ref}: {path.stat().st_size // 1024} КБ"
    assert not (STATIC / "logo-full.png").exists() and not (STATIC / "logo-icon.png").exists()
    assert (STATIC / "brand-mark.webp").stat().st_size <= 20 * 1024            # лежит в шапке каждой страницы


# ───────────────────────── каталог: фильтры слева / панель на телефоне ─────────────────────────

def test_catalog_filters_are_a_side_panel_that_becomes_a_drawer_on_phones(web, mem_db):
    seed_item(mem_db, name="Роза Красная", color="Красный")
    seed_item(mem_db, name="Гвоздика Микс", kind="Гвоздика", color="Розовый")
    html = client_page(web, mem_db)
    assert re.search(r'<aside class="catalog-aside offcanvas-lg offcanvas-start"[^>]*id="catalogFilters"', html)
    assert html.count('<details class="filter-group"') >= 3                   # наличие, вид, цвет …
    assert 'catalog-filters-btn' in html and 'data-bs-target="#catalogFilters"' in html
    assert "Показать товары (2)" in html                                      # кнопка закрытия панели на телефоне
    assert "active-filters" not in html                                       # ничего не выбрано — плашек нет


def test_picked_filters_are_shown_as_removable_chips(web, mem_db):
    seed_item(mem_db, name="Роза Красная")
    seed_item(mem_db, name="Гвоздика Микс", kind="Гвоздика")
    rose = mem_db.query(Kind).filter(Kind.name == "Роза").one().id
    html = client_page(web, mem_db, f"/catalog?kind={rose}")
    chips = html.split('class="active-filters"')[1].split("</div>")[0]
    assert "Вид: Роза" in chips and "Сбросить все" in chips
    assert re.search(r'href="/catalog"[^>]*title="Убрать фильтр «Роза»"', chips)     # крестик снимает фильтр


# ───────────────────────── кэш браузера и подписи полей ─────────────────────────

def test_static_files_are_cached_by_the_browser(web):
    font = web.get("/static/fonts/montserrat-latin-wght-normal.woff2")
    assert font.status_code == 200 and "max-age=604800" in font.headers["cache-control"]
    css = web.get("/static/style.css")
    assert css.status_code == 200 and "max-age=3600" in css.headers["cache-control"]
    assert web.get("/static/no-such-file.css").status_code == 404


def test_profile_labels_point_to_their_fields(web, mem_db):
    html = client_page(web, mem_db, "/profile")
    for_ids = re.findall(r'<label[^>]*for="([^"]+)"', html)
    assert len(for_ids) >= 12
    for target in for_ids:
        assert f'id="{target}"' in html, target
    page = re.sub(r"<script.*?</script>", "", html, flags=re.S)
    bare = [f for f in re.findall(r"<input[^>]*>", page)
            if 'type="hidden"' not in f and 'aria-label=' not in f and 'id="' not in f]
    assert not bare, bare


def test_guest_cards_have_one_register_link_and_no_button_per_card(web, mem_db):
    for n in range(3):
        seed_item(mem_db, name=f"Роза {n}", stock=30)
    html = web.get("/catalog?view=cards").text
    assert "product-card-v2__out-btn--cta" not in html
    links = re.findall(r'<a href="/register" class="product-card-v2__price-locked"', html)
    assert len(links) == 3


def test_static_fonts_and_webp_pictures_have_correct_types(web):
    """В образе Docker нет системного списка типов файлов: без своей подсказки шрифт и WebP уходили бы как octet-stream."""
    assert web.get("/static/fonts/montserrat-latin-wght-normal.woff2").headers["content-type"] == "font/woff2"
    assert web.get("/static/emblem.webp").headers["content-type"] == "image/webp"


def test_landing_hides_the_stock_counter_while_the_catalog_is_empty(web, mem_db):
    empty = web.get("/").text
    assert "позиций в наличии" not in empty and "позиция в наличии" not in empty
    seed_item(mem_db, name="Роза Красная", stock=40)
    assert "позиция в наличии" in web.get("/").text             # одна позиция: число и слово согласованы
