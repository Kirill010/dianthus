# Генерация PDF-счёта через xhtml2pdf (с поддержкой кириллицы).
import logging
from datetime import datetime, timezone
from html import escape as _e
from io import BytesIO

from ..timeutils import DATE_FMT, DATETIME_FMT, fmt_local
from .pdf_fonts import get_font_css, get_font_family, link_callback

logger = logging.getLogger(__name__)


def generate_invoice_pdf(order, user, shop_info: dict) -> bytes:
    # Генерирует PDF-счёт. Возвращает bytes.
    try:
        from xhtml2pdf import pisa
    except ImportError:
        logger.error("xhtml2pdf не установлен")
        raise

    # Шрифт
    font_css = get_font_css()
    font_family = get_font_family()
    logger.info("PDF: используем шрифт %s", font_family)

    # Данные клиента
    # Всё, что ввёл пользователь, экранируем: xhtml2pdf умеет подгружать
    # картинки по ссылкам из HTML, а значит, без этого — SSRF/чтение файлов.
    buyer_company = _e(user.company_name if user else "Гость")
    buyer_name = _e(user.full_name if user else "—")
    buyer_inn = _e((user.inn if user else "") or "—")
    buyer_phone = _e((user.phone if user else "") or "—")
    buyer_email = _e((user.email if user else "") or "—")

    # Позиции заказа
    rows = []
    for i, item in enumerate(order.items, 1):
        pack = item.package_size or 1
        stems = item.quantity * pack
        rows.append(f"""
            <tr>
                <td align="center">{i}</td>
                <td>{_e(item.product_name)}</td>
                <td align="center">{item.quantity}</td>
                <td align="center">{pack}</td>
                <td align="center">{stems}</td>
                <td align="right">{item.price:.2f}</td>
                <td align="right">{item.subtotal:.2f}</td>
            </tr>
        """)

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

    comment_block = ""
    if order.comment:
        comment_block = f"""
            <p style="background:#ecf1e8; padding:8px;
                border-left:3px solid #4a6741;">
                <b>Комментарий:</b> {_e(order.comment)}
            </p>
        """

    shop_address = shop_info.get('address') or ''
    shop_phone = shop_info.get('phone') or ''
    supplier_extra = ''
    if shop_address:
        supplier_extra += f"<p>Адрес: {_e(shop_address)}</p>"
    if shop_phone:
        supplier_extra += f"<p>Тел: {_e(shop_phone)}</p>"
    # Банковские реквизиты (SHOP_REQUISITES в .env, строки через «|»)
    for line in str(shop_info.get('requisites') or '').split('|'):
        if line.strip():
            supplier_extra += f"<p>{_e(line.strip())}</p>"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            {font_css}

            @page {{
                size: A4;
                margin: 1.5cm;
            }}
            body {{
                font-family: "{font_family}";
                font-size: 11pt;
                color: #222;
            }}
            h1 {{
                color: #35492f;
                text-align: center;
                font-size: 20pt;
                margin-bottom: 5px;
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
                font-size: 10pt;
            }}
            .info-table h3 {{
                color: #4a6741;
                font-size: 11pt;
                margin: 0 0 5px;
            }}
            .info-table p {{
                margin: 2px 0;
            }}
            table.items {{
                width: 100%;
                margin-bottom: 15px;
            }}
            table.items th {{
                background: #4a6741;
                color: white;
                padding: 8px;
                font-size: 10pt;
                text-align: left;
            }}
            table.items td {{
                padding: 8px;
                font-size: 10pt;
            }}
            .total-row td {{
                font-size: 13pt;
                font-weight: bold;
                color: #35492f;
                padding-top: 12px;
            }}
            .footer {{
                text-align: center;
                color: #777;
                font-size: 9pt;
                margin-top: 30px;
                padding-top: 10px;
            }}
        </style>
    </head>
    <body>
        <h1>Счёт на оплату №{order.id}</h1>
        <div class="subtitle">
            от {fmt_local(order.created_at, DATE_FMT)}
        </div>

        <table class="info-table">
            <tr>
                <td width="50%">
                    <h3>Поставщик</h3>
                    <p><b>{_e(shop_info.get('name', 'ООО «Диантус»'))}</b></p>
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
            Счёт сгенерирован автоматически · {fmt_local(datetime.now(timezone.utc), DATETIME_FMT)}<br>
            {_e(shop_info.get('name', 'ООО «Диантус»'))}
        </div>
    </body>
    </html>
    """

    # PDF с link_callback
    pdf_buffer = BytesIO()
    pisa_status = pisa.CreatePDF(
        src=html,
        dest=pdf_buffer,
        encoding="utf-8",
        link_callback=link_callback,
    )

    if pisa_status.err:
        logger.error("xhtml2pdf ошибка: %s", pisa_status.err)
        raise RuntimeError("Не удалось сгенерировать PDF")

    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()