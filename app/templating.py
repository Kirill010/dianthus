# app/templating.py (полный код)
"""Общий рендер шаблонов с кэшированием счётчика предзаказов."""
import logging
import os
import tempfile
import time
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemBytecodeCache
from sqlalchemy import nulls_last
from sqlalchemy.orm import Session, selectinload

from .config import config
from .deps import get_current_user
from .security import ensure_csrf_token
from .services.preorder_service import preorder_count_db
from . import models

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ✅ Кэш счётчика предзаказов (TTL 30 сек)
_preorder_cache: dict[str, dict] = {}
_CACHE_CLEANUP_THRESHOLD = 5000


def _cleanup_preorder_cache() -> None:
    """fix #9: не даём словарю расти бесконечно."""
    if len(_preorder_cache) < _CACHE_CLEANUP_THRESHOLD:
        return
    import time as _t
    now = _t.time()
    dead = [k for k, v in _preorder_cache.items()
            if now - v.get("ts", 0) > 120]
    for k in dead:
        _preorder_cache.pop(k, None)
    logger.info("Preorder cache cleanup: удалено %d", len(dead))


def _get_cached_preorder_count(db: Session, user_id: int, ttl: int = 30) -> int:
    _cleanup_preorder_cache()
    key = f"preorder_count:{user_id}"
    now = time.time()
    if key in _preorder_cache and now - _preorder_cache[key]["ts"] < ttl:
        return _preorder_cache[key]["value"]
    value = preorder_count_db(db, user_id)
    _preorder_cache[key] = {"value": value, "ts": now}
    return value


def _invalidate_preorder_cache(user_id: int | None = None) -> None:
    if user_id is None:
        _preorder_cache.clear()
    else:
        _preorder_cache.pop(f"preorder_count:{user_id}", None)


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
            logger.info("Кэш Jinja будет сохранён в: %s", d)
            return d
        except Exception as e:
            logger.debug("Кэш Jinja: %s недоступен (%s)", d, e)
            continue
    logger.warning("Не нашёл ни одного каталога для кэша Jinja")
    return str(TEMPLATES_DIR / ".cache")


JINJA_CACHE_DIR = _pick_cache_dir()

try:
    templates.env.bytecode_cache = FileSystemBytecodeCache(
        directory=JINJA_CACHE_DIR,
        pattern="__jinja2_%s.cache",
    )
    logger.info("Jinja bytecode cache инициализирован: %s", JINJA_CACHE_DIR)
except Exception as e:
    logger.warning("Не удалось инициализировать bytecode cache Jinja: %s", e)
    templates.env.bytecode_cache = None

templates.env.cache_size = -1
templates.env.auto_reload = (config.ENV != "prod")


def render(request: Request, template: str, db: Session, **context):
    user = context.pop("user", None)
    if user is None:
        user = get_current_user(request, db)

    flash = request.session.pop("flash", None)
    cart = request.session.get("cart", [])
    cart_count = sum(item.get("quantity", 0) for item in cart)

    # ✅ Кэшированный счётчик предзаказов
    preorder_total = (
        _get_cached_preorder_count(db, user.id) if user else 0
    )
    csrf_token = ensure_csrf_token(request)

    active_supply = None
    unread_count = 0
    if user:
        active_supply = (
            db.query(models.Supply)
            .options(selectinload(models.Supply.items))
            .filter(
                models.Supply.status.in_(["Ожидается", "В пути"]),
                models.Supply.is_service.is_(False),
            )
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