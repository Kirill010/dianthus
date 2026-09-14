"""
Excel: экспорт заказов, экспорт клиентов, импорт товаров.

Всё через BytesIO — файлы на диск не сохраняются.
"""
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill


HEADER_FILL = PatternFill("solid", fgColor="2D6A4F")
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_ALIGN = Alignment(horizontal="center", vertical="center")


def _style_header(ws) -> None:
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = HEADER_ALIGN


def _autosize(ws, max_width: int = 40) -> None:
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, max_width)


def _to_stream(wb: Workbook) -> BytesIO:
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


# ═══════ ЭКСПОРТ ЗАКАЗОВ ════════════════════════════════════

def export_orders_to_excel(orders: list) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Заказы"
    ws.append([
        "№ заказа", "Дата", "Компания", "Контактное лицо", "Email",
        "Товар", "Кол-во", "Ед.", "Цена", "Сумма позиции",
        "Итого по заказу", "Статус", "Комментарий",
    ])
    _style_header(ws)

    for order in orders:
        user = order.user
        first = True
        for item in order.items or [None]:
            if item is None:
                ws.append([
                    order.id, order.created_at.strftime("%d.%m.%Y %H:%M"),
                    user.company_name if user else "Гость",
                    user.full_name if user else "",
                    user.email if user else "",
                    "—", 0, "", 0, 0,
                    order.total_price, order.status, order.comment,
                ])
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
    return _to_stream(wb)


# ═══════ ЭКСПОРТ КЛИЕНТОВ ═══════════════════════════════════

def export_customers_to_excel(customers: list[dict]) -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Клиенты"
    ws.append([
        "Компания", "Контактное лицо", "Email", "Телефон",
        "Заказов", "Сумма заказов, ₽", "Последний заказ",
    ])
    _style_header(ws)
    for c in customers:
        ws.append([
            c.get("company_name", ""),
            c.get("full_name", ""),
            c.get("email", ""),
            c.get("phone", ""),
            c.get("orders_count", 0),
            c.get("total_sum", 0),
            c.get("last_order_date", ""),
        ])
    _autosize(ws)
    return _to_stream(wb)


# ═══════ ИМПОРТ ТОВАРОВ ═════════════════════════════════════

PRODUCT_IMPORT_HEADERS = [
    "Название", "Цена (опт)", "Остаток",
    "Единица", "В упаковке", "Мин. заказ",
    "Страна", "Длина, см", "Описание",
]

_MIN_COLUMNS = 3   # название, цена, остаток — обязательные


def import_products_from_excel(file_bytes: bytes) -> tuple[list[dict], list[str]]:
    """Парсит .xlsx → (товары, предупреждения)."""
    warnings: list[str] = []
    products: list[dict] = []

    try:
        wb = load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return [], [f"Не удалось открыть файл: {e}"]

    ws = wb.active
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True),
                                  start=2):
        if not row or all(c is None for c in row):
            continue
        if len(row) < _MIN_COLUMNS:
            warnings.append(
                f"Строка {row_idx}: слишком мало колонок "
                f"(нужно минимум {_MIN_COLUMNS})"
            )
            continue
        try:
            name = str(row[0]).strip() if row[0] else ""
            if not name:
                warnings.append(f"Строка {row_idx}: пустое название")
                continue

            price = float(row[1] or 0)
            stock = int(row[2] or 0)
            if price < 0 or stock < 0:
                warnings.append(f"Строка {row_idx}: отрицательные значения")
                continue

            def _safe(idx, default=""):
                return row[idx] if idx < len(row) else None

            products.append({
                "name": name,
                "price": price,
                "stock": stock,
                "unit": str(_safe(3) or "упаковка").strip(),
                "package_size": int(_safe(4) or 1),
                "min_quantity": int(_safe(5) or 1),
                "country": str(_safe(6) or "").strip(),
                "length_cm": int(_safe(7) or 0),
                "description": str(_safe(8) or "").strip(),
                "image_url": "",
            })
        except (ValueError, TypeError) as e:
            warnings.append(f"Строка {row_idx}: {e}")
    return products, warnings


def build_products_import_template() -> BytesIO:
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(PRODUCT_IMPORT_HEADERS)
    ws.append(["Роза Freedom 60 см", 110, 40,
               "упаковка", 25, 1, "Эквадор", 60, "Классическая красная роза"])
    _style_header(ws)
    _autosize(ws, max_width=30)
    return _to_stream(wb)