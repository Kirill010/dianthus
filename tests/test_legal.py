# tests/test_legal.py
"""Юридический блок: безопасная разметка, документы, согласия, выгрузка и
удаление данных, права доступа."""
import json
from datetime import date

from app.config import config
from app.models import (Claim, ConsentLog, DataRequest, LegalDocumentRevision,
                        Order, User)
from app.services import legal, markdown_lite, preorder_rules
from tests.helpers import csrf_from, login, make_order, make_user, seed_item


# ───────────────────────── безопасная разметка ─────────────────────────

def test_markdown_escapes_html_and_blocks_js_links():
    src = ("Привет <script>alert(1)</script> <img src=x onerror=alert(2)>\n\n"
           "[плохо](javascript:alert(3)) [тоже](data:text/html,x) "
           "[хорошо](https://a.ru/x) **жирный** *курсив*\n\n"
           "- один\n- два\n\n1. раз\n2. два\n\n---\n\n## Заголовок")
    out = str(markdown_lite.render(src))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<img" not in out
    assert "javascript:" not in out.lower() and "data:text" not in out.lower()
    assert 'href="https://a.ru/x"' in out and 'rel="noopener noreferrer"' in out
    assert "<strong>жирный</strong>" in out and "<em>курсив</em>" in out
    assert "<ul><li>один</li><li>два</li></ul>" in out
    assert "<ol><li>раз</li><li>два</li></ol>" in out
    assert "<hr>" in out and "<h2>Заголовок</h2>" in out


def test_markdown_link_cannot_break_out_of_attribute():
    from html.parser import HTMLParser

    class Attrs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.names = []

        def handle_starttag(self, tag, attrs):
            self.names += [name for name, _ in attrs]

    out = str(markdown_lite.render(
        '[a](https://x.ru/"onmouseover="alert(1)) [b](https://y.ru/ onclick=alert(2))'))
    parser = Attrs()
    parser.feed(out)
    assert not [n for n in parser.names if n.startswith("on")], out
    assert "href" in parser.names


def test_markdown_asterisk_in_url_does_not_break_link():
    out = str(markdown_lite.render("[a](https://x.ru/*q*) и *курсив*"))
    assert 'href="https://x.ru/*q*"' in out
    assert "<em>курсив</em>" in out


# ───────────────────────── публичные документы ─────────────────────────

def test_legal_pages_are_public_and_show_missing_requisites(web, monkeypatch):
    for attr in ("SHOP_INN", "SHOP_OGRN", "SHOP_PD_RESPONSIBLE"):
        monkeypatch.setattr(config, attr, "")
    for url in ("/privacy", "/consent", "/marketing-consent", "/terms"):
        assert web.get(url).status_code == 200, url
    assert "[укажите: ИНН]" in web.get("/privacy").text


def test_legal_placeholders_are_filled_from_settings(web, monkeypatch):
    monkeypatch.setattr(config, "SHOP_INN", "7700000001")
    monkeypatch.setattr(config, "SHOP_EMAIL", "pd@example.ru")
    text = web.get("/privacy").text
    assert "7700000001" in text and "pd@example.ru" in text
    assert "[укажите: ИНН]" not in text


def test_terms_show_lead_days_from_settings(web):
    text = web.get("/terms").text
    assert "Эквадор" in text and "за 10 дн." in text


def test_footer_links_to_legal_pages(web):
    page = web.get("/").text
    for url in ("/privacy", "/consent", "/terms", "/deadlines"):
        assert f'href="{url}"' in page


# ───────────────────────── регистрация и согласия ─────────────────────────

def _register(web, **over):
    token = csrf_from(web.get("/register").text)
    data = dict(csrf_token=token, email="new@test.ru", password="Passw0rd1",
                full_name="Пётр Петров", phone="+79001112233",
                company_name="ООО Тест", inn="7701234567", city="Москва",
                consent="1")
    data.update(over)
    for k in [k for k, v in data.items() if v is None]:
        data.pop(k)
    return web.post("/register", data=data, follow_redirects=False)


def test_register_requires_pd_consent(web, mem_db):
    r = _register(web, consent=None)
    assert r.status_code == 303 and r.headers["location"] == "/register"
    assert mem_db.query(User).count() == 0


