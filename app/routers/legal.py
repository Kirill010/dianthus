# app/routers/legal.py
"""Юридические страницы, согласия клиентов, выгрузка/удаление данных.

Публично:   /privacy  /consent  /marketing-consent  /terms
Клиент:     /profile/consent/*  /profile/data.json  /profile/data/delete-request
Админ:      /admin/legal  /admin/legal/{slug}/edit  /admin/data-requests
"""
import json
import logging
from datetime import datetime, timezone
from html import escape

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import (DATA_REQUEST_KINDS, DataRequest, LegalDocument, User)
from ..security import check_csrf, check_rate_limit
from ..services import legal
from ..services.audit import log_action
from ..services.notifier import send_email, send_email_to
from ..services.site_settings import flag_is_set, set_setting
from ..templating import render

logger = logging.getLogger(__name__)
router = APIRouter()


# ═══════════════════════════════════════════════════════════
# ПУБЛИЧНЫЕ ДОКУМЕНТЫ
# ═══════════════════════════════════════════════════════════

def _doc_page(slug: str):
    def page(request: Request, db: Session = Depends(get_db)):
        doc = legal.get_document(db, slug)
        if doc is None:
            raise HTTPException(404, "Документ не найден")
        return render(
            request, "legal_doc.html", db,
            doc=doc, body_html=legal.render_document(db, doc),
            other_docs=[(s, u) for s, u in legal.DOC_ORDER if s != slug],
            doc_titles={d.slug: d.title for d in db.query(LegalDocument).all()},
        )
    page.__name__ = f"legal_page_{slug}"
    return page


for _slug, _url in legal.DOC_ORDER:
    router.add_api_route(_url, _doc_page(_slug), methods=["GET"],
                         response_class=HTMLResponse)


# ═══════════════════════════════════════════════════════════
# КЛИЕНТ: СОГЛАСИЯ И ДАННЫЕ
# ═══════════════════════════════════════════════════════════

