# tests/test_site_quality.py
"""Планка качества публичных страниц — то же, что проверяют Lighthouse и scripts/site_audit.py.

Если кто-то в будущем сломает заголовки, описания, подписи полей, разметку или вернёт «прыгающий» баннер,
тест упадёт раньше, чем изменение попадёт на сайт. Правила («какая длина описания хорошая» и т. п.) —
те же, что в сводке аудита."""
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import templating
from app.services import help_page, seo_pages
from tests.helpers import seed_item

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("site_audit", ROOT / "scripts" / "site_audit.py")
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)

PUBLIC_PAGES = ["/", "/catalog", "/contacts", "/deadlines", "/help", "/terms", "/privacy", "/consent",
                "/marketing-consent", "/register", "/optom/rozy"]


def parse(html: str):
    p = audit.PageParser()
    p.feed(html)
    return p


@pytest.fixture
def shelf(mem_db):
    """Витрина с двумя розами и одним хризантемой: страницы не пустые, есть из чего строить описания."""
    seed_item(mem_db, name="Мондиал 60 Tessa", country="Эквадор", height=60, pack=25, stock=4)
    seed_item(mem_db, name="Кустовая роза 60 SIAN", country="Кения", height=60, pack=10, stock=3)
    seed_item(mem_db, name="Алтай", kind="Хризантема", country="Голландия", height=None, pack=5, stock=2)
    return mem_db


@pytest.fixture
def pages(web, shelf):
    """HTML всех публичных страниц, разобранный парсером аудита."""
    out = {}
    for path in PUBLIC_PAGES:
        r = web.get(path)
        assert r.status_code == 200, path
        out[path] = (r.text, parse(r.text))
    return out


# ───────────── заголовки, описания, заголовки h1–h6 ─────────────

def test_titles_and_descriptions_have_good_length_and_are_unique(pages):
    titles, descs = {}, {}
    for path, (_html, p) in pages.items():
        title = re.sub(r"\s+", " ", p.title).strip()
        desc = p.meta.get("description", "").strip()
        assert 20 <= len(title) <= 75, (path, len(title), title)
        assert 70 <= len(desc) <= 175, (path, len(desc), desc)
        assert title not in titles, (path, titles.get(title))
        assert desc not in descs, (path, descs.get(desc))
        titles[title], descs[desc] = path, path


def test_every_page_has_one_h1_and_headings_do_not_skip_levels(pages):
    for path, (_html, p) in pages.items():
        assert len([1 for lvl, _t in p.headings if lvl == 1]) == 1, path
        prev = 0
        for lvl, text in p.headings:
            assert not (prev and lvl > prev + 1), (path, f"после h{prev} сразу h{lvl}: {text}")
            prev = lvl


def test_pages_declare_language_viewport_canonical_and_social_tags(pages):
    for path, (_html, p) in pages.items():
        assert p.html_attrs.get("lang") == "ru", path
        assert "width=device-width" in p.meta.get("viewport", ""), path
        canonical = next((l["href"] for l in p.links if "canonical" in l.get("rel", "").split()), "")
        assert canonical.startswith("http"), (path, canonical)
        for key in ("og:title", "og:description", "og:image", "og:url", "twitter:card"):
            assert p.meta.get(key), (path, key)
        assert p.meta["og:image"].startswith("http"), path


# ───────────── картинки, подписи полей, ссылки, разметка ─────────────

def test_images_have_alt_and_form_fields_have_labels(pages):
    for path, (_html, p) in pages.items():
        for img in p.images:
            assert "alt" in img, (path, img.get("src"))
        for field in p.inputs:
            a = field["attrs"]
            if a.get("type", "text").lower() in ("hidden", "submit", "button", "image", "reset"):
                continue
            named = (field["in_label"] or a.get("id", "") in p.label_for or a.get("aria-label")
                     or a.get("aria-labelledby") or a.get("title"))
            assert named, (path, a)
        for link in p.anchors:
            a = link["attrs"]
            if a.get("aria-hidden") == "true":              # ссылка-дубль «картинка рядом с названием»: скринридер её пропускает
                continue
            assert ("".join(link["text"]).strip() or link["has_img_alt"] or a.get("aria-label")
                    or a.get("title") or a.get("aria-labelledby")), (path, a.get("href"))


def test_json_ld_blocks_are_valid_json(pages):
    for path, (_html, p) in pages.items():
        for block in p.ld_blocks:
            data = json.loads(block)                       # не разобралось бы — тест упал
            assert data.get("@context") == "https://schema.org", path


# ───────────── страница «Как заказать» ─────────────

def test_help_page_has_steps_faq_and_matching_markup(pages):
    html, p = pages["/help"]
    assert "Как заказать цветы оптом" in html
    for title, _text, _link in help_page.STEPS:
        assert title in html
    faq_ld = next(json.loads(b) for b in p.ld_blocks if json.loads(b).get("@type") == "FAQPage")
    questions = [q["name"] for q in faq_ld["mainEntity"]]
    assert questions == [q for q, _a in help_page.FAQ]
    for question, answer in help_page.FAQ:                 # в разметке — тот же текст, что виден на странице
        assert question in html and answer.split(".")[0][:40] in html
    assert [a["acceptedAnswer"]["text"] for a in faq_ld["mainEntity"]] == [a for _q, a in help_page.FAQ]


