# Каталог: публичная витрина (без цен) + полный режим для авторизованных.
#
# Фильтры «вид», «цвет», «рост», «страна» — это ОТДЕЛЬНЫЕ ТАБЛИЦЫ (kinds, colors,
# heights, countries). В адресе они передаются по id и могут повторяться:
#     /catalog?kind=1&color=3&color=5&height=2&country=4
# Старые ссылки с текстом (?category=Роза, ?country=Эквадор) продолжают работать:
# названия переводятся в id по справочнику.
from typing import Optional
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..deps import get_current_user
from ..models import (
    QUANTITY_BIG_MIN, QUANTITY_LEVELS, QUANTITY_MEDIUM_MIN, QUANTITY_SMALL_MIN,
    Color, Country, Height, Kind, Product, Supply, SupplyItem,
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
# (строка на товар, группы по видам). Выбор запоминается в cookie.
VIEWS = ("cards", "list")
VIEW_COOKIE = "catalog_view"
LIST_PAGE_SIZE = 50

# Фильтры-справочники: ключ в адресе → (колонка товара, модель справочника)
FACETS = {
    "kind": (Product.kind_id, Kind),
    "color": (Product.color_id, Color),
    "height": (Product.height_id, Height),
    "country": (Product.country_id, Country),
}
FACET_TITLES = {"kind": "Вид", "color": "Цвет", "height": "Рост", "country": "Страна"}
MAX_IDS_PER_FACET = 50


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


# ───────────────────────── разбор фильтров из адреса ─────────────────────────

def _split(values) -> list[str]:
    """['1,2', '3'] → ['1', '2', '3']; пустое отбрасывается."""
    out = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()[:100]
            if part:
                out.append(part)
    return out


def _is_id(text: str) -> bool:
    return text.isdigit() and len(text) <= 9


def _resolve_ids(db: Session, key: str, values) -> list[int]:
    """id из адреса; текст (старые ссылки) переводится в id по названию."""
    parts = _split(values)
    ids = [int(p) for p in parts if _is_id(p)]
    names = [p.casefold() for p in parts if not _is_id(p)]
    if names and key != "height":              # у роста нет названия — только id
        model = FACETS[key][1]
        # справочники маленькие: сравниваем в Python, чтобы не зависеть от
        # того, как СУБД приводит кириллицу к нижнему регистру
        for row_id, row_name in db.query(model.id, model.name).all():
            if (row_name or "").casefold() in names:
                ids.append(row_id)
    seen, result = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            result.append(i)
    return result[:MAX_IDS_PER_FACET]


def _read_selection(db: Session, request: Request) -> dict:
    qp = request.query_params
    return {
        # ?category= — старое имя параметра «вид»
        "kind": _resolve_ids(db, "kind", qp.getlist("kind") + qp.getlist("category")),
        "color": _resolve_ids(db, "color", qp.getlist("color")),
        "height": _resolve_ids(db, "height", qp.getlist("height")),
        "country": _resolve_ids(db, "country", qp.getlist("country")),
    }


# ───────────────────────────── условия запроса ─────────────────────────────

def _search_condition(q: str):
    """Поиск: название, артикул, штрихкод, описание и названия из справочников
    («роза», «белый», «эквадор»); число до трёх цифр — ещё и рост («60»)."""
    pattern = contains_pattern(q)

    def named(model):
        return select(model.id).where(model.name.ilike(pattern, escape=LIKE_ESCAPE))

    conditions = [
        Product.name.ilike(pattern, escape=LIKE_ESCAPE),
        Product.sku.ilike(pattern, escape=LIKE_ESCAPE),
        Product.barcode.ilike(pattern, escape=LIKE_ESCAPE),
        Product.description.ilike(pattern, escape=LIKE_ESCAPE),
        Product.kind_id.in_(named(Kind)),
        Product.color_id.in_(named(Color)),
        Product.country_id.in_(named(Country)),
    ]
    if q.isdigit() and len(q) <= 3:
        conditions.append(Product.height_id.in_(select(Height.id).where(Height.cm == int(q))))
    return or_(*conditions)


def _apply_filters(query, q, sel, min_price, max_price, skip=()):
    """skip — какие фильтры-справочники не применять (нужно для счётчиков)."""
    if q:
        query = query.filter(_search_condition(q))
    for key, (column, _model) in FACETS.items():
        ids = sel.get(key)
        if ids and key not in skip:
            query = query.filter(column.in_(ids))
    if min_price is not None:
        query = query.filter(SupplyItem.price >= min_price)
    if max_price is not None:
        query = query.filter(SupplyItem.price <= max_price)
    return query


# Прайс-лист идёт группами по видам — в порядке, заданном в «Фильтрах каталога».
# Товар без вида лежит в группе «Прочее».
_KIND_ORDER = select(Kind.sort_order).where(Kind.id == Product.kind_id).correlate(Product).scalar_subquery()
_KIND_NAME = select(Kind.name).where(Kind.id == Product.kind_id).correlate(Product).scalar_subquery()


def _other_kind_order(db: Session) -> int:
    order = db.query(Kind.sort_order).filter(Kind.name == "Прочее").scalar()
    return 9999 if order is None else order


def _apply_sort(query, sort: str, grouped: bool = False, other_order: int = 9999):
    if sort == "price_asc":
        return query.order_by(SupplyItem.price.asc(), SupplyItem.id.asc())
    if sort == "price_desc":
        return query.order_by(SupplyItem.price.desc(), SupplyItem.id.asc())
    if sort == "name":
        return query.order_by(Product.name.asc(), SupplyItem.id.asc())
    if grouped:
        return query.order_by(func.coalesce(_KIND_ORDER, other_order),
                              func.coalesce(_KIND_NAME, "Прочее"),
                              Product.name.asc(), SupplyItem.id.asc())
    return query.order_by(SupplyItem.created_at.desc(), SupplyItem.id.desc())


def _make_page_url(request: Request):
    pairs = [(k, v) for k, v in request.query_params.multi_items()
             if v and k != "page"]

    def page_url(page: int) -> str:
        return "/catalog?" + urlencode(pairs + [("page", str(page))])
    return page_url


def _count_by_level(db: Session, q, sel, min_price, max_price) -> dict:
    """Сколько позиций на каждом уровне остатка — при ТЕКУЩИХ фильтрах."""
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
    query = _apply_filters(query, q, sel, min_price, max_price)
    row = query.one()
    return dict(zip(("available", "big", "medium", "small", "out"),
                    (int(v or 0) for v in row)))


def _facet_counts(db: Session, key: str, level: str, q, sel, min_price,
                  max_price) -> dict:
    """{id значения: сколько позиций увидит клиент, выбрав его}. Считается при
    текущем остатке, поиске и ДРУГИХ фильтрах, но без самого этого фильтра —
    поэтому выбранные значения не «обнуляют» соседей."""
    column = FACETS[key][0]
    query = (
        db.query(column, func.count(SupplyItem.id))
        .select_from(SupplyItem)
        .join(Product, SupplyItem.product_id == Product.id)
        .filter(SupplyItem.is_active.is_(True))
    )
    query = _apply_quantity_filter(query, level)
    query = _apply_filters(query, q, sel, min_price, max_price, skip=(key,))
    return {fid: int(cnt) for fid, cnt in query.group_by(column).all()
            if fid is not None}


# ──────────────────────────────── ссылки ────────────────────────────────

# Что сохраняется в ссылках фильтров (цена и длина в интерфейсе не выбираются)
_KEEP = ("kind", "color", "height", "country", "level", "q", "sort")


def _catalog_url(current: dict, **change) -> str:
    """Ссылка на каталог: текущие фильтры + то, что меняем. Значения по умолчанию
    (все остатки, сортировка «новые») в адрес не пишем; списки id повторяют ключ."""
    params = {k: current.get(k) for k in _KEEP}
    params.update(change)
    if params.get("level") == "available":
        params["level"] = ""
    if params.get("sort") == "newest":
        params["sort"] = ""
    clean = {k: v for k, v in params.items() if v}
    # quote (а не quote_plus): пробел в адресе пишется как %20, как и раньше
    return "/catalog" + ("?" + urlencode(clean, doseq=True, quote_via=quote)
                         if clean else "")


def _toggle_url(current: dict, key: str, value: int) -> str:
    """Ссылка «включить/выключить» одно значение фильтра (множественный выбор)."""
    chosen = list(current.get(key) or [])
    if value in chosen:
        chosen.remove(value)
    else:
        chosen.append(value)
    return _catalog_url(current, **{key: chosen})


def _options(db: Session, key: str, counts: dict, chosen: list, current: dict) -> list:
    """Значения фильтра для шаблона: id, название, сколько товаров, выбрано ли,
    ссылка. Пустые значения скрываются — кроме выбранных."""
    model = FACETS[key][1]
    query = db.query(model)
    if key == "height":
        query = query.order_by(Height.cm.asc())
    else:
        query = query.order_by(model.sort_order.asc(), model.name.asc())
    result = []
    for row in query.all():
        count = counts.get(row.id, 0)
        selected = row.id in chosen
        if not count and not selected:
            continue
        result.append({
            "id": row.id,
            "name": row.name,
            "count": count,
            "selected": selected,
            "hex": getattr(row, "hex", "") or "",
            "url": _toggle_url(current, key, row.id),
        })
    return result


@router.get("/catalog", response_class=HTMLResponse)
def catalog(
    request: Request,
    q: str = "",
    level: str = "available",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
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
    # Поисковая строка не должна быть бесконечно длинной: длинный запрос
    # тормозит ILIKE-поиск и бесполезен для пользователя.
    q = q.strip()[:100]

    # Фильтры-справочники (kind / color / height / country) — списки id
    sel = _read_selection(db, request)

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
    query = _apply_filters(query, q, sel, min_price, max_price)
    group_by_kind = view == "list" and sort == "newest"
    query = _apply_sort(query, sort, grouped=group_by_kind,
                        other_order=_other_kind_order(db) if group_by_kind else 9999)

    page_size = LIST_PAGE_SIZE if view == "list" else config.PAGE_SIZE
    total = query.count()
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)

    items = query.offset((page - 1) * page_size).limit(page_size).all()

    preorder_items = []
    if (user and page == 1 and not q and not any(sel.values())
            and level == "available"):
        preorder_items = (
            _preorder_query(db)
            .order_by(Supply.arrival_date.asc(), Product.name.asc())
            .limit(12)
            .all()
        )

    # Для ссылок и подписей в шаблоне
    current_filters = {
        "q": q, "level": level, "sort": sort, "view": view,
        "min_price": min_price, "max_price": max_price,
        "kind": sel["kind"], "color": sel["color"],
        "height": sel["height"], "country": sel["country"],
    }

    common = (q, sel, min_price, max_price)
    options = {
        key: _options(db, key, _facet_counts(db, key, level, *common),
                      sel[key], current_filters)
        for key in FACETS
    }

    level_counts = _count_by_level(db, *common)

    active_supplies = (
        db.query(Supply)
        .filter(Supply.status.in_(["Ожидается", "В пути"]))
        .order_by(Supply.arrival_date.asc())
        .all()
    )

    # «Цены и остатки обновлены …»: когда каталог последний раз успешно пришёл
    # из 1С (или файлом). Пока обмена не было — строку не показываем.
    last_update = exchange_log.last_catalog_update(db)

    response = render(
        request, "catalog.html", db,
        user=user,
        public=public,
        items=items,
        preorder_items=preorder_items,
        # фильтры-справочники: списки {id, name, count, selected, hex, url}
        filter_groups=[(title, key, options[key]) for key, title in FACET_TITLES.items()],
        has_filter_options=any(options.values()),
        selected=sel,
        picked_filters=sum(len(ids) for ids in sel.values()),
        quantity_levels=QUANTITY_LEVELS,
        level_counts=level_counts,
        sort_options=SORT_OPTIONS,
        current_filters=current_filters,
        search_q=q,
        view=view,
        group_by_kind=group_by_kind,
        my_discount=(float(user.discount_percent or 0) if user else 0.0),
        catalog_updated_at=(last_update.created_at if last_update else None),
        page=page, total_pages=total_pages, total_items=total,
        page_url=_make_page_url(request),
        has_filters=any([
            q, sel["kind"], sel["color"], sel["height"], sel["country"],
            min_price, max_price, level != "available",
        ]),
        active_supplies=active_supplies,
        preorder_max_packs=MAX_PREORDER_PACKS,
        level_url=lambda l: _catalog_url(current_filters, level=l),
        sort_url=lambda s: _catalog_url(current_filters, sort=s),
        view_url=lambda v: _catalog_url(current_filters, view=v),
        clear_search_url=_catalog_url(current_filters, q=""),
        clear_filters_url=_catalog_url(
            current_filters, kind=[], color=[], height=[], country=[]),
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

    pattern = contains_pattern(q)
    rows = (
        db.query(Product.name, SupplyItem.id.label("si"))
        .join(SupplyItem, SupplyItem.product_id == Product.id)
        .filter(
            SupplyItem.is_active.is_(True),
            or_(Product.name.ilike(pattern, escape=LIKE_ESCAPE),
                Product.sku.ilike(pattern, escape=LIKE_ESCAPE),
                Product.barcode.ilike(pattern, escape=LIKE_ESCAPE)),
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