# tests/test_smoke_pages.py
"""Обход ВСЕХ страниц сайта под каждой ролью (аноним, клиент, менеджер, владелец).

Страницы берутся из самого приложения (схема маршрутов), поэтому новая страница
попадает под проверку автоматически — ничего не нужно дописывать. Проверяем:
  • ни одна страница не отвечает ошибкой сервера (5xx) и не печатает трассировку;
  • закрытые страницы закрыты: аноним и клиент не открывают /admin, менеджер — то,
    что только для владельца;
  • ссылки и картинки/стили на страницах не ведут «в никуда».
"""
import re

from app.main import _routers, app
from app.models import Claim, LegalDocument
from tests.helpers import login, make_order, make_user, seed_item

EXPECTED_ROUTERS = {
    "admin", "cart", "catalog", "profile", "integration_1c", "integration_admin",
    "legal", "deadlines", "claims", "staff", "filters", "password_reset",
}

# Адреса, которые открыты без входа
PUBLIC = {
    "/", "/login", "/register", "/registration_pending", "/catalog", "/contacts",
    "/deadlines", "/terms", "/privacy", "/consent", "/marketing-consent", "/forgot",
    "/1c-docs", "/1c-docs.md", "/1c-docs.json", "/sitemap.xml", "/health", "/sw.js",
    "/api/search-suggest", "/product/{item_id}", "/reset/{token}",
}
# Только для владельца (менеджеру — отказ)
OWNER_ONLY_PREFIXES = (
    "/admin/integration", "/admin/legal", "/admin/products", "/admin/supplies",
    "/admin/customers/export", "/admin/data-requests", "/admin/filters",
)
# Страницы «моё»: чужому человеку (даже владельцу) они отвечают 404 — так и задумано,
# владелец смотрит те же заказы и жалобы в админке
CLIENT_OWN = {"/claims/{claim_id}", "/orders/{order_id}/invoice"}
# Не страницы: у обмена с 1С своя проверка ключа (см. test_1c_*), выход — действие
SKIP = ("/api/1c/", "/logout", "/docs", "/redoc", "/openapi.json")


def get_paths():
    paths = app.openapi()["paths"]
    found = {p for p, methods in paths.items() if "get" in methods}
    found.add("/sw.js")                                  # скрыт из схемы, но это страница
    return sorted(p for p in found if not p.startswith(SKIP))


def seed(db):
    """Данные, на которых страницы показывают всё: товар, заказ, жалоба, предзаказ."""
    client = make_user(db, "client@test.ru", "Client123", discount_percent=5)
    make_user(db, "manager@test.ru", "Manager123", is_manager=True,
              full_name="Мария Менеджер", company_name="Диантус")
    make_user(db, "boss@test.ru", "Boss12345", admin=True,
              full_name="Олег Владелец", company_name="Диантус")
    item = seed_item(db, name="Роза Фридом", stock=40, price=55.0)
    transit = seed_item(db, name="Роза В Пути", stock=10, active=False, status="В пути")
    order = make_order(db, client, item, packs=2)
    claim = Claim(user_id=client.id, order_id=order.id, product_name="Роза Фридом",
                  description="Часть бутонов вялая", affected_qty=5, total_qty=50)
    db.add(claim)
    db.commit()
    slug = db.query(LegalDocument).first().slug
    return {
        "item_id": item.id, "product_id": item.product_id, "supply_id": item.supply_id,
        "order_id": order.id, "claim_id": claim.id, "user_id": client.id,
        "slug": slug, "token": "no-such-token", "transit_id": transit.id,
    }


def fill(path, ids):
    return re.sub(r"{(\w+)}", lambda m: str(ids[m.group(1)]), path)


def crawl(web, ids):
    """{шаблон адреса: (статус, тело)} для текущего пользователя web."""
    out = {}
    for tpl in get_paths():
        r = web.get(fill(tpl, ids), follow_redirects=False)
        out[tpl] = (r.status_code, r.text if "text/html" in r.headers.get("content-type", "") else "")
    return out


def sign_in(web, email, password):
    web.cookies.clear()
    r = login(web, email, password)
    assert r.status_code == 303, (email, r.text[:200])


def broken_html(pages):
    bad = []
    for tpl, (status, body) in pages.items():
        if status == 200 and body:
            low = body.lower()
            if "</html>" not in low:
                bad.append(f"{tpl}: страница оборвана")
            if "traceback" in low or "jinja2.exceptions" in low or "undefinederror" in low:
                bad.append(f"{tpl}: в страницу попала ошибка шаблона")
    return bad


