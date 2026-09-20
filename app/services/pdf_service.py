"""Генерация PDF-счёта по заказу."""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def generate_invoice_pdf(order, user, shop_info: dict) -> bytes:
    """Генерирует PDF-счёт для заказа. Возвращает bytes."""
    try:
        from weasyprint import HTML
    except ImportError:
        logger.error("weasyprint не установлен: pip install weasyprint")
        raise

    buyer_company = user.company_name if user else "Гость"
    buyer_name = user.full_name if user else "—"
    buyer_inn = (user.inn if user else "") or "—"
    buyer_phone = (user.phone if user else "") or "—"
    buyer_email = (user.email if user else "") or "—"

    rows = []
    for i, item in enumerate(order.items, 1):
        pack = item.package_size or 1
        stems = item.quantity * pack
        rows.append(f"""
            <tr>
                <td>{i}</td>
                <td>{item.product_name}</td>
                <td style="text-align:center;">{item.quantity}</td>
                <td style="text-align:center;">{pack}</td>
                <td style="text-align:center;">{stems}</td>
                <td style="text-align:right;">{item.price:.2f}</td>
                <td style="text-align:right;">{item.subtotal:.2f}</td>
            </tr>
        """)

    discount_html = ""
    if order.discount_percent and order.discount_percent > 0:
        discount_html = f"""
            <tr>
                <td colspan="6" style="text-align:right;">
                    Скидка {order.discount_percent:.0f}%:
                </td>
                <td style="text-align:right; color:#c97b4a;">
                    −{order.discount_amount:.2f} ₽
                </td>
            </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{ size: A4; margin: 1.5cm; }}
            body {{
                font-family: 'DejaVu Sans', Arial, sans-serif;
                color: #222; font-size: 11pt; line-height: 1.4;
            }}
            h1 {{
                color: #35492f; text-align: center;
                margin-bottom: 0.3em; font-size: 22pt;
            }}
            .subtitle {{
                text-align: center; color: #777;
                margin-bottom: 2em; font-size: 10pt;
            }}
            .info-block {{
                display: flex; justify-content: space-between;
                margin-bottom: 2em; padding: 1em;
                background: #faf6ef; border-radius: 8px;
            }}
            .info-block > div {{ flex: 1; }}
            .info-block h3 {{
                margin: 0 0 0.5em; color: #4a6741; font-size: 11pt;
            }}
            .info-block p {{ margin: 0.2em 0; font-size: 10pt; }}
            table {{
                width: 100%; border-collapse: collapse;
                margin-bottom: 1.5em;
            }}
            th {{
                background: #4a6741; color: white;
                padding: 8px; text-align: left; font-size: 10pt;
            }}
            th:first-child, th:nth-child(3) {{ text-align: center; }}
            td {{
                padding: 8px; border-bottom: 1px solid #e5dcc9;
                font-size: 10pt;
            }}
            tfoot td {{ padding: 8px; border: none; font-size: 11pt; }}
            .total {{
                font-size: 14pt; font-weight: bold; color: #35492f;
            }}
            .footer {{
                margin-top: 3em; padding-top: 1em;
                border-top: 1px solid #e5dcc9;
                text-align: center; font-size: 9pt; color: #777;
            }}
        </style>
    </head>
    <body>
        <h1>Счёт на оплату №{order.id}</h1>
        <div class="subtitle">
            от {order.created_at.strftime('%d.%m.%Y')}
        </div>

        <div class="info-block">
            <div>
                <h3>Поставщик</h3>
                <p><b>{shop_info.get('name', 'ООО «Диантус»')}</b></p>
                {f"<p>Адрес: {shop_info.get('address')}</p>" if shop_info.get('address') else ""}
                {f"<p>Тел: {shop_info.get('phone')}</p>" if shop_info.get('phone') else ""}
            </div>
            <div>
                <h3>Покупатель</h3>
                <p><b>{buyer_company}</b></p>
                <p>Контакт: {buyer_name}</p>
                <p>ИНН: {buyer_inn}</p>
                <p>Тел: {buyer_phone}</p>
                <p>Email: {buyer_email}</p>
            </div>
        </div>

        <table>
            <thead>
                <tr>
                    <th>№</th>
                    <th>Товар</th>
                    <th>Упак.</th>
                    <th>Шт/упак</th>
                    <th>Всего шт</th>
                    <th style="text-align:right;">Цена/шт</th>
                    <th style="text-align:right;">Сумма</th>
                </tr>
            </thead>
            <tbody>
                {''.join(rows)}
            </tbody>
            <tfoot>
                <tr>
                    <td colspan="6" style="text-align:right;">Подытог:</td>
                    <td style="text-align:right;">{order.subtotal:.2f} ₽</td>
                </tr>
                {discount_html}
                <tr>
                    <td colspan="6" style="text-align:right;" class="total">
                        ИТОГО:
                    </td>
                    <td style="text-align:right;" class="total">
                        {order.total_price:.2f} ₽
                    </td>
                </tr>
            </tfoot>
        </table>

        {f"<p><b>Комментарий:</b> {order.comment}</p>" if order.comment else ""}

        <div class="footer">
            Счёт сгенерирован автоматически · {datetime.now().strftime('%d.%m.%Y %H:%M')}<br>
            {shop_info.get('name', 'ООО «Диантус»')}
        </div>
    </body>
    </html>
    """

    return HTML(string=html).write_pdf()