def test_register_logs_pd_consent_with_version_and_ip(web, mem_db):
    r = _register(web)
    assert r.headers["location"] == "/registration_pending"
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.consent_at is not None and user.consent_version == "1"
    assert user.marketing_consent is False
    logs = mem_db.query(ConsentLog).filter_by(user_id=user.id).all()
    assert [(l.kind, l.granted, l.doc_version, l.source) for l in logs] == [
        ("pd", True, "1", "registration")]
    assert logs[0].ip


def test_register_marketing_consent_is_separate_and_optional(web, mem_db):
    _register(web, marketing_consent="1")
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.marketing_consent is True and user.marketing_consent_at is not None
    kinds = sorted(l.kind for l in mem_db.query(ConsentLog).filter_by(user_id=user.id))
    assert kinds == ["marketing", "pd"]


def test_consent_log_records_new_document_version(web, mem_db):
    doc = legal.get_document(mem_db, "consent")
    legal.update_document(mem_db, doc, doc.title, doc.body + "\nДополнение к тексту согласия.\n")
    _register(web)
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.consent_version == "2"


def test_register_form_has_two_separate_checkboxes(web):
    html = web.get("/register").text
    assert 'name="consent"' in html and 'name="marketing_consent"' in html
    consent_tag = html.split('name="consent"')[1].split(">")[0]
    marketing_tag = html.split('name="marketing_consent"')[1].split(">")[0]
    assert "required" in consent_tag and "checked" not in consent_tag
    assert "required" not in marketing_tag and "checked" not in marketing_tag


# ───────────────────────── профиль: согласия, выгрузка, удаление ─────────────────────────

def test_profile_marketing_toggle_is_logged(web, mem_db):
    user = make_user(mem_db)
    login(web, user.email, "Client123")
    token = csrf_from(web.get("/profile").text)
    web.post("/profile/consent/marketing", data={"csrf_token": token, "grant": "1"})
    mem_db.expire_all()
    assert mem_db.get(User, user.id).marketing_consent is True
    token = csrf_from(web.get("/profile").text)
    web.post("/profile/consent/marketing", data={"csrf_token": token, "grant": "0"})
    mem_db.expire_all()
    u = mem_db.get(User, user.id)
    assert u.marketing_consent is False and u.marketing_consent_at is None
    assert [(l.kind, l.granted) for l in mem_db.query(ConsentLog).order_by(ConsentLog.id)] == [
        ("marketing", True), ("marketing", False)]


def test_old_account_can_confirm_pd_consent_in_profile(web, mem_db):
    user = make_user(mem_db)                       # зарегистрирован «до журнала согласий»
    login(web, user.email, "Client123")
    page = web.get("/profile").text
    assert "Подтвердить согласие" in page
    # плашка-напоминание видна на других страницах, но не на самом профиле
    assert "Подтвердите согласие на обработку персональных данных" in web.get("/catalog").text
    token = csrf_from(page)
    web.post("/profile/consent/pd", data={"csrf_token": token})           # без галочки — отказ
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is None
    web.post("/profile/consent/pd", data={"csrf_token": token, "consent": "1"})
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is not None
    assert "Подтвердить согласие" not in web.get("/profile").text


