#!/usr/bin/env python
"""Проверка данных сайта: всё ли на месте, нет ли задвоений, нет ли странностей. ТОЛЬКО ЧТЕНИЕ: ничего не меняет.

Запуск на сервере (из папки /root/dianthus):
    docker compose exec -T app python scripts/data_check.py          # отчёт для человека
    docker compose exec -T app python scripts/data_check.py --rows   # строки полки таблицей (для сверки с 1С)
Без выкладки скрипта в образ:  docker compose exec -T app python - < scripts/data_check.py

Что проверяет: сколько чего; двойные коды 1С; одна и та же позиция дважды в поставке; одна и та же поставка дважды;
ручные и служебные партии, которые показывают товар второй раз рядом с 1С; одинаковые названия у разных карточек;
позиции без цены, с отрицательным остатком или резервом больше остатка; зависшие «в пути»; давно не было обмена.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
except NameError:                    # запуск через stdin (python - < data_check.py): рабочая папка уже /app
    pass

IN_TRANSIT = ("Ожидается", "В пути")


@dataclass
class Finding:
    level: str                       # problem | warn | ok
    title: str
    details: list[str] = field(default_factory=list)


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").lower().replace("ё", "е")).strip()


def run_checks(db, now: datetime | None = None) -> tuple[dict, list[Finding]]:
    from app.models import ExchangeLog, Order, Product, Supply, SupplyItem, User

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    products = db.query(Product).all()
    supplies = db.query(Supply).all()
    items = db.query(SupplyItem).all()
    by_product = {p.id: p for p in products}
    by_supply = {s.id: s for s in supplies}

    def on_shelf(it) -> bool:
        s = by_supply.get(it.supply_id)
        return bool(s) and it.is_available and s.status not in IN_TRANSIT

    shelf = [it for it in items if on_shelf(it)]
    stats = {
        "products": len(products),
        "supplies": len(supplies),
        "supplies_1c": sum(1 for s in supplies if s.external_id),
        "supplies_manual": sum(1 for s in supplies if not s.external_id and not s.is_service),
        "supplies_service": sum(1 for s in supplies if s.is_service),
        "shelf_items": len(shelf),
        "shelf_packs": sum(it.available_stock for it in shelf),
        "shelf_stems": sum(it.available_stems for it in shelf),
        "transit_items": sum(1 for it in items if it.is_active and by_supply.get(it.supply_id) is not None
                             and by_supply[it.supply_id].status in IN_TRANSIT),
        "orders": db.query(Order).count(),
        "orders_waiting_1c": db.query(Order).filter(Order.exported_at.is_(None), Order.status != "Отменён").count(),
        "users": db.query(User).count(),
        "users_pending": db.query(User).filter(User.is_approved.is_(False)).count(),
    }
    out: list[Finding] = []

    def add(level, title, details=None):
        out.append(Finding(level, title, list(details or [])))

    # ── задвоения ──
    ext = defaultdict(list)
    for p in products:
        if (p.external_id or "").strip():
            ext[p.external_id.strip()].append(p)
    twins = {k: v for k, v in ext.items() if len(v) > 1}
    add("problem" if twins else "ok", "Два товара с одним и тем же кодом 1С" if twins else "Нет двух товаров с одним кодом 1С",
        [f"код {k}: " + "; ".join(f"№{p.id} «{p.name}»" for p in v) for k, v in list(twins.items())[:15]])

    barcodes = defaultdict(list)
    for p in products:
        if (p.barcode or "").strip():
            barcodes[p.barcode.strip()].append(p)
    bc = {k: v for k, v in barcodes.items() if len({p.external_id for p in v}) > 1}
    add("warn" if bc else "ok", "Один штрихкод у разных товаров" if bc else "Нет одного штрихкода у разных товаров",
        [f"штрихкод {k}: " + "; ".join(f"№{p.id} «{p.name}»" for p in v) for k, v in list(bc.items())[:10]])

    pair = defaultdict(int)
    for it in items:
        pair[(it.supply_id, it.product_id)] += 1
    dup_items = [(k, n) for k, n in pair.items() if n > 1]
    add("problem" if dup_items else "ok", "Одна позиция дважды в одной поставке" if dup_items else "Нет позиции, повторённой в одной поставке",
        [f"поставка №{k[0]}, товар «{by_product[k[1]].name if k[1] in by_product else k[1]}»: {n} раза" for k, n in dup_items[:15]])

    sup_ext = defaultdict(list)
    for s in supplies:
        if (s.external_id or "").strip():
            sup_ext[s.external_id.strip()].append(s)
    dup_sup = {k: v for k, v in sup_ext.items() if len(v) > 1}
    add("problem" if dup_sup else "ok", "Одна поставка из 1С заведена дважды" if dup_sup else "Нет двух поставок с одним номером из 1С",
        [f"{k}: поставки " + ", ".join(f"№{s.id}" for s in v) for k, v in list(dup_sup.items())[:15]])

    manual_shelf = [it for it in shelf if by_supply[it.supply_id].external_id == "" and not by_supply[it.supply_id].is_service]
    add("warn" if manual_shelf else "ok", "Ручные поставки (не из 1С) выставлены на полку" if manual_shelf else "Ручных поставок на полке нет",
        [f"«{by_product[it.product_id].name}» ×{it.available_stock} упак. (поставка №{it.supply_id})" for it in manual_shelf[:15]])

    onec_products = {it.product_id for it in shelf if by_supply[it.supply_id].external_id}
    double_view = [it for it in shelf if by_supply[it.supply_id].is_service and it.product_id in onec_products]
    add("problem" if double_view else "ok", "Товар виден дважды: «остаток без поставки» и инвойс 1С" if double_view else "Нет товара, показанного рядом «без поставки» и из инвойса",
        [f"«{by_product[it.product_id].name}»" for it in double_view[:15]])

    groups = defaultdict(set)
    for it in shelf:
        p = by_product[it.product_id]
        groups[(norm(p.name), p.kind_id, p.height_id, p.country_id)].add(p.id)
    name_twins = {k: v for k, v in groups.items() if len(v) > 1}
    add("warn" if name_twins else "ok", "Разные карточки с одинаковым названием (возможный дубль в справочнике 1С)" if name_twins
        else "Нет разных карточек с одинаковым названием",
        [f"«{k[0]}»: карточки " + ", ".join(f"№{i}" for i in sorted(v)) for k, v in list(name_twins.items())[:15]])

    # ── странности ──
    no_price = [it for it in shelf if (it.price or 0) <= 0]
    add("problem" if no_price else "ok", "На полке позиции без цены" if no_price else "У всех позиций на полке есть цена",
        [f"«{by_product[it.product_id].name}» (поставка №{it.supply_id})" for it in no_price[:15]])

    bad_stock = [it for it in items if (it.stock or 0) < 0 or (it.reserved_stock or 0) < 0 or (it.reserved_stock or 0) > (it.stock or 0)]
    add("warn" if bad_stock else "ok", "Резерв больше остатка или отрицательные числа" if bad_stock else "Остатки и резервы без противоречий",
        [f"позиция №{it.id}: остаток {it.stock}, резерв {it.reserved_stock}" for it in bad_stock[:15]])

    hidden = [it for it in items if not it.is_active and (it.stock or 0) > 0 and by_supply.get(it.supply_id) is not None
              and by_supply[it.supply_id].status not in IN_TRANSIT]
    add("warn" if hidden else "ok", "Остаток есть, но позиция снята с полки" if hidden else "Нет скрытых остатков",
        [f"«{by_product[it.product_id].name}» ×{it.stock} (поставка №{it.supply_id})" for it in hidden[:15]])

    bad_pack = [p for p in products if (p.package_size or 0) <= 0]
    add("problem" if bad_pack else "ok", "Товары без размера упаковки" if bad_pack else "У всех товаров задан размер упаковки",
        [f"№{p.id} «{p.name}»" for p in bad_pack[:15]])

    unclassified = {it.product_id for it in shelf if by_product[it.product_id].kind_id is None or by_product[it.product_id].country_id is None}
    add("warn" if unclassified else "ok", "На полке товары без вида или страны" if unclassified else "У всех товаров на полке есть вид и страна",
        [f"«{by_product[i].name}»" for i in list(unclassified)[:15]])

    stale = [s for s in supplies if s.external_id and s.status in IN_TRANSIT and s.arrival_date and s.arrival_date < now - timedelta(days=21)]
    add("warn" if stale else "ok", "«В пути» дольше 21 дня" if stale else "Нет зависших «в пути»",
        [f"поставка {s.invoice_no or s.id}, плановая дата {s.arrival_date:%d.%m.%Y}" for s in stale[:15]])

    no_photo = sum(1 for it in shelf if not by_product[it.product_id].all_photos)
    add("warn" if no_photo else "ok", f"Фото нет у {no_photo} из {len(shelf)} позиций на полке" if no_photo else "У всех позиций на полке есть фото")

    # ── заказы ──
    old_new = db.query(Order).filter(Order.status == "Новый", Order.created_at < now - timedelta(days=1)).count()
    add("warn" if old_new else "ok", f"Новых заказов старше суток: {old_new}" if old_new else "Нет заказов, зависших в «Новый»")
    old_wait = db.query(Order).filter(Order.exported_at.is_(None), Order.status != "Отменён", Order.created_at < now - timedelta(days=1)).count()
    add("warn" if old_wait else "ok", f"Заказов, не забранных 1С больше суток: {old_wait}" if old_wait else "Нет заказов, которые 1С не забирает больше суток")

    # ── обмен ──
    from app.services.exchange_log import DRY_RUN_MARK

    recent_ok = (db.query(ExchangeLog).filter(ExchangeLog.endpoint == "supplies/sync", ExchangeLog.ok.is_(True))
                 .order_by(ExchangeLog.created_at.desc()).limit(60).all())
    real = next((r for r in recent_ok if not (r.summary or "").startswith(DRY_RUN_MARK)), None)
    dry = next((r for r in recent_ok if (r.summary or "").startswith(DRY_RUN_MARK)), None)
    if real is None:
        add("warn", "Настоящих отправок поставок из 1С в журнале нет" + (" (были только проверки без сохранения)" if dry else ""))
    else:
        hours = int((now - real.created_at).total_seconds() // 3600)
        details = [real.summary or ""]
        if dry is not None and dry.created_at > real.created_at:
            details.append(f"после неё была только проверка без сохранения ({dry.created_at:%d.%m.%Y %H:%M} UTC)")
        add("ok" if hours < 24 else "warn",
            f"Последняя настоящая отправка поставок из 1С: {real.created_at:%d.%m.%Y %H:%M} UTC ({hours} ч назад)", details)
    failed = (db.query(ExchangeLog).filter(ExchangeLog.ok.is_(False), ExchangeLog.created_at > now - timedelta(days=1))
              .order_by(ExchangeLog.created_at.desc()).all())
    add("warn" if failed else "ok", f"Неудачных обращений 1С за сутки: {len(failed)}" if failed else "За сутки неудачных обращений 1С не было",
        [f"{r.created_at:%d.%m %H:%M} UTC, {r.endpoint}, код {r.status_code}: {(r.summary or '')[:110]}" for r in failed[:5]])
    return stats, out


def shelf_rows(db) -> list[list[str]]:
    """Строки полки для сверки с 1С: номер инвойса, дата, название, штук в упаковке, упаковок, цена за штуку, страна."""
    from app.models import Supply, SupplyItem

    rows = []
    for it in db.query(SupplyItem).all():
        s, p = it.supply, it.product
        if s is None or p is None or not it.is_available or s.status in IN_TRANSIT:
            continue
        rows.append([s.invoice_no or f"ручная-{s.id}", s.arrival_date.strftime("%d.%m.%Y") if s.arrival_date else "",
                     p.name, str(p.package_size or 1), str(it.available_stock), f"{it.price:.2f}", p.country_name])
    return sorted(rows)


def main() -> int:
    from app.database import SessionLocal

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    db = SessionLocal()
    try:
        if "--rows" in sys.argv:
            for r in shelf_rows(db):
                print("\t".join(r))
            return 0
        stats, findings = run_checks(db)
    finally:
        db.close()

    print(f"ПРОВЕРКА ДАННЫХ САЙТА (только чтение), {datetime.now():%d.%m.%Y %H:%M}")
    print("\n== Сколько чего ==")
    print(f"Товаров в справочнике: {stats['products']}")
    print(f"Поставок: {stats['supplies']} (из 1С: {stats['supplies_1c']}, ручных: {stats['supplies_manual']}, служебных: {stats['supplies_service']})")
    print(f"На полке: {stats['shelf_items']} позиций, {stats['shelf_packs']} упаковок, {stats['shelf_stems']} штук; «в пути»: {stats['transit_items']} позиций")
    print(f"Заказов: {stats['orders']} (ждут забора из 1С: {stats['orders_waiting_1c']}); клиентов: {stats['users']} (ждут одобрения: {stats['users_pending']})")
    marks = {"ok": "ОК     ", "warn": "ВНИМАНИЕ", "problem": "ПРОБЛЕМА"}
    print("\n== Проверки ==")
    for f in findings:
        print(f"[{marks[f.level]}] {f.title}")
        for d in f.details:
            print(f"            - {d}")
    problems = sum(1 for f in findings if f.level == "problem")
    warns = sum(1 for f in findings if f.level == "warn")
    print(f"\nИТОГ: проблем {problems}, замечаний {warns}, проверок {len(findings)}.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
