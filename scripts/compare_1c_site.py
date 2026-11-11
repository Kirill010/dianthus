#!/usr/bin/env python
"""Построчная сверка 1С и сайта: всё ли на месте, нет ли лишнего, сходятся ли остатки, цены, пачки и страны.

Что нужно:
  1) отчёт 1С «Сверка»: в 1С «Файл → Открыть → ОбменССайтом → 5. ДОПОЛНИТЕЛЬНО → Сверка: все строки инвойсов»,
     в окне отчёта щёлкнуть в текст, Ctrl+A, Ctrl+C, вставить в файл (например, sverka.txt). Ничего не отправляется;
  2) строки полки сайта: на сервере `docker compose exec -T app python scripts/data_check.py --rows > rows.tsv`.

Запуск:   python scripts/compare_1c_site.py sverka.txt rows.tsv
Остаток на сайте считается целыми упаковками (остаток меньше упаковки на полку не попадает), цена — за штуку.
Расхождения в остатках бывают честными: после последней отправки в 1С продали или оприходовали товар. Поэтому
сверку делают сразу после отправки поставок («1. ПОСТАВКА → Да»), тогда расхождений быть не должно.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass


def norm(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").lower().replace("ё", "е")).strip()


def num(text: str) -> float:
    try:
        return float(str(text).replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0


@dataclass
class Row1C:
    invoice: str
    status: str
    name: str
    pack: int
    stems: int
    price: float
    country: str


def parse_report(text: str) -> list[Row1C]:
    """Строки отчёта «Сверка»: «поля инвойса || поля строки и карточки», поля через « | »."""
    rows = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if " || " not in line:
            continue
        left, right = line.split(" || ", 1)
        a = [x.strip() for x in left.split(" | ")]
        b = [x.strip() for x in right.split(" | ")]
        if len(a) < 9 or len(b) < 21 or not re.match(r"^(\d|д-)", a[0], re.I):
            continue
        # «ИТОГ пачка»: число, а если пачку программа угадала (в строке её нет), после числа стоит «?»
        rows.append(Row1C(invoice=a[0], status=a[8], name=b[0], pack=int(num(b[20].rstrip("?")) or 1), stems=int(num(b[17])),
                          price=num(b[15]), country=b[18]))
    return rows


def parse_site(text: str) -> dict[tuple[str, str], dict]:
    rows = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 7:
            continue
        rows[(p[0], norm(p[2]))] = {"name": p[2], "pack": int(p[3]), "packs": int(p[4]), "price": float(p[5]), "country": p[6]}
    return rows


def compare(rows_1c: list[Row1C], site: dict) -> dict:
    expect: dict[tuple[str, str], dict] = {}
    for r in rows_1c:
        if r.status != "received" or r.stems <= 0 or r.pack <= 0:
            continue
        e = expect.setdefault((r.invoice, norm(r.name)), {"name": r.name, "stems": 0, "pack": r.pack, "price": r.price, "country": r.country})
        e["stems"] += r.stems
    expect = {k: dict(v, packs=v["stems"] // v["pack"]) for k, v in expect.items() if v["stems"] // v["pack"] > 0}

    missing = [k for k in expect if k not in site]
    extra = [k for k in site if k not in expect]
    # «пропавшая» и «лишняя» позиции в одном инвойсе с той же ценой и числом упаковок: скорее всего, товар переименовали в 1С
    renamed, free = [], list(extra)
    for m in list(missing):
        e = expect[m]
        twin = next((x for x in free if x[0] == m[0] and abs(site[x]["price"] - e["price"]) < 0.011 and site[x]["pack"] == e["pack"]), None)
        if twin is not None:
            renamed.append((m, twin))
            free.remove(twin)
    renamed_missing = {m for m, _ in renamed}
    renamed_extra = {x for _, x in renamed}
    missing = [m for m in missing if m not in renamed_missing]
    extra = [x for x in extra if x not in renamed_extra]
    qty, price, pack, country = [], [], [], []
    for k, e in expect.items():
        s = site.get(k)
        if s is None:
            continue
        if s["packs"] != e["packs"]:
            qty.append((k, e["packs"], s["packs"]))
        if abs(s["price"] - e["price"]) > 0.011:
            price.append((k, e["price"], s["price"]))
        if s["pack"] != e["pack"]:
            pack.append((k, e["pack"], s["pack"]))
        if norm(s["country"]) != norm(e["country"]):
            country.append((k, e["country"], s["country"]))
    return {"expect": expect, "missing": missing, "extra": extra, "renamed": renamed, "qty": qty, "price": price, "pack": pack, "country": country,
            "invoices_1c": len({r.invoice for r in rows_1c if r.status == "received" and r.stems > 0}),
            "invoices_site": len({k[0] for k in site}),
            "packs_1c": sum(e["packs"] for e in expect.values()), "packs_site": sum(s["packs"] for s in site.values())}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    rows_1c = parse_report(open(sys.argv[1], encoding="utf-8").read())
    site = parse_site(open(sys.argv[2], encoding="utf-8").read())
    r = compare(rows_1c, site)
    print(f"1С: строк в отчёте {len(rows_1c)}, позиций с остатком от одной упаковки {len(r['expect'])}, инвойсов {r['invoices_1c']}, упаковок {r['packs_1c']}")
    print(f"Сайт: позиций на полке {len(site)}, инвойсов {r['invoices_site']}, упаковок {r['packs_site']}")
    print(f"\nЕсть в 1С, нет на сайте: {len(r['missing'])}")
    for k in r["missing"][:25]:
        print(f"   {k[0]}  {r['expect'][k]['name']}  ({r['expect'][k]['packs']} упак.)")
    print(f"Есть на сайте, нет в 1С: {len(r['extra'])}")
    for k in r["extra"][:25]:
        print(f"   {k[0]}  {site[k]['name']}  ({site[k]['packs']} упак.)")
    print(f"Названия в 1С и на сайте разные (скорее всего, товар переименовали в 1С после последней отправки): {len(r['renamed'])}")
    for m, x in r["renamed"][:25]:
        print(f"   {m[0]}  в 1С «{r['expect'][m]['name']}», на сайте «{site[x]['name']}»")
    print(f"Остаток отличается: {len(r['qty'])}")
    for k, e, s in sorted(r["qty"], key=lambda x: -abs(x[1] - x[2]))[:25]:
        print(f"   {k[0]}  {r['expect'][k]['name']}: в 1С {e} упак., на сайте {s}")
    print(f"Цена отличается: {len(r['price'])}")
    for k, e, s in r["price"][:15]:
        print(f"   {k[0]}  {r['expect'][k]['name']}: в 1С {e:.2f}, на сайте {s:.2f}")
    print(f"Размер упаковки отличается: {len(r['pack'])}")
    for k, e, s in r["pack"][:15]:
        print(f"   {k[0]}  {r['expect'][k]['name']}: в 1С {e}, на сайте {s}")
    print(f"Страна отличается: {len(r['country'])}")
    for k, e, s in r["country"][:15]:
        print(f"   {k[0]}  {r['expect'][k]['name']}: в 1С {e}, на сайте {s}")
    same = len(r["expect"]) - len(r["qty"]) - len(r["missing"]) - len(r["renamed"])
    more_site = sum(1 for _, e, s in r["qty"] if s > e)
    print(f"\nИТОГ: совпало по остатку {same} из {len(r['expect'])} позиций (на сайте больше, чем в 1С: {more_site}; меньше: {len(r['qty']) - more_site}); "
          f"нет на сайте {len(r['missing'])}; лишних на сайте {len(r['extra'])}; переименованы {len(r['renamed'])}.")
    return 0 if not (r["missing"] or r["extra"] or r["price"] or r["pack"] or r["country"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
