# app/routers/integration_1c.py
"""
Обмен с 1С (Basic Auth + лимит запросов).

  POST /api/1c/products/sync     — приём каталога, цен и остатков из 1С
  GET  /api/1c/products/export   — выгрузка каталога сайта в 1С
  GET  /api/1c/orders/export     — выгрузка заказов сайта в 1С
  GET  /api/1c/ping              — проверка связи

Формат товара (одинаковый в обе стороны):

    {"id": 1, "external_id": "ROSA-FREEDOM-60", "sku": "ROSA-FREEDOM-60",
     "name": "Роза Freedom 60 см", "description": "Классическая красная роза",
     "country": "Эквадор", "length_cm": 60, "unit": "упаковка",
     "package_size": 25, "min_quantity": 1, "category": "Роза Эквадор",
     "price_per_stem": 42.5, "stock_packs": 120,
     "photos": ["https://dianthus-opt.ru/static/uploads/abc.webp"]}

ВНИМАНИЕ по единицам: price_per_stem — цена за ШТ (стебель),
stock_packs — остаток в УПАКОВКАХ, package_size — штук в упаковке.
"""
import ipaddress
import logging
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..models import ORDER_STATUSES, Order, Product, Supply, SupplyItem
from ..security import check_rate_limit

logger = logging.getLogger(__name__)
async def _rate_1c(request: Request) -> None:
    """Защита Basic Auth от перебора пароля: не более 120 запросов/мин с IP."""
    await check_rate_limit(request, "onec", max_hits=120, window=60)


router = APIRouter(
    prefix="/api/1c", tags=["1C Integration"],
    dependencies=[Depends(_rate_1c)],
)

_basic = HTTPBasic(auto_error=False)


# ═══════════════════════════════════════════════════════════
# SSRF PROTECTION
# ═══════════════════════════════════════════════════════════

_PRIVATE_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

_ALLOWED_IMAGE_DOMAINS = {
    "dianthus-opt.ru",
    "www.dianthus-opt.ru",
}


def _is_safe_url(url: str) -> bool:
    """Проверяет, что URL не ведёт на внутренние ресурсы (SSRF protection)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        hostname = parsed.hostname
        if not hostname:
            return False

        # Разрешаем только известные домены или внешние URL
        if hostname in _ALLOWED_IMAGE_DOMAINS:
            return True

        # Резолвим IP
        try:
            import socket
            ip = socket.gethostbyname(hostname)
            addr = ipaddress.ip_address(ip)
            for private_range in _PRIVATE_IP_RANGES:
                if addr in private_range:
                    logger.warning("SSRF: заблокирован URL на внутренний IP: %s", url)
                    return False
        except (socket.gaierror, ValueError):
            return False

        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════════════════

def _verify_basic(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> bool:
    auth_configured = bool(
        config.INTEGRATION_USER and config.INTEGRATION_PASSWORD
    )

    if not auth_configured:
        if config.ENV == "prod":
            logger.error("1С: INTEGRATION_USER/PASSWORD не заданы")
            raise HTTPException(
                status_code=503,
                detail="1С-интеграция не настроена на сервере.",
            )
        logger.warning("1С: INTEGRATION_PASSWORD не задан")
        raise HTTPException(
            status_code=503,
            detail="1С-интеграция отключена: задайте INTEGRATION_PASSWORD.",
        )

    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация Basic Auth.",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )

    user_ok = secrets.compare_digest(
        creds.username or "", config.INTEGRATION_USER
    )
    pass_ok = secrets.compare_digest(
        creds.password or "", config.INTEGRATION_PASSWORD
    )
    if not (user_ok and pass_ok):
        logger.warning(
            "1С: неверные учётные данные, IP=%s",
            request.client.host if request.client else "?",
        )
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль.",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )
    return True


# ═══════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

MAX_PRODUCTS_PER_SYNC = 20_000
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


def _normalize_photos(raw) -> list:
    """Приводит поле photo/photos из 1С к списку валидных URL-ов."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                for key in ("url", "photo", "image", "link"):
                    if key in item and item[key]:
                        parts.append(str(item[key]))
                        break
            elif item is not None:
                parts.append(str(item).strip())
    else:
        return []

    result = []
    for s in parts:
        if not s:
            continue
        low = s.lower()
        if any(low.startswith(bad) for bad in _BAD_SCHEMES):
            continue
        if s.startswith(("http://", "https://", "/static/")):
            # SSRF проверка
            if s.startswith(("http://", "https://")) and not _is_safe_url(s):
                continue
            result.append(s)
    return result[:20]


