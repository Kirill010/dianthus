# app/services/supply_sync.py
"""Инвойсы из 1С → «товары в пути» на сайте.

В 1С приход цветов от поставщика оформляют документом «Инвойс …» (рейс, поставщик,
дата, валюта, строки товаров). Сайт показывает такие рейсы клиентам как «Товары в
пути»: клиент может сделать предзаказ, пока цветы летят.

Как это устроено:
  * 1С присылает инвойсы за последние недели вместе с их ТЕКУЩИМ состоянием:
      "in_transit" — ещё в пути (не оприходован),
      "received"   — оприходован: цветы приехали и числятся в остатках 1С,
      "cancelled"  — отменён / удалён.
    Сайт создаёт или обновляет поставку по идентификатору инвойса (повторная
    отправка ничего не дублирует).
  * Остатки «на полке» по-прежнему приходят из 1С отдельно (products/sync). Поэтому
    поставка из 1С НИКОГДА не «разгружается» на полку сама и вручную: иначе товар был
    бы виден дважды. Когда инвойс получает статус «received», поставка закрывается,
    а клиентам с предзаказом резервируется товар с полки и уходит уведомление.
  * Пустой список ничего не закрывает: статус приходит явно, а не «по отсутствию».
  * Забытые поставки (1С давно не присылала инвойс) закрываются сами через 21 день.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from ..models import Notification, OrderItem, Preorder, Product, Supply, SupplyItem
from .catalog_sync import (MAX_ERRORS_IN_REPLY, Catalog, RowError, SyncCommitError,
                           clean, fill_empty_from_name, lock_exchange, new_product,
                           parse_product_row)
from .product_guess import guess_country

logger = logging.getLogger(__name__)

IN_TRANSIT = "В пути"
CLOSED = "Разгружен"            # для поставки из 1С значит «закрыта: принята в 1С»
MAX_SUPPLIES_PER_SYNC = 200
MAX_LINES_PER_SUPPLY = 2000
STALE_DAYS = 21

_STATUS_ALIASES = {
    "in_transit": "in_transit", "transit": "in_transit", "expected": "in_transit",
    "в пути": "in_transit", "ожидается": "in_transit", "в дороге": "in_transit",
    "received": "received", "arrived": "received", "posted": "received",
    "принят": "received", "принято": "received", "получен": "received",
    "оприходован": "received", "прибыл": "received", "проведён": "received",
    "проведен": "received",
    "cancelled": "cancelled", "canceled": "cancelled", "deleted": "cancelled",
    "отменён": "cancelled", "отменен": "cancelled", "удалён": "cancelled",
    "удален": "cancelled",
}


@dataclass
class SupplySyncResult:
    processed: int = 0          # инвойсов обработано
    created: int = 0            # новых поставок «в пути»
    updated: int = 0            # обновлено поставок «в пути»
    received: int = 0           # закрыто как принятые в 1С
    cancelled: int = 0          # отменено
    skipped: int = 0            # «принят/отменён», а на сайте такой поставки не было
    lines: int = 0              # строк товаров принято
    products_created: int = 0   # новых товаров в справочнике
    preorders_fulfilled: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    emails: list[dict] = field(default_factory=list)      # письма клиентам (шлём после commit)

    def as_dict(self) -> dict:
        return {
            "status": "ok",
            "dry_run": self.dry_run,
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "received": self.received,
            "cancelled": self.cancelled,
            "skipped": self.skipped,
            "lines": self.lines,
            "products_created": self.products_created,
            "preorders_fulfilled": self.preorders_fulfilled,
            "errors": self.errors[:MAX_ERRORS_IN_REPLY],
            "errors_total": len(self.errors),
            "warnings": self.warnings[:MAX_ERRORS_IN_REPLY],
        }

    def summary(self) -> str:
        text = (f"инвойсов: {self.processed}, новых в пути: {self.created}, "
                f"обновлено: {self.updated}, принято: {self.received}")
        if self.cancelled:
            text += f", отменено: {self.cancelled}"
        if self.preorders_fulfilled:
            text += f", предзаказов выполнено: {self.preorders_fulfilled}"
        if self.errors:
            text += f", ошибок: {len(self.errors)}"
        if self.dry_run:
            text = "ПРОВЕРКА, ничего не сохранено — " + text
        return text


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════
# РАЗБОР ВХОДНЫХ ДАННЫХ
# ═══════════════════════════════════════════════════════════

def parse_arrival(value) -> datetime | None:
    """«2026-10-29», «2026-10-29T12:00:00+03:00», «29.10.2026» → datetime (дата без времени)."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M"):
        try:
            d = datetime.strptime(text, fmt)
            return datetime(d.year, d.month, d.day)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
        if not m:
            return None
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    if not (2000 <= d.year <= 2100):
        return None
    return datetime(d.year, d.month, d.day)


