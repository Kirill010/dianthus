"""Сборка приложения и общие маршруты."""
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import router as auth_router
from .bootstrap import ensure_default_admin
from .config import config
from .database import Base, engine, get_db
from .deps import get_current_user
from .migrations import auto_migrate, ensure_notifications_table
from .templating import render
from .routers import admin, cart, catalog, profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

try:
    Base.metadata.create_all(bind=engine)
    auto_migrate()
    ensure_notifications_table()
    db_name = (config.DATABASE_URL.split("@")[-1]
               if "@" in config.DATABASE_URL else config.DATABASE_URL)
    logger.info("✅ Схема БД готова (%s)", db_name)
except Exception as e:
    logger.error("❌ Не удалось подключиться к БД: %s", e)
    raise

app = FastAPI(title="Диантус — оптовый магазин цветов")

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


def _check_vendor() -> None:
    vendor = STATIC_DIR / "vendor"
    needed = ["bootstrap.min.css", "bootstrap.bundle.min.js",
              "fontawesome/css/all.min.css"]
    missing = [n for n in needed if not (vendor / n).exists()]
    if missing:
        logger.warning(
            "⚠️  Не найдены vendor-файлы: %s. Запустите: "
            "python download_vendor.py", ", ".join(missing))


_check_vendor()

app.include_router(auth_router)
app.include_router(catalog.router)
app.include_router(cart.router)
app.include_router(admin.router)
app.include_router(profile.router)

ensure_default_admin()


@app.get("/", response_class=HTMLResponse)
async def root(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return render(request, "login.html", db,
                  register_errors=request.session.pop("register_errors", None),
                  register_data=request.session.pop("register_data", {}),
                  login_error=request.session.pop("login_error", None))


@app.get("/registration_pending", response_class=HTMLResponse)
async def registration_pending(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db):
        return RedirectResponse(url="/catalog", status_code=303)
    return render(request, "registration_pending.html", db)


@app.get("/health")
async def health():
    return {"status": "ok", "env": config.ENV}


logger.info("🌸 Диантус готов (%s)", config.ENV)