def _ensure_1c_supply(db: Session) -> Supply:
    supply = (
        db.query(Supply)
        .filter(
            Supply.status == "Ожидается",
            Supply.notes.like("%Авто-синхронизация из 1С%"),
        )
        .first()
    )
    if not supply:
        supply = Supply(
            country="1С",
            status="Ожидается",
            arrival_date=datetime.now(timezone.utc).replace(tzinfo=None),
            notes="Авто-синхронизация из 1С",
            is_service=True,
        )
        db.add(supply)
        db.flush()
        logger.info("1С: создана служебная поставка №%d", supply.id)
    return supply


def _extract_photos_from_item(item: dict) -> list:
    for key in ("photos", "photo", "image_url"):
        value = item.get(key)
        if value:
            photos = _normalize_photos(value)
            if photos:
                return photos
    return []


def _ext_id_of(item: dict) -> str:
    """Идентификатор товара в 1С: поле id / external_id / guid / code.

    Именно по нему товар сопоставляется при обмене (приоритет выше sku и
    названия), поэтому переименование номенклатуры в 1С не создаёт дубль.
    """
    for key in ("id", "external_id", "guid", "code"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value).strip()[:64]
    return ""


# ═══════════════════════════════════════════════════════════
# SYNC ENDPOINT
# ═══════════════════════════════════════════════════════════

