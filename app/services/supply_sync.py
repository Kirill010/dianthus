# app/services/supply_sync.py
"""Поставки (инвойсы) из 1С: остатки и цены ПО ПОСТАВКАМ.

В 1С приход цветов от поставщика оформляют документом «Инвойс …» (рейс, поставщик,
дата, строки товаров). Сайт работает по поставкам: каждая строка инвойса — отдельная
позиция каталога со своей ценой и своим остатком; клиент заказывает конкретную позицию
конкретной поставки, а в заказе для 1С у каждой строки указана эта поставка — по ней 1С
списывает товар из нужной партии.

Как это устроено:
  * 1С присылает инвойсы вместе с их ТЕКУЩИМ состоянием:
      "in_transit" — ещё в пути: клиенты могут сделать предзаказ;
      "received"   — оприходован: товар на полке, quantity строки — ОСТАТОК партии
                     (сколько штук ещё не продано), а не количество по инвойсу;
      "cancelled"  — отменён / удалён.
    Сайт создаёт или обновляет поставку по идентификатору инвойса (повторная отправка
    ничего не дублирует). Строка с остатком 0 снимается с полки.
  * Когда инвойс получает статус «received», клиентам с предзаказом резервируется товар
    этой же строки и уходит уведомление.
  * Пустой список ничего не закрывает: статус приходит явно, а не «по отсутствию».
    Чтобы закрыть сразу все поставки, которых 1С больше не присылает, список отправляют
    с полем full=true.
  * Забытые «в пути» (1С давно не присылала инвойс) закрываются сами через 21 день.
"""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from ..models import Notification, OrderItem, Preorder, Product, Supply, SupplyItem
from .exchange_log import DRY_RUN_MARK
from .catalog_sync import (MAX_ERRORS_IN_REPLY, Catalog, RowError, SyncCommitError,
                           apply_classification, clean, ext_id_of, lock_exchange,
                           new_product, parse_product_row, pending_packs,
                           resolve_classification)
from .taxonomy import Taxonomy, TaxonomyError, describe

logger = logging.getLogger(__name__)

IN_TRANSIT = "В пути"
# Поставка из 1С приехала: позиции с остатком лежат на полке. Тот же статус получает
# закрытая поставка (позиции сняты с полки), поэтому о полке судят по самим позициям.
ARRIVED = "Разгружен"
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
    created: int = 0            # новых поставок
    updated: int = 0            # обновлено поставок
    received: int = 0           # стали «оприходованы» (товар на полке)
    cancelled: int = 0          # отменено
    closed: int = 0             # закрыто при full=true (1С их больше не присылает)
    skipped: int = 0            # «отменён», а на сайте такой поставки не было
    lines: int = 0              # строк товаров принято
    products_created: int = 0   # новых товаров в справочнике
    preorders_fulfilled: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    emails: list[dict] = field(default_factory=list)      # письма клиентам (шлём после commit)
    # что при этом появилось в справочниках (вид, цвет, рост, страна): {"color": ["Белый"], …}
    dictionaries: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "status": "ok",
            "dry_run": self.dry_run,
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "received": self.received,
            "cancelled": self.cancelled,
            "closed": self.closed,
            "skipped": self.skipped,
            "lines": self.lines,
            "products_created": self.products_created,
            "preorders_fulfilled": self.preorders_fulfilled,
            "errors": self.errors[:MAX_ERRORS_IN_REPLY],
            "errors_total": len(self.errors),
            "warnings": self.warnings[:MAX_ERRORS_IN_REPLY],
            "dictionaries_created": self.dictionaries,
        }

    def summary(self) -> str:
        text = (f"инвойсов: {self.processed}, новых: {self.created}, "
                f"обновлено: {self.updated}, на полке: {self.received}")
        if self.cancelled:
            text += f", отменено: {self.cancelled}"
        if self.closed:
            text += f", закрыто: {self.closed}"
        if self.preorders_fulfilled:
            text += f", предзаказов выполнено: {self.preorders_fulfilled}"
        if self.dictionaries:
            text += ", новых значений справочников — " + describe(self.dictionaries)
        if self.errors:
            text += f", ошибок: {len(self.errors)}"
        if self.dry_run:
            text = f"{DRY_RUN_MARK} — {text}"
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
    lines = item.get("items")                        # None — строки не присылали: состав не меняем
    if lines is not None and not isinstance(lines, list):
        raise RowError("items должен быть массивом")
    if lines is not None and len(lines) > MAX_LINES_PER_SUPPLY:
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
        "lines": None if lines is None else [ln for ln in lines if isinstance(ln, dict)],
    }


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