def test_every_router_is_loaded():
    """Если в роутере опечатка, сайт молча стартует без целого раздела (main.py
    только пишет ошибку в журнал) — тут это становится громкой ошибкой."""
    assert set(_routers) == EXPECTED_ROUTERS


def test_pages_per_role_no_server_errors_and_access_is_closed(web, mem_db):
    ids = seed(mem_db)
    problems = []

    # ── аноним ──
    web.cookies.clear()
    anon = crawl(web, ids)
    for tpl, (status, _) in anon.items():
        if status >= 500:
            problems.append(f"аноним {tpl}: {status}")
        if tpl not in PUBLIC and status == 200:
            problems.append(f"аноним открыл закрытую страницу {tpl}")
        if tpl in PUBLIC and status >= 400 and tpl not in ("/reset/{token}", "/health"):
            problems.append(f"публичная страница {tpl} не открывается: {status}")
    problems += [f"аноним {m}" for m in broken_html(anon)]

    # ── клиент ──
    sign_in(web, "client@test.ru", "Client123")
    client = crawl(web, ids)
    for tpl, (status, _) in client.items():
        if status >= 500:
            problems.append(f"клиент {tpl}: {status}")
        if tpl.startswith("/admin") and status == 200:
            problems.append(f"клиент открыл админку {tpl}")
    for tpl in ("/catalog", "/cart", "/orders", "/profile", "/preorders", "/notifications",
                "/claims", "/claims/new", "/checkout", "/orders/{order_id}/invoice",
                "/claims/{claim_id}", "/product/{item_id}"):
        if client[tpl][0] not in (200, 303):                  # /checkout без корзины ведёт в корзину
            problems.append(f"клиент не открывает свою страницу {tpl}: {client[tpl][0]}")
    problems += [f"клиент {m}" for m in broken_html(client)]

    # ── менеджер ──
    sign_in(web, "manager@test.ru", "Manager123")
    manager = crawl(web, ids)
    for tpl, (status, _) in manager.items():
        if status >= 500:
            problems.append(f"менеджер {tpl}: {status}")
        if tpl.startswith(OWNER_ONLY_PREFIXES) and status == 200:
            problems.append(f"менеджер открыл страницу владельца {tpl}")
    for tpl in ("/admin", "/admin/orders/{order_id}", "/admin/claims", "/admin/clients/{user_id}"):
        if manager[tpl][0] != 200:
            problems.append(f"менеджер не открывает рабочую страницу {tpl}: {manager[tpl][0]}")
    problems += [f"менеджер {m}" for m in broken_html(manager)]

    # ── владелец ──
    sign_in(web, "boss@test.ru", "Boss12345")
    owner = crawl(web, ids)
    for tpl, (status, _) in owner.items():
        if status >= 500:
            problems.append(f"владелец {tpl}: {status}")
        elif status == 404 and tpl not in CLIENT_OWN:
            problems.append(f"владелец: страница {tpl} не найдена (404)")
    problems += [f"владелец {m}" for m in broken_html(owner)]

    assert not problems, "\n" + "\n".join(problems)


def test_links_and_assets_on_pages_are_not_broken(web, mem_db):
    """Берём все ссылки и файлы (стили, скрипты, картинки), которые реально стоят на
    страницах анонима, клиента и владельца, и открываем каждую ТЕМ ЖЕ пользователем.
    404 и 5xx — это баг (битая ссылка, пропавший файл)."""
    ids = seed(mem_db)
    skip_links = ("/logout", "/api/", "/static/uploads/", "//")
    link_re = re.compile(r'(?:href|src)="(/[^"#]*)')
    problems, total = [], 0
    for email, pwd in ((None, None), ("client@test.ru", "Client123"), ("boss@test.ru", "Boss12345")):
        if email:
            sign_in(web, email, pwd)
        else:
            web.cookies.clear()                      # аноним: витрина и страницы входа
        who = email or "аноним"
        pages = {tpl: body for tpl, (status, body) in crawl(web, ids).items()
                 if status == 200 and body}
        # страницы, которых нет в схеме без параметров: прайс-режим и товар «в пути»
        pages["/catalog?view=list"] = web.get("/catalog?view=list").text
        pages["/product/{transit}"] = web.get(f"/product/{ids['transit_id']}").text
        seen = set()
        for src, body in pages.items():
            for link in link_re.findall(body):
                link = link.replace("&amp;", "&")
                if link in seen or link.startswith(skip_links):
                    continue
                seen.add(link)
                r = web.get(link, follow_redirects=False)
                if r.status_code >= 400:
                    problems.append(f"{who}: {link} (со страницы {src}): {r.status_code}")
        total += len(seen)
    assert total > 60, "обход почти ничего не нашёл — что-то не так с самим тестом"
    assert not problems, "\n" + "\n".join(problems)