def test_data_export_contains_only_own_data(web, mem_db):
    me = make_user(mem_db, email="me@test.ru")
    other = make_user(mem_db, email="other@test.ru", company_name="ООО Чужая")
    item = seed_item(mem_db, stock=20)
    make_order(mem_db, me, item, packs=2)
    make_order(mem_db, other, item, packs=1)
    login(web, "me@test.ru", "Client123")
    r = web.get("/profile/data.json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    data = json.loads(r.text)
    assert data["profile"]["email"] == "me@test.ru"
    assert len(data["orders"]) == 1
    line = data["orders"][0]["items"][0]
    assert (line["quantity"], line["package_size"], line["price"]) == (50, 25, 50.0)   # 2 упаковки по 25 шт
    assert line["sum"] == 2500.0 and data["orders"][0]["sum"] == 2500.0
    assert "other@test.ru" not in r.text and "ООО Чужая" not in r.text
    assert "hashed_password" not in r.text and "totp" not in r.text


def test_data_export_requires_login(web):
    r = web.get("/profile/data.json", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_delete_request_then_admin_anonymizes_keeping_orders(web, mem_db):
    me = make_user(mem_db, email="me@test.ru")
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    item = seed_item(mem_db, stock=20)
    order = make_order(mem_db, me, item, packs=2)
    legal.record_consent(mem_db, me, "pd", True, None, source="registration")
    legal.record_consent(mem_db, me, "marketing", True, None, source="profile")

    login(web, "me@test.ru", "Client123")
    token = csrf_from(web.get("/profile").text)
    web.post("/profile/data/delete-request", data={"csrf_token": token})
    web.post("/profile/data/delete-request", data={"csrf_token": token})   # повтор не плодит заявки
    assert mem_db.query(DataRequest).count() == 1
    assert "Запрос на удаление данных принят" in web.get("/profile").text

    web.cookies.clear()
    login(web, "boss@test.ru", "Admin1234")
    page = web.get("/admin/data-requests")
    assert page.status_code == 200 and "me@test.ru" in page.text
    token = csrf_from(page.text)
    req = mem_db.query(DataRequest).one()
    r = web.post(f"/admin/data-requests/{req.id}/done",
                 data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303

    mem_db.expire_all()
    user = mem_db.get(User, me.id)
    assert user.email.endswith("@deleted.invalid") and user.hashed_password == "!"
    assert user.full_name == "Удалённый клиент" and user.phone == "" and user.inn == ""
    assert user.is_approved is False and user.anonymized_at is not None
    assert user.consent_at is None and user.marketing_consent is False
    kept = mem_db.get(Order, order.id)
    assert kept is not None and kept.user_id == me.id and len(kept.items) == 1
    assert mem_db.query(DataRequest).one().status == "Выполнена"
    # журнал согласий сохранён и дополнен отзывами
    pairs = [(l.kind, l.granted) for l in mem_db.query(ConsentLog).order_by(ConsentLog.id)]
    assert ("pd", False) in pairs and ("marketing", False) in pairs and ("pd", True) in pairs

    web.cookies.clear()
    r = login(web, "me@test.ru", "Client123")
    assert "/catalog" not in r.headers["location"]


def test_data_requests_page_is_admin_only(web, mem_db):
    make_user(mem_db, email="m@test.ru", password="Manager123", is_manager=True)
    login(web, "m@test.ru", "Manager123")
    r = web.get("/admin/data-requests", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"
    r = web.get("/admin/legal", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog"


def test_reject_user_with_consent_logs_deletes_cleanly_under_foreign_keys(web, mem_db):
    """Регрессия: на PostgreSQL удаление падало из-за ссылок из журнала согласий."""
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    applicant = make_user(mem_db, email="app@test.ru", approved=False)
    applicant_id = applicant.id
    legal.record_consent(mem_db, applicant, "pd", True, None)
    mem_db.add(Claim(user_id=applicant_id, product_name="Роза", description="x"))
    mem_db.add(DataRequest(user_id=applicant_id, email_snapshot="app@test.ru", kind="export"))
    mem_db.commit()
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin").text)
    r = web.post(f"/admin/users/{applicant_id}/reject", data={"csrf_token": token},
                 follow_redirects=False)
    assert r.status_code == 303
    mem_db.expire_all()
    assert mem_db.get(User, applicant_id) is None
    assert mem_db.query(ConsentLog).count() == 0
    assert mem_db.query(DataRequest).one().user_id is None      # запись о запросе осталась


def test_reject_user_with_orders_anonymizes_instead_of_deleting(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    buyer = make_user(mem_db, email="buyer@test.ru")
    order = make_order(mem_db, buyer, seed_item(mem_db, stock=20), packs=1)
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin").text)
    web.post(f"/admin/users/{buyer.id}/reject", data={"csrf_token": token})
    mem_db.expire_all()
    user = mem_db.get(User, buyer.id)
    assert user is not None and user.anonymized_at is not None
    assert mem_db.get(Order, order.id).user_id == buyer.id


# ───────────────────────── правка документов ─────────────────────────

def test_admin_edit_creates_new_version_and_keeps_history(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    page = web.get("/admin/legal/terms/edit")
    assert page.status_code == 200
    token = csrf_from(page.text)
    doc = legal.get_document(mem_db, "terms")
    old_body = doc.body
    new_body = old_body + "\n## 13. Особые условия\n\nТекст добавлен владельцем.\n"
    r = web.post("/admin/legal/terms/edit",
                 data={"csrf_token": token, "title": doc.title, "body": new_body},
                 follow_redirects=False)
    assert r.status_code == 303
    mem_db.expire_all()
    doc = legal.get_document(mem_db, "terms")
    assert doc.version == 2
    rev = mem_db.query(LegalDocumentRevision).one()
    assert rev.version == 1 and rev.body == old_body
    assert "Текст добавлен владельцем." in web.get("/terms").text

    # повторное сохранение того же текста версию не меняет
    web.post("/admin/legal/terms/edit",
             data={"csrf_token": token, "title": doc.title, "body": doc.body})
    mem_db.expire_all()
    assert legal.get_document(mem_db, "terms").version == 2


def test_admin_edit_rejects_short_text_and_escapes_html(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin/legal/privacy/edit").text)
    web.post("/admin/legal/privacy/edit", data={"csrf_token": token, "title": "T", "body": "коротко"})
    mem_db.expire_all()
    assert legal.get_document(mem_db, "privacy").version == 1

    evil = "<script>alert(1)</script> " + "текст " * 30
    web.post("/admin/legal/privacy/edit", data={"csrf_token": token, "title": "Политика", "body": evil})
    page = web.get("/privacy").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_admin_legal_page_shows_readiness(web, mem_db, monkeypatch):
    monkeypatch.setattr(config, "SHOP_INN", "")
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    page = web.get("/admin/legal")
    assert page.status_code == 200
    assert "SHOP_INN" in page.text and "Роскомнадзор" in page.text
    token = csrf_from(page.text)
    web.post("/admin/legal/todo/rkn", data={"csrf_token": token})
    web.post("/admin/legal/reviewed/privacy", data={"csrf_token": token})
    state = legal.readiness(mem_db)
    assert [t["done"] for t in state["todo"] if t["key"] == "rkn"] == [True]
    assert [d["reviewed"] for d in state["docs"] if d["doc"].slug == "privacy"] == [True]


# ───────────────────────── мелкие функции ─────────────────────────

def test_response_deadline_counts_working_days():
    assert legal.add_working_days(date(2026, 10, 23), 10) == date(2026, 11, 6)   # пятница → пятница


# ───────────────────────── сроки предзаказа ─────────────────────────

def test_lead_days_parsing_and_synonyms():
    rules = preorder_rules.parse_lead_days("Эквадор=10, Голландия=10; Колумбия: 7, мусор, Россия=3 дн.")
    assert rules == {"Эквадор": 10, "Голландия": 10, "Колумбия": 7, "Россия": 3}
    assert preorder_rules.days_for(rules, "Нидерланды") == 10
    assert preorder_rules.days_for(rules, "РФ") == 3
    assert preorder_rules.days_for(rules, "Нарния") is None


def test_deadline_table_and_states():
    rules = preorder_rules.parse_lead_days("Эквадор=10, Китай=10, Колумбия=7, Россия=3")
    rows = preorder_rules.table_for_delivery(rules, date(2027, 3, 8), date(2026, 10, 25))
    assert [r["days"] for r in rows] == [10, 7, 3]
    assert rows[0]["last"] == date(2027, 2, 26) and rows[0]["state"] == "ok"
    late = preorder_rules.table_for_delivery(rules, date(2026, 10, 30), date(2026, 10, 25))
    assert [r["state"] for r in late] == ["passed", "passed", "ok"]
    edge = preorder_rules.table_for_delivery(rules, date(2026, 11, 4), date(2026, 10, 25))
    assert edge[0]["state"] == "today"            # 4 ноября − 10 дней = сегодня
    assert edge[1]["last"] == date(2026, 10, 28) and edge[1]["state"] == "ok"
    weekend = preorder_rules.table_for_delivery(rules, date(2026, 11, 1), date(2026, 10, 1))
    assert weekend[2]["last"] == date(2026, 10, 29)


def test_peak_dates_include_mothers_day_last_sunday_of_november():
    names = dict((n, d) for d, n in preorder_rules.peak_dates(2026))
    assert names["День матери"] == date(2026, 11, 29)
    assert date(2026, 3, 8) in [d for d, _ in preorder_rules.peak_dates(2026)]


def test_description_groups_countries_by_days():
    rules = preorder_rules.parse_lead_days("Эквадор=10, Китай=10, Колумбия=7")
    assert preorder_rules.describe(rules) == "Эквадор, Китай — за 10 дн.; Колумбия — за 7 дн."
