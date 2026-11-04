# app/services/legal.py
"""Юридические документы сайта, согласия клиентов, выгрузка и удаление данных.

Что здесь есть:
  * тексты политики/согласий/условий хранятся в БД, правятся в админке, каждая
    правка = новая версия, прежняя сохраняется (по версии согласия всегда можно
    восстановить, что именно видел клиент);
  * журнал согласий (consent_log) — неизменяемый: «дал» и «отозвал» — отдельные
    строки с датой, версией текста и IP;
  * выгрузка данных клиента (право на доступ) и обезличивание (право на удаление)
    с учётом того, что учётные документы по закону нельзя уничтожать.
"""
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

from markupsafe import Markup
from sqlalchemy.orm import Session

from ..config import config
from ..models import (ApiKey, AuditLog, Claim, ConsentLog, DataRequest,
                      LegalDocument, LegalDocumentRevision, Notification,
                      Order, PasswordReset, Preorder, SiteSetting, User)
from ..security import _client_ip
from ..timeutils import fmt_local, utc_iso
from . import markdown_lite, preorder_rules
from .legal_texts import DEFAULTS
from .site_settings import DEFAULT_PROCESSORS, flag_is_set, get_setting
from .upload_service import delete_upload

logger = logging.getLogger(__name__)

# порядок показа в подвале и в админке: (slug, адрес публичной страницы)
DOC_ORDER = [
    ("privacy", "/privacy"),
    ("consent", "/consent"),
    ("marketing", "/marketing-consent"),
    ("terms", "/terms"),
]
DOC_URL = dict(DOC_ORDER)
ANON_EMAIL_DOMAIN = "deleted.invalid"
RESPONSE_WORKING_DAYS = 10          # срок ответа на обращение субъекта данных


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════
# ДОКУМЕНТЫ
# ═══════════════════════════════════════════════════════════

def _digest(text: str) -> str:
    return hashlib.sha256((text or "").replace("\r\n", "\n").strip().encode("utf-8")).hexdigest()


# Отпечатки прежних СТАРТОВЫХ текстов. Документ в базе, который до сих пор слово в слово совпадает
# с одним из них, владелец не правил — его безопасно заменить новым стартовым текстом (прежняя
# редакция остаётся в истории, версия растёт). Текст, правленный владельцем или юристом,
# не трогается никогда. Меняете стартовый текст — добавьте отпечаток прежнего сюда.
LEGACY_DEFAULTS: dict[str, set[str]] = {
    "privacy": {"d1a375f04a075958c39420b132d8f168a8d093edda11dff8119b426110dbd476"},
    "consent": {"01cf1153ad9673b505a16612d20a71db77437b7f66249e09c099cd3a8c90f7c4"},
    "marketing": {"b6f69f4cb2494222de61bac8ce380741304d36e9564608b224476dbb739d2332"},
    "terms": {"f3012db0dea3227629d1b0cc5abf1e1f06962490f826ceae8b3066b7be9db36c"},
}


