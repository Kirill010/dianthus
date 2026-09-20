"""Генерация PDF-счёта по заказу — через xhtml2pdf.

Кириллица поддерживается через шрифт DejaVu Sans.
"""
import logging
from datetime import datetime
from io import BytesIO

from .pdf_fonts import register_pdf_fonts, FONT_NAME

logger = logging.getLogger(__name__)


def generate_invoice_pdf(order, user, shop_info: dict) -> bytes:
    """Генерирует PDF-счёт. Возвращает bytes."""
    try:
        from xhtml2pdf import pisa
    except ImportError:
        logger.error("xhtml2pdf не установлен: pip install xhtml2pdf")
        raise

    # Регистрируем шрифт (один раз при первом вызове)
    font_name = register_pdf_fonts()

    # ── Данные клиента ──
    buyer_company = user.company_name if user else "Гость"
    buyer_name = user.full_name if user else "—"
    buyer_inn = (user.inn if user else "") or "—"
    buyer_phone = (user.phone if user else "") or "—"
    buyer_email = (user.email if user else "") or "—"

    # ── Позиции заказа ──
    rows = []
    for i, item in enumerate(order.items, 1):
        pack = item.package_size or 1
        stems = item.quantity * pack
        rows.append(f"""
            <tr>
                <td align="center">{i}</td>
                <td>{item.product_name}</td>
                <td align="center">{item.quantity}</td>
                <td align="center">{pack}</td>
                <td align="center">{stems}</td>
                <td align="right">{item.price:.2f}</td>
                <td align="right">{item.subtotal:.2f}</td>
            </tr>
        """)

    # ── Скидка ──
    discount_row = ""
    if order.discount_percent and order.discount_percent > 0:
        discount_row = f"""
            <tr>
                <td colspan="6" align="right">
                    Скидка {order.discount_percent:.0f}%:
                </td>
                <td align="right" style="color:#c97b4a;">
                    −{order.discount_amount:.2f} ₽
                </td>
            </tr>
        """

    # ── Комментарий ──
    comment_block = ""
    if order.comment:
        comment_block = f"""
            <p style="background:#ecf1e8; padding:8px;
                border-left:3px solid #4a6741;">
                <b>Комментарий:</b> {order.comment}
            </p>
        """

    # ── Адрес и телефон поставщика ──
    shop_address = shop_info.get('address') or ''
    shop_phone = shop_info.get('phone') or ''
    supplier_extra = ''
    if shop_address:
        supplier_extra += f"<p>Адрес: {shop_address}</p>"
    if shop_phone:
        supplier_extra += f"<p>Тел: {shop_phone}</p>"

    # ── HTML для PDF ──
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{
                size: A4;
                margin: 1.5cm;
            }}
            body {{
                font-family: "{font_name}";
                font-size: 11pt;
                color: #222;
            }}
            h1 {{
                color: #35492f;
                text-align: center;
                font-size: 20pt;
                margin-bottom: 5px;
                font-family: "{font_name}";
            }}
            .subtitle {{
                text-align: center;
                color: #777;
                font-size: 10pt;
                margin-bottom: 20px;
            }}
            .info-table {{
                width: 100%;
                margin-bottom: 20px;
                background: #faf6ef;
            }}
            .info-table td {{
                vertical-align: top;
                padding: 10px;
                border: none;
                font-size: 10pt;
            }}
            .info-table h3 {{
                color: #4a6741;
                font-size: 11pt;
                margin: 0 0 5px;
                font-family: "{font_name}";
            }}
            .info-table p {{
                margin: 2px 0;
            }}
            table.items {{
                width: 100%;
                border-collapse: collapse;
                margin-bottom: 15px;
            }}
            table.items th {{
                background: #4a6741;
                color: white;
                padding: 8px;
                font-size: 10pt;
                text-align: left;
                font-family: "{font_name}";
            }}
            table.items td {{
                padding: 8px;
                border-bottom: 1px solid #e5dcc9;
                font-size: 10pt;
            }}
            .total-row td {{
                font-size: 13pt;
                font-weight: bold;
                color: #35492f;
                padding-top: 12px;
                border: none;
                font-family: "{font_name}";
            }}
            .footer {{
                text-align: center;
                color: #777;
                font-size: 9pt;
                margin-top: 30px;
                border-top: 1px solid #e5dcc9;
                padding-top: 10px;
            }}
        </style>
    </head>
    <body>
        <h1>Счёт на оплату №{order.id}</h1>
        <div class="subtitle">
            от {order.created_at.strftime('%d.%m.%Y')}
        </div>

        <table class="info-table">
            <tr>
                <td width="50%">
                    <h3>Поставщик</h3>
                    <p><b>{shop_info.get('name', 'ООО «Диантус»')}</b></p>
                    {supplier_extra}
                </td>
                <td width="50%">
                    <h3>Покупатель</h3>
                    <p><b>{buyer_company}</b></p>
                    <p>Контакт: {buyer_name}</p>
                    <p>ИНН: {buyer_inn}</p>
                    <p>Тел: {buyer_phone}</p>
                    <p>Email: {buyer_email}</p>
                </td>
            </tr>
        </table>

        <table class="items">
            <thead>
                <tr>
                    <th align="center">№</th>
                    <th>Товар</th>
                    <th align="center">Упак.</th>
                    <th align="center">Шт/упак</th>
                    <th align="center">Всего шт</th>
                    <th align="right">Цена/шт</th>
                    <th align="right">Сумма</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows)}
            </tbody>
            <tfoot>
                <tr>
                    <td colspan="6" align="right">Подытог:</td>
                    <td align="right">{order.subtotal:.2f} ₽</td>
                </tr>
                {discount_row}
                <tr class="total-row">
                    <td colspan="6" align="right">ИТОГО:</td>
                    <td align="right">{order.total_price:.2f} ₽</td>
                </tr>
            </tfoot>
        </table>

        {comment_block}

        <div class="footer">
            Счёт сгенерирован автоматически · {datetime.now().strftime('%d.%m.%Y %H:%M')}<br>
            {shop_info.get('name', 'ООО «Диантус»')}
        </div>
    </body>
    </html>
    """

    # ── Генерация PDF ──
    pdf_buffer = BytesIO()
    pisa_status = pisa.CreatePDF(
        src=html,
        dest=pdf_buffer,
        encoding="utf-8",
    )

    if pisa_status.err:
        logger.error("xhtml2pdf вернул ошибку: %s", pisa_status.err)
        raise RuntimeError("Не удалось сгенерировать PDF")

    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()