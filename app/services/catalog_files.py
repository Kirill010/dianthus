# app/services/catalog_files.py
"""Разбор файлов с каталогом из 1С: CSV (текст) и Excel (.xlsx / .xls).

Зачем. Не у каждой 1С можно «подключить» обмен по интернету (старые версии,
облако без внешних запросов). Но выгрузить прайс-лист в Excel или CSV может
любой бухгалтер. Этот модуль превращает такой файл в строки, которые понимает
общий загрузчик (catalog_sync.apply_products) — те же проверки и те же правила,
что и при обмене по API.

Названия колонок угадываются по-русски и по-английски («Артикул», «Номенклатура»,
«Цена опт.», «В пач.», «Остаток», sku, name, price …). Строки-«заголовки
групп» и итоговые строки пропускаются.
"""
import csv
import io
import re

from .catalog_sync import MAX_PRODUCTS_PER_SYNC, RowError, clean, to_number
from .excel_service import (_is_stop_row, _read_xls, _read_xlsx,
                            _to_str_preserve, guess_package_size)

MAX_FILE_ROWS = MAX_PRODUCTS_PER_SYNC + 200

# поле → названия колонок (после нормализации: строчные, ё→е, без точек и скобок)
ALIASES: dict[str, list[str]] = {
    "id": ["id", "guid", "external id", "externalid", "external_id", "uid",
           "идентификатор", "код", "код номенклатуры", "код 1с", "ссылка"],
    "sku": ["артикул", "sku", "арт", "артикул товара"],
    "barcode": ["штрихкод", "штрих код", "штрих-код", "barcode", "ean", "ean13", "ean 13"],
    "name": ["наименование", "название", "номенклатура", "товар", "name",
             "наименование товара", "наименование номенклатуры"],
    "price": ["price per stem", "price_per_stem", "цена опт", "оптовая цена",
              "цена оптовая", "цена", "price", "цена за шт", "цена за штуку",
              "мелкий опт", "цена мелкий опт", "цена розн", "розничная цена"],
    "stock_stems": ["остаток шт", "остаток в штуках", "количество шт", "кол-во шт",
                    "stock stems", "stock_stems", "в наличии шт"],
    "stock_packs": ["остаток уп", "остаток упак", "остаток в упаковках",
                    "кол-во упак", "количество упак", "stock packs", "stock_packs"],
    "stock": ["остаток", "остатки", "количество", "кол-во", "колво", "stock",
              "в наличии", "свободный остаток", "доступно", "qty", "quantity"],
    "package_size": ["в пач", "в пачке", "шт в пач", "шт в пачке", "в упак",
                     "в упаковке", "шт в упаковке", "шт/упак", "упаковка",
                     "пачка", "package size", "package_size", "кратность"],
    # Справочники-фильтры: вид, цвет, рост, страна («Группа» сюда не относим: в 1С это
    # обычно «Срезка» на всё сразу, и из неё получился бы один «вид» на весь каталог)
    "kind": ["вид", "вид цветка", "категория", "kind", "category"],
    "color": ["цвет", "окрас", "color", "colour"],
    "height": ["рост", "рост см", "высота", "высота см", "длина", "длина см", "height",
               "height cm", "length cm"],
    "country": ["страна", "страна происхождения", "происхождение", "country"],
    "min_quantity": ["мин заказ", "минимальный заказ", "мин кол-во",
                     "min quantity", "min_quantity"],
    "description": ["описание", "description"],
    "photos": ["фото", "photo", "photos", "картинка", "image url", "image_url",
               "ссылка на фото"],
}


def norm_header(value) -> str:
    s = str(value if value is not None else "").lower().replace("ё", "е")
    s = re.sub(r"[.,()\[\]:;_]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NORMALIZED = {
    field: [norm_header(a) for a in names] for field, names in ALIASES.items()
}


def _map_columns(header: list, price_column: str = "") -> tuple[dict[str, int], dict]:
    """{поле: номер колонки} и сведения для пользователя (какие колонки цены нашли)."""
    cells = [norm_header(c) for c in header]
    mapping: dict[str, int] = {}
    price_candidates: list[tuple[int, int]] = []        # (приоритет, колонка)
    wanted = norm_header(price_column)

    for field, names in _NORMALIZED.items():
        for rank, alias in enumerate(names):
            for j, cell in enumerate(cells):
                if cell != alias:
                    continue
                if field == "price":
                    price_candidates.append((rank, j))
                elif field not in mapping:
                    mapping[field] = j
    if wanted:
        for j, cell in enumerate(cells):
            if cell == wanted:
                mapping["price"] = j
                break
    elif price_candidates:
        price_candidates.sort()
        mapping["price"] = price_candidates[0][1]
    others = [header[j] for _r, j in sorted(price_candidates) if j != mapping.get("price")]
    info = {"other_price_columns": [str(c).strip() for c in others if str(c).strip()]}
    return mapping, info


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_csv_table(text: str) -> list[list[str]]:
    lines = text.splitlines()
    sample = next((ln for ln in lines if ln.strip()), "")
    delimiter = max(";\t,|", key=lambda d: (sample.count(d), d == ";"))
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    table = []
    for i, row in enumerate(reader):
        if i > MAX_FILE_ROWS:
            raise RowError(f"В файле больше {MAX_FILE_ROWS} строк — разделите его на части")
        table.append(row)
    return table