def normalize_status(value) -> str:
    key = clean(value, 30).lower()
    if not key:
        return "in_transit"
    if key not in _STATUS_ALIASES:
        raise RowError(f"неизвестный status «{key}» (нужно in_transit, received или cancelled)")
    return _STATUS_ALIASES[key]


def parse_supply(item: dict) -> dict:
    """Проверка и очистка одного инвойса. Бросает RowError."""
    ext = clean(item.get("id") or item.get("external_id") or item.get("number"), 64)
    if not ext:
        raise RowError("нет id (номер или идентификатор инвойса в 1С)")
    status = normalize_status(item.get("status"))
    arrival = parse_arrival(item.get("date") or item.get("arrival_date"))
    if status == "in_transit" and arrival is None:
        raise RowError("нет date (дата прибытия) в формате 2026-10-29")
    lines = item.get("items")
    if lines is None:
        lines = []
    if not isinstance(lines, list):
        raise RowError("items должен быть массивом")
    if len(lines) > MAX_LINES_PER_SUPPLY:
        raise RowError(f"слишком много строк ({len(lines)}), максимум {MAX_LINES_PER_SUPPLY}")
    return {
        "ext": ext,
        "number": clean(item.get("number") or ext, 64),
        "status": status,
        "arrival": arrival,
        "country": clean(item.get("country"), 100),
        "city": clean(item.get("city"), 100),
        "supplier": clean(item.get("supplier"), 120),
        "comment": clean(item.get("comment") or item.get("notes"), 300),
        "lines": [ln for ln in lines if isinstance(ln, dict)],
    }


def _line_to_product_item(line: dict) -> dict:
    """Строка инвойса → то же, что строка каталога (quantity_* → stock_*)."""
    mapped = dict(line)
    if "quantity_packs" in line and "stock_packs" not in line:
        mapped["stock_packs"] = line["quantity_packs"]
    if "quantity_stems" in line and "stock_stems" not in line:
        mapped["stock_stems"] = line["quantity_stems"]
    return mapped


def _notes(sp: dict) -> str:
    text = f"Инвойс 1С № {sp['number']}"
    if sp["supplier"]:
        text += f", поставщик {sp['supplier']}"
    if sp["comment"]:
        text += f". {sp['comment']}"
    return text[:500]


def _known_price(db: Session, product: Product) -> float:
    """Последняя известная цена товара на сайте (если инвойс пришёл без цены продажи)."""
    if product.id is None:
        return 0.0
    row = (db.query(SupplyItem.price)
           .filter(SupplyItem.product_id == product.id, SupplyItem.price > 0)
           .order_by(SupplyItem.id.desc()).first())
    return float(row[0]) if row else 0.0


# ═══════════════════════════════════════════════════════════
# ПРИМЕНЕНИЕ
# ═══════════════════════════════════════════════════════════

def _has_open_preorders(db: Session, item_ids: list[int]) -> int:
    if not item_ids:
        return 0
    return (db.query(Preorder)
            .filter(Preorder.supply_item_id.in_(item_ids), Preorder.is_fulfilled.is_(False))
            .count())


def _upsert_lines(db: Session, supply: Supply, sp: dict, catalog: Catalog,
                  result: SupplySyncResult) -> None:
    by_product: dict[Product, SupplyItem] = {si.product: si for si in supply.items}
    seen: set[int] = set()
    for idx, line in enumerate(sp["lines"], start=1):
        label = clean(line.get("name"), 60) or "?"
        try:
            row = parse_product_row(_line_to_product_item(line))
        except RowError as e:
            result.errors.append(f"инвойс {sp['number']}, строка {idx} ({label}): {e}")
            continue
        product = catalog.find(row["ext"], row["sku"], row["name"])
        if product is not None:
            pack = max(1, product.package_size or 1)
        else:
            pack = max(1, row["package_size"] or row.get("package_size_hint") or 1)
        packs = row["stock_packs"] if row["stock_packs"] is not None else row["stock_stems"] // pack
        if packs <= 0:
            result.warnings.append(
                f"инвойс {sp['number']}, строка {idx} ({label}): меньше одной упаковки "
                f"({pack} шт) — пропущена")
            continue
        price = row["price"] or (_known_price(db, product) if product is not None else 0.0)
        if price <= 0:
            result.errors.append(
                f"инвойс {sp['number']}, строка {idx} ({label}): нет цены продажи "
                f"(price_per_stem) и на сайте её тоже нет")
            continue

        if product is None:
            if row["country"] is None:
                # страна: из названия («Роза Кения 40»), иначе — страна самого инвойса
                row = {**row, "country": guess_country(row["name"]) or sp["country"] or None}
            product = new_product(row, photos=row["photos"],
                                 main_image=row["photos"][0] if row["photos"] else "")
            db.add(product)
            catalog.add(product)
            result.products_created += 1
        else:
            fill_empty_from_name(product, row)
            if not product.country and row["country"] is None and sp["country"]:
                product.country = sp["country"]

        item = by_product.get(product)
        if item is None:
            item = SupplyItem(supply=supply, product=product, price=price,
                              stock=packs, is_active=False)
            db.add(item)
            by_product[product] = item
        else:
            item.price, item.stock, item.is_active = price, packs, False
        seen.add(id(product))
        result.lines += 1

    # позиции, которых в инвойсе больше нет
    for product, item in list(by_product.items()):
        if id(product) in seen:
            continue
        if item.id is not None and _has_open_preorders(db, [item.id]):
            item.stock, item.is_active = 0, False          # клиенты ждут — не удаляем
        elif item.id is not None:
            supply.items.remove(item)                      # delete-orphan удалит строку


def fulfil_preorders(db: Session, result: SupplySyncResult) -> None:
    """Предзаказы по ПРИНЯТЫМ в 1С инвойсам: резервируем товар с полки и зовём клиента.

    Повторяется при каждом обмене, поэтому если остатки доехали позже инвойса,
    предзаказ выполнится при следующем обмене. Очерёдность — по времени предзаказа.
    """
    pending = (
        db.query(Preorder)
        .options(joinedload(Preorder.user), joinedload(Preorder.product))
        .join(SupplyItem, SupplyItem.id == Preorder.supply_item_id)
        .join(Supply, Supply.id == SupplyItem.supply_id)
        .filter(Preorder.is_fulfilled.is_(False), Supply.external_id != "",
                Supply.status == CLOSED)
        .order_by(Preorder.created_at, Preorder.id)
        .all()
    )
    if not pending:
        return
    service_ids = [s.id for s in db.query(Supply.id).filter(Supply.is_service.is_(True)).all()]
    if not service_ids:
        return
    live: dict[int, SupplyItem] = {}
    for si in (db.query(SupplyItem).options(joinedload(SupplyItem.product))
               .filter(SupplyItem.supply_id.in_(service_ids), SupplyItem.is_active.is_(True))
               .order_by(SupplyItem.id).all()):
        live.setdefault(si.product_id, si)

    for p in pending:
        item = live.get(p.product_id)
        if item is None:
            continue
        take = min(p.quantity, item.available_stock)
        if take <= 0:
            continue
        item.reserved_stock = (item.reserved_stock or 0) + take
        partial = take < p.quantity
        asked = p.quantity
        p.quantity = take
        p.supply_item_id = item.id
        p.is_fulfilled = True
        result.preorders_fulfilled += 1
        name = p.product.name if p.product else item.product.name
        text = (f"🌸 Предзаказ поступил: «{name}» — {take} упак."
                + (f" (из {asked} заказанных — столько есть сейчас)" if partial else "")
                + ". Перейдите в «Мои предзаказы», чтобы оформить заказ.")
        db.add(Notification(user_id=p.user_id, text=text))
        if p.user and p.user.email:
            from ..scheduler import _build_preorder_email
            result.emails.append({
                "to": p.user.email,
                "subject": f"🌸 Предзаказ поступил: {name}",
                "body_html": _build_preorder_email(p, item),
            })
    if result.preorders_fulfilled:
        try:
            from ..templating import _invalidate_preorder_cache
            _invalidate_preorder_cache()
        except Exception:                                    # noqa: BLE001
            pass


def apply_supplies(db: Session, supplies: list[dict], *, dry_run: bool = False) -> SupplySyncResult:
    """Применяет присланные инвойсы. Делает commit (или rollback при dry_run)."""
    lock_exchange(db)
    result = SupplySyncResult(dry_run=dry_run)
    now = _utcnow()

    parsed: list[tuple[int, dict]] = []
    for idx, raw in enumerate(supplies, start=1):
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append((idx, parse_supply(raw)))
        except RowError as e:
            label = clean(raw.get("number") or raw.get("id"), 40) or "?"
            result.errors.append(f"#{idx} (инвойс {label}): {e}")

    all_lines = [_line_to_product_item(ln) for _, sp in parsed if sp["status"] == "in_transit"
                 for ln in sp["lines"]]
    catalog = Catalog(db, all_lines)

    for idx, sp in parsed:
        result.processed += 1
        supply = db.query(Supply).filter(Supply.external_id == sp["ext"],
                                         Supply.is_service.is_(False)).first()

        if sp["status"] == "in_transit":
            fresh = supply is None
            if fresh:
                supply = Supply(external_id=sp["ext"], is_service=False)
                db.add(supply)
            supply.country = sp["country"] or sp["supplier"] or "Поставка из 1С"
            supply.city = sp["city"]
            supply.arrival_date = sp["arrival"]
            supply.status = IN_TRANSIT
            supply.notes = _notes(sp)
            supply.external_synced_at = now
            db.flush()
            _upsert_lines(db, supply, sp, catalog, result)
            result.created += 1 if fresh else 0
            result.updated += 0 if fresh else 1
            continue

        if supply is None:
            result.skipped += 1
            continue
        supply.external_synced_at = now
        if sp["status"] == "received":
            if supply.status != CLOSED:
                result.received += 1
            supply.status = CLOSED
            for item in supply.items:
                item.is_active = False
        else:                                                # cancelled
            item_ids = [i.id for i in supply.items if i.id is not None]
            waiting = _has_open_preorders(db, item_ids)
            if waiting:
                supply.status = CLOSED
                supply.notes = (supply.notes or "")[:440] + " (отменён в 1С)"
                for item in supply.items:
                    item.stock, item.is_active = 0, False
                result.warnings.append(
                    f"инвойс {sp['number']} отменён, но {waiting} предзаказ(ов) ждали его "
                    f"товар: они останутся в ожидании")
            else:
                if item_ids:
                    db.query(OrderItem).filter(OrderItem.supply_item_id.in_(item_ids)).update(
                        {OrderItem.supply_item_id: None}, synchronize_session=False)
                    db.query(Preorder).filter(Preorder.supply_item_id.in_(item_ids)).update(
                        {Preorder.supply_item_id: None}, synchronize_session=False)
                db.delete(supply)
            result.cancelled += 1

    db.flush()
    fulfil_preorders(db, result)

    if dry_run:
        db.rollback()
        result.emails = []
        return result
    try:
        db.commit()
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        logger.exception("1С: ошибка коммита инвойсов")
        raise SyncCommitError(str(e)) from e
    if result.emails:
        try:
            from .notifier import send_batch_emails
            sent = send_batch_emails(result.emails)
            logger.info("📧 Предзаказы по инвойсам: отправлено %d/%d", sent, len(result.emails))
        except Exception:                                    # noqa: BLE001
            logger.exception("Не удалось отправить письма о предзаказах")
    return result


# ═══════════════════════════════════════════════════════════
# ФОНОВАЯ УБОРКА
# ═══════════════════════════════════════════════════════════

def close_stale_supplies(db: Session, days: int = STALE_DAYS, now: datetime | None = None) -> int:
    """Закрывает «поставки из 1С», про которые 1С давно ничего не присылала
    (инвойс удалили или обмен остановился): иначе они бы вечно висели «в пути»."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=days)
    stale = (db.query(Supply)
             .filter(Supply.external_id != "", Supply.status.in_([IN_TRANSIT, "Ожидается"]),
                     or_(Supply.external_synced_at < cutoff,
                         and_(Supply.external_synced_at.is_(None), Supply.created_at < cutoff)))
             .all())
    for supply in stale:
        supply.status = CLOSED
        supply.notes = ((supply.notes or "")[:440] +
                        f" (закрыта автоматически: 1С {days}+ дн. не присылала этот инвойс)")
        for item in supply.items:
            item.is_active = False
    if stale:
        db.commit()
        logger.info("Закрыто забытых поставок из 1С: %d", len(stale))
    return len(stale)


def close_stale_job() -> None:
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        close_stale_supplies(db)
    except Exception as e:                                   # noqa: BLE001
        db.rollback()
        logger.warning("close_stale_supplies: %s", e)
    finally:
        db.close()
