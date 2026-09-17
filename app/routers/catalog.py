"""Каталог: категории + уровни наличия."""
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import (PRODUCT_CATEGORIES, QUANTITY_BIG_MIN, QUANTITY_LEVELS,
                      QUANTITY_MEDIUM_MIN, QUANTITY_SMALL_MIN, Product,
                      Supply, SupplyItem)
from ..templating import render

router = APIRouter()

SORT_OPTIONS = [
    ("newest", "Сначала новые"),
    ("price_asc", "Цена ↑"),
    ("price_desc", "Цена ↓"),
    ("name", "По названию"),
]


def _stems_expr():
    return SupplyItem.stock * Product.package_size


def _base_query(db: Session):
    return (db.query(SupplyItem)
            .join(Product, SupplyItem.product_id == Product.id)
            .filter(SupplyItem.is_active == True))  # noqa: E712


def _apply_quantity_filter(query, level: str):
    stems = _stems_expr()
    if level == "big":
        return query.filter(stems >= QUANTITY_BIG_MIN)
    if level == "medium":
        return query.filter(stems >= QUANTITY_MEDIUM_MIN,
                            stems < QUANTITY_BIG_MIN)
    if level == "small":
        return query.filter(stems >= QUANTITY_SMALL_MIN,
                            stems < QUANTITY_MEDIUM_MIN)
    if level == "out":
        return query.filter(stems < QUANTITY_SMALL_MIN)
    return query.filter(stems >= QUANTITY_SMALL_MIN)


def _apply_filters(query, q, country, category, min_price, max_price,
                   min_length, max_length):
    if q:
        pattern = f"%{q}%"
        query = query.filter(or_(
            Product.name.ilike(pattern),
            Product.description.ilike(pattern),
        ))
    if country:
        query = query.filter(Product.country == country)
    if category:
        query = query.filter(Product.category == category)
    if min_price is not None:
        query = query.filter(SupplyItem.price >= min_price)
    if max_price is not None:
        query = query.filter(SupplyItem.price <= max_price)
    if min_length is not None:
        query = query.filter(Product.length_cm >= min_length)
    if max_length is not None:
        query = query.filter(Product.length_cm <= max_length)
    return query


def _apply_sort(query, sort: str):
    if sort == "price_asc":
        return query.order_by(SupplyItem.price.asc())
    if sort == "price_desc":
        return query.order_by(SupplyItem.price.desc())
    if sort == "name":
        return query.order_by(Product.name.asc())
    return query.order_by(SupplyItem.created_at.desc(), SupplyItem.id.desc())


def _page_range(current: int, total: int, window: int = 2) -> list[int]:
    return list(range(max(1, current - window),
                      min(total, current + window) + 1))


def _make_page_url(request: Request):
    def page_url(page: int) -> str:
        params = {k: v for k, v in request.query_params.items() if v}
        params["page"] = str(page)
        return f"/catalog?{urlencode(params)}"
    return page_url


def _count_by_level(db: Session, category: str) -> dict[str, int]:
    stems = _stems_expr()
    base = (db.query(SupplyItem)
            .join(Product, SupplyItem.product_id == Product.id)
            .filter(SupplyItem.is_active == True))  # noqa: E712
    if category:
        base = base.filter(Product.category == category)
    return {
        "available": base.filter(stems >= QUANTITY_SMALL_MIN).count(),
        "big": base.filter(stems >= QUANTITY_BIG_MIN).count(),
        "medium": base.filter(stems >= QUANTITY_MEDIUM_MIN,
                              stems < QUANTITY_BIG_MIN).count(),
        "small": base.filter(stems >= QUANTITY_SMALL_MIN,
                             stems < QUANTITY_MEDIUM_MIN).count(),
        "out": base.filter(stems < QUANTITY_SMALL_MIN).count(),
    }


def _category_url(current: dict, new_category: str) -> str:
    params = {}
    if new_category:
        params["category"] = new_category
    if current.get("level") and current["level"] != "available":
        params["level"] = current["level"]
    if current.get("q"):
        params["q"] = current["q"]
    if current.get("country"):
        params["country"] = current["country"]
    return "/catalog" + ("?" + urlencode(params) if params else "")


def _level_url(current: dict, new_level: str) -> str:
    params = {}
    if current.get("category"):
        params["category"] = current["category"]
    if new_level and new_level != "available":
        params["level"] = new_level
    if current.get("q"):
        params["q"] = current["q"]
    if current.get("country"):
        params["country"] = current["country"]
    return "/catalog" + ("?" + urlencode(params) if params else "")


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    q: str = "",
    country: str = "",
    category: str = "",
    level: str = "available",
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
    q, country, category = q.strip(), country.strip(), category.strip()

    valid_levels = {k for k, _ in QUANTITY_LEVELS}
    if level not in valid_levels:
        level = "available"

    query = _base_query(db)
    query = _apply_quantity_filter(query, level)
    query = _apply_filters(query, q, country, category, min_price, max_price,
                           min_length, max_length)
    query = _apply_sort(query, sort)

    page_size = config.PAGE_SIZE
    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)

    items = query.offset((page - 1) * page_size).limit(page_size).all()

    countries = sorted({
        c[0] for c in db.query(Product.country).distinct().all() if c[0]
    })

    active_categories = sorted({
        row[0] for row in
        db.query(Product.category)
        .join(SupplyItem, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.is_active == True)  # noqa: E712
        .distinct().all() if row[0]
    })

    level_counts = _count_by_level(db, category)

    active_supplies = (
        db.query(Supply)
        .filter(Supply.status.in_(["Ожидается", "В пути"]))
        .order_by(Supply.arrival_date.asc())
        .all()
    )

    current_filters = {
        "q": q, "country": country, "category": category, "level": level,
        "min_price": min_price, "max_price": max_price,
        "min_length": min_length, "max_length": max_length,
        "sort": sort,
    }

    return render(request, "catalog.html", db,
                  user=user,
                  items=items,
                  countries=countries,
                  active_categories=active_categories,
                  all_categories=PRODUCT_CATEGORIES,
                  quantity_levels=QUANTITY_LEVELS,
                  level_counts=level_counts,
                  sort_options=SORT_OPTIONS,
                  current_filters=current_filters,
                  page=page, total_pages=total_pages, total_items=total,
                  page_range=_page_range(page, total_pages),
                  page_url=_make_page_url(request),
                  has_filters=any([q, country, category, min_price,
                                   max_price, min_length, max_length,
                                   level != "available"]),
                  active_supplies=active_supplies,
                  category_url=lambda c: _category_url(current_filters, c),
                  level_url=lambda l: _level_url(current_filters, l))


@router.get("/product/{item_id}", response_class=HTMLResponse)
async def product_detail(item_id: int, request: Request,
                         db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    item = (db.query(SupplyItem)
            .join(Product, SupplyItem.product_id == Product.id)
            .filter(SupplyItem.id == item_id,
                    SupplyItem.is_active == True)  # noqa: E712
            .first())
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    return render(request, "product_detail.html", db,
                  user=user, item=item, product=item.product,
                  quantity_levels=QUANTITY_LEVELS)