def read_table(content: bytes, filename: str) -> list[list]:
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        return _read_xlsx(content)
    if name.endswith(".xls"):
        return _read_xls(content)
    return read_csv_table(decode_text(content))


def _find_header(table: list[list]) -> int:
    for i, row in enumerate(table[:40]):
        mapping, _ = _map_columns(row)
        if "name" in mapping and ("price" in mapping or any(
                k in mapping for k in ("stock", "stock_packs", "stock_stems"))):
            return i
    return -1


def rows_from_table(table: list[list], *, stock_unit: str = "stems",
                    price_unit: str = "stem", price_column: str = "",
                    guess_pack: bool = True) -> tuple[list[dict], dict]:
    """Таблица → (строки для загрузчика, сведения для пользователя).

    stock_unit: что значит колонка «Остаток» — "stems" (штуки) или "packs" (упаковки);
    price_unit: что значит колонка цены — "stem" (за штуку) или "pack" (за упаковку).
    Бросает RowError, если не нашла заголовок таблицы.
    """
    start = _find_header(table)
    if start < 0:
        raise RowError(
            "Не нашёл таблицу с товарами. В первой строке таблицы нужны названия "
            "колонок, например: Артикул · Наименование · Цена · Остаток · В пач.")
    header = table[start]
    mapping, info = _map_columns(header, price_column)
    if "name" not in mapping:
        raise RowError("Не нашёл колонку с названием товара («Наименование»/«Номенклатура»).")
    if "price" not in mapping:
        raise RowError("Не нашёл колонку с ценой («Цена», «Цена опт.»).")
    if not any(k in mapping for k in ("stock", "stock_packs", "stock_stems")):
        raise RowError("Не нашёл колонку с остатком («Остаток», «Количество»).")

    rows: list[dict] = []
    skipped: list[str] = []
    for offset, raw in enumerate(table[start + 1:], start=start + 2):
        raw = list(raw) + [None] * (max(mapping.values()) + 1 - len(raw))
        if all(c in (None, "") for c in raw) or _is_stop_row(raw):
            continue

        def cell(field):
            j = mapping.get(field)
            return raw[j] if j is not None and j < len(raw) else None

        name = clean(_to_str_preserve(cell("name")), 200)
        if not name:
            continue
        price_raw, stock_raw = cell("price"), cell("stock")
        has_stock = any(cell(k) not in (None, "") for k in
                        ("stock", "stock_packs", "stock_stems"))
        if price_raw in (None, "") and not has_stock:
            continue                                    # заголовок группы, а не товар
        item: dict = {"name": name}

        def put(field, value):
            """Пустая (или нулевая) ячейка = «не передано»: существующее значение не затираем."""
            if value is not None and str(value).strip() not in ("", "0", "0.0"):
                item[field] = value

        try:
            for field in ("id", "sku"):
                put(field, _to_str_preserve(cell(field)))
            pack = None
            pack_raw = cell("package_size")
            if pack_raw not in (None, ""):
                n = to_number(pack_raw, "В пач.")
                if n:
                    item["package_size"] = n
                    pack = int(n)
            if pack is None and guess_pack:
                item["package_size_hint"] = pack = guess_package_size(name)

            price = to_number(price_raw, "Цена")
            if price_unit == "pack":
                price = price / max(1, pack or 1)
            item["price"] = round(price, 4)

            # quantity — в штуках; «в упаковках» помечается отдельно (quantity_unit)
            if cell("stock_packs") not in (None, ""):
                item["quantity"] = to_number(cell("stock_packs"), "Остаток, упак.")
                item["quantity_unit"] = "packs"
            elif cell("stock_stems") not in (None, ""):
                item["quantity"] = to_number(cell("stock_stems"), "Остаток, шт.")
            elif stock_raw not in (None, ""):
                item["quantity"] = max(0, to_number(stock_raw, "Остаток"))
                if stock_unit == "packs":
                    item["quantity_unit"] = "packs"
            else:
                item["quantity"] = 0

            for field in ("barcode", "kind", "color", "height", "country", "description"):
                put(field, _to_str_preserve(cell(field)))
            if cell("min_quantity") not in (None, ""):
                put("min_quantity", to_number(cell("min_quantity"), "min_quantity"))
            put("photos", str(cell("photos") or "").strip())
        except RowError as e:
            skipped.append(f"строка {offset} ({name[:40]}): {e}")
            continue
        rows.append(item)

    if len(rows) > MAX_PRODUCTS_PER_SYNC:
        raise RowError(f"В файле {len(rows)} товаров — максимум {MAX_PRODUCTS_PER_SYNC} за раз")
    info.update({
        "header_row": start + 1,
        "columns": {f: str(header[j]).strip() for f, j in mapping.items()},
        "skipped": skipped,
        "rows": len(rows),
    })
    return rows, info


def parse_catalog_file(content: bytes, filename: str, **options) -> tuple[list[dict], dict]:
    return rows_from_table(read_table(content, filename), **options)


def parse_catalog_text(text: str, **options) -> tuple[list[dict], dict]:
    """CSV, присланный 1С прямо в теле запроса."""
    return rows_from_table(read_csv_table(text), **options)
