# app/routers/password_reset.py
"""«Забыли пароль?»: ссылка на почту → новый пароль.

Безопасность:
  * ответ на запрос всегда одинаковый — по нему нельзя узнать, есть ли такая почта;
  * ссылка одноразовая, живёт 60 минут; в базе хранится только хэш токена;
  * запросов — не больше 5 в час с одного адреса и не больше 3 в час на одну почту;
  * после смены пароля старые ссылки перестают работать, а блокировка входа снимается;
  * если почта на сервере не настроена, сайт честно говорит позвонить менеджеру
    (а менеджер может выдать временный пароль в карточке клиента).
"""
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import PasswordReset, User
from ..security import (check_csrf, check_rate_limit,
                        reset_login_failures, _client_ip)
from ..services.notifier import _get_smtp_config, send_email_to
from ..services.passwords import hash_password
from ..templating import render
from ..validators import validate_password

logger = logging.getLogger(__name__)
router = APIRouter()

TOKEN_TTL = timedelta(minutes=60)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def smtp_ready() -> bool:
    return _get_smtp_config() is not None


async def _rate_forgot(request: Request) -> None:
    await check_rate_limit(request, "forgot", max_hits=5, window=3600)


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/profile", status_code=303)
    return render(request, "forgot.html", db, user=None, mail_ok=smtp_ready(),
                  sent=request.session.pop("forgot_sent", False))


@router.post("/forgot", dependencies=[Depends(_rate_forgot)])
async def forgot_submit(
    request: Request, background_tasks: BackgroundTasks,
    email: str = Form(""), db: Session = Depends(get_db),
    _csrf: None = Depends(check_csrf),
):
    email = (email or "").strip().lower()[:120]
    request.session["forgot_sent"] = True          # ответ одинаковый для любого адреса
    if not email or not smtp_ready():
        return RedirectResponse(url="/forgot", status_code=303)
    user = db.query(User).filter(User.email == email).first()
    if user is None or user.anonymized_at is not None:
        return RedirectResponse(url="/forgot", status_code=303)

    recent = (db.query(PasswordReset)
              .filter(PasswordReset.user_id == user.id,
                      PasswordReset.created_at >= _now() - timedelta(hours=1)).count())
    if recent >= 3:
        return RedirectResponse(url="/forgot", status_code=303)

    token = secrets.token_urlsafe(32)
    now = _now()
    # прежние неиспользованные ссылки гасим: рабочей остаётся только самая свежая
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id,
                                   PasswordReset.used_at.is_(None)).update(
        {PasswordReset.used_at: now}, synchronize_session=False)
    db.add(PasswordReset(user_id=user.id, token_hash=_hash(token),
                         expires_at=now + TOKEN_TTL, ip=_client_ip(request)))
    db.commit()
    link = f"{config.APP_URL.rstrip('/')}/reset/{token}"
    background_tasks.add_task(
        send_email_to, user.email, "Восстановление пароля — Диантус",
        f"<p>Здравствуйте, {escape(user.full_name)}!</p>"
        f"<p>Вы (или кто-то другой) попросили сбросить пароль на сайте {escape(config.SHOP_NAME)}. "
        f"Чтобы задать новый пароль, перейдите по ссылке — она действует 60 минут:</p>"
        f"<p><a href=\"{escape(link)}\">{escape(link)}</a></p>"
        f"<p>Если это были не вы — просто проигнорируйте письмо, пароль не изменится.</p>",
        f"Новый пароль: {link} (ссылка действует 60 минут)",
    )
    logger.info("Сброс пароля запрошен: %s", user.email)
    return RedirectResponse(url="/forgot", status_code=303)


def _valid_reset(db: Session, token: str) -> PasswordReset | None:
    row = db.query(PasswordReset).filter(PasswordReset.token_hash == _hash(token)).first()
    if row is None or row.used_at is not None or row.expires_at < _now():
        return None
    return row


@router.get("/reset/{token}", response_class=HTMLResponse)
def reset_page(token: str, request: Request, db: Session = Depends(get_db)):
    ok = _valid_reset(db, token) is not None
    return render(request, "reset.html", db, user=None, token_ok=ok, token=token,
                  errors=request.session.pop("reset_errors", None))


@router.post("/reset/{token}", dependencies=[Depends(_rate_forgot)])
async def reset_submit(
    token: str, request: Request,
    password: str = Form(""), password2: str = Form(""),
    db: Session = Depends(get_db), _csrf: None = Depends(check_csrf),
):
    row = _valid_reset(db, token)
    if row is None:
        request.session["flash"] = "Ссылка недействительна или устарела. Запросите новую."
        return RedirectResponse(url="/forgot", status_code=303)
    errors = []
    if password != password2:
        errors.append("Пароли не совпадают")
    message = validate_password(password)
    if message:
        errors.append(message)
    if errors:
        request.session["reset_errors"] = errors
        return RedirectResponse(url=f"/reset/{token}", status_code=303)

    user = db.get(User, row.user_id)
    if user is None or user.anonymized_at is not None:
        return RedirectResponse(url="/forgot", status_code=303)
    user.hashed_password = hash_password(password)
    row.used_at = _now()
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id,
                                   PasswordReset.id != row.id).delete(synchronize_session=False)
    db.commit()
    await reset_login_failures(user.email)
    request.session["flash"] = "Пароль изменён. Теперь войдите с новым паролем."
    logger.info("Пароль сброшен по ссылке: %s", user.email)
    return RedirectResponse(url="/login", status_code=303)
