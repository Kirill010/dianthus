# app/routers/claims.py
"""Рекламации: клиент сообщает о проблеме с качеством, сотрудники отвечают.

Поля карточки повторяют карточку рекламации владельца: что, сколько, когда
получили и вскрыли, как хранили, слова клиента, чего он ждёт, фото.
Правила, которые видны сотрудникам в интерфейсе:
  * обещаний клиенту не даём — решение принимает владелец;
  * если с момента получения прошло больше суток — не отказываем, а помечаем
    «срочно владельцу».
"""
import logging
from datetime import datetime, timedelta, timezone
from html import escape
from typing import List

from fastapi import (APIRouter, BackgroundTasks, Depends, File, Form,
                     HTTPException, Request, UploadFile)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import get_current_user, require_staff
from ..models import (CLAIM_STATUSES, CLAIM_URGENCY, Claim, ClaimPhoto,
                      Notification, Order, Product, User)
from ..security import check_csrf, check_rate_limit
from ..services import preorder_rules
from ..services.audit import log_action
from ..services.notifier import send_email
from ..services.upload_service import save_uploads
from ..templating import render
from ..timeutils import shop_tz

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_TEXT = 3000
MAX_QTY = 1_000_000
CLAIM_PHOTO_MAX_BYTES = 15 * 1024 * 1024      # фото с телефона бывают крупнее фото товаров
UNITS = ("шт", "упак.")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_or_none(text: str):
    d = preorder_rules.parse_user_date(text) if (text or "").strip() else None
    return datetime(d.year, d.month, d.day) if d else None


# ═══════════════════════════════════════════════════════════
# КЛИЕНТ
# ═══════════════════════════════════════════════════════════

def _own_claim(db: Session, user: User, claim_id: int) -> Claim:
    claim = (db.query(Claim).options(selectinload(Claim.photos), joinedload(Claim.order))
             .filter(Claim.id == claim_id, Claim.user_id == user.id).first())
    if claim is None:
        raise HTTPException(404, "Обращение не найдено")
    return claim