def test_help_page_is_linked_and_listed_in_sitemap(web, shelf):
    assert 'href="/help"' in web.get("/").text                        # подвал и блок «Как начать»
    assert 'href="/help"' in web.get("/contacts").text
    sitemap = web.get("/sitemap.xml").text
    assert "/help</loc>" in sitemap


def test_help_text_stays_consistent_with_the_rest_of_the_site():
    """Обещания страницы не должны расходиться с «Условиями работы» и формами: проверяем ключевые слова."""
    text = (" ".join(t + " " + d for t, d, _l in help_page.STEPS) + " ".join(q + " " + a for q, a in help_page.FAQ)).lower()
    for phrase in ("юридические лица", "упаковками", "сроки заказа", "сообщить о проблеме", "инн"):
        assert phrase in text, phrase
    assert "физическим лицам" in text                                  # продажа только юрлицам и ИП


# ───────────── карточка товара: «О товаре» и ссылки на разделы ─────────────

def test_product_page_describes_the_product_and_links_to_its_sections(web, mem_db):
    item = seed_item(mem_db, name="Мондиал 60 Tessa", country="Эквадор", height=60, pack=25, stock=4)
    html = web.get(f"/product/{item.id}").text
    block = re.search(r'<section class="product-about.*?</section>', html, re.S).group(0)   # подвал содержит все разделы
    assert "О товаре" in block
    assert "роза из Эквадора, длина стебля 60 см" in block and "упаковками по 25 шт" in block
    assert 'href="/optom/rozy"' in block and 'href="/optom/ekvadorskie-rozy"' in block
    assert 'href="/optom/kenijskie-rozy"' not in block                 # чужая страна не подставляется


def test_pot_plants_are_described_by_height_and_single_items_are_sold_one_by_one(web, mem_db):
    item = seed_item(mem_db, name="Фикус 12/30", kind="Горшечное растение", country="Голландия", height=30, pack=1, stock=5)
    html = web.get(f"/product/{item.id}").text
    assert "высота 30 см" in html and "поштучно" in html and "длина стебля" not in html


def test_sections_for_product_general_first_then_specific():
    rose = SimpleNamespace(kind_name="Роза", country_name="Кения", name="Кустовая роза 60 SIAN")
    assert [s.slug for s in seo_pages.sections_for_product(rose)] == ["rozy", "kustovye-rozy", "kenijskie-rozy"]
    chrysanthemum = SimpleNamespace(kind_name="Хризантема", country_name="Голландия", name="Алтай")
    assert [s.slug for s in seo_pages.sections_for_product(chrysanthemum)] == ["hrizantemy"]
    assert seo_pages.sections_for_product(SimpleNamespace(kind_name="", country_name="", name="Рамка для фото")) == []


# ───────────── файлы с отпечатком, баннер без сдвига ─────────────

def test_asset_url_changes_with_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_asset_hashes", {})
    (tmp_path / "a.css").write_text("body{color:red}", encoding="utf-8")
    first = templating.asset_url("a.css")
    assert re.fullmatch(r"/static/a\.css\?v=[0-9a-f]{10}", first)
    assert templating.asset_url("a.css") == first                      # без изменений — тот же адрес
    (tmp_path / "a.css").write_text("body{color:blue;margin:0}", encoding="utf-8")
    assert templating.asset_url("a.css") != first                      # файл изменился — адрес новый
    assert templating.asset_url("нет-такого.css") == "/static/нет-такого.css"


def test_pwa_banner_is_an_overlay_and_does_not_shift_the_page():
    """Раньше плашка «поставить на экран» вставлялась в поток и сдвигала страницу на ~90 px (CLS 0,097)."""
    css = (ROOT / "app" / "static" / "style.css").read_text(encoding="utf-8")
    rule = re.search(r"\n\.pwa-banner\s*\{([^}]*)\}", css)
    assert rule and re.search(r"position:\s*fixed", rule.group(1))
    js = (ROOT / "app" / "static" / "pwa.js").read_text(encoding="utf-8")
    assert "cookie-notice" in js                                       # не показывается поверх вопроса про cookie


# ───────────── сам скрипт аудита ─────────────

def test_audit_parser_reads_the_page_like_a_browser_would():
    p = parse("""<!doctype html><html lang="ru"><head><title> Тест  страницы </title>
        <meta name="description" content="Описание"><link rel="canonical" href="https://x.ru/a">
        <script type="application/ld+json">{"@type": "Florist"}</script></head><body>
        <h1>Один</h1><h3>Пропуск</h3><img src="a.png"><img src="b.png" alt="Б" width="1" height="1">
        <label for="n">Имя</label><input id="n"><input name="bare"><a href="/x"><i></i></a>
        <button>Ок</button><script>var x = "<h2>не заголовок</h2>";</script></body></html>""")
    assert p.html_attrs["lang"] == "ru" and p.title.strip() == "Тест  страницы"
    assert p.meta["description"] == "Описание"
    assert [h[0] for h in p.headings] == [1, 3]
    assert [("alt" in i) for i in p.images] == [False, True]
    assert "n" in p.label_for and len(p.inputs) == 2
    assert p.anchors[0]["text"] == [] and p.buttons[0]["text"] == ["Ок"]
    assert json.loads(p.ld_blocks[0]) == {"@type": "Florist"}