@router.post("/profile/consent/marketing")
def marketing_consent_toggle(
    request: Request,
    grant: str = Form("0"),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    want = grant == "1"
    if want == bool(user.marketing_consent):
        request.session["flash"] = "Настройка уже такая"
        return RedirectResponse(url="/profile#consent", status_code=303)
    legal.record_consent(db, user, "marketing", want, request, source="profile")
    request.session["flash"] = (
        "Рекламные сообщения включены. Отключить можно в любой момент."
        if want else "Рекламные сообщения отключены. Сообщения по заказам продолжат приходить."
    )
    return RedirectResponse(url="/profile#consent", status_code=303)


@router.post("/profile/consent/pd")
def pd_consent_confirm(
    request: Request,
    consent: str = Form(""),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    """Клиенты, зарегистрированные до появления журнала согласий, подтверждают его здесь."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not consent:
        request.session["flash"] = "Отметьте согласие, чтобы подтвердить его"
        return RedirectResponse(url="/profile#consent", status_code=303)
    if user.consent_at is None:
        legal.record_consent(db, user, "pd", True, request, source="profile")
        request.session["flash"] = "Спасибо! Согласие на обработку данных сохранено."
    return RedirectResponse(url="/profile#consent", status_code=303)


async def _rate_export(request: Request) -> None:
    await check_rate_limit(request, "data-export", max_hits=5, window=3600)


@router.get("/profile/data.json", dependencies=[Depends(_rate_export)])
def download_my_data(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    payload = legal.export_user_data(db, user)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        content=body, media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="my-data.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/profile/data/delete-request")
def request_data_deletion(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if user.is_staff:
        request.session["flash"] = (
            "Для учётных записей сотрудников удаление выполняет владелец сайта."
        )
        return RedirectResponse(url="/profile#consent", status_code=303)
    already = db.query(DataRequest).filter(
        DataRequest.user_id == user.id, DataRequest.kind == "delete",
        DataRequest.status == "Новая").first()
    if already:
        request.session["flash"] = "Ваш запрос уже принят и обрабатывается."
        return RedirectResponse(url="/profile#consent", status_code=303)
    req = DataRequest(user_id=user.id, email_snapshot=user.email, kind="delete")
    db.add(req)
    db.commit()
    background_tasks.add_task(
        send_email,
        f"Запрос на удаление данных: {user.company_name}",
        f"<p>Клиент <b>{escape(user.company_name)}</b> ({escape(user.email)}) просит "
        f"удалить свои данные и отозвать согласие.</p>"
        f"<p>Срок ответа по закону — до 10 рабочих дней. "
        f"<a href=\"{escape(config.APP_URL)}/admin/data-requests\">Открыть запросы</a></p>",
    )
    request.session["flash"] = (
        "Запрос принят. Мы обработаем его в срок, установленный законом, и напишем вам на почту."
    )
    return RedirectResponse(url="/profile#consent", status_code=303)


# ═══════════════════════════════════════════════════════════
# АДМИН: ЮРИДИЧЕСКИЕ ТЕКСТЫ
# ═══════════════════════════════════════════════════════════

@router.get("/admin/legal", response_class=HTMLResponse)
def admin_legal(request: Request, db: Session = Depends(get_db),
                admin=Depends(require_admin)):
    state = legal.readiness(db)
    clients = db.query(User).filter(User.is_admin.is_(False), User.is_manager.is_(False))
    return render(
        request, "admin_legal.html", db, user=admin, state=state,
        consents_total=clients.count(),
        consents_given=clients.filter(User.consent_at.isnot(None)).count(),
        marketing_yes=db.query(User).filter(User.marketing_consent.is_(True)).count(),
        open_requests=legal.open_data_requests(db),
    )


@router.post("/admin/legal/todo/{key}")
def admin_legal_todo(key: str, request: Request, db: Session = Depends(get_db),
                     admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    if key not in {k for k, _t, _x in legal.OFFLINE_TODO}:
        raise HTTPException(404, "Неизвестный пункт")
    name = f"legal.todo.{key}"
    set_setting(db, name, "" if flag_is_set(db, name) else
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), admin)
    return RedirectResponse(url="/admin/legal#todo", status_code=303)


@router.post("/admin/legal/reviewed/{slug}")
def admin_legal_reviewed(slug: str, request: Request, db: Session = Depends(get_db),
                         admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    if slug not in legal.DOC_URL:
        raise HTTPException(404, "Документ не найден")
    name = f"legal.reviewed.{slug}"
    set_setting(db, name, "" if flag_is_set(db, name) else
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), admin)
    return RedirectResponse(url="/admin/legal#docs", status_code=303)


@router.get("/admin/legal/{slug}/edit", response_class=HTMLResponse)
def admin_legal_edit_page(slug: str, request: Request, db: Session = Depends(get_db),
                          admin=Depends(require_admin)):
    doc = legal.get_document(db, slug)
    if doc is None:
        raise HTTPException(404, "Документ не найден")
    return render(
        request, "admin_legal_edit.html", db, user=admin, doc=doc,
        url=legal.DOC_URL.get(slug, "#"), placeholders=legal.PLACEHOLDERS,
        revisions=list(doc.revisions),
        errors=request.session.pop("legal_errors", None),
    )


@router.post("/admin/legal/{slug}/edit")
def admin_legal_edit(slug: str, request: Request,
                     title: str = Form(""), body: str = Form(""),
                     db: Session = Depends(get_db), admin=Depends(require_admin),
                     _csrf: None = Depends(check_csrf)):
    doc = legal.get_document(db, slug)
    if doc is None:
        raise HTTPException(404, "Документ не найден")
    errors = []
    if not title.strip():
        errors.append("Укажите заголовок")
    if len(body.strip()) < 50:
        errors.append("Текст слишком короткий")
    if len(body) > 100_000:
        errors.append("Текст слишком длинный")
    if errors:
        request.session["legal_errors"] = errors
        return RedirectResponse(url=f"/admin/legal/{slug}/edit", status_code=303)
    if legal.update_document(db, doc, title, body, admin):
        log_action(db, request, admin, "legal_edit", "legal", doc.id,
                   f"«{doc.title}»: новая версия {doc.version}")
        request.session["flash"] = (
            f"Сохранено: версия {doc.version}. Прежняя редакция осталась в истории. "
            f"Новые клиенты будут соглашаться уже с этим текстом."
        )
    else:
        request.session["flash"] = "Изменений нет — версия не менялась."
    return RedirectResponse(url="/admin/legal#docs", status_code=303)


# ═══════════════════════════════════════════════════════════
# АДМИН: ЗАПРОСЫ КЛИЕНТОВ О ЕГО ДАННЫХ
# ═══════════════════════════════════════════════════════════

@router.get("/admin/data-requests", response_class=HTMLResponse)
def admin_data_requests(request: Request, db: Session = Depends(get_db),
                        admin=Depends(require_admin)):
    # новые — сверху, затем по убыванию номера
    rows = (db.query(DataRequest)
            .order_by(DataRequest.status != "Новая", DataRequest.id.desc())
            .limit(200).all())
    user_ids = [r.user_id for r in rows if r.user_id]
    users = ({u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
             if user_ids else {})
    return render(
        request, "admin_data_requests.html", db, user=admin,
        rows=rows, kinds=DATA_REQUEST_KINDS, users=users,
        deadline=legal.response_deadline,
    )


@router.post("/admin/data-requests/{req_id}/done")
def admin_data_request_done(
    req_id: int, request: Request, background_tasks: BackgroundTasks,
    note: str = Form(""), db: Session = Depends(get_db),
    admin=Depends(require_admin), _csrf: None = Depends(check_csrf),
):
    req = db.get(DataRequest, req_id)
    if req is None:
        raise HTTPException(404, "Запрос не найден")
    if req.status != "Новая":
        request.session["flash"] = "Этот запрос уже обработан"
        return RedirectResponse(url="/admin/data-requests", status_code=303)

    user = db.get(User, req.user_id) if req.user_id else None
    email = req.email_snapshot or (user.email if user else "")
    if req.kind == "delete" and user is not None:
        if user.is_staff:
            request.session["flash"] = "Учётную запись сотрудника сначала лишите прав"
            return RedirectResponse(url="/admin/data-requests", status_code=303)
        # в журнал — только то, что реально отзывается
        if user.consent_at:
            legal.record_consent(db, user, "pd", False, request, source="admin", commit=False)
        if user.marketing_consent:
            legal.record_consent(db, user, "marketing", False, request, source="admin", commit=False)
        legal.anonymize_user(db, user)
    req.status = "Выполнена"
    req.note = note.strip()[:1000]
    req.handled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    req.handled_by_id = admin.id
    db.commit()
    log_action(db, request, admin, "data_request_done", "data_request", req.id,
               f"{req.kind}: {email}")
    if email and not email.endswith("@" + legal.ANON_EMAIL_DOMAIN):
        background_tasks.add_task(
            send_email_to, email, "Ваш запрос о персональных данных выполнен",
            "<p>Здравствуйте! Ваш запрос о персональных данных выполнен: "
            + ("данные удалены или обезличены, согласие отозвано."
               if req.kind == "delete" else "мы выслали данные.") + "</p>",
        )
    request.session["flash"] = "Запрос выполнен" + (
        ": данные клиента обезличены" if req.kind == "delete" and user else "")
    return RedirectResponse(url="/admin/data-requests", status_code=303)


@router.post("/admin/data-requests/{req_id}/reject")
def admin_data_request_reject(
    req_id: int, request: Request, note: str = Form(""),
    db: Session = Depends(get_db), admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    req = db.get(DataRequest, req_id)
    if req is None:
        raise HTTPException(404, "Запрос не найден")
    if req.status == "Новая":
        req.status = "Отклонена"
        req.note = note.strip()[:1000]
        req.handled_at = datetime.now(timezone.utc).replace(tzinfo=None)
        req.handled_by_id = admin.id
        db.commit()
        log_action(db, request, admin, "data_request_reject", "data_request", req.id,
                   req.email_snapshot or "")
    request.session["flash"] = "Запрос отклонён (причину клиенту лучше сообщить письмом)"
    return RedirectResponse(url="/admin/data-requests", status_code=303)
