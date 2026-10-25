# app/services/exchange_export.py
"""Выгрузка данных сайта для 1С: JSON-словари и CSV-таблицы.

Общий код для двух мест: интернет-обмена (/api/1c/*) и ручной выгрузки из
админки («Интеграция» → «Заказы для 1С»).
"""
import csv
import io

from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..models import Order, Product, SupplyItem
from ..timeutils import local_iso, utc_iso

ORDER_NUMBER_PREFIX = "САЙТ-"

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value) -> str:
    """Текст клиента в CSV: если начинается с «=», «+», «@» … — Excel мог бы
    принять его за формулу. Добавляем пробел в начало."""
    s = "" if value is None else str(value)
    return " " + s if s.startswith(_FORMULA_START) else s


def csv_response(header: list[str], rows: list[list], filename: str,
                 encoding: str = "utf-8") -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    text = buf.getvalue()
    if (encoding or "").lower() in ("cp1251", "windows-1251", "win1251"):
        data, charset = text.encode("cp1251", errors="replace"), "windows-1251"
    else:
        data, charset = ("﻿" + text).encode("utf-8"), "utf-8"
    return Response(
        content=data, media_type=f"text/csv; charset={charset}",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "Cache-Control": "no-store"},
    )


# ───────────────────────── товары ─────────────────────────

def product_payload(product: Product, item: SupplyItem | None) -> dict:
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


PRODUCT_CSV_HEADER = ["id", "external_id", "sku", "name", "country", "category",
                      "length_cm", "package_size", "min_quantity", "price_per_stem",
                      "stock_packs", "reserved_packs", "available_packs", "photos"]


def products_csv_rows(products: list[dict]) -> list[list]:
    return [[p["id"], p["external_id"], p["sku"], p["name"], p["country"],
             p["category"], p["length_cm"], p["package_size"], p["min_quantity"],
             p["price_per_stem"], p["stock_packs"], p["reserved_packs"],
             p["available_packs"], ",".join(p["photos"])] for p in products]


# ───────────────────────── заказы ─────────────────────────

def order_payload(o: Order, skus: dict) -> dict:
    user = o.user
    return {
        "id": o.id,
        "number": f"{ORDER_NUMBER_PREFIX}{o.id}",
        # created_at — как раньше (UTC без пояса). Для разбора надёжнее
        # created_at_utc (с «+00:00») или created_at_local (с поясом магазина).
        "created_at": o.created_at.isoformat(),
        "created_at_utc": utc_iso(o.created_at),
        "created_at_local": local_iso(o.created_at),
        # к какой дате клиенту нужны цветы (может быть пусто)
        "desired_date": o.desired_date.date().isoformat() if o.desired_date else "",
        # pickup — самовывоз, delivery — доставка (тогда адрес в delivery_address), пусто — не указано
        "delivery_method": o.delivery_method or "",
        "delivery_address": o.delivery_address or "",
        "status": o.status,
        "comment": o.comment or "",
        "exported": o.exported_at is not None,
        "onec_number": o.onec_number or "",
        "subtotal": o.subtotal,
        "discount_percent": o.discount_percent,
        "total": o.total_price,
        "buyer": {
            "company": user.company_name if user else "",
            "contact": user.full_name if user else "",
            "inn": (user.inn if user else "") or "",
            "phone": (user.phone if user else "") or "",
            "email": (user.email if user else "") or "",
            "city": (user.city if user else "") or "",
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


def orders_to_payloads(db: Session, orders: list[Order]) -> list[dict]:
    """Заказы → словари. Артикулы товаров подтягиваются ОДНИМ запросом (без N+1)."""
    product_ids = {i.product_id for o in orders for i in o.items if i.product_id}
    skus = (
        dict(db.query(Product.id, Product.sku)
             .filter(Product.id.in_(product_ids)).all())
        if product_ids else {}
    )
    return [order_payload(o, skus) for o in orders]


ORDER_CSV_HEADER = ["order_number", "order_id", "created_at_local", "desired_date",
                    "delivery_method", "delivery_address", "status", "company", "inn", "contact", "phone", "email", "city",
                    "comment", "discount_percent", "order_total", "sku", "name",
                    "packs", "pieces_per_pack", "pieces", "price_per_piece", "line_sum"]


def orders_csv_rows(payloads: list[dict]) -> list[list]:
    """Одна строка = одна позиция заказа (удобно разбирать в старых версиях 1С)."""
    lines = []
    for p in payloads:
        b = p["buyer"]
        for it in p["items"]:
            lines.append([
                p["number"], p["id"], p["created_at_local"], p["desired_date"],
                p["delivery_method"], safe_text(p["delivery_address"]),
                p["status"], safe_text(b["company"]), b["inn"],
                safe_text(b["contact"]), b["phone"], safe_text(b["email"]),
                safe_text(b["city"]), safe_text(p["comment"]),
                p["discount_percent"], p["total"], it["sku"], safe_text(it["name"]),
                it["quantity_packs"], it["package_size"], it["stems"],
                it["price_per_stem"], it["sum"],
            ])
    return lines