@router.get("/claims", response_class=HTMLResponse)
def my_claims(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    claims = (db.query(Claim).filter(Claim.user_id == user.id)
              .order_by(Claim.id.desc()).limit(100).all())
    return render(request, "claims.html", db, user=user, claims=claims)


@router.get("/claims/new", response_class=HTMLResponse)
def claim_form(request: Request, order_id: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    orders = (db.query(Order).options(selectinload(Order.items))
              .filter(Order.user_id == user.id, Order.status != "Отменён")
              .order_by(Order.id.desc()).limit(30).all())
    order = next((o for o in orders if o.id == order_id), None)
    today = datetime.now(shop_tz()).date()
    return render(
        request, "claim_form.html", db, user=user, orders=orders, order=order,
        form=request.session.pop("claim_form", {}),
        errors=request.session.pop("claim_errors", None),
        today=today.isoformat(), max_photos=config.CLAIMS_MAX_PHOTOS,
    )


async def _rate_claims(request: Request) -> None:
    await check_rate_limit(request, "claims", max_hits=10, window=3600)


@router.post("/claims", dependencies=[Depends(_rate_claims)])
def claim_create(
    request: Request,
    background_tasks: BackgroundTasks,
    order_id: int = Form(0),
    product_name: str = Form(""),
    affected_qty: int = Form(0),
    total_qty: int = Form(0),
    unit: str = Form("шт"),
    received_at: str = Form(""),
    opened_at: str = Form(""),
    storage_notes: str = Form(""),
    description: str = Form(""),
    expectation: str = Form(""),
    photos: List[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    product_name = " ".join(product_name.split())[:200]
    description = description.strip()
    expectation = expectation.strip()
    storage_notes = storage_notes.strip()
    unit = unit if unit in UNITS else "шт"
    received = _date_or_none(received_at)
    opened = _date_or_none(opened_at)

    order = None
    if order_id:
        order = db.query(Order).filter(Order.id == order_id, Order.user_id == user.id).first()

    errors: list[str] = []
    if len(description) < 10:
        errors.append("Опишите, что случилось (хотя бы одно-два предложения)")
    if len(description) > MAX_TEXT or len(expectation) > MAX_TEXT or len(storage_notes) > MAX_TEXT:
        errors.append("Текст слишком длинный — сократите его")
    if not product_name:
        errors.append("Укажите, какой товар")
    if not (0 <= affected_qty <= MAX_QTY and 0 <= total_qty <= MAX_QTY):
        errors.append("Количество указано неверно")
    elif total_qty and affected_qty > total_qty:
        errors.append("Испорченных штук не может быть больше, чем получено")
    today = datetime.now(shop_tz()).date()
    if (received_at or "").strip():
        if received is None:
            errors.append("Дата получения указана неверно")
        elif received.date() > today:
            errors.append("Дата получения не может быть в будущем")
        elif received.date() < today - timedelta(days=366):
            errors.append("Дата получения слишком давняя")
    if (opened_at or "").strip() and opened is None:
        errors.append("Дата вскрытия указана неверно")
    if order_id and order is None:
        errors.append("Заказ не найден")

    if errors:
        request.session["claim_errors"] = errors
        request.session["claim_form"] = {
            "order_id": order_id, "product_name": product_name,
            "affected_qty": affected_qty or "", "total_qty": total_qty or "", "unit": unit,
            "received_at": received_at, "opened_at": opened_at,
            "storage_notes": storage_notes, "description": description,
            "expectation": expectation,
        }
        return RedirectResponse(url=f"/claims/new?order_id={order_id or 0}", status_code=303)

    country = ""
    if order is not None:
        match = next((i for i in order.items if i.product_name == product_name and i.product_id), None)
        if match is not None:
            product = db.get(Product, match.product_id)
            country = (product.country if product else "") or ""

    claim = Claim(
        user_id=user.id, order_id=order.id if order else None,
        product_name=product_name, country=country[:100],
        affected_qty=affected_qty, total_qty=total_qty, unit=unit,
        received_at=received, opened_at=opened,
        storage_notes=storage_notes, description=description, expectation=expectation,
    )
    db.add(claim)
    db.flush()
    files = [f for f in (photos or []) if f and f.filename][: config.CLAIMS_MAX_PHOTOS]
    skipped: list[str] = []
    for url in save_uploads(files, max_size=CLAIM_PHOTO_MAX_BYTES, skipped=skipped):
        db.add(ClaimPhoto(claim_id=claim.id, url=url))
    db.commit()

    background_tasks.add_task(
        send_email,
        f"{'⚠️ СРОЧНО ' if claim.is_late else ''}Рекламация {claim.number}: {user.company_name}",
        f"<p><b>{escape(user.company_name)}</b> ({escape(user.full_name)}, {escape(user.phone or '')}) "
        f"сообщает о проблеме: <b>{escape(product_name)}</b>, "
        f"{affected_qty} из {total_qty} {escape(unit)}.</p>"
        f"<p>{escape(description[:500])}</p>"
        + ("<p><b>С момента получения прошло больше суток</b> — по правилам передать владельцу.</p>"
           if claim.is_late else "")
        + f"<p><a href=\"{escape(config.APP_URL)}/admin/claims/{claim.id}\">Открыть карточку</a></p>",
    )
    request.session["flash"] = (
        f"Обращение {claim.number} принято. Мы разберёмся и ответим вам здесь и по почте."
    )
    if skipped:
        # молча терять фото нельзя: говорим, какие не загрузились и почему
        request.session["flash"] += (
            " Не удалось загрузить фото: " + "; ".join(skipped[:3])
            + ". Пришлите их менеджеру или сделайте снимок в обычном режиме."
        )
    return RedirectResponse(url=f"/claims/{claim.id}", status_code=303)


@router.get("/claims/{claim_id}", response_class=HTMLResponse)
def claim_view(claim_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return render(request, "claim_detail.html", db, user=user,
                  claim=_own_claim(db, user, claim_id))


# ═══════════════════════════════════════════════════════════
# СОТРУДНИКИ
# ═══════════════════════════════════════════════════════════

@router.get("/admin/claims", response_class=HTMLResponse)
def admin_claims(request: Request, status: str = "", db: Session = Depends(get_db),
                 staff=Depends(require_staff)):
    query = (db.query(Claim).options(joinedload(Claim.user))
             .order_by(Claim.status.in_(["Решена", "Отклонена"]), Claim.id.desc()))
    if status in CLAIM_STATUSES:
        query = query.filter(Claim.status == status)
    claims = query.limit(300).all()
    counts = {s: db.query(Claim).filter(Claim.status == s).count() for s in CLAIM_STATUSES}
    return render(request, "admin_claims.html", db, user=staff, claims=claims,
                  status=status, statuses=CLAIM_STATUSES, counts=counts)


def _claim_for_staff(db: Session, claim_id: int) -> Claim:
    claim = (db.query(Claim)
             .options(selectinload(Claim.photos), joinedload(Claim.user), joinedload(Claim.order))
             .filter(Claim.id == claim_id).first())
    if claim is None:
        raise HTTPException(404, "Обращение не найдено")
    return claim


@router.get("/admin/claims/{claim_id}", response_class=HTMLResponse)
def admin_claim_detail(claim_id: int, request: Request, db: Session = Depends(get_db),
                       staff=Depends(require_staff)):
    return render(request, "admin_claim_detail.html", db, user=staff,
                  claim=_claim_for_staff(db, claim_id), statuses=CLAIM_STATUSES,
                  urgencies=CLAIM_URGENCY)


@router.post("/admin/claims/{claim_id}")
def admin_claim_update(
    claim_id: int, request: Request, background_tasks: BackgroundTasks,
    status: str = Form(...), urgency: str = Form("обычная"),
    staff_comment: str = Form(""), owner_decision: str = Form(""),
    reply_to_client: str = Form(""),
    db: Session = Depends(get_db), staff=Depends(require_staff),
    _csrf: None = Depends(check_csrf),
):
    claim = _claim_for_staff(db, claim_id)
    if status not in CLAIM_STATUSES or urgency not in CLAIM_URGENCY:
        raise HTTPException(400, "Неизвестный статус или срочность")
    reply_to_client = reply_to_client.strip()[:MAX_TEXT]
    old_status, old_reply = claim.status, claim.reply_to_client or ""

    claim.status = status
    claim.urgency = urgency
    claim.staff_comment = staff_comment.strip()[:MAX_TEXT]
    if staff.is_admin:                                   # решение принимает владелец
        claim.owner_decision = owner_decision.strip()[:MAX_TEXT]
    claim.reply_to_client = reply_to_client
    claim.updated_at = _now()
    claim.resolved_at = _now() if status in ("Решена", "Отклонена") else None

    messages = []
    if reply_to_client and reply_to_client != old_reply:
        messages.append(f"Ответ по обращению {claim.number}: {reply_to_client}")
    elif status != old_status:
        messages.append(f"Обращение {claim.number}: статус «{status}»")
    for text in messages:
        db.add(Notification(user_id=claim.user_id, order_id=claim.order_id, text=text[:2000]))
    db.commit()
    log_action(db, request, staff, "claim_update", "claim", claim.id,
               f"{claim.number}: {old_status} → {status}")

    user = claim.user
    if messages and user and user.email and not user.anonymized_at:
        from ..services.notifier import send_email_to
        background_tasks.add_task(
            send_email_to, user.email, f"Обращение {claim.number}: {status}",
            f"<p>Здравствуйте, {escape(user.full_name)}!</p><p>{escape(messages[0])}</p>"
            f"<p><a href=\"{escape(config.APP_URL)}/claims/{claim.id}\">Открыть обращение</a></p>",
        )
    request.session["flash"] = f"Сохранено: {claim.number} — «{status}»"
    return RedirectResponse(url=f"/admin/claims/{claim.id}", status_code=303)
