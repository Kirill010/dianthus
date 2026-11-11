# app/templating.py (полный код)
"""Общий рендер шаблонов с кэшированием счётчика предзаказов."""
import hashlib
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemBytecodeCache
from sqlalchemy import case, nulls_last
from sqlalchemy.orm import Session

from .config import config
from .deps import get_current_user
from .security import ensure_csrf_token
from .services import seo_pages
from .services.preorder_service import preorder_count_db
from .services.pricing import apply_discount
from .services.site_settings import get_settings
from .timeutils import DATETIME_FMT, fmt_local, fmt_when, shop_tz
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


def _fmt_price(value) -> str:
    """95.00 → «95», 95.50 → «95.50». Пустое/нечисло — как есть."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    return f"{int(v)}" if v == int(v) else f"{v:.2f}"


templates.env.filters["price"] = _fmt_price
# {{ order.created_at|localdt }} → «14.02.2026 12:31» по времени магазина (МСК),
# а не «09:31» по UTC, как лежит в БД.
templates.env.filters["localdt"] = lambda v, fmt=DATETIME_FMT: fmt_local(v, fmt)
# {{ dt|when }} → «сегодня в 14:05» / «вчера в 18:30» / «25.10 в 09:00».
templates.env.filters["when"] = fmt_when
# {{ item.price|with_discount(5) }} → цена клиента со скидкой (тем же округлением,
# что и в корзине: apply_discount).
templates.env.filters["with_discount"] = lambda price, pct: apply_discount(price, pct)[1]
# {{ 7.5|pct }} → «7.5», {{ 5.0|pct }} → «5»
templates.env.filters["pct"] = lambda v: f"{float(v or 0):g}"


def _plural(n, one: str, few: str, many: str) -> str:
    """{{ 253|plural('позиция', 'позиции', 'позиций') }} → «позиций»; 1 → «позиция», 22 → «позиции»."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    return one if last == 1 else (few if 2 <= last <= 4 else many)


# «из <страны>» в родительном падеже для описаний: «Кения» → «Кении», «Эквадор» → «Эквадора».
# Неизвестное название оставляем как есть (лучше «из ЮАР», чем выдуманное окончание).
_FROM_COUNTRY = {
    "кения": "Кении", "эквадор": "Эквадора", "голландия": "Голландии", "россия": "России", "израиль": "Израиля",
    "колумбия": "Колумбии", "италия": "Италии", "китай": "Китая", "таиланд": "Таиланда", "эфиопия": "Эфиопии",
    "турция": "Турции", "испания": "Испании", "дания": "Дании", "нидерланды": "Нидерландов",
}


def _from_country(name) -> str:
    text = str(name or "").strip()
    return _FROM_COUNTRY.get(text.lower(), text)


templates.env.filters["plural"] = _plural
templates.env.filters["from_country"] = _from_country


STATIC_DIR = Path(__file__).resolve().parent / "static"
_asset_hashes: dict[str, tuple[int, int, str]] = {}


def asset_url(path: str) -> str:
    """{{ asset('style.css') }} → «/static/style.css?v=3fa9c1d2e0».

    Отпечаток считается по содержимому файла: при выкладке новой версии адрес меняется сам, поэтому
    браузер может хранить файл с этим адресом год (см. CachedStaticFiles), а свежий стиль подтягивается сразу.
    Раньше номер версии в ?v=… правился руками и о нём легко было забыть."""
    rel = str(path).lstrip("/")
    try:
        stat = (STATIC_DIR / rel).stat()
    except OSError:
        return f"/static/{rel}"
    cached = _asset_hashes.get(rel)
    if cached is None or cached[0] != stat.st_mtime_ns or cached[1] != stat.st_size:
        digest = hashlib.sha256((STATIC_DIR / rel).read_bytes()).hexdigest()[:10]
        cached = (stat.st_mtime_ns, stat.st_size, digest)
        _asset_hashes[rel] = cached
    return f"/static/{rel}?v={cached[2]}"


templates.env.globals["asset"] = asset_url


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

    # Баннер активной поставки показываем всем — и анонимам на публичных
    # страницах (лендинг/каталог): это рабочий маркетинговый триггер.
    # Сначала ближайшая машина с датой впереди; задержавшиеся (дата прошла) — после неё.
    today = datetime.combine(datetime.now(shop_tz()).date(), datetime.min.time())
    active_supply = (
        db.query(models.Supply)
        .filter(
            models.Supply.status.in_(["Ожидается", "В пути"]),
            models.Supply.is_service.is_(False),
        )
        .order_by(case((models.Supply.arrival_date < today, 1), else_=0),
                  nulls_last(models.Supply.arrival_date.asc()))
        .first()
    )

    unread_count = 0
    if user and request.url.path != "/notifications":
        unread_count = (
            db.query(models.Notification)
            .filter(
                models.Notification.user_id == user.id,
                models.Notification.is_read.is_(False),
            )
            .count()
        )

    # реквизиты для подвала: внесённые в админке главнее .env
    shop = get_settings(db, ("shop_inn", "shop_ogrn", "shop_address"))

    context.update({
        "user": user,
        "flash": flash,
        "shop_inn": shop["shop_inn"],
        "shop_ogrn": shop["shop_ogrn"],
        "shop_address": shop["shop_address"],
        "cart_count": cart_count,
        "preorder_count": preorder_total,
        "active_supply": active_supply,
        "csrf_token": csrf_token,
        "config": config,
        "seo_sections": seo_pages.SECTIONS,
        "unread_count": unread_count,
        "current_year": datetime.now(shop_tz()).year,
        # CSP nonce из SecurityHeadersMiddleware — для встроенных <script>.
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
    })
    return templates.TemplateResponse(
        request=request, name=template, context=context,
    )