@router.post("/products/sync")
def sync_products(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    client_ip = request.client.host if request.client else "?"

    products_data = payload.get("products", [])
    if not isinstance(products_data, list):
        raise HTTPException(400, "Поле 'products' должно быть массивом")
    if len(products_data) > MAX_PRODUCTS_PER_SYNC:
        raise HTTPException(
            413, f"Слишком много товаров за раз (максимум "
                 f"{MAX_PRODUCTS_PER_SYNC}). Разбейте выгрузку на части."
        )
    # Элементы не-объекты (null, числа, строки) пропускаем, а не валим весь обмен
    products_data = [it for it in products_data if isinstance(it, dict)]

    logger.info(
        "1С sync: ip=%s, товаров в запросе: %d",
        client_ip, len(products_data),
    )

    if not products_data:
        return JSONResponse({
            "status": "ok", "processed": 0, "created": 0,
            "updated": 0, "errors": [], "supply_id": None,
        })

    supply = _ensure_1c_supply(db)

    skus, names, ext_ids = [], [], []
    for it in products_data:
        s = str(it.get("sku", "")).strip()
        n = str(it.get("name", "")).strip()
        e = _ext_id_of(it)
        if s:
            skus.append(s)
        if n:
            names.append(n)
        if e:
            ext_ids.append(e)

    existing_by_sku: dict[str, Product] = {}
    existing_by_name: dict[str, Product] = {}
    existing_by_ext: dict[str, Product] = {}
    if skus or names or ext_ids:
        conds = []
        if skus:
            conds.append(Product.sku.in_(skus))
        if names:
            conds.append(Product.name.in_(names))
        if ext_ids:
            conds.append(Product.external_id.in_(ext_ids))
        for p in db.query(Product).filter(or_(*conds)).all():
            if p.sku:
                existing_by_sku[p.sku] = p
            if p.external_id:
                existing_by_ext[p.external_id] = p
            existing_by_name[p.name] = p

    processed = created = updated = 0
    errors: list[str] = []

    for idx, item in enumerate(products_data, start=1):
        try:
            sku = str(item.get("sku", "")).strip()
            name = str(item.get("name", "")).strip()
            ext_id = _ext_id_of(item)
            if not name:
                errors.append(f"#{idx}: нет name")
                continue

            # Порядок сопоставления: id из 1С → sku → название
            product = None
            if ext_id and ext_id in existing_by_ext:
                product = existing_by_ext[ext_id]
            elif sku and sku in existing_by_sku:
                product = existing_by_sku[sku]
            elif name in existing_by_name:
                product = existing_by_name[name]

            # Проверяем числа ДО создания товара: мусор из 1С не должен попасть в БД
            price = float(item.get("price_per_stem", 0) or 0)
            stock = int(item.get("stock_packs", 0) or 0)
            if not (0 <= price <= 10_000_000):
                errors.append(f"#{idx} ({name}): недопустимая цена {price}")
                continue
            if not (0 <= stock <= 1_000_000):
                errors.append(f"#{idx} ({name}): недопустимый остаток {stock}")
                continue

            photos = _extract_photos_from_item(item)
            main_image = photos[0] if photos else ""

            if not product:
                product = Product(
                    sku=sku,
                    external_id=ext_id,
                    name=name,
                    description=str(item.get("description", "") or ""),
                    country=str(item.get("country", "") or ""),
                    length_cm=int(item.get("length_cm", 0) or 0),
                    unit="упаковка",
                    package_size=int(item.get("package_size", 1) or 1),
                    min_quantity=int(item.get("min_quantity", 1) or 1),
                    category=str(item.get("category", "Прочее") or "Прочее"),
                    image_url=main_image,
                    photos=photos,
                )
                db.add(product)
                db.flush()
                if sku:
                    existing_by_sku[sku] = product
                if ext_id:
                    existing_by_ext[ext_id] = product
                existing_by_name[name] = product
                created += 1
            else:
                product.sku = sku or product.sku
                if ext_id:
                    product.external_id = ext_id
                product.name = name
                if "description" in item:
                    product.description = str(item.get("description") or "")
                if "country" in item:
                    product.country = str(item.get("country") or "")
                if "length_cm" in item:
                    product.length_cm = int(item.get("length_cm") or 0)
                if "package_size" in item:
                    product.package_size = int(item.get("package_size") or 1)
                if "min_quantity" in item:
                    product.min_quantity = int(item.get("min_quantity") or 1)
                if "category" in item:
                    product.category = str(
                        item.get("category") or product.category
                    )
                if photos:
                    product.photos = photos
                    product.image_url = main_image
                updated += 1

            si = (
                db.query(SupplyItem)
                .filter(
                    SupplyItem.supply_id == supply.id,
                    SupplyItem.product_id == product.id,
                )
                .first()
            )
            if si:
                si.price = price
                si.stock = stock
                si.is_active = stock > 0
            else:
                db.add(SupplyItem(
                    supply_id=supply.id,
                    product_id=product.id,
                    price=price,
                    stock=stock,
                    is_active=stock > 0,
                ))
            processed += 1

        except Exception as e:
            logger.exception("1С: ошибка на #%d", idx)
            errors.append(f"#{idx} ({item.get('name', '?')}): {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("1С: ошибка коммита")
        raise HTTPException(500, f"Ошибка БД: {e}")

    logger.info(
        "1С sync: ip=%s, processed=%d, created=%d, updated=%d, errors=%d",
        client_ip, processed, created, updated, len(errors),
    )

    return JSONResponse({
        "status": "ok",
        "processed": processed,
        "created": created,
        "updated": updated,
        "errors": errors[:20],
        "supply_id": supply.id,
    })


@router.get("/ping")
def ping(_auth: bool = Depends(_verify_basic)):
    return {
        "status": "ok",
        "service": "dianthus-1c",
        "env": config.ENV,
        "auth": bool(config.INTEGRATION_PASSWORD),
    }


def _product_payload(product: Product, item: SupplyItem | None) -> dict:
    """Одна запись каталога в формате обмена с 1С.

    Формат тот же, что принимает POST /api/1c/products/sync: выгрузку можно
    поправить в 1С и отправить обратно без преобразований.
    """
    return {
        # id — идентификатор товара НА САЙТЕ
        "id": product.id,
        # external_id — идентификатор товара В 1С (то, что 1С присылает в "id")
        "external_id": product.external_id or "",
        "sku": product.sku or "",
        "name": product.name,
        "description": product.description or "",
        "country": product.country or "",
        "length_cm": product.length_cm or 0,
        "unit": product.unit or "упаковка",
        "package_size": product.package_size or 1,
        "min_quantity": product.min_quantity or 1,
        "category": product.category or "Прочее",
        # Цена за ШТ (стебель), остаток — в УПАКОВКАХ
        "price_per_stem": float(item.price) if item else 0.0,
        "stock_packs": int(item.stock) if item else 0,
        "reserved_packs": int(item.reserved_stock or 0) if item else 0,
        "available_packs": int(item.available_stock) if item else 0,
        "photos": product.all_photos,
        "supply_item_id": item.id if item else None,
    }


@router.get("/products/export")
def export_products_to_1c(
    all: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Каталог сайта для 1С — одна запись на товар.

        GET /api/1c/products/export          — товары, которые сейчас на полке
        GET /api/1c/products/export?all=1    — весь справочник (включая отсутствующие)

    Цена и остаток берутся из активной партии поставки.
    """
    rows: dict[int, dict] = {}
    items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.is_active.is_(True))
        .order_by(SupplyItem.id.desc())
        .all()
    )
    for item in items:
        product = item.product
        if product and product.id not in rows:
            rows[product.id] = _product_payload(product, item)

    if all:
        for product in db.query(Product).order_by(Product.id).all():
            rows.setdefault(product.id, _product_payload(product, None))

    products = [rows[pid] for pid in sorted(rows)]
    return JSONResponse({"count": len(products), "products": products})


# ───────────────────────── ЗАКАЗЫ ДЛЯ 1С ─────────────────────────

ORDERS_MAX_PER_CALL = 200


@router.get("/orders/export")
def export_orders_to_1c(
    since: str = "",
    status: str = "",
    limit: int = ORDERS_MAX_PER_CALL,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Заказы сайта для загрузки в 1С (документ «Заказ клиента»).

    1С вызывает: GET /api/1c/orders/export?since=2026-02-01T00:00:00
    Повторный вызов безопасен: заказы идут по возрастанию id, 1С отсеивает
    уже загруженные по номеру «САЙТ-<id>» — идемпотентность на её стороне.
    """
    try:
        limit = max(1, min(int(limit), ORDERS_MAX_PER_CALL))
    except (TypeError, ValueError):
        limit = ORDERS_MAX_PER_CALL

    query = (
        db.query(Order)
        .options(selectinload(Order.items), selectinload(Order.user))
        .order_by(Order.id.asc())
    )
    if since:
        try:
            query = query.filter(Order.created_at >= datetime.fromisoformat(since))
        except ValueError:
            raise HTTPException(400, "since — дата в формате ISO 8601")
    if status:
        if status not in ORDER_STATUSES:
            raise HTTPException(400, "Неизвестный статус заказа")
        query = query.filter(Order.status == status)

    orders = query.limit(limit).all()

    # SKU подтягиваем одним запросом — без N+1
    product_ids = {i.product_id for o in orders for i in o.items if i.product_id}
    skus = (
        dict(db.query(Product.id, Product.sku)
             .filter(Product.id.in_(product_ids)).all())
        if product_ids else {}
    )

    return JSONResponse({
        "count": len(orders),
        "orders": [
            {
                "id": o.id,
                "number": f"САЙТ-{o.id}",
                "created_at": o.created_at.isoformat(),
                "status": o.status,
                "comment": o.comment or "",
                "subtotal": o.subtotal,
                "discount_percent": o.discount_percent,
                "total": o.total_price,
                "buyer": {
                    "company": o.user.company_name if o.user else "",
                    "contact": o.user.full_name if o.user else "",
                    "inn": (o.user.inn if o.user else "") or "",
                    "phone": (o.user.phone if o.user else "") or "",
                    "email": (o.user.email if o.user else "") or "",
                    "city": (o.user.city if o.user else "") or "",
                },
                "items": [
                    {
                        "sku": skus.get(i.product_id, ""),
                        "name": i.product_name,
                        "quantity_packs": i.quantity,
                        "package_size": i.package_size or 1,
                        "stems": i.total_stems,
                        "price_per_stem": i.price,
                        "sum": i.subtotal,
                    }
                    for i in o.items
                ],
            }
            for o in orders
        ],
    })