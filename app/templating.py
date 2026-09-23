# Общий рендер шаблонов.
import logging
import os
import tempfile
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemBytecodeCache
from sqlalchemy import nulls_last
from sqlalchemy.orm import Session

from .config import config
from .deps import get_current_user
from .security import ensure_csrf_token
from .services.preorder_service import preorder_count_db
from . import models

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _pick_cache_dir() -> str:
    """Выбирает первый рабочий каталог для кэша Jinja."""
    candidates = [
        os.getenv("JINJA_CACHE_DIR", "").strip(),
        "/var/cache/dianthus/jinja",
        str(Path(tempfile.gettempdir()) / "dianthus_jinja"),
        str(TEMPLATES_DIR / ".cache"),
    ]
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = Path(d) / ".wtest"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return d
        except Exception as e:
            logger.debug("Кэш Jinja: %s недоступен (%s)", d, e)
            continue
    logger.warning("Не нашёл ни одного каталога для кэша Jinja")
    return str(TEMPLATES_DIR / ".cache")


JINJA_CACHE_DIR = _pick_cache_dir()
logger.info("Jinja bytecode cache: %s", JINJA_CACHE_DIR)

templates.env.bytecode_cache = FileSystemBytecodeCache(
    directory=JINJA_CACHE_DIR,
    pattern="__jinja2_%s.cache",
)
templates.env.cache_size = -1
# В проде — False; но проверим, что шаблоны читаются
templates.env.auto_reload = (config.ENV != "prod")


def render(request: Request, template: str, db: Session, **context):
    user = context.pop("user", None)
    if user is None:
        user = get_current_user(request, db)

    flash = request.session.pop("flash", None)
    cart = request.session.get("cart", [])
    cart_count = sum(item.get("quantity", 0) for item in cart)
    preorder_total = preorder_count_db(db, user.id) if user else 0
    csrf_token = ensure_csrf_token(request)

    active_supply = None
    unread_count = 0
    if user:
        active_supply = (
            db.query(models.Supply)
            .filter(models.Supply.status.in_(["Ожидается", "В пути"]))
            .order_by(nulls_last(models.Supply.arrival_date.asc()))
            .first()
        )
        if request.url.path != "/notifications":
            unread_count = (
                db.query(models.Notification)
                .filter(
                    models.Notification.user_id == user.id,
                    models.Notification.is_read.is_(False),
                )
                .count()
            )

    context.update({
        "user": user,
        "flash": flash,
        "cart_count": cart_count,
        "preorder_count": preorder_total,
        "active_supply": active_supply,
        "csrf_token": csrf_token,
        "config": config,
        "unread_count": unread_count,
    })
    return templates.TemplateResponse(
        request=request, name=template, context=context,
    )