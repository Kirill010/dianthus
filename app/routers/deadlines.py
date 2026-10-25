# app/routers/deadlines.py
"""«Сроки заказа»: до какого числа заказать цветы к нужной дате и к праздникам.

Страница открыта всем (без входа): это и подсказка клиентам, и повод зайти на
сайт перед 8 Марта. Расчёты — в services/preorder_rules.py.
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import preorder_rules as rules_lib
from ..templating import render
from ..timeutils import shop_tz

router = APIRouter()


@router.get("/deadlines", response_class=HTMLResponse)
def deadlines_page(request: Request, date: str = "", db: Session = Depends(get_db)):
    rules = rules_lib.lead_days(db)
    today = datetime.now(shop_tz()).date()

    chosen = rules_lib.parse_user_date(date) if date.strip() else None
    error = ""
    table = []
    if date.strip() and chosen is None:
        error = "Дата указана неверно — выберите её в календаре."
    elif chosen is not None and chosen < today:
        error, chosen = "Эта дата уже прошла.", None
    elif chosen is not None:
        if chosen > today + timedelta(days=730):
            error, chosen = "Выберите дату в пределах двух лет.", None
        else:
            table = rules_lib.table_for_delivery(rules, chosen, today)

    return render(
        request, "deadlines.html", db,
        rules=rules_lib.group_by_days(rules),
        today=today, chosen=chosen,
        chosen_text=rules_lib.fmt_date(chosen) if chosen else "",
        table=table, error=error,
        peaks=rules_lib.upcoming_peaks(rules, today, 240) if rules else [],
        min_date=today.isoformat(),
    )
