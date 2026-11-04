# tests/test_legal.py
"""Юридический блок: безопасная разметка, документы, согласия, выгрузка и
удаление данных, права доступа."""
import json
from datetime import date

import pytest

from app.config import config
from app.models import (Claim, ConsentLog, DataRequest, LegalDocumentRevision,
                        Order, SiteSetting, User)
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
                consent="1", privacy_ack="1", terms_accept="1")
    data.update(over)
    for k in [k for k, v in data.items() if v is None]:
        data.pop(k)
    return web.post("/register", data=data, follow_redirects=False)


@pytest.mark.parametrize("missing,message", [
    ("consent", "Нужно согласие на обработку персональных данных"),
    ("privacy_ack", "ознакомились с политикой"),
    ("terms_accept", "принять условия работы"),
])
def test_register_requires_each_of_the_three_checkboxes(web, mem_db, missing, message):
    """Каждый документ — своя отметка; без любой из трёх регистрация не проходит."""
    r = _register(web, **{missing: None})
    assert r.status_code == 303 and r.headers["location"] == "/register"
    assert mem_db.query(User).count() == 0
    page = web.get("/register").text
    assert message in page
    # то, что человек уже отметил, при ошибке не теряется
    other = [k for k in ("consent", "privacy_ack", "terms_accept") if k != missing]
    for name in other:
        assert 'name="%s"' % name in page and "checked" in page.split('name="%s"' % name)[1].split(">")[0]


def test_register_logs_every_consent_with_version_and_ip(web, mem_db):
    r = _register(web)
    assert r.headers["location"] == "/registration_pending"
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.consent_at is not None and user.consent_version == "1"
    assert user.marketing_consent is False
    logs = mem_db.query(ConsentLog).filter_by(user_id=user.id).all()
    assert sorted((l.kind, l.granted, l.doc_version, l.source) for l in logs) == [
        ("pd", True, "1", "registration"),
        ("privacy", True, "1", "registration"),
        ("terms", True, "1", "registration")]
    assert all(l.ip and l.created_at for l in logs)


def test_register_marketing_consent_is_separate_and_optional(web, mem_db):
    _register(web, marketing_consent="1")
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.marketing_consent is True and user.marketing_consent_at is not None
    kinds = sorted(l.kind for l in mem_db.query(ConsentLog).filter_by(user_id=user.id))
    assert kinds == ["marketing", "pd", "privacy", "terms"]


def test_consent_log_records_new_document_version(web, mem_db):
    doc = legal.get_document(mem_db, "consent")
    legal.update_document(mem_db, doc, doc.title, doc.body + "\nДополнение к тексту согласия.\n")
    _register(web)
    mem_db.expire_all()
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    assert user.consent_version == "2"


def test_register_form_has_four_separate_unticked_checkboxes(web):
    """Три обязательные отметки (согласие, политика, условия) и одна необязательная (реклама);
    ни одна не отмечена заранее, у каждой свой документ по ссылке."""
    html = web.get("/register").text
    tags = {name: html.split('name="%s"' % name)[1].split(">")[0]
            for name in ("consent", "privacy_ack", "terms_accept", "marketing_consent")}
    for name in ("consent", "privacy_ack", "terms_accept"):
        assert "required" in tags[name] and "checked" not in tags[name], name
    assert "required" not in tags["marketing_consent"] and "checked" not in tags["marketing_consent"]
    for url in ("/consent", "/privacy", "/terms", "/marketing-consent"):
        assert 'href="%s"' % url in html, url
    # согласие не склеено с политикой и условиями: в подписи первой отметки их нет
    label = html.split('id="reg-consent"')[1].split("</label>")[0]
    assert "/privacy" not in label and "/terms" not in label


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
    assert "Сохранить отметки" in page
    # плашка-напоминание видна на других страницах, но не на самом профиле
    assert "Подтвердите согласие на обработку персональных данных" in web.get("/catalog").text
    token = csrf_from(page)
    web.post("/profile/consent/pd", data={"csrf_token": token})           # без галочки — отказ
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is None
    web.post("/profile/consent/pd", data={"csrf_token": token, "consent": "1"})
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is not None
    assert "Сохранить отметки" in web.get("/profile").text      # политика и условия ещё не отмечены


