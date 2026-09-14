"""Каталог, карточка товара, фильтры, пагинация."""
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..templating import render
from .. import models

router = APIRouter()

SORT_OPTIONS = [
    ("newest", "Сначала новые"),
    ("price_asc", "Цена: по возрастанию"),
    ("price_desc", "Цена: по убыванию"),
    ("name", "По названию"),
]


def _apply_filters(query, q, country, min_price, max_price,
                   min_length, max_length):
    if q:
        pattern = f"%{q}%"
        query = query.filter(or_(
            models.Product.name.ilike(pattern),
            models.Product.description.ilike(pattern),
        ))
    if country:
        query = query.filter(models.Product.country == country)
    if min_price is not None:
        query = query.filter(models.Product.price >= min_price)
    if max_price is not None:
        query = query.filter(models.Product.price <= max_price)
    if min_length is not None:
        query = query.filter(models.Product.length_cm >= min_length)
    if max_length is not None:
        query = query.filter(models.Product.length_cm <= max_length)
    return query


def _apply_sort(query, sort: str):
    if sort == "price_asc":
        return query.order_by(models.Product.price.asc())
    if sort == "price_desc":
        return query.order_by(models.Product.price.desc())
    if sort == "name":
        return query.order_by(models.Product.name.asc())
    return query.order_by(models.Product.created_at.desc(),
                          models.Product.id.desc())


def _page_range(current: int, total: int, window: int = 2) -> list[int]:
    return list(range(max(1, current - window),
                      min(total, current + window) + 1))


def _make_page_url(request: Request):
    def page_url(page: int) -> str:
        params = {k: v for k, v in request.query_params.items() if v}
        params["page"] = str(page)
        return f"/catalog?{urlencode(params)}"
    return page_url


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    q: str = "",
    country: str = "",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_length: Optional[int] = None,
    max_length: Optional[int] = None,
    sort: str = "newest",
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    page = max(1, page)
    sort = sort if any(sort == k for k, _ in SORT_OPTIONS) else "newest"
    q, country = q.strip(), country.strip()

    query = db.query(models.Product)
    query = _apply_filters(query, q, country, min_price, max_price,
                           min_length, max_length)
    query = _apply_sort(query, sort)

    page_size = config.PAGE_SIZE
    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)

    products = query.offset((page - 1) * page_size).limit(page_size).all()

    countries = sorted({
        c[0] for c in db.query(models.Product.country).distinct().all() if c[0]
    })

    active_supplies = (
        db.query(models.Supply)
        .filter(models.Supply.status.in_(["Ожидается", "В пути"]))
        .order_by(models.Supply.arrival_date.asc())
        .all()
    )

    current_filters = {
        "q": q, "country": country,
        "min_price": min_price, "max_price": max_price,
        "min_length": min_length, "max_length": max_length,
        "sort": sort,
    }

    return render(request, "catalog.html", db,
                  user=user,                # ← не делаем лишний запрос
                  products=products, countries=countries,
                  sort_options=SORT_OPTIONS,
                  current_filters=current_filters,
                  page=page, total_pages=total_pages, total_items=total,
                  page_range=_page_range(page, total_pages),
                  page_url=_make_page_url(request),
                  has_filters=any([q, country, min_price, max_price,
                                   min_length, max_length]),
                  active_supplies=active_supplies)


@router.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(product_id: int, request: Request,
                         db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    product = db.query(models.Product).filter(
        models.Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Товар не найден")

    return render(request, "product_detail.html", db,
                  user=user, product=product)