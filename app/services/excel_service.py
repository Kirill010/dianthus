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