# Каталог: публичная витрина (без цен) + полный режим для авторизованных.
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import (
    PRODUCT_CATEGORIES, QUANTITY_BIG_MIN, QUANTITY_LEVELS,
    QUANTITY_MEDIUM_MIN, QUANTITY_SMALL_MIN, Product, Supply, SupplyItem,
)
from ..security import check_rate_limit
from ..services import exchange_log
from ..services.preorder_service import MAX_PREORDER_PACKS
from ..templating import render
from ..web import LIKE_ESCAPE, contains_pattern

router = APIRouter()

SORT_OPTIONS = [
    ("newest", "Сначала новые"),
    ("price_asc", "Цена ↑"),
    ("price_desc", "Цена ↓"),
    ("name", "По названию"),
]

# Два вида каталога: «cards» — карточки с фото, «list» — компактный прайс-лист
# (строка на товар, группы по категориям). Выбор запоминается в cookie.
VIEWS = ("cards", "list")
VIEW_COOKIE = "catalog_view"
LIST_PAGE_SIZE = 50


def _stems_expr():
    return (func.coalesce(SupplyItem.stock, 0)
            - func.coalesce(SupplyItem.reserved_stock, 0)) * Product.package_size

def _base_query(db: Session):
    return (
        db.query(SupplyItem)
        .options(
            joinedload(SupplyItem.product),
            selectinload(SupplyItem.supply),
        )
        .join(Product, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.is_active.is_(True))
    )

