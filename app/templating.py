"""
Общий рендер шаблонов.

Пути строятся от файла — работает при запуске из любой папки.
"""
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .deps import get_current_user
from .security import ensure_csrf_token
from . import models


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def render(request: Request, template: str, db: Session, **context):
    """Готовит общий контекст и рендерит шаблон."""
    # Если роутер уже передал user — не делаем лишний запрос в БД
    user = context.pop("user", None)
    if user is None:
        user = get_current_user(request, db)

    flash = request.session.pop("flash", None)
    cart = request.session.get("cart", [])
    cart_count = sum(item["quantity"] for item in cart)

    csrf_token = ensure_csrf_token(request)

    active_supply = None
    if user:
        active_supply = (
            db.query(models.Supply)
            .filter(models.Supply.status.in_(["Ожидается", "В пути"]))
            .order_by(models.Supply.arrival_date.asc())
            .first()
        )

    context.update({
        "user": user,
        "flash": flash,
        "cart_count": cart_count,
        "active_supply": active_supply,
        "csrf_token": csrf_token,
    })

    return templates.TemplateResponse(
        request=request, name=template, context=context,
    )