def test_old_account_confirms_all_three_marks_one_by_one(web, mem_db):
    user = make_user(mem_db)
    login(web, user.email, "Client123")
    token = csrf_from(web.get("/profile").text)
    # одной отметки мало: нужны все недостающие, ничего не записывается
    web.post("/profile/consent/accept", data={"csrf_token": token, "consent": "1"})
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is None
    assert mem_db.query(ConsentLog).count() == 0
    web.post("/profile/consent/accept", data={"csrf_token": token, "consent": "1",
                                             "privacy_ack": "1", "terms_accept": "1"})
    mem_db.expire_all()
    assert mem_db.get(User, user.id).consent_at is not None
    assert sorted(l.kind for l in mem_db.query(ConsentLog)) == ["pd", "privacy", "terms"]
    page = web.get("/profile").text
    assert "Сохранить отметки" not in page and "ознакомлен" in page and "приняты" in page


def test_consent_state_marks_outdated_versions(web, mem_db):
    _register(web)
    user = mem_db.query(User).filter_by(email="new@test.ru").one()
    doc = legal.get_document(mem_db, "terms")
    legal.update_document(mem_db, doc, doc.title, doc.body + "\nНовый пункт.\n")
    state = legal.consent_state(mem_db, user)
    assert state["terms"]["given"] and state["terms"]["outdated"] and state["terms"]["current"] == "2"
    assert state["privacy"]["given"] and not state["privacy"]["outdated"]


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


def test_data_export_lists_every_consent_mark(web, mem_db):
    me = make_user(mem_db, email="me@test.ru")
    for kind in ("pd", "privacy", "terms", "marketing"):
        legal.record_consent(mem_db, me, kind, True, None, source="registration")
    login(web, "me@test.ru", "Client123")
    consents = json.loads(web.get("/profile/data.json").text)["consents"]
    assert consents["personal_data"]["given"] and consents["marketing"]["given"]
    assert consents["privacy_policy_read"]["given"] and consents["privacy_policy_read"]["document_version"] == "1"
    assert consents["terms_accepted"]["given"] and consents["terms_accepted"]["at"]
    assert sorted(h["kind"] for h in consents["history"]) == ["marketing", "pd", "privacy", "terms"]


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
    assert "ИНН организации" in page.text and "/admin/settings#requisites" in page.text
    assert "Роскомнадзор" in page.text
    token = csrf_from(page.text)
    web.post("/admin/legal/todo/rkn", data={"csrf_token": token})
    web.post("/admin/legal/reviewed/privacy", data={"csrf_token": token})
    state = legal.readiness(mem_db)
    assert [t["done"] for t in state["todo"] if t["key"] == "rkn"] == [True]
    assert [d["reviewed"] for d in state["docs"] if d["doc"].slug == "privacy"] == [True]


# ───────────────────────── тексты: полнота и соответствие поведению сайта ─────────────────────────

def test_default_legal_texts_use_only_known_placeholders():
    import re
    from app.services.legal_texts import DEFAULTS
    for slug, (_title, body) in DEFAULTS.items():
        used = set(re.findall(r"\{([А-ЯЁA-Z_]+)\}", body))
        assert used <= set(legal.PLACEHOLDERS), (slug, used - set(legal.PLACEHOLDERS))


