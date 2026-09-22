"""Приём данных из 1С. Картинки загружаются вручную через админку."""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..config import config
from ..database import get_db
from ..models import Product, Supply, SupplyItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/1c", tags=["1C Integration"])


def _verify_secret(x_api_key: str = Header("")):
    """Проверяет пароль от 1С."""
    if not config.INTEGRATION_SECRET:
        raise HTTPException(503, "Интеграция с 1С не настроена на сервере")
    if x_api_key != config.INTEGRATION_SECRET:
        logger.warning("1С: неверный API-ключ")
        raise HTTPException(403, "Неверный API-ключ")
    return True


@router.post("/products/sync")
async def sync_products(
    payload: dict,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_secret),
):
    """
    1С отправляет сюда JSON с товарами.

    Создаёт новую поставку «Авто-синхронизация из 1С»,
    если её нет, и кладёт туда все товары.
    Картинки НЕ принимаются — их грузит админ вручную.
    """
    products_data = payload.get("products", [])
    if not products_data:
        return JSONResponse({
            "status": "ok", "processed": 0, "errors": [],
            "supply_id": None,
        })

    # Ищем открытую поставку для 1С
    supply = (
        db.query(Supply)
        .filter(Supply.status == "Ожидается",
                Supply.notes.like("%Авто-синхронизация из 1С%"))
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
        logger.info("1С: создана поставка №%d", supply.id)

    processed = 0
    created_products = 0
    errors = []

    for item in products_data:
        try:
            sku = str(item.get("sku", "")).strip()
            name = str(item.get("name", "")).strip()

            if not name:
                errors.append("Товар без названия — пропущен")
                continue

            # Ищем товар: сначала по sku, потом по имени
            product = None
            if sku:
                product = (
                    db.query(Product)
                    .filter(Product.sku == sku)
                    .first()
                )
            if not product:
                product = (
                    db.query(Product)
                    .filter(Product.name == name)
                    .first()
                )

            if not product:
                product = Product(
                    sku=sku,
                    name=name,
                    description=item.get("description", ""),
                    country=item.get("country", ""),
                    length_cm=int(item.get("length_cm", 0) or 0),
                    unit=item.get("unit", "упаковка"),
                    package_size=int(item.get("package_size", 1) or 1),
                    min_quantity=int(item.get("min_quantity", 1) or 1),
                    category=item.get("category", "Прочее"),
                    image_url="",  # ← картинку админ загрузит сам
                )
                db.add(product)
                db.flush()
                created_products += 1
                logger.info("1С: создан товар «%s»", name)
            else:
                # Обновляем данные (кроме картинки!)
                product.sku = sku or product.sku
                product.description = item.get("description", product.description)
                product.country = item.get("country", product.country)
                product.length_cm = int(item.get("length_cm", product.length_cm) or 0)
                product.package_size = int(item.get("package_size", product.package_size) or 1)
                product.min_quantity = int(item.get("min_quantity", product.min_quantity) or 1)
                product.category = item.get("category", product.category)

            # Позиция в поставке
            price = float(item.get("price_per_stem", 0) or 0)
            stock = int(item.get("stock_packs", 0) or 0)

            supply_item = (
                db.query(SupplyItem)
                .filter(SupplyItem.supply_id == supply.id,
                        SupplyItem.product_id == product.id)
                .first()
            )

            if supply_item:
                supply_item.price = price
                supply_item.stock = stock
            else:
                db.add(SupplyItem(
                    supply_id=supply.id,
                    product_id=product.id,
                    price=price,
                    stock=stock,
                ))

            processed += 1

        except Exception as e:
            logger.exception("1С: ошибка обработки товара")
            errors.append(f"{item.get('name', '?')}: {e}")

    db.commit()

    logger.info("1С: синхронизация завершена. Обработано: %d, создано: %d, ошибок: %d",
                processed, created_products, len(errors))

    return JSONResponse({
        "status": "ok",
        "processed": processed,
        "created_products": created_products,
        "errors": errors[:10],
        "supply_id": supply.id,
    })