def ensure_default_legal_docs() -> None:
    """Создаёт недостающие документы со стартовым текстом и обновляет нетронутые старые
    стартовые тексты до новой редакции (шаг prepare_database)."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        existing = {d.slug: d for d in db.query(LegalDocument).all()}
        created = upgraded = 0
        for slug, (title, body) in DEFAULTS.items():
            doc = existing.get(slug)
            if doc is None:
                db.add(LegalDocument(slug=slug, title=title, body=body, version=1))
                created += 1
            elif (doc.updated_by_id is None and _digest(doc.body) in LEGACY_DEFAULTS.get(slug, set())):
                if update_document(db, doc, title, body):
                    upgraded += 1
        if created:
            db.commit()
            logger.info("📄 Созданы юридические документы: %d", created)
        if upgraded:
            logger.info("📄 Обновлены нетронутые стартовые тексты до новой редакции: %d", upgraded)
    except Exception as e:                                  # noqa: BLE001
        db.rollback()
        logger.exception("Не удалось создать юридические документы: %s", e)
    finally:
        db.close()


def get_document(db: Session, slug: str) -> LegalDocument | None:
    return db.query(LegalDocument).filter(LegalDocument.slug == slug).first()


def current_version(db: Session, slug: str) -> str:
    doc = get_document(db, slug)
    return str(doc.version) if doc else "0"


def legal_address(db: Session | None = None) -> str:
    """Юридический адрес: из админки (если внесён), иначе из .env, иначе адрес магазина."""
    saved = get_setting(db, "shop_legal_address") if db is not None else config.SHOP_LEGAL_ADDRESS
    return saved or config.SHOP_ADDRESS


def processors_markdown(db: Session) -> str:
    """Список получателей данных для политики и согласия: свой (из админки) или стандартный.
    Каждая строка становится пунктом списка."""
    custom = get_setting(db, "pd_processors")
    if not custom.strip():
        return DEFAULT_PROCESSORS
    lines = [ln.strip() for ln in custom.replace("\r\n", "\n").split("\n") if ln.strip()]
    return "\n".join(ln if ln.startswith(("- ", "* ")) else f"- {ln}" for ln in lines)


def _placeholder_sources(db: Session, doc: LegalDocument | None) -> dict[str, tuple[str, str]]:
    """подстановка → (значение из настроек, подсказка)."""
    rules = preorder_rules.lead_days(db)
    return {
        "ОПЕРАТОР": (config.SHOP_NAME, "SHOP_NAME — название организации"),
        "ИНН": (get_setting(db, "shop_inn"), "ИНН (Админка → Настройки магазина)"),
        "ОГРН": (get_setting(db, "shop_ogrn"), "ОГРН или ОГРНИП (Админка → Настройки магазина)"),
        "АДРЕС": (legal_address(db), "юридический адрес (Админка → Настройки магазина)"),
        "ПОЧТА": (get_setting(db, "shop_email"), "почта для обращений (Админка → Настройки магазина)"),
        "ТЕЛЕФОН": (config.SHOP_PHONE, "SHOP_PHONE — телефон"),
        "ОТВЕТСТВЕННЫЙ": (get_setting(db, "shop_pd_responsible"),
                          "ответственный за персональные данные (Админка → Настройки магазина)"),
        "САЙТ": (config.APP_URL, "APP_URL — адрес сайта"),
        "ЧАСЫ_РАБОТЫ": (config.SHOP_WORKING_HOURS, "SHOP_WORKING_HOURS — часы работы"),
        "УСЛОВИЯ_ОПЛАТЫ": (get_setting(db, "payment_terms"), "условия оплаты"),
        "СРОКИ_ПРЕДЗАКАЗА": (preorder_rules.describe(rules), "сроки предзаказа"),
        "ОБРАБОТЧИКИ": (processors_markdown(db), "кто обрабатывает данные по нашему поручению"),
        "ДАТА_РЕДАКЦИИ": (fmt_local(doc.updated_at, "%d.%m.%Y") if doc and doc.updated_at else "",
                          "дата редакции"),
        "ВЕРСИЯ": (str(doc.version) if doc else "", "версия"),
    }


PLACEHOLDERS = [
    "ОПЕРАТОР", "ИНН", "ОГРН", "АДРЕС", "ПОЧТА", "ТЕЛЕФОН", "ОТВЕТСТВЕННЫЙ",
    "САЙТ", "ЧАСЫ_РАБОТЫ", "УСЛОВИЯ_ОПЛАТЫ", "СРОКИ_ПРЕДЗАКАЗА", "ОБРАБОТЧИКИ",
    "ДАТА_РЕДАКЦИИ", "ВЕРСИЯ",
]


def fill_placeholders(db: Session, text: str, doc: LegalDocument | None = None) -> str:
    """{ИНН} → значение. Не заполнено — заметный маркер, чтобы не пропустить."""
    out = text or ""
    for key, (value, _hint) in _placeholder_sources(db, doc).items():
        replacement = (value or "").strip() or f"[укажите: {key}]"
        out = out.replace("{" + key + "}", replacement)
    return out


def render_document(db: Session, doc: LegalDocument) -> Markup:
    return markdown_lite.render(fill_placeholders(db, doc.body, doc))


def unfilled_placeholders(db: Session, doc: LegalDocument) -> list[str]:
    """Какие подстановки в тексте остались пустыми (для списка «что заполнить»)."""
    sources = _placeholder_sources(db, doc)
    return [k for k, (v, _h) in sources.items()
            if "{" + k + "}" in doc.body and not (v or "").strip()]


def update_document(db: Session, doc: LegalDocument, title: str, body: str,
                    admin=None) -> bool:
    """Сохраняет правку. Возвращает False, если ничего не изменилось."""
    title, body = title.strip()[:200], body.replace("\r\n", "\n").strip() + "\n"
    if title == doc.title and body.strip() == (doc.body or "").strip():
        return False
    db.add(LegalDocumentRevision(
        document_id=doc.id, version=doc.version, title=doc.title, body=doc.body,
        saved_at=doc.updated_at or _now(), saved_by_id=doc.updated_by_id,
    ))
    doc.title, doc.body = title, body
    doc.version = (doc.version or 1) + 1
    doc.updated_at = _now()
    doc.updated_by_id = getattr(admin, "id", None)
    db.commit()
    return True


# ═══════════════════════════════════════════════════════════
# ГОТОВНОСТЬ: ЧТО ЕЩЁ НЕ ЗАПОЛНЕНО / НЕ СДЕЛАНО
# ═══════════════════════════════════════════════════════════

# (ключ в настройках, что сделать, подробность)
OFFLINE_TODO = [
    ("lawyer", "Показать тексты юристу",
     "Разовая консультация: юрист проверит политику, согласия и условия под вашу "
     "ситуацию и подскажет, нужно ли что-то менять."),
    ("rkn", "Подать уведомление об обработке персональных данных в Роскомнадзор",
     "Бесплатно, через сайт pd.rkn.gov.ru, обычно 20–30 минут (нужна учётная запись "
     "Госуслуг руководителя). Юрист подтвердит, что вам оно нужно (чаще всего — да)."),
    ("order_pd", "Приказ о назначении ответственного за обработку персональных данных",
     "Образец и положение — в папке docs/legal/. ФИО ответственного впишите в «Настройки магазина»."),
    ("processors", "Дополнить список тех, кто обрабатывает данные по вашему поручению",
     "В политике и в согласии закон требует назвать получателей данных: хостинг, почту, облако 1С, "
     "перевозчиков. Впишите их названия и адреса из договоров в «Настройки магазина» "
     "(поле «Кто обрабатывает данные…») — пока там стандартный список без названий."),
    ("servers_ru", "Убедиться, что сервер сайта и облако 1С находятся в России",
     "Закон требует хранить базы данных граждан РФ на территории РФ. Reg.ru, Timeweb, "
     "Selectel и 1С:Фреш — российские; уточните у провайдера облака 1С."),
    ("mailbox", "Завести рабочую почту на домене сайта",
     "Сайт отправляет клиентам письма и принимает обращения по данным. Нужен ящик вида "
     "info@dianthus-opt.ru (не личный): он указывается в политике и как отправитель писем."),
    ("esia", "Подтвердить владельца домена .ru через Госуслуги (ЕСИА)",
     "С 1 сентября 2026 года администраторы доменов .ru обязаны пройти идентификацию "
     "у регистратора. Зайдите в личный кабинет регистратора домена и следуйте подсказкам."),
    ("incident", "Прочитать план действий при утечке данных",
     "docs/legal/incident_plan.md — кому и за сколько часов сообщать (24 ч / 72 ч)."),
]


def missing_requisites(db: Session) -> list[tuple[str, str]]:
    """[(ключ, пояснение)] для пустых реквизитов оператора (внесённых в админке или в .env)."""
    checks = [
        ("shop_name", config.SHOP_NAME, "полное название организации (как в ЕГРЮЛ)"),
        ("shop_inn", get_setting(db, "shop_inn"), "ИНН организации"),
        ("shop_ogrn", get_setting(db, "shop_ogrn"), "ОГРН (для ИП — ОГРНИП)"),
        ("shop_legal_address", legal_address(db), "юридический адрес"),
        ("shop_email", get_setting(db, "shop_email"), "почта для обращений клиентов"),
        ("shop_phone", config.SHOP_PHONE, "телефон магазина"),
        ("shop_pd_responsible", get_setting(db, "shop_pd_responsible"),
         "ФИО ответственного за персональные данные"),
    ]
    return [(k, hint) for k, ok, hint in checks if not ok]


def readiness(db: Session) -> dict:
    todo = [{
        "key": key, "title": title, "text": text,
        "done": flag_is_set(db, f"legal.todo.{key}"),
    } for key, title, text in OFFLINE_TODO]
    docs = []
    for slug, url in DOC_ORDER:
        doc = get_document(db, slug)
        if doc is None:
            continue
        docs.append({
            "doc": doc, "url": url,
            "unfilled": unfilled_placeholders(db, doc),
            "reviewed": flag_is_set(db, f"legal.reviewed.{slug}"),
        })
    return {
        "missing": missing_requisites(db),
        "todo": todo, "docs": docs,
        "todo_left": sum(1 for t in todo if not t["done"]),
    }


# ═══════════════════════════════════════════════════════════
# СОГЛАСИЯ
# ═══════════════════════════════════════════════════════════

def _ua(request) -> str:
    try:
        return (request.headers.get("user-agent") or "")[:255]
    except Exception:                                       # noqa: BLE001
        return ""


def _ip(request) -> str:
    try:
        return _client_ip(request)[:45]
    except Exception:                                       # noqa: BLE001
        return ""


# вид отметки → документ, на который клиент соглашается. Каждая отметка — отдельная строка журнала.
#   pd        — согласие на обработку персональных данных (документ «consent»)
#   privacy   — «ознакомился с политикой обработки персональных данных»
#   terms     — «принимаю условия работы и оферту»
#   marketing — необязательное согласие на рекламу
CONSENT_DOC = {"pd": "consent", "privacy": "privacy", "terms": "terms", "marketing": "marketing"}


def record_consent(db: Session, user: User, kind: str, granted: bool,
                   request=None, source: str = "registration",
                   commit: bool = True) -> None:
    """Пишет в журнал (дата, версия текста, IP, браузер) и обновляет «текущее состояние»
    согласия у клиента."""
    if kind not in CONSENT_DOC:
        raise ValueError(kind)
    version = current_version(db, CONSENT_DOC[kind])
    now = _now()
    ip = _ip(request) if request is not None else ""
    db.add(ConsentLog(
        user_id=user.id, kind=kind, granted=granted, doc_version=version,
        source=source, ip=ip, user_agent=_ua(request) if request is not None else "",
        created_at=now,
    ))
    if kind == "pd":
        user.consent_at = now if granted else None
        user.consent_version = version if granted else ""
        user.consent_ip = ip if granted else ""
    elif kind == "marketing":
        user.marketing_consent = bool(granted)
        user.marketing_consent_at = now if granted else None
    # privacy и terms — только запись в журнале: «текущее состояние» читается из него же
    if commit:
        db.commit()


def consent_state(db: Session, user: User) -> dict[str, dict]:
    """Что клиент отметил и когда — для страницы «Мои данные и согласия».
    {вид: {given, at, version, current, outdated}}; outdated — текст с тех пор менялся."""
    last: dict[str, ConsentLog] = {}
    for row in (db.query(ConsentLog).filter(ConsentLog.user_id == user.id)
                .order_by(ConsentLog.created_at.desc(), ConsentLog.id.desc())):
        last.setdefault(row.kind, row)
    out = {}
    for kind, slug in CONSENT_DOC.items():
        row = last.get(kind)
        current = current_version(db, slug)
        given = bool(row and row.granted)
        out[kind] = {
            "given": given,
            "at": row.created_at if given else None,
            "version": row.doc_version if given else "",
            "current": current,
            "outdated": bool(given and row.doc_version != current),
        }
    return out


# ═══════════════════════════════════════════════════════════
# ВЫГРУЗКА И УДАЛЕНИЕ ДАННЫХ КЛИЕНТА
# ═══════════════════════════════════════════════════════════

def export_user_data(db: Session, user: User) -> dict:
    """Всё, что сайт хранит о клиенте, — для скачивания (право на доступ)."""
    orders = db.query(Order).filter(Order.user_id == user.id).order_by(Order.id).all()
    marks = consent_state(db, user)
    return {
        "exported_at": utc_iso(_now()),
        "profile": {
            "email": user.email, "full_name": user.full_name,
            "phone": user.phone, "company_name": user.company_name,
            "inn": user.inn, "city": user.city, "address": user.address or "",
            "discount_percent": user.discount_percent,
            "registered_at": utc_iso(user.created_at),
            "last_login_at": utc_iso(user.last_login_at),
        },
        "consents": {
            "personal_data": {
                "given": bool(user.consent_at), "at": utc_iso(user.consent_at),
                "document_version": user.consent_version or "",
            },
            "marketing": {
                "given": bool(user.marketing_consent),
                "at": utc_iso(user.marketing_consent_at),
            },
            "privacy_policy_read": {
                "given": marks["privacy"]["given"], "at": utc_iso(marks["privacy"]["at"]),
                "document_version": marks["privacy"]["version"],
            },
            "terms_accepted": {
                "given": marks["terms"]["given"], "at": utc_iso(marks["terms"]["at"]),
                "document_version": marks["terms"]["version"],
            },
            "history": [{
                "kind": c.kind, "granted": c.granted, "document_version": c.doc_version,
                "source": c.source, "at": utc_iso(c.created_at),
            } for c in db.query(ConsentLog).filter(ConsentLog.user_id == user.id)
                .order_by(ConsentLog.id).all()],
        },
        "orders": [{
            "number": o.id, "created_at": utc_iso(o.created_at), "status": o.status,
            "sum": o.subtotal, "total": o.total_price, "comment": o.comment or "",
            "desired_date": o.desired_date.date().isoformat() if o.desired_date else "",
            "items": [{
                "name": i.product_name, "package_size": i.package_size, "price": i.price,
                "quantity": i.total_stems, "sum": i.subtotal,
            } for i in o.items],
        } for o in orders],
        "claims": [{
            "number": c.number, "status": c.status, "product": c.product_name,
            "description": c.description, "created_at": utc_iso(c.created_at),
            "reply": c.reply_to_client or "",
        } for c in db.query(Claim).filter(Claim.user_id == user.id).order_by(Claim.id).all()],
        "data_requests": [{
            "kind": r.kind, "status": r.status, "created_at": utc_iso(r.created_at),
        } for r in db.query(DataRequest).filter(DataRequest.user_id == user.id).all()],
    }


def _drop_claim_files(claim: Claim) -> None:
    """Фото рекламации лежат на диске в static/uploads — удаляем и файлы."""
    for photo in claim.photos:
        delete_upload(photo.url)


def purge_user_refs(db: Session, user_id: int) -> None:
    """Убирает из других таблиц ссылки на пользователя перед его УДАЛЕНИЕМ.

    На PostgreSQL внешние ключи строгие: без этого db.delete(user) падает, если
    у клиента есть журнал согласий, рекламации или записи в журнале действий.
    """
    db.query(ConsentLog).filter(ConsentLog.user_id == user_id).delete(synchronize_session=False)
    db.query(PasswordReset).filter(PasswordReset.user_id == user_id).delete(synchronize_session=False)
    db.query(DataRequest).filter(DataRequest.user_id == user_id).update(
        {DataRequest.user_id: None}, synchronize_session=False)
    db.query(DataRequest).filter(DataRequest.handled_by_id == user_id).update(
        {DataRequest.handled_by_id: None}, synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.user_id == user_id).update(
        {AuditLog.user_id: None}, synchronize_session=False)
    for claim in db.query(Claim).filter(Claim.user_id == user_id).all():
        _drop_claim_files(claim)
        db.delete(claim)                                    # записи о фото уходят каскадом
    db.query(Preorder).filter(Preorder.user_id == user_id).delete(synchronize_session=False)
    # документы/настройки/ключи, которые правил этот человек
    db.query(LegalDocument).filter(LegalDocument.updated_by_id == user_id).update(
        {LegalDocument.updated_by_id: None}, synchronize_session=False)
    db.query(LegalDocumentRevision).filter(
        LegalDocumentRevision.saved_by_id == user_id).update(
        {LegalDocumentRevision.saved_by_id: None}, synchronize_session=False)
    db.query(SiteSetting).filter(SiteSetting.updated_by_id == user_id).update(
        {SiteSetting.updated_by_id: None}, synchronize_session=False)
    db.query(ApiKey).filter(ApiKey.created_by_id == user_id).update(
        {ApiKey.created_by_id: None}, synchronize_session=False)


def anonymize_user(db: Session, user: User) -> None:
    """Обезличивает клиента: заказы остаются (учёт), личные данные — нет.

    Журнал согласий (ConsentLog) не трогаем: это доказательство того, что
    согласие было, и оно нужно в течение срока исковой давности.
    """
    uid = user.id
    user.email = f"deleted-{uid}@{ANON_EMAIL_DOMAIN}"
    user.hashed_password = "!"                              # войти нельзя
    user.full_name = "Удалённый клиент"
    user.phone = ""
    user.company_name = f"Клиент №{uid} (данные удалены)"
    user.inn = ""
    user.city = ""
    user.address = ""
    user.external_id = ""                                   # связь с контрагентом 1С тоже разрываем
    user.totp_secret = ""
    user.staff_note = ""
    user.is_approved = False
    user.is_admin = False
    user.is_manager = False
    user.discount_percent = 0
    user.marketing_consent = False
    user.marketing_consent_at = None
    user.consent_at = None
    user.consent_version = ""
    user.consent_ip = ""
    user.anonymized_at = _now()
    db.query(Notification).filter(Notification.user_id == uid).delete(synchronize_session=False)
    db.query(Preorder).filter(Preorder.user_id == uid).delete(synchronize_session=False)
    db.query(PasswordReset).filter(PasswordReset.user_id == uid).delete(synchronize_session=False)
    for claim in db.query(Claim).filter(Claim.user_id == uid).all():
        claim.description = "(текст удалён по запросу клиента)"
        claim.storage_notes = ""
        claim.expectation = ""
        claim.reply_to_client = ""
        _drop_claim_files(claim)
        for photo in list(claim.photos):
            db.delete(photo)


# ═══════════════════════════════════════════════════════════
# ЗАПРОСЫ КЛИЕНТОВ О ЕГО ДАННЫХ
# ═══════════════════════════════════════════════════════════

def add_working_days(start: date, days: int) -> date:
    d = start
    while days > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days -= 1
    return d


def response_deadline(req: DataRequest) -> date:
    created = req.created_at.date() if req.created_at else _now().date()
    return add_working_days(created, RESPONSE_WORKING_DAYS)


def open_data_requests(db: Session) -> int:
    return db.query(DataRequest).filter(DataRequest.status == "Новая").count()