def _fill_requisites(monkeypatch):
    for attr, value in (("SHOP_NAME", "ООО «Тест»"), ("SHOP_INN", "7700000001"),
                        ("SHOP_OGRN", "1027700000001"), ("SHOP_EMAIL", "pd@example.ru"),
                        ("SHOP_PHONE", "+7 (900) 000-00-00"), ("SHOP_LEGAL_ADDRESS", "г. Москва, ул. Тестовая, 1"),
                        ("SHOP_WORKING_HOURS", "Пн–Вс 09:00–21:00"),
                        ("SHOP_PD_RESPONSIBLE", "Иванов Иван Иванович, директор")):
        monkeypatch.setattr(config, attr, value)


def test_documents_have_no_holes_or_raw_braces_when_requisites_are_filled(web, monkeypatch):
    _fill_requisites(monkeypatch)
    for url in ("/privacy", "/consent", "/marketing-consent", "/terms"):
        body = web.get(url).text.split("<main")[1].split("</main>")[0]
        assert "[укажите" not in body, url
        assert "{" not in body and "}" not in body, url
        assert "7700000001" in body and "1027700000001" in body, url


def test_consent_is_a_separate_complete_document(web, monkeypatch):
    """Элементы согласия по статье 9 закона: субъект, оператор, цель, данные, действия, получатели,
    срок и способ отзыва, как фиксируется. Согласие не ссылается на условия и оферту как на своё продолжение."""
    _fill_requisites(monkeypatch)
    page = web.get("/consent").text
    for part in ("Субъект", "Оператор", "Цели обработки", "Какие данные", "Действия и способы обработки",
                 "Кому могут передаваться данные", "Срок действия и отзыв", "Как фиксируется согласие",
                 "Это отдельный документ"):
        assert part in page, part
    assert "pd@example.ru" in page and "Трансграничная передача не осуществляется" in page


def test_privacy_policy_describes_the_cookies_the_site_really_sets(web):
    policy = web.get("/privacy").text
    for name in ("dianthus_sid", "catalog_view", "pwa_banner_until"):
        assert name in policy, name
    web.get("/register")
    assert web.cookies.get("dianthus_sid")                      # cookie сессии действительно ставится
    assert "catalog_view" in web.get("/catalog?view=list").headers.get("set-cookie", "")


def test_policy_promises_match_what_the_site_does(web):
    """Что обещано в политике про срок хранения журналов, то и делает планировщик."""
    from app.services import audit, exchange_log
    policy = web.get("/privacy").text
    assert audit.AUDIT_KEEP_DAYS == 365 and "журнал действий — 12 месяцев" in policy
    assert exchange_log.LOG_KEEP_DAYS == 60 and "журнал обмена с учётной системой — 60 дней" in policy
    assert legal.RESPONSE_WORKING_DAYS == 10 and "в течение 10 рабочих дней" in policy


def test_audit_log_keeps_twelve_months(web, mem_db):
    from datetime import datetime, timedelta, timezone
    from app.models import AuditLog
    from app.services import audit
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mem_db.add_all([AuditLog(action="old", created_at=now - timedelta(days=400)),
                    AuditLog(action="fresh", created_at=now - timedelta(days=30))])
    mem_db.commit()
    assert audit.prune_job() == 1
    mem_db.expire_all()
    assert [r.action for r in mem_db.query(AuditLog)] == ["fresh"]


# ───────────────────────── реквизиты и список получателей данных вносятся в админке ─────────────────────────

def _save_settings(web, token, **over):
    data = {"csrf_token": token, "payment_terms": "Предоплата 100%", "preorder_lead_days": "Эквадор=10",
            "shop_inn": "", "shop_ogrn": "", "shop_legal_address": "", "shop_email": "",
            "shop_pd_responsible": "", "pd_processors": ""}
    data.update(over)
    return web.post("/admin/settings", data=data, follow_redirects=False)


