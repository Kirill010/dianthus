"""Excel: экспорт заказов/клиентов, импорт товаров."""
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

HEADER_FILL = PatternFill("solid", fgColor="2D6A4F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")


def _header(ws) -> None:
    for c in ws[1]:
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = HEADER_ALIGN


def _autosize(ws, max_width: int = 40) -> None:
    for col in ws.columns:
        ml = max((len(str(c.value or "")) for c in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(ml + 3, max_width)


def _stream(wb: Workbook) -> BytesIO:
    s = BytesIO()
    wb.save(s)
    s.seek(0)
    return s


def export_orders_to_excel(orders: list) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заказы"
    ws.append(["№", "Дата", "Компания", "Контакт", "Email",
               "Товар", "Кол-во", "Ед.", "Цена", "Сумма позиции",
               "Итого", "Статус", "Комментарий"])
    _header(ws)
    for order in orders:
        user = order.user
        first = True
        for item in order.items or [None]:
            if item is None:
                ws.append([order.id,
                           order.created_at.strftime("%d.%m.%Y %H:%M"),
                           user.company_name if user else "Гость",
                           user.full_name if user else "",
                           user.email if user else "",
                           "—", 0, "", 0, 0, order.total_price,
                           order.status, order.comment])
                break
            ws.append([
                order.id if first else "",
                order.created_at.strftime("%d.%m.%Y %H:%M") if first else "",
                (user.company_name if user else "Гость") if first else "",
                (user.full_name if user else "") if first else "",
                (user.email if user else "") if first else "",
                item.product_name, item.quantity, item.unit,
                item.price, item.subtotal,
                order.total_price if first else "",
                order.status if first else "",
                order.comment if first else "",
            ])
            first = False
    _autosize(ws)
    return _stream(wb)


def export_customers_to_excel(customers: list[dict]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Клиенты"
    ws.append(["Компания", "Контакт", "Email", "Телефон",
               "Заказов", "Сумма, ₽", "Последний заказ"])
    _header(ws)
    for c in customers:
        ws.append([c.get("company_name", ""), c.get("full_name", ""),
                   c.get("email", ""), c.get("phone", ""),
                   c.get("orders_count", 0), c.get("total_sum", 0),
                   c.get("last_order_date", "")])
    _autosize(ws)
    return _stream(wb)


PRODUCT_IMPORT_HEADERS = ["Название", "Страна", "Длина, см",
                          "Единица", "В упаковке", "Мин. заказ",
                          "Категория", "Описание"]


def import_products_from_excel(content: bytes) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    products: list[dict] = []
    try:
        wb = load_workbook(BytesIO(content), data_only=True)
    except Exception as e:
        return [], [f"Не удалось открыть файл: {e}"]

    ws = wb.active
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or all(c is None for c in row):
            continue
        try:
            name = str(row[0]).strip() if row[0] else ""
            if not name:
                warnings.append(f"Строка {i}: пустое название")
                continue

            def safe(idx, default=""):
                return row[idx] if idx < len(row) else None

            products.append({
                "name": name,
                "country": str(safe(1) or "").strip(),
                "length_cm": int(safe(2) or 0),
                "unit": str(safe(3) or "упаковка").strip(),
                "package_size": int(safe(4) or 1),
                "min_quantity": int(safe(5) or 1),
                "category": str(safe(6) or "Прочее").strip(),
                "description": str(safe(7) or "").strip(),
                "image_url": "",
            })
        except (ValueError, TypeError) as e:
            warnings.append(f"Строка {i}: {e}")
    return products, warnings


def build_products_import_template() -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(PRODUCT_IMPORT_HEADERS)
    ws.append(["Роза Freedom 60 см", "Эквадор", 60, "упаковка",
               25, 1, "Роза Эквадор", "Классическая красная"])
    _header(ws)
    _autosize(ws, max_width=30)
    return _stream(wb)

# ═══════════════════════════════════════════════════════════
# ПАРСЕР НАКЛАДНЫХ ОТ ПОСТАВЩИКОВ
# ═══════════════════════════════════════════════════════════
#
# Поддерживает .xls (старый Excel) и .xlsx.
# Ищет в документе шапку таблицы по ключевым словам
# ("Товар", "Количество", "Цена", "Сумма") и читает строки.
#
# НЕ путать с import_products_from_excel — тот импортирует
# справочник товаров из строгого шаблона.
# ═══════════════════════════════════════════════════════════

import re

# Возможные названия колонок в накладных (case-insensitive, подстрока)
_COL_ALIASES: dict[str, list[str]] = {
    "name":     ["товар", "наименование", "название"],
    "quantity": ["количество", "кол-во", "колво"],
    "unit":     ["ед.", "ед ", "единица", "ед.изм"],
    "price":    ["цена"],
    "total":    ["сумма", "стоимость"],
    "country":  ["страна происхождения", "страна"],
}

# Стоп-слова: если в строке они есть — таблица закончилась
_STOP_WORDS = ["итого", "всего наименований", "в том числе",
               "всего:", "ндс", "подпись", "отпустил", "получил"]

# Длина стебля: реалистичный диапазон в см
_LENGTH_MIN = 20
_LENGTH_MAX = 150

# Эвристики для категорий по названию
_CATEGORY_PATTERNS: list[tuple[str, str]] = [
    (r"\bроза\b|\brose\b|\bnaomi\b|\bavalanche\b|\bfreedom\b", "Роза Эквадор"),
    (r"\bгвоздик", "Гвоздика"),
    (r"\bхризантем", "Хризантема"),
    (r"\bтюльпан", "Тюльпан"),
    (r"\bпион", "Пион"),
    (r"\bгоршеч", "Горшечные"),
    (r"\bзелень\b|\bgreenery\b", "Зелень"),
]


def _read_xls(content: bytes) -> list[list]:
    """Читает .xls через xlrd. Возвращает список списков."""
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    sheet = wb.sheet_by_index(0)
    return [sheet.row_values(r) for r in range(sheet.nrows)]


def _read_xlsx(content: bytes) -> list[list]:
    """Читает .xlsx через openpyxl. Возвращает список списков."""
    wb = load_workbook(BytesIO(content), data_only=True)
    ws = wb.active
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _to_float(v) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = (str(v).strip()
         .replace(",", ".")
         .replace(" ", "")
         .replace("\xa0", "")
         .replace("₽", "")
         .replace("RUB", ""))
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_int(v) -> int:
    return int(_to_float(v))


def _clean_name(raw) -> str:
    """Убирает переносы строк, лишние пробелы."""
    if raw is None:
        return ""
    return re.sub(r"\s+", " ", str(raw)).strip()


def _find_header(rows: list[list]) -> tuple[int, dict[str, int]]:
    """
    Ищет строку-шапку в первых 60 строках.
    Возвращает (индекс строки, {поле: индекс колонки}).
    Если шапки нет — (None, {}).
    """
    for i, row in enumerate(rows[:60]):
        col_map: dict[str, int] = {}
        for j, cell in enumerate(row):
            s = _clean_name(cell).lower().replace("\n", " ")
            if not s:
                continue
            for field, aliases in _COL_ALIASES.items():
                if field in col_map:
                    continue
                for alias in aliases:
                    if alias in s:
                        col_map[field] = j
                        break
        # Шапка считается найденной, если есть товар + количество + цена
        if {"name", "quantity", "price"} <= col_map.keys():
            return i, col_map
    return -1, {}


def _is_stop_row(row: list) -> bool:
    """Строка — конец таблицы?"""
    if not row:
        return True
    # Берём первые 8 ячеек
    chunk = " ".join(_clean_name(c) for c in row[:8] if c).lower()
    if not chunk.strip():
        return True
    return any(w in chunk for w in _STOP_WORDS)


def _extract_length(name: str) -> int:
    """Ищет длину стебля в названии (число 20-150)."""
    for m in re.finditer(r"\b(\d{2,3})\b", name):
        n = int(m.group(1))
        if _LENGTH_MIN <= n <= _LENGTH_MAX:
            return n
    return 0


def _guess_category(name: str) -> str:
    low = name.lower()
    for pattern, category in _CATEGORY_PATTERNS:
        if re.search(pattern, low):
            return category
    return "Прочее"


def parse_invoice(content: bytes, filename: str
                  ) -> tuple[list[dict], list[str]]:
    """
    Универсальный парсер накладных поставщиков.

    Возвращает:
        items — список позиций
            [{"name", "quantity", "unit", "price", "total",
              "length_cm", "category", "country"}, ...]
        warnings — список предупреждений (не ошибок).

    Пример использования:
        items, warnings = parse_invoice(file_bytes, "nakladnaya.xls")
    """
    warnings: list[str] = []
    fn = (filename or "").lower()

    # 1. Читаем файл в зависимости от формата
    try:
        if fn.endswith(".xls"):
            rows = _read_xls(content)
        elif fn.endswith(".xlsx"):
            rows = _read_xlsx(content)
        else:
            return [], ["Нужен файл .xls или .xlsx"]
    except Exception as e:
        return [], [f"Не удалось открыть файл: {e}"]

    if not rows:
        return [], ["Файл пуст"]

    # 2. Ищем шапку
    header_idx, col_map = _find_header(rows)
    if header_idx < 0:
        return [], [
            "Не нашёл шапку таблицы. Нужны колонки: "
            "«Товар», «Количество», «Цена»"
        ]

    # 3. Читаем данные построчно
    items: list[dict] = []

    def cell(row: list, field: str):
        j = col_map.get(field)
        if j is None or j >= len(row):
            return None
        return row[j]

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]

        if _is_stop_row(row):
            break

        name = _clean_name(cell(row, "name"))
        if len(name) < 2:
            continue

        # На всякий случай: пропускаем встроенные «итого» внутри таблицы
        if any(w in name.lower() for w in ["итого", "всего", "ндс"]):
            continue

        quantity = _to_int(cell(row, "quantity"))
        price = _to_float(cell(row, "price"))
        total = _to_float(cell(row, "total"))

        if quantity <= 0 or price <= 0:
            warnings.append(
                f"Строка {i + 1}: пропущена (кол-во={quantity}, цена={price})"
            )
            continue

        # Проверка суммы (если в файле указана)
        calc = quantity * price
        if total and abs(total - calc) > 1:
            warnings.append(
                f"Строка {i + 1}: сумма {total} ≠ {quantity}×{price}={calc}"
            )

        unit = _clean_name(cell(row, "unit")) or "шт"
        country = _clean_name(cell(row, "country"))

        items.append({
            "name": name,
            "quantity": quantity,
            "unit": unit,
            "price": price,
            "total": total,
            "length_cm": _extract_length(name),
            "category": _guess_category(name),
            "country": country,
        })

    if not items:
        warnings.append("В таблице не нашлось строк с товарами")

    return items, warnings