def _referenced(db: Session, item_id: int) -> bool:
    """На позицию ссылаются заказы или предзаказы: удалить её нельзя, только снять с полки."""
    return bool(db.query(OrderItem.id).filter(OrderItem.supply_item_id == item_id).first()
                or db.query(Preorder.id).filter(Preorder.supply_item_id == item_id).first())


def _upsert_lines(db: Session, supply: Supply, sp: dict, catalog: Catalog, tax: Taxonomy,
                  result: SupplySyncResult, *, on_shelf: bool, pending: dict[int, int]) -> None:
    """Строки инвойса → позиции поставки. on_shelf — поставка оприходована: позиции с остатком
    видны в каталоге; иначе (в пути) они только для предзаказа."""
    by_product: dict[Product, SupplyItem] = {si.product: si for si in supply.items}
    wanted: dict[int, list] = {}             # id(Product) → [товар, упаковок, цена]
    protected: set[int] = set()              # строка пришла с ошибкой — позицию не трогаем
    for idx, line in enumerate(sp["lines"], start=1):
        label = clean(line.get("name"), 60) or "?"
        where = f"инвойс {sp['number']}, строка {idx} ({label})"
        try:
            row = parse_product_row(line)
        except RowError as e:
            result.errors.append(f"{where}: {e}")
            known = catalog.find(ext_id_of(line), clean(line.get("sku"), 100),
                                 clean(line.get("name"), 200), clean(line.get("barcode"), 64))
            if known is not None:
                protected.add(id(known))
            continue
        product = catalog.find(row["ext"], row["sku"], row["name"], row["barcode"] or "")
        try:
            resolved = resolve_classification(tax, row)
        except TaxonomyError as e:
            result.errors.append(f"{where}: {e}")
            if product is not None:
                protected.add(id(product))
            continue
        if product is not None:
            pack = max(1, product.package_size or 1)
        else:
            pack = max(1, row["package_size"] or row.get("package_size_hint") or 1)
        if row["stock_packs"] is not None:
            packs = row["stock_packs"]
        elif row["stock_stems"] is not None:
            packs = row["stock_stems"] // pack            # неполную упаковку не продаём
        else:
            result.errors.append(f"{where}: нет quantity (количество в штуках)")
            if product is not None:
                protected.add(id(product))
            continue
        price = row["price"] or (_known_price(db, product) if product is not None else 0.0)
        if packs > 0 and price <= 0:
            result.errors.append(f"{where}: нет price (цена продажи за штуку) и на сайте её тоже нет")
            if product is not None:
                protected.add(id(product))
            continue
        if packs <= 0 and not on_shelf:
            result.warnings.append(f"{where}: меньше одной упаковки ({pack} шт) — пропущена")
            continue

        if product is None:
            product = new_product(row, photos=row["photos"],
                                  main_image=row["photos"][0] if row["photos"] else "")
            apply_classification(tax, product, resolved, row)
            db.add(product)
            catalog.add(product)
            result.products_created += 1
        else:
            apply_classification(tax, product, resolved, row)
        # страна: из справочника/названия («Роза Кения 40»), иначе — страна самого инвойса
        if product.country_ref is None and row["refs"]["country"] is None and sp["country"]:
            try:
                product.country_ref = tax.by_name("country", sp["country"])
            except TaxonomyError:
                pass

        got = wanted.get(id(product))
        if got is None:
            wanted[id(product)] = [product, packs, price]
        else:
            got[1] += packs                                # один товар в двух строках — складываем

    for product, packs, price in wanted.values():
        item = by_product.get(product)
        stock = packs
        if on_shelf and item is not None and item.id is not None and pending:
            stock = max(0, stock - pending.get(item.id, 0))   # заказы, которые 1С ещё не забрала
        active = on_shelf and stock > 0
        if item is None:
            if on_shelf and stock <= 0:
                continue                                   # распродано, а у нас такой позиции и не было
            item = SupplyItem(supply=supply, product=product, price=price, stock=stock,
                              is_active=active)
            db.add(item)
            by_product[product] = item
        else:
            item.price, item.stock, item.is_active = price or item.price, stock, active
        result.lines += 1

    # позиции, которых в инвойсе больше нет
    in_payload = {id(p) for p, _packs, _price in wanted.values()} | protected
    for product, item in list(by_product.items()):
        if id(product) in in_payload or item.id is None:
            continue
        if _referenced(db, item.id):
            item.stock, item.is_active = 0, False          # заказы и клиенты ссылаются — не удаляем
        else:
            supply.items.remove(item)                      # delete-orphan удалит строку