def _preorder_query(db: Session):
    return (
        db.query(SupplyItem)
        .options(
            joinedload(SupplyItem.product),
            selectinload(SupplyItem.supply),
        )
        .join(Product, SupplyItem.product_id == Product.id)
        .join(Supply, SupplyItem.supply_id == Supply.id)
        .filter(
            SupplyItem.is_active.is_(False),
            Supply.status.in_(["Ожидается", "В пути"]),
        )
    )

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
        pattern = contains_pattern(q)
        query = query.filter(or_(
            Product.name.ilike(pattern, escape=LIKE_ESCAPE),
            Product.description.ilike(pattern, escape=LIKE_ESCAPE),
            Product.country.ilike(pattern, escape=LIKE_ESCAPE),
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

def _apply_sort(query, sort: str, grouped: bool = False):
    if sort == "price_asc":
        return query.order_by(SupplyItem.price.asc(), SupplyItem.id.asc())
    if sort == "price_desc":
        return query.order_by(SupplyItem.price.desc(), SupplyItem.id.asc())
    if sort == "name":
        return query.order_by(Product.name.asc(), SupplyItem.id.asc())
    if grouped:
        # Прайс-лист: группы по категориям, внутри группы — по алфавиту
        return query.order_by(Product.category.asc(), Product.name.asc(),
                              SupplyItem.id.asc())
    return query.order_by(SupplyItem.created_at.desc(), SupplyItem.id.desc())

def _make_page_url(request: Request):
    def page_url(page: int) -> str:
        params = {k: v for k, v in request.query_params.items() if v}
        params["page"] = str(page)
        return f"/catalog?{urlencode(params)}"
    return page_url

def _count_by_level(db: Session, q, country, category, min_price, max_price,
                    min_length, max_length) -> dict:
    """Сколько позиций на каждом уровне остатка — при ТЕКУЩЕМ поиске и категории
    (раньше цифры не зависели от поиска и врали). Один запрос вместо пяти."""
    stems = _stems_expr()

    def n(cond):
        return func.coalesce(func.sum(case((cond, 1), else_=0)), 0)

    query = (
        db.query(
            n(stems >= QUANTITY_SMALL_MIN),
            n(stems >= QUANTITY_BIG_MIN),
            n(and_(stems >= QUANTITY_MEDIUM_MIN, stems < QUANTITY_BIG_MIN)),
            n(and_(stems >= QUANTITY_SMALL_MIN, stems < QUANTITY_MEDIUM_MIN)),
            n(stems < QUANTITY_SMALL_MIN),
        )
        .select_from(SupplyItem)
        .join(Product, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.is_active.is_(True))
    )
    query = _apply_filters(query, q, country, category, min_price, max_price,
                           min_length, max_length)
    row = query.one()
    return dict(zip(("available", "big", "medium", "small", "out"),
                    (int(v or 0) for v in row)))

def _category_counts(db: Session, level: str, q, country, min_price, max_price,
                     min_length, max_length) -> dict:
    """{категория: сколько позиций увидит клиент, нажав на неё}. Считается при
    текущем уровне остатка и поиске, но БЕЗ выбранной категории."""
    query = (
        db.query(Product.category, func.count(SupplyItem.id))
        .select_from(SupplyItem)
        .join(Product, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.is_active.is_(True))
    )
    query = _apply_quantity_filter(query, level)
    query = _apply_filters(query, q, country, "", min_price, max_price,
                           min_length, max_length)
    return {(cat or ""): int(cnt) for cat, cnt in query.group_by(Product.category).all()}

def _category_chips(counts: dict, selected: str) -> list:
    """Кнопки категорий: сначала привычные (в порядке справочника), затем
    остальные из 1С по алфавиту. Пустые не показываем — кроме выбранной."""
    names = [c for c in PRODUCT_CATEGORIES if counts.get(c)]
    names += sorted(c for c, n in counts.items()
                    if c and n and c not in PRODUCT_CATEGORIES)
    if selected and selected not in names:
        names.append(selected)
    return [(name, counts.get(name, 0)) for name in names]

# Что сохраняется в ссылках фильтров (цена и длина в интерфейсе не выбираются)
_KEEP = ("category", "level", "q", "country", "sort")

def _catalog_url(current: dict, **change) -> str:
    """Ссылка на каталог: текущие фильтры + то, что меняем. Значения по умолчанию
    (все остатки, сортировка «новые») в адрес не пишем."""
    params = {k: current.get(k) for k in _KEEP}
    params.update(change)
    if params.get("level") == "available":
        params["level"] = ""
    if params.get("sort") == "newest":
        params["sort"] = ""
    clean = {k: v for k, v in params.items() if v}
    # quote (а не quote_plus): пробел в адресе пишется как %20, как и раньше
    return "/catalog" + ("?" + urlencode(clean, quote_via=quote) if clean else "")

@router.get("/catalog", response_class=HTMLResponse)
def catalog(
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
    view: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    # Публичный режим: аноним видит каталог без цен и без кнопок заказа —
    # это SEO-витрина и приглашение к регистрации.
    public = user is None

    page = max(1, page)
    sort = sort if any(sort == k for k, _ in SORT_OPTIONS) else "newest"
    q, country, category = q.strip(), country.strip(), category.strip()
    # Поисковая строка не должна быть бесконечно длинной: длинный запрос
    # тормозит ILIKE-поиск и бесполезен для пользователя.
    q, country, category = q[:100], country[:100], category[:100]

    valid_levels = {k for k, _ in QUANTITY_LEVELS}
    if level not in valid_levels:
        level = "available"

    # Вид: явный ?view= важнее cookie; без обоих — карточки.
    explicit_view = view.strip() in VIEWS
    view = view.strip() if explicit_view else request.cookies.get(VIEW_COOKIE, "")
    if view not in VIEWS:
        view = "cards"

    query = _base_query(db)
    query = _apply_quantity_filter(query, level)
    query = _apply_filters(
        query, q, country, category, min_price, max_price,
        min_length, max_length,
    )
    group_by_category = view == "list" and sort == "newest"
    query = _apply_sort(query, sort, grouped=group_by_category)

    page_size = LIST_PAGE_SIZE if view == "list" else config.PAGE_SIZE
    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)

    items = query.offset((page - 1) * page_size).limit(page_size).all()

    preorder_items = []
    if user and page == 1 and not q and not category and level == "available":
        preorder_items = (
            _preorder_query(db)
            .order_by(Supply.arrival_date.asc(), Product.name.asc())
            .limit(12)
            .all()
        )

    countries = sorted({
        c[0] for c in db.query(Product.country).distinct().all() if c[0]
    })

    filters = (q, country, min_price, max_price, min_length, max_length)
    category_counts = _category_counts(db, level, *filters)
    category_chips = _category_chips(category_counts, category)
    all_count = sum(category_counts.values())

    level_counts = _count_by_level(db, q, country, category, min_price,
                                   max_price, min_length, max_length)

    active_supplies = (
        db.query(Supply)
        .filter(Supply.status.in_(["Ожидается", "В пути"]))
        .order_by(Supply.arrival_date.asc())
        .all()
    )

    # «Цены и остатки обновлены …»: когда каталог последний раз успешно пришёл
    # из 1С (или файлом). Пока обмена не было — строку не показываем.
    last_update = exchange_log.last_catalog_update(db)

    current_filters = {
        "q": q, "country": country, "category": category, "level": level,
        "min_price": min_price, "max_price": max_price,
        "min_length": min_length, "max_length": max_length, "sort": sort,
        "view": view,
    }

    response = render(
        request, "catalog.html", db,
        user=user,
        public=public,
        items=items,
        preorder_items=preorder_items,
        countries=countries,
        category_chips=category_chips,
        all_count=all_count,
        quantity_levels=QUANTITY_LEVELS,
        level_counts=level_counts,
        sort_options=SORT_OPTIONS,
        current_filters=current_filters,
        search_q=q,
        view=view,
        group_by_category=group_by_category,
        my_discount=(float(user.discount_percent or 0) if user else 0.0),
        catalog_updated_at=(last_update.created_at if last_update else None),
        page=page, total_pages=total_pages, total_items=total,
        page_url=_make_page_url(request),
        has_filters=any([
            q, country, category, min_price, max_price,
            min_length, max_length, level != "available",
        ]),
        active_supplies=active_supplies,
        preorder_max_packs=MAX_PREORDER_PACKS,
        category_url=lambda c: _catalog_url(current_filters, category=c),
        level_url=lambda l: _catalog_url(current_filters, level=l),
        sort_url=lambda s: _catalog_url(current_filters, sort=s),
        view_url=lambda v: _catalog_url(current_filters, view=v),
        clear_search_url=_catalog_url(current_filters, q=""),
    )
    if explicit_view and request.cookies.get(VIEW_COOKIE) != view:
        # Запоминаем выбор на год: это удобство, а не слежка — в cookie только
        # слово «cards» или «list».
        response.set_cookie(
            VIEW_COOKIE, view, max_age=365 * 24 * 3600, path="/",
            samesite="lax", httponly=True, secure=(config.ENV == "prod"),
        )
    return response

async def _rate_suggest(request: Request) -> None:
    # Подсказки доступны и анонимам (публичный каталог), но с лимитом —
    # ILIKE-поиск по всем товарам не должен стать вектором абьюза.
    await check_rate_limit(request, "search-suggest", max_hits=60, window=60)


@router.get("/api/search-suggest", dependencies=[Depends(_rate_suggest)])
def search_suggest(
    q: str = "",
    db: Session = Depends(get_db),
):
    # Обычная `def` (а не async): запрос к БД синхронный, FastAPI выполнит
    # его в пуле потоков, и поиск «на лету» не будет замораживать сайт.
    q = (q or "").strip()[:100]
    if len(q) < 2:
        return JSONResponse({"items": []})

    rows = (
        db.query(Product.name, SupplyItem.id.label("si"))
        .join(SupplyItem, SupplyItem.product_id == Product.id)
        .filter(
            SupplyItem.is_active.is_(True),
            Product.name.ilike(contains_pattern(q), escape=LIKE_ESCAPE),
        )
        .limit(8)
        .all()
    )
    return JSONResponse({
        "items": [
            {"name": r.name, "url": f"/product/{r.si}"}
            for r in rows
        ]
    })

@router.get("/product/{item_id}", response_class=HTMLResponse)
def product_detail(
    item_id: int, request: Request, db: Session = Depends(get_db)
):
    user = get_current_user(request, db)
    # Публичный режим: аноним видит карточку товара без цены и без заказа.
    public = user is None

    item = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product), joinedload(SupplyItem.supply))
        .join(Product, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.id == item_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Товар не найден")

    # Товар «в пути» (позиция ещё не на полке) тоже можно открыть — каталог сам
    # ведёт на него из блока предзаказов. Раньше такая ссылка давала «404».
    preorder_mode = False
    if not item.is_active:
        supply = item.supply
        if supply is not None and not supply.is_service and supply.status in (
                "Ожидается", "В пути"):
            preorder_mode = True
        else:
            raise HTTPException(status_code=404, detail="Товар не найден")

    return render(
        request, "product_detail.html", db,
        user=user, public=public, item=item, product=item.product,
        quantity_levels=QUANTITY_LEVELS, preorder_mode=preorder_mode,
        preorder_max_packs=MAX_PREORDER_PACKS,
    )
