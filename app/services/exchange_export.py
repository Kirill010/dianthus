# app/services/exchange_export.py
"""Выгрузка данных сайта для 1С: JSON-словари и CSV-таблицы.

Общий код для двух мест: интернет-обмена (/api/1c/*) и ручной выгрузки из
админки («Интеграция» → «Заказы для 1С»).

У каждой сущности есть два идентификатора: `id` — на САЙТЕ и `external_id` — в 1С
(пусто, пока 1С не сообщила свой). Контрагент: id, external_id, name, inn, email,
address, phone. Товар: id, external_id, sku, barcode, name. Заказ: id (на сайте),
number («САЙТ-57»), date.
"""
import csv
import io

from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..models import Order, Product, SupplyItem, User
from ..timeutils import to_local

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


def shop_time_iso(dt) -> str:
    """Время магазина (МСК) без смещения: «2026-10-27T12:31:00». Так его читает 1С."""
    local = to_local(dt)
    return local.replace(tzinfo=None).isoformat(timespec="seconds") if local else ""


# ───────────────────────── контрагенты ─────────────────────────

def counterparty_payload(user: User | None) -> dict:
    """Контрагент: id (на сайте), external_id (в 1С), название, ИНН, email, адрес, телефон."""
    return {
        "id": user.id if user else None,
        "external_id": (user.external_id if user else "") or "",
        "name": user.company_name if user else "",
        "inn": (user.inn if user else "") or "",
        "email": (user.email if user else "") or "",
        "address": (user.address if user else "") or "",
        "phone": (user.phone if user else "") or "",
    }


# ───────────────────────── товары ─────────────────────────

def product_payload(product: Product, item: SupplyItem | None) -> dict:
    """Одна запись каталога в формате обмена с 1С.

    Те же поля (и те же названия), что принимает POST /api/1c/products/sync: выгрузку можно
    поправить в 1С и отправить обратно. Исключение одно: в выгрузке `id` — товар НА САЙТЕ,
    а идентификатор 1С лежит в `external_id` (в ответной загрузке он главнее `id`).
    Вид, цвет, страна — названия (рядом их id в справочниках), рост — число сантиметров.
    """
    kind, color = product.kind_ref, product.color_ref
    height, country = product.height_ref, product.country_ref
    return {
        "id": product.id,
        "external_id": product.external_id or "",
        "sku": product.sku or "",
        "barcode": product.barcode or "",
        "name": product.name,
        "description": product.description or "",
        "kind": kind.name if kind else "", "kind_id": product.kind_id,
        "color": color.name if color else "", "color_id": product.color_id,
        "height": height.cm if height else 0, "height_id": product.height_id,
        "country": country.name if country else "", "country_id": product.country_id,
        "unit": product.unit or "упаковка",
        "package_size": product.package_size or 1,
        "min_quantity": product.min_quantity or 1,
        # Цена за ШТ (стебель), остаток — в УПАКОВКАХ
        "price_per_stem": float(item.price) if item else 0.0,
        "stock_packs": int(item.stock) if item else 0,
        "reserved_packs": int(item.reserved_stock or 0) if item else 0,
        "available_packs": int(item.available_stock) if item else 0,
        "photos": product.all_photos,
    }


PRODUCT_CSV_HEADER = ["id", "external_id", "sku", "barcode", "name", "kind", "color", "height",
                      "country", "unit", "package_size", "min_quantity", "price_per_stem",
                      "stock_packs", "reserved_packs", "available_packs", "photos"]


def products_csv_rows(products: list[dict]) -> list[list]:
    return [[p["id"], p["external_id"], p["sku"], p["barcode"], safe_text(p["name"]), p["kind"],
             p["color"], p["height"], p["country"], p["unit"], p["package_size"],
             p["min_quantity"], p["price_per_stem"], p["stock_packs"], p["reserved_packs"],
             p["available_packs"], ",".join(p["photos"])] for p in products]


# ───────────────────────── заказы ─────────────────────────

def order_payload(o: Order, products: dict) -> dict:
    """Заказ для 1С. products — {id товара: (external_id, sku, barcode)}."""
    items = []
    for i in o.items:
        external_id, sku, barcode = products.get(i.product_id, ("", "", ""))
        items.append({
            "product_id": i.product_id,
            "external_id": external_id or "",
            "sku": sku or "",
            "barcode": barcode or "",
            "name": i.product_name,
            "quantity_packs": i.quantity,
            "package_size": i.package_size or 1,
            "stems": i.total_stems,
            "price_per_stem": i.price,
            "sum": i.subtotal,
        })
    return {
        "id": o.id,                                         # id заказа на сайте
        "number": f"{ORDER_NUMBER_PREFIX}{o.id}",           # номер: его 1С записывает в документ
        "date": shop_time_iso(o.created_at),                # когда оформлен (время магазина, МСК)
        "status": o.status,
        "comment": o.comment or "",
        # к какой дате клиенту нужны цветы (пусто, если не указано)
        "desired_date": o.desired_date.isoformat(timespec="seconds") if o.desired_date else "",
        # pickup — самовывоз, delivery — доставка (тогда адрес в delivery_address), пусто — не указано
        "delivery_method": o.delivery_method or "",
        "delivery_address": o.delivery_address or "",
        "subtotal": o.subtotal,
        "discount_percent": o.discount_percent,
        "total": o.total_price,
        "counterparty": counterparty_payload(o.user),
        "items": items,
    }


def orders_to_payloads(db: Session, orders: list[Order]) -> list[dict]:
    """Заказы → словари. Идентификаторы товаров подтягиваются ОДНИМ запросом (без N+1)."""
    product_ids = {i.product_id for o in orders for i in o.items if i.product_id}
    products = (
        {pid: (ext, sku, barcode) for pid, ext, sku, barcode in
         db.query(Product.id, Product.external_id, Product.sku, Product.barcode)
         .filter(Product.id.in_(product_ids)).all()}
        if product_ids else {}
    )
    return [order_payload(o, products) for o in orders]


ORDER_CSV_HEADER = ["order_number", "order_id", "date", "desired_date", "delivery_method",
                    "delivery_address", "status", "comment", "subtotal", "discount_percent", "total",
                    "counterparty_id", "counterparty_external_id", "counterparty_name",
                    "counterparty_inn", "counterparty_email", "counterparty_address",
                    "counterparty_phone", "product_id", "external_id", "sku", "barcode", "name",
                    "packs", "pieces_per_pack", "pieces", "price_per_piece", "line_sum"]


def orders_csv_rows(payloads: list[dict]) -> list[list]:
    """Одна строка = одна позиция заказа (удобно разбирать в старых версиях 1С)."""
    lines = []
    for p in payloads:
        c = p["counterparty"]
        for it in p["items"]:
            lines.append([
                p["number"], p["id"], p["date"], p["desired_date"], p["delivery_method"],
                safe_text(p["delivery_address"]), p["status"], safe_text(p["comment"]),
                p["subtotal"], p["discount_percent"], p["total"],
                c["id"], c["external_id"], safe_text(c["name"]), c["inn"], safe_text(c["email"]),
                safe_text(c["address"]), c["phone"],
                it["product_id"], it["external_id"], it["sku"], it["barcode"], safe_text(it["name"]),
                it["quantity_packs"], it["package_size"], it["stems"], it["price_per_stem"], it["sum"],
            ])
    return lines