def test_requisites_saved_in_admin_reach_documents_and_footer(web, mem_db, monkeypatch):
    for attr in ("SHOP_INN", "SHOP_OGRN", "SHOP_EMAIL", "SHOP_PD_RESPONSIBLE", "SHOP_LEGAL_ADDRESS"):
        monkeypatch.setattr(config, attr, "")
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    page = web.get("/admin/settings")
    assert page.status_code == 200 and "Пока не заполнено" in page.text
    token = csrf_from(page.text)
    r = _save_settings(web, token, shop_inn="7701 234 567", shop_ogrn="1027700132195",
                       shop_legal_address="г. Москва, Вешняковская ул., 39Г", shop_email="Info@Example.ru",
                       shop_pd_responsible="Иванов Иван Иванович, директор")
    assert r.status_code == 303
    web.cookies.clear()
    policy = web.get("/privacy").text
    assert "7701234567" in policy and "1027700132195" in policy and "info@example.ru" in policy
    assert "Иванов Иван Иванович" in policy and "[укажите: ИНН]" not in policy
    footer = web.get("/").text.split('class="app-footer"')[1]
    assert "ИНН 7701234567" in footer and "ОГРН 1027700132195" in footer
    # пустое поле возвращает значение из .env
    login(web, "boss@test.ru", "Admin1234")
    _save_settings(web, csrf_from(web.get("/admin/settings").text), shop_inn="")
    web.cookies.clear()
    assert "[укажите: ИНН]" in web.get("/privacy").text


def test_admin_settings_reject_wrong_requisites(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    token = csrf_from(web.get("/admin/settings").text)
    _save_settings(web, token, shop_inn="123", shop_ogrn="12345", shop_email="не-почта")
    text = web.get("/admin/settings").text
    for part in ("ИНН: 10 цифр", "ОГРН: 13 цифр", "Почта для обращений"):
        assert part in text, part
    assert legal.missing_requisites(mem_db)          # ничего из неверного не сохранилось
    mem_db.expire_all()
    assert not mem_db.get(SiteSetting, "shop_inn")


def test_processors_list_from_admin_is_used_in_policy_and_consent(web, mem_db):
    make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    login(web, "boss@test.ru", "Admin1234")
    assert "хостинг-провайдер" in web.get("/privacy").text          # стандартный список без названий
    token = csrf_from(web.get("/admin/settings").text)
    _save_settings(web, token, pd_processors="ООО «Хостинг», г. Москва\n- ООО «Почта», г. Москва")
    for url in ("/privacy", "/consent"):
        body = web.get(url).text
        assert "<li>ООО «Хостинг», г. Москва</li>" in body and "<li>ООО «Почта», г. Москва</li>" in body, url
        assert "хостинг-провайдер" not in body, url


def test_untouched_old_starter_texts_are_upgraded_but_edited_ones_are_kept(web, mem_db, monkeypatch):
    from app.services.legal_texts import DEFAULTS
    privacy = legal.get_document(mem_db, "privacy")
    old = "Старая стартовая редакция политики. " * 5
    privacy.body = old
    mem_db.commit()
    monkeypatch.setitem(legal.LEGACY_DEFAULTS, "privacy", {legal._digest(old)})

    boss = make_user(mem_db, email="boss@test.ru", password="Admin1234", admin=True)
    terms = legal.get_document(mem_db, "terms")
    mine = "Текст, который написал владелец. " * 10
    legal.update_document(mem_db, terms, terms.title, mine, admin=boss)
    monkeypatch.setitem(legal.LEGACY_DEFAULTS, "terms", {legal._digest(mine)})

    legal.ensure_default_legal_docs()
    mem_db.expire_all()
    privacy = legal.get_document(mem_db, "privacy")
    assert privacy.body.strip() == DEFAULTS["privacy"][1].strip() and privacy.version == 2
    assert mem_db.query(LegalDocumentRevision).filter_by(document_id=privacy.id).one().body == old
    terms = legal.get_document(mem_db, "terms")
    assert terms.body.startswith("Текст, который написал владелец.") and terms.version == 2

    legal.ensure_default_legal_docs()                    # повторный запуск ничего не меняет
    mem_db.expire_all()
    assert legal.get_document(mem_db, "privacy").version == 2
    assert legal.get_document(mem_db, "consent").version == 1


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
