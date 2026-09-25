# app/routers/integration_1c.py (полный код)
"""
Приём данных из 1С. Basic Auth (в prod).
POST /api/1c/products/sync
"""
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ..config import config
from ..database import get_db
from ..models import Product, Supply, SupplyItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/1c", tags=["1C Integration"])

_basic = HTTPBasic(auto_error=False)


# ═══════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════════════════

def _verify_basic(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> bool:
    """
    Проверяет Basic Auth.

    ✅ ИСПРАВЛЕНО: в dev-режиме при отсутствии пароля — 503,
    а не молчаливый пропуск (раньше любой мог вызвать sync).
    """
    auth_configured = bool(
        config.INTEGRATION_USER and config.INTEGRATION_PASSWORD
    )

    if not auth_configured:
        # В prod — всегда отказ
        if config.ENV == "prod":
            raise HTTPException(
                503, "Интеграция с 1С не настроена на сервере"
            )
        # ✅ В dev — требуем пароль, если интеграция используется
        if not config.INTEGRATION_PASSWORD:
            logger.warning(
                "1С: INTEGRATION_PASSWORD не задан — "
                "запрос отклонён (503)."
            )
            raise HTTPException(
                503,
                "Задайте INTEGRATION_PASSWORD для разработки "
                "или используйте ENV=prod"
            )
        return True

    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация",
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
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )
    return True


# ═══════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════

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
            is_service=True,   # fix #20
        )
        db.add(supply)
        db.flush()
        logger.info("1С: создана служебная поставка №%d", supply.id)
    return supply


def _extract_photos_from_item(item: dict) -> list:
    """Достаёт фото из item — поддерживает photo, photos, image_url."""
    for key in ("photos", "photo", "image_url"):
        value = item.get(key)
        if value:
            photos = _normalize_photos(value)
            if photos:
                return photos
    return []


# ═══════════════════════════════════════════════════════════
# SYNC ENDPOINT
# ═══════════════════════════════════════════════════════════

@router.post("/products/sync")
async def sync_products(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Приём товаров из 1С."""
    client_ip = request.client.host if request.client else "?"

    products_data = payload.get("products", [])
    if not isinstance(products_data, list):
        raise HTTPException(400, "Поле 'products' должно быть массивом")

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

    # Batch-load существующих товаров (по sku и name)
    skus, names = [], []
    for it in products_data:
        s = str(it.get("sku", "")).strip()
        n = str(it.get("name", "")).strip()
        if s:
            skus.append(s)
        if n:
            names.append(n)

    existing_by_sku: dict[str, Product] = {}
    existing_by_name: dict[str, Product] = {}
    if skus or names:
        conds = []
        if skus:
            conds.append(Product.sku.in_(skus))
        if names:
            conds.append(Product.name.in_(names))
        for p in db.query(Product).filter(or_(*conds)).all():
            if p.sku:
                existing_by_sku[p.sku] = p
            existing_by_name[p.name] = p

    processed = created = updated = 0
    errors: list[str] = []

    for idx, item in enumerate(products_data, start=1):
        try:
            sku = str(item.get("sku", "")).strip()
            name = str(item.get("name", "")).strip()
            if not name:
                errors.append(f"#{idx}: нет name")
                continue

            product = None
            if sku and sku in existing_by_sku:
                product = existing_by_sku[sku]
            elif name in existing_by_name:
                product = existing_by_name[name]

            photos = _extract_photos_from_item(item)
            main_image = photos[0] if photos else ""

            if not product:
                product = Product(
                    sku=sku,
                    name=name,
                    description=str(item.get("description", "") or ""),
                    country=str(item.get("country", "") or ""),
                    length_cm=int(item.get("length_cm", 0) or 0),
                    unit="упаковка",
                    package_size=int(item.get("package_size", 1) or 1),
                    min_quantity=int(item.get("min_quantity", 1) or 1),
                    category=str(
                        item.get("category", "Прочее") or "Прочее"
                    ),
                    image_url=main_image,
                    photos=photos,
                )
                db.add(product)
                db.flush()
                if sku:
                    existing_by_sku[sku] = product
                existing_by_name[name] = product
                created += 1
            else:
                product.sku = sku or product.sku
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

            price = float(item.get("price_per_stem", 0) or 0)
            stock = int(item.get("stock_packs", 0) or 0)

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
async def ping(_auth: bool = Depends(_verify_basic)):
    return {
        "status": "ok",
        "service": "dianthus-1c",
        "env": config.ENV,
        "auth": bool(config.INTEGRATION_PASSWORD),
    }


@router.get("/products/export")
async def export_products_to_1c(
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Выгрузка товаров для 1С (JSON)."""
    items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.is_active.is_(True))
        .all()
    )
    return JSONResponse({
        "products": [
            {
                "id": item.id,
                "sku": item.product.sku or "",
                "name": item.product.name,
                "price_per_stem": item.price,
                "stock_packs": item.stock,
                "country": item.product.country or "",
                "length_cm": item.product.length_cm or 0,
                "package_size": item.product.package_size or 1,
                "min_quantity": item.product.min_quantity or 1,
                "category": item.product.category or "Прочее",
                "description": item.product.description or "",
                "photo": item.product.all_photos,
            }
            for item in items
        ]
    })