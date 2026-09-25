# app/main.py
"""Сборка приложения и общие маршруты."""
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import import_module
from pathlib import Path

from fastapi import Depends, FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .bootstrap import ensure_default_admin
from .config import config
from .database import Base, engine, get_db
from .deps import get_current_user
from .migrations import auto_migrate, ensure_notifications_table
from .scheduler import start_scheduler, stop_scheduler
from .sentry_config import init_sentry
from . import security
from .security import CsrfError, init_rate_limiter, close_rate_limiter
from .templating import render
from .middleware import SecurityHeadersMiddleware


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

init_sentry()


# fix #67: безопасная загрузка роутеров — если один упал, сервис продолжит
# работать, а в лог попадёт понятная причина.
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
        Base.metadata.create_all(bind=engine)
        auto_migrate()
        ensure_notifications_table()
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
    try:
        yield
    finally:
        # fix: корректное завершение за отведённое systemd время
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
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    max_age=60 * 60 * 24 * 7,
    same_site="lax",
    https_only=(config.ENV == "prod"),
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "uploads").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_check_vendor()

# ── РОУТЕРЫ ──
app.include_router(auth_router)

for _name, _mod in _routers.items():
    try:
        app.include_router(_mod.router)
        logger.info("✅ Подключён роутер: %s", _name)
    except Exception as e:
        logger.exception("❌ Ошибка include_router(%s): %s", _name, e)

ensure_default_admin()


# ═══════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ ОШИБОК
# ═══════════════════════════════════════════════════════════

@app.exception_handler(CsrfError)
async def csrf_error_handler(request: Request, exc: CsrfError):
    request.session["flash"] = f"⚠️ {exc.message}"
    referer = request.headers.get("referer") or "/login"
    return RedirectResponse(url=referer, status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in (301, 302, 303, 307, 308):
        return RedirectResponse(
            url=exc.headers.get("Location", "/login"),
            status_code=exc.status_code,
        )

    if request.headers.get("accept", "").startswith("text/html"):
        titles = {
            403: "Доступ запрещён",
            404: "Страница не найдена",
            429: "Слишком много запросов",
            500: "Ошибка сервера",
        }
        title = titles.get(exc.status_code, "Ошибка")
        db = next(get_db())
        try:
            return render(
                request, "error.html", db,
                code=exc.status_code, title=title,
                message=exc.detail or "",
            )
        finally:
            db.close()

    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception(
        "💥 Необработанная ошибка на %s: %s", request.url.path, exc
    )

    if request.headers.get("accept", "").startswith("text/html"):
        try:
            db = next(get_db())
            try:
                return render(
                    request, "error.html", db,
                    code=500, title="Ошибка сервера",
                    message="Мы уже знаем о проблеме и чиним её.",
                )
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
async def root(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if user:
        return RedirectResponse(url="/catalog", status_code=303)
    return render(
        request, "login.html", db,
        user=None,
        register_errors=request.session.pop("register_errors", None),
        register_data=request.session.pop("register_data", {}),
        login_error=request.session.pop("login_error", None),
    )


@app.get("/registration_pending", response_class=HTMLResponse)
async def registration_pending(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return render(request, "registration_pending.html", db)


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    result = {
        "status": "ok",
        "env": config.ENV,
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        db.execute(text("SELECT 1"))
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


logger.info("🌸 Диантус готов (%s)", config.ENV)