def fulfil_preorders(db: Session, result: SupplySyncResult) -> None:
    """Предзаказы по оприходованным поставкам: резервируем товар и зовём клиента.

    Берём ту же позицию, на которую клиент делал предзаказ (она теперь на полке); если её
    уже нет, — позицию «без поставки» (products/sync). Повторяется при каждом обмене, поэтому
    если остатки доехали позже инвойса, предзаказ выполнится при следующем обмене.
    Очерёдность — по времени предзаказа.
    """
    pending = (
        db.query(Preorder)
        .options(joinedload(Preorder.user), joinedload(Preorder.product),
                 joinedload(Preorder.supply_item))
        .join(SupplyItem, SupplyItem.id == Preorder.supply_item_id)
        .join(Supply, Supply.id == SupplyItem.supply_id)
        .filter(Preorder.is_fulfilled.is_(False), Supply.external_id != "",
                Supply.status == ARRIVED)
        .order_by(Preorder.created_at, Preorder.id)
        .all()
    )
    if not pending:
        return
    service_ids = [s.id for s in db.query(Supply.id).filter(Supply.is_service.is_(True)).all()]
    live: dict[int, SupplyItem] = {}
    if service_ids:
        for si in (db.query(SupplyItem).options(joinedload(SupplyItem.product))
                   .filter(SupplyItem.supply_id.in_(service_ids), SupplyItem.is_active.is_(True))
                   .order_by(SupplyItem.id).all()):
            live.setdefault(si.product_id, si)

    for p in pending:
        own = p.supply_item
        item = own if (own is not None and own.is_active and own.available_stock > 0) \
            else live.get(p.product_id)
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


def _close(supply: Supply, why: str) -> None:
    supply.status = ARRIVED
    supply.notes = ((supply.notes or "")[:440] + f" ({why})")
    for item in supply.items:
        item.is_active = False


