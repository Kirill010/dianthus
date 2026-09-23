"""
Приём данных из 1С.

Формат запроса (POST /api/1c/products/sync):
{
  "products": [
    {
      "id": "1",
      "sku": "тест1",
      "name": "товар",
      "price_per_stem": 100,
      "stock_packs": 5,
      "country": "Эквадор",
      "length_cm": 60,
      "package_size": 25,
      "min_quantity": 1,
      "category": "Роза Эквадор",
      "description": "тест",
      "photo": ["https://cdn/1.jpg", "https://cdn/2.jpg"]
    }
  ]
}

Авторизация: HTTP Basic Auth (INTEGRATION_USER / INTEGRATION_PASSWORD).
"""
import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..models import Product, Supply, SupplyItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/1c", tags=["1C Integration"])

_basic = HTTPBasic(auto_error=False)


def _verify_basic(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> bool:
    if not config.INTEGRATION_USER or not config.INTEGRATION_PASSWORD:
        raise HTTPException(503, "Интеграция с 1С не настроена на сервере")
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
        logger.warning("1С: неверные учётные данные, IP=%s",
                       request.client.host if request.client else "?")
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )
    return True


def _normalize_photos(raw) -> list:
    """
    Приводит photo к списку ВАЛИДНЫХ URL-ов.
    ID (числа), пустые строки, мусор — отбрасываются.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        parts = [str(p).strip() for p in raw if p is not None]
    else:
        return []

    result = []
    for s in parts:
        if s and s.startswith(("http://", "https://", "/static/")):
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
            arrival_date=datetime.utcnow(),
            notes="Авто-синхронизация из 1С",
        )
        db.add(supply)
        db.flush()
        logger.info("1С: создана служебная поставка №%d", supply.id)
    return supply


@router.post("/products/sync")
async def sync_products(
    payload: dict,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    products_data = payload.get("products", [])
    if not isinstance(products_data, list):
        raise HTTPException(400, "Поле 'products' должно быть массивом")
    if not products_data:
        return JSONResponse({
            "status": "ok", "processed": 0, "created": 0,
            "updated": 0, "errors": [], "supply_id": None,
        })

    supply = _ensure_1c_supply(db)

    processed = 0
    created = 0
    updated = 0
    errors: list = []

    for idx, item in enumerate(products_data, start=1):
        try:
            sku = str(item.get("sku", "")).strip()
            name = str(item.get("name", "")).strip()
            if not name:
                errors.append(f"#{idx}: нет name")
                continue

            product = None
            if sku:
                product = db.query(Product).filter(Product.sku == sku).first()
            if not product:
                product = db.query(Product).filter(Product.name == name).first()

            photos = _normalize_photos(item.get("photo"))
            main_image = photos[0] if photos else ""

            if not product:
                product = Product(
                    sku=sku, name=name,
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
            else:
                db.add(SupplyItem(
                    supply_id=supply.id,
                    product_id=product.id,
                    price=price,
                    stock=stock,
                ))
            processed += 1

        except Exception as e:
            logger.exception("1С: ошибка обработки #%d", idx)
            errors.append(f"#{idx} ({item.get('name', '?')}): {e}")

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("1С: ошибка коммита")
        raise HTTPException(500, f"Ошибка БД: {e}")

    logger.info(
        "1С: processed=%d, created=%d, updated=%d, errors=%d",
        processed, created, updated, len(errors),
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
    return {"status": "ok", "service": "dianthus-1c"}