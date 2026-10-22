# app/main.py
"""Сборка приложения и общие маршруты."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload
from starlette.concurrency import run_in_threadpool
# Именно класс Starlette, а не fastapi.HTTPException: «страница не найдена»
# (404 на несуществующий адрес) бросает Starlette, и обработчик, повешенный
# на подкласс FastAPI, её не ловит — пользователь видел сырой JSON.
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import router as auth_router
from .bootstrap import ensure_default_admin
from .config import config
from .database import check_cyrillic_search, get_db
from .deps import get_current_user
from .migrations import prepare_database
from .scheduler import start_scheduler, stop_scheduler
from .sentry_config import init_sentry
from . import models, security
from .security import CsrfError, init_rate_limiter, close_rate_limiter
from .templating import render
from .middleware import SecurityHeadersMiddleware
from .session import ServerSessionMiddleware
from .web import safe_back


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

init_sentry()


def _safe_load_routers() -> dict:
    out = {}
    for name in ("admin", "cart", "catalog", "profile", "integration_1c"):
        try:
            mod = import_module(f".routers.{name}", package="app")
            if not hasattr(mod, "router"):
                logger.error(
                    "❌ Роутер %s импортирован, но не содержит 'router'. "
                    "Проверьте файл app/routers/%s.py",
                    name, name,
                )
                continue
            out[name] = mod
        except Exception as e:
            logger.exception(
                "❌ Не удалось импортировать роутер %s: %s", name, e,
            )
    return out


_routers = _safe_load_routers()


if config.ENV == "prod" and config.DATABASE_URL.startswith("sqlite"):
    logger.warning(
        "⚠️ ENV=prod и DATABASE_URL=sqlite — race conditions в "
        "place_order не защищены. Используйте PostgreSQL."
    )


def _check_vendor() -> None:
    static_dir = Path(__file__).resolve().parent / "static"
    vendor = static_dir / "vendor"
    needed = [
        "bootstrap.min.css",
        "bootstrap.bundle.min.js",
        "fontawesome/css/all.min.css",
    ]
    missing = [n for n in needed if not (vendor / n).exists()]
    if missing:
        logger.warning(
            "⚠️  Не найдены vendor-файлы: %s. Запустите: "
            "python download_vendor.py",
            ", ".join(missing),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Таблицы → миграции → админ из .env. Всё под одной блокировкой
        # (см. prepare_database): на пустой базе админ создаётся ПОСЛЕ таблиц,
        # а два воркера не дерутся за CREATE TABLE. Движок внутри берётся через
        # current_bind(): в тестах SessionLocal перепривязан к in-memory БД.
        prepare_database(ensure_default_admin)
        check_cyrillic_search()
        db_name = (
            config.DATABASE_URL.split("@")[-1]
            if "@" in config.DATABASE_URL
            else config.DATABASE_URL
        )
        logger.info("✅ Схема БД готова (%s)", db_name)
    except Exception as e:
        logger.error("❌ Не удалось подготовить БД: %s", e)
        raise

    _check_vendor()
    await init_rate_limiter()
    start_scheduler()
    # Сообщение «готов» — ПОСЛЕ подготовки БД, Redis и планировщика (раньше оно
    # печаталось при импорте модуля, до того как что-либо было готово)
    logger.info("🌸 Диантус готов (%s)", config.ENV)
    try:
        yield
    finally:
        logger.info("🛑 Завершение приложения...")
        stop_scheduler()
        await close_rate_limiter()
        try:
            import sentry_sdk
            sentry_sdk.flush(timeout=2.0)
        except Exception:
            pass
        logger.info("✅ Приложение остановлено")


app = FastAPI(
    title="Диантус — оптовый магазин цветов",
    lifespan=lifespan,
    docs_url=None if config.ENV == "prod" else "/docs",
    redoc_url=None if config.ENV == "prod" else "/redoc",
    openapi_url=None if config.ENV == "prod" else "/openapi.json",
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    ServerSessionMiddleware,
    secret_key=config.SECRET_KEY,
    max_age=config.SESSION_MAX_AGE,
    same_site="lax",
    https_only=(config.ENV == "prod"),
    cookie_name="dianthus_sid",
    redis_getter=security.get_redis_client,
)

# Доверенные Host-заголовки: отсекаем запросы с поддельным Host
# (Host header poisoning — например, для подмены ссылок в письмах).
# Middleware добавлен ПОСЛЕДНИМ = самый внешний слой (Starlette оборачивает
# в обратном порядке): чужой Host отбрасывается до любой логики.
if config.ENV == "prod":
    from urllib.parse import urlparse
    _host = (urlparse(config.APP_URL).hostname or "").lower()
    _allowed_hosts = [h for h in {
        _host,
        f"www.{_host}" if _host and not _host.startswith("www.") else "",
        "localhost", "127.0.0.1",   # healthcheck из docker-контейнера
        "app",                      # имя сервиса в docker-сети
    } if h]
    if _allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── РОУТЕРЫ ──
app.include_router(auth_router)

for _name, _mod in _routers.items():
    try:
        app.include_router(_mod.router)
        logger.info("✅ Подключён роутер: %s", _name)
    except Exception as e:
        logger.exception("❌ Ошибка include_router(%s): %s", _name, e)


# ═══════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ ОШИБОК
# ═══════════════════════════════════════════════════════════

@app.exception_handler(CsrfError)
async def csrf_error_handler(request: Request, exc: CsrfError):
    request.session["flash"] = f"⚠️ {exc.message}"
    return RedirectResponse(url=safe_back(request, "/login"), status_code=303)


_ERROR_TITLES = {
    400: "Некорректный запрос",
    401: "Требуется вход",
    403: "Доступ запрещён",
    404: "Страница не найдена",
    405: "Метод не поддерживается",
    413: "Слишком большой запрос",
    429: "Слишком много запросов",
    500: "Ошибка сервера",
    503: "Сервис временно недоступен",
}

# Стандартные англоязычные тексты Starlette — для людей переводим.
_DEFAULT_DETAILS = {
    "Not Found": "Такой страницы нет. Возможно, товар уже снят с продажи.",
    "Method Not Allowed": "Это действие нельзя выполнить таким способом.",
}


def _wants_html(request: Request) -> bool:
    return request.headers.get("accept", "").startswith("text/html")


def _error_page(request: Request, status_code: int, detail,
                headers: dict | None = None):
    """HTML-страница ошибки с настоящим HTTP-статусом (раньше было 200 OK)."""
    title = _ERROR_TITLES.get(status_code, "Ошибка")
    message = _DEFAULT_DETAILS.get(str(detail), detail or "")
    db = next(get_db())
    try:
        response = render(request, "error.html", db, code=status_code,
                          title=title, message=message)
        response.status_code = status_code
        for key, value in (headers or {}).items():
            response.headers[key] = value          # напр. Retry-After у 429
        return response
    finally:
        db.close()


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (301, 302, 303, 307, 308):
        return RedirectResponse(
            url=(exc.headers or {}).get("Location", "/login"),
            status_code=exc.status_code,
        )
    if _wants_html(request):
        return _error_page(request, exc.status_code, exc.detail, exc.headers)
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError):
    """Неверный параметр запроса. В браузере — понятная страница, а не JSON.

    Раньше адрес вроде /product/abc показывал человеку сырой JSON с
    описанием ошибки валидации.
    """
    if _wants_html(request):
        in_path = any(tuple(e.get("loc", ()))[:1] == ("path",) for e in exc.errors())
        return _error_page(
            request, 404 if in_path else 400,
            "Такой страницы нет." if in_path else "Проверьте введённые данные.",
        )
    return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "💥 Необработанная ошибка на %s: %s", request.url.path, exc
    )

    if request.headers.get("accept", "").startswith("text/html"):
        try:
            db = next(get_db())
            try:
                response = render(
                    request, "error.html", db,
                    code=500, title="Ошибка сервера",
                    message="Что-то пошло не так. Попробуйте обновить "
                            "страницу или зайдите чуть позже.",
                )
                response.status_code = 500      # не «200 OK» на странице ошибки
                return response
            finally:
                db.close()
        except Exception as render_err:
            logger.error("Не удалось отрисовать error.html: %s", render_err)

    return JSONResponse(
        {"detail": "Internal server error"},
        status_code=500,
    )


# ═══════════════════════════════════════════════════════════
# ОБЩИЕ МАРШРУТЫ
# ═══════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/catalog", status_code=303)

    preview = (
        db.query(models.SupplyItem)
        .options(joinedload(models.SupplyItem.product))
        .join(models.Product, models.SupplyItem.product_id == models.Product.id)
        .filter(models.SupplyItem.is_active.is_(True))
        .order_by(models.SupplyItem.created_at.desc())
        .limit(24)
        .all()
    )
    preview = [it for it in preview if it.product.all_photos][:8]
    in_stock = (
        db.query(models.SupplyItem)
        .filter(models.SupplyItem.is_active.is_(True))
        .count()
    )
    return render(
        request, "landing.html", db,
        preview_items=preview, in_stock_count=in_stock,
    )


@app.get("/sitemap.xml")
def sitemap(db: Session = Depends(get_db)):
    base = config.APP_URL.rstrip("/")
    today = datetime.now(timezone.utc).date().isoformat()

    urls = [
        ("/", today, "weekly", "1.0"),
        ("/catalog", today, "daily", "0.9"),
        ("/contacts", today, "monthly", "0.6"),
        ("/register", today, "monthly", "0.5"),
        ("/privacy", today, "yearly", "0.2"),
    ]
    rows = (
        db.query(models.SupplyItem.id, models.SupplyItem.created_at)
        .filter(models.SupplyItem.is_active.is_(True))
        .all()
    )
    for item_id, created_at in rows:
        lastmod = (created_at.date().isoformat()
                   if created_at else today)
        urls.append((f"/product/{item_id}", lastmod, "weekly", "0.8"))

    body = "\n".join(
        f"  <url>\n"
        f"    <loc>{base}{path}</loc>\n"
        f"    <lastmod>{lastmod}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        f"  </url>"
        for path, lastmod, freq, priority in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n"
        "</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return render(
        request, "register.html", db,
        register_errors=request.session.pop("register_errors", None),
        register_data=request.session.pop("register_data", {}),
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/catalog", status_code=303)
    return render(
        request, "login.html", db,
        user=None,
        register_errors=request.session.pop("register_errors", None),
        register_data=request.session.pop("register_data", {}),
        login_error=request.session.pop("login_error", None),
        show_totp=request.session.pop("show_totp", False),
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request, db: Session = Depends(get_db)):
    return render(request, "privacy.html", db)


@app.get("/registration_pending", response_class=HTMLResponse)
def registration_pending(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return render(request, "registration_pending.html", db)


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    result = {
        "status": "ok",
        "env": config.ENV,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # синхронный драйвер БД не должен блокировать event loop
        await run_in_threadpool(db.execute, text("SELECT 1"))
        result["db"] = "ok"
    except Exception as e:
        result["db"] = f"error: {str(e)[:100]}"
        result["status"] = "degraded"

    try:
        client = security.get_redis_client()
        if security.is_redis_enabled() and client is not None:
            await client.ping()
            result["redis"] = "ok"
        else:
            result["redis"] = "not_configured"
    except Exception as e:
        result["redis"] = f"error: {str(e)[:100]}"
        result["status"] = "degraded"

    status_code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=status_code)