def apply_supplies(db: Session, supplies: list[dict], *, dry_run: bool = False,
                   full: bool = False, reserve_unexported: bool = False) -> SupplySyncResult:
    """Применяет присланные инвойсы. Делает commit (или rollback при dry_run).

    full=True — «полный список»: все поставки из 1С, которых в этой выгрузке нет,
    закрываются (позиции снимаются с полки). Включайте, только когда присылаете ВСЕ
    поставки, которые ещё в пути или имеют остаток.
    """
    lock_exchange(db)
    result = SupplySyncResult(dry_run=dry_run)
    now = _utcnow()
    pending = pending_packs(db) if reserve_unexported else {}

    parsed: list[tuple[int, dict]] = []
    for idx, raw in enumerate(supplies, start=1):
        if not isinstance(raw, dict):
            continue
        try:
            parsed.append((idx, parse_supply(raw)))
        except RowError as e:
            label = clean(raw.get("number") or raw.get("id"), 40) or "?"
            result.errors.append(f"#{idx} (инвойс {label}): {e}")

    all_lines = [ln for _, sp in parsed if sp["status"] != "cancelled" for ln in (sp["lines"] or [])]
    catalog = Catalog(db, all_lines)
    tax = Taxonomy(db)
    listed: set[str] = set()

    for idx, sp in parsed:
        result.processed += 1
        listed.add(sp["ext"])
        supply = db.query(Supply).filter(Supply.external_id == sp["ext"],
                                         Supply.is_service.is_(False)).first()

        if sp["status"] in ("in_transit", "received"):
            on_shelf = sp["status"] == "received"
            fresh = supply is None
            if fresh:
                supply = Supply(external_id=sp["ext"], is_service=False)
                db.add(supply)
            was_arrived = (not fresh) and supply.status == ARRIVED
            supply.country = sp["country"] or sp["supplier"] or "Поставка из 1С"
            supply.city = sp["city"]
            supply.number = sp["number"]
            supply.arrival_date = sp["arrival"] or supply.arrival_date
            supply.status = ARRIVED if on_shelf else IN_TRANSIT
            supply.notes = _notes(sp)
            supply.external_synced_at = now
            db.flush()
            if sp["lines"] is None:
                # строки не присылали: состав и количества остаются прежними, меняется только
                # статус — оприходованный инвойс выкладывает на полку то, что в нём уже было
                for line_item in supply.items:
                    line_item.is_active = on_shelf and line_item.available_stock > 0
            else:
                _upsert_lines(db, supply, sp, catalog, tax, result, on_shelf=on_shelf,
                              pending=pending)
            if fresh and not supply.items:
                db.delete(supply)                              # пустую поставку не заводим
                result.skipped += 1
                result.warnings.append(f"инвойс {sp['number']}: нет ни одной подходящей строки "
                                       f"товара — не создан")
                continue
            if on_shelf and not was_arrived:
                result.received += 1
            result.created += 1 if fresh else 0
            result.updated += 0 if fresh else 1
            continue

        if supply is None:
            result.skipped += 1
            continue
        supply.external_synced_at = now
        item_ids = [i.id for i in supply.items if i.id is not None]
        waiting = db.query(Preorder).filter(
            Preorder.supply_item_id.in_(item_ids), Preorder.is_fulfilled.is_(False)).count() \
            if item_ids else 0
        if waiting:
            supply.status = ARRIVED
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

    if full:
        if not listed:
            result.warnings.append("full=true без поставок не применяется (он закрыл бы все поставки)")
        else:
            for supply in (db.query(Supply)
                           .filter(Supply.external_id != "", Supply.is_service.is_(False),
                                   Supply.status.in_([IN_TRANSIT, ARRIVED])).all()):
                if supply.external_id in listed or not (
                        supply.status == IN_TRANSIT or supply.active_items_count):
                    continue
                _close(supply, "закрыта: 1С не прислала её в полном списке")
                result.closed += 1

    db.flush()
    fulfil_preorders(db, result)
    result.dictionaries = tax.report()

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
    """Закрывает «поставки в пути» из 1С, про которые 1С давно ничего не присылала
    (инвойс удалили или обмен остановился): иначе они бы вечно висели «в пути»."""
    now = now or _utcnow()
    cutoff = now - timedelta(days=days)
    stale = (db.query(Supply)
             .filter(Supply.external_id != "", Supply.status.in_([IN_TRANSIT, "Ожидается"]),
                     or_(Supply.external_synced_at < cutoff,
                         and_(Supply.external_synced_at.is_(None), Supply.created_at < cutoff)))
             .all())
    for supply in stale:
        _close(supply, f"закрыта автоматически: 1С {days}+ дн. не присылала этот инвойс")
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
