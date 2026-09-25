# Уведомления админам о важных событиях — через email (SMTP).
import logging
import os
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from ..config import config

logger = logging.getLogger(__name__)

TIMEOUT = 10
MAX_ATTEMPTS = 3  # fix #63: retry при временных сбоях SMTP


# ── КОНФИГУРАЦИЯ ──────────────────────────────────────────

def _get_smtp_config() -> dict | None:
    """Возвращает конфиг SMTP или None, если не настроено."""
    host = (os.getenv("SMTP_HOST") or "").strip()
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    sender = (os.getenv("SMTP_FROM") or user).strip()
    recipients_raw = (os.getenv("SMTP_TO") or "").strip()

    if not host or not user or not password or not recipients_raw:
        logger.warning(
            "⚠️ SMTP не настроен: host=%s, user=%s, pass=%s, to=%s",
            bool(host), bool(user), bool(password), bool(recipients_raw),
        )
        return None

    recipients = [e.strip() for e in recipients_raw.split(",") if e.strip()]
    if not recipients:
        return None

    try:
        port = int(os.getenv("SMTP_PORT", "465"))
    except ValueError:
        port = 465

    use_ssl = (os.getenv("SMTP_USE_SSL", "true").strip().lower()
               in ("1", "true", "yes", "on"))

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "sender": sender,
        "recipients": recipients,
        "use_ssl": use_ssl,
    }


# ── НИЗКОУРОВНЕВАЯ ОТПРАВКА + RETRY ───────────────────────

def _smtp_send_raw(cfg: dict, msg: MIMEMultipart,
                   recipients: list[str]) -> None:
    """Отправляет письмо. Бросает исключения (для retry)."""
    if cfg["use_ssl"]:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                              timeout=TIMEOUT, context=context) as server:
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_string())
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=TIMEOUT) as server:
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
            server.login(cfg["user"], cfg["password"])
            server.sendmail(cfg["sender"], recipients, msg.as_string())


def _send_with_retry(cfg: dict, msg: MIMEMultipart,
                     recipients: list[str]) -> bool:
    """
    fix #63: отправка с retry (3 попытки, exponential backoff 1s→2s).
    НЕ retry-им при ошибках аутентификации / отказа получателя.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            _smtp_send_raw(cfg, msg, recipients)
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error(
                "SMTP: неверный логин/пароль. Для Яндекс.Почты нужен "
                "ПАРОЛЬ ПРИЛОЖЕНИЯ, не обычный. %s", e,
            )
            return False
        except (smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused) as e:
            logger.error("SMTP: получатель/отправитель отклонён: %s", e)
            return False
        except Exception as e:
            logger.warning(
                "SMTP: попытка %d/%d не удалась: %s",
                attempt, MAX_ATTEMPTS, e,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s
    logger.error("SMTP: не удалось отправить после %d попыток", MAX_ATTEMPTS)
    return False


# ── ПУБЛИЧНЫЕ ФУНКЦИИ ─────────────────────────────────────

def send_email(subject: str, body_html: str) -> bool:
    """Отправляет email админам. Никогда не бросает исключение."""
    cfg = _get_smtp_config()
    if not cfg:
        logger.debug("SMTP не настроен — письмо пропущено")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.attach(MIMEText("Откройте письмо в HTML-совместимом клиенте.",
                        "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    ok = _send_with_retry(cfg, msg, cfg["recipients"])
    if ok:
        logger.info("📧 Письмо отправлено: %s", subject)
    return ok


def send_email_to(to: str, subject: str, body_html: str) -> bool:
    """Отправка конкретному получателю (клиенту)."""
    cfg = _get_smtp_config()
    if not cfg:
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = to
    msg.attach(MIMEText("Откройте в HTML-клиенте.", "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    ok = _send_with_retry(cfg, msg, [to])
    if ok:
        logger.info("📧 Клиенту %s: %s", to, subject)
    return ok


def send_batch_emails(messages: list[dict]) -> int:
    """
    fix #63: batch-рассылка. Одна SMTP-сессия на все письма +
    retry на уровне всей сессии (не на каждое письмо).
    """
    cfg = _get_smtp_config()
    if not cfg or not messages:
        return 0

    sent = 0
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        sent = 0
        last_error = None
        try:
            if cfg["use_ssl"]:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(cfg["host"], cfg["port"],
                                      timeout=TIMEOUT, context=ctx) as s:
                    s.login(cfg["user"], cfg["password"])
                    sent = _do_batch(s, cfg, messages)
            else:
                with smtplib.SMTP(cfg["host"], cfg["port"],
                                  timeout=TIMEOUT) as s:
                    s.ehlo()
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                    s.login(cfg["user"], cfg["password"])
                    sent = _do_batch(s, cfg, messages)
            # Если всё ушло — успех
            if sent == len(messages):
                return sent
            # Если что-то не ушло (refused), retry не поможет
            return sent
        except smtplib.SMTPAuthenticationError as e:
            logger.error("SMTP: неверный логин/пароль в batch: %s", e)
            return 0
        except Exception as e:
            last_error = e
            logger.warning(
                "Batch: попытка %d/%d провалилась: %s",
                attempt, MAX_ATTEMPTS, e,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(2 ** (attempt - 1))

    if last_error:
        logger.error("send_batch_emails: %s", last_error)
    return sent


def _do_batch(s: smtplib.SMTP, cfg: dict, messages: list[dict]) -> int:
    """Отправляет пачку в одной сессии. Возвращает число успешных."""
    sent = 0
    for m in messages:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = m["subject"]
        msg["From"] = cfg["sender"]
        msg["To"] = m["to"]
        msg.attach(MIMEText("Откройте в HTML-клиенте.", "plain", "utf-8"))
        msg.attach(MIMEText(m["body_html"], "html", "utf-8"))
        try:
            s.sendmail(cfg["sender"], [m["to"]], msg.as_string())
            sent += 1
        except Exception as e:
            logger.warning("Batch: %s → %s", m["to"], e)
    return sent


# ── HTML-ШАБЛОНЫ ПИСЕМ ────────────────────────────────────

_BASE_STYLE = """
<style>
  body { font-family: -apple-system, Arial, sans-serif;
         background: #f5efe4; margin: 0; padding: 20px; }
  .card { background: #fffdf8; border-radius: 12px;
          padding: 24px; max-width: 600px; margin: 0 auto;
          border: 1px solid #e5dcc9; }
  h1 { color: #35492f; font-size: 20px; margin: 0 0 16px; }
  table { width: 100%; border-collapse: collapse; margin: 16px 0; }
  td { padding: 8px 0; border-bottom: 1px solid #efe7d5;
       font-size: 14px; }
  td:first-child { color: #8a857a; width: 40%; }
  td:last-child { color: #2b2b2b; font-weight: 600; }
  .total { font-size: 18px; color: #35492f; font-weight: 700;
           padding-top: 12px; border: none; }
  .btn { display: inline-block; background: #4a6741; color: #fff;
         text-decoration: none; padding: 12px 24px; border-radius: 30px;
         font-weight: 600; margin-top: 16px; }
  .footer { text-align: center; color: #8a857a; font-size: 12px;
            margin-top: 24px; }
</style>
"""


def notify_admin_new_order(order, user) -> None:
    """Уведомление о новом заказе на email админам."""
    if not user:
        return

    items_html = ""
    for item in order.items:
        pack = item.package_size or 1
        stems = item.quantity * pack
        items_html += f"""
            <tr>
                <td colspan="2" style="padding: 8px 0;
                    border-bottom: 1px solid #efe7d5;">
                    <b>{item.product_name}</b><br>
                    <small style="color:#8a857a;">
                        {item.quantity} упак. × {pack} шт = {stems} шт
                        · {item.price:.2f} ₽/шт
                    </small>
                </td>
                <td style="text-align:right; font-weight:600;">
                    {item.subtotal:.2f} ₽
                </td>
            </tr>
        """

    discount_html = ""
    if order.discount_percent and order.discount_percent > 0:
        discount_html = f"""
            <tr>
                <td colspan="2">Скидка {order.discount_percent:.0f}%:</td>
                <td style="text-align:right; color:#c97b4a;">
                    −{order.discount_amount:.2f} ₽
                </td>
            </tr>
        """

    comment_html = ""
    if order.comment:
        comment_html = f"""
            <p style="background:#ecf1e8; padding:12px;
                border-radius:8px; color:#35492f;
                border-left:3px solid #4a6741;">
                <b>Комментарий клиента:</b><br>{order.comment}
            </p>
        """

    body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>🌸 Новый заказ №{order.id}</h1>

        <table>
          <tr><td>Компания:</td>
              <td><b>{user.company_name}</b></td></tr>
          <tr><td>Контакт:</td>
              <td>{user.full_name}</td></tr>
          <tr><td>Телефон:</td>
              <td><a href="tel:{user.phone}">{user.phone or '—'}</a></td></tr>
          <tr><td>Email:</td>
              <td><a href="mailto:{user.email}">{user.email}</a></td></tr>
          <tr><td>ИНН:</td>
              <td>{user.inn or '—'}</td></tr>
          <tr><td>Город:</td>
              <td>{user.city or '—'}</td></tr>
        </table>

        <h3 style="color:#4a6741; font-size:15px;
            margin-top:20px;">Состав заказа:</h3>
        <table>
          {items_html}
          <tr><td colspan="2" style="text-align:right;
              padding-top:12px; border:none;">Подытог:</td>
              <td style="text-align:right; border:none;">
                  {order.subtotal:.2f} ₽</td></tr>
          {discount_html}
          <tr>
            <td colspan="2" style="text-align:right;
                border:none; padding-top:8px;">
                <b>Итого:</b>
            </td>
            <td class="total" style="text-align:right;
                border:none; padding-top:8px;">
                {order.total_price:.2f} ₽
            </td>
          </tr>
        </table>

        {comment_html}

        <a href="{config.APP_URL}/admin/orders/{order.id}"
           class="btn">Открыть заказ в админке</a>

        <div class="footer">
          {config.SHOP_NAME} · Оптовые поставки цветов<br>
          {order.created_at.strftime('%d.%m.%Y %H:%M')}
        </div>
      </div>
    </body></html>
    """

    send_email(f"🌸 Новый заказ №{order.id} — {user.company_name}", body)


def notify_admin_new_client(user) -> None:
    """Уведомление о новом клиенте (заявке на регистрацию)."""
    if not user:
        return

    body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>👤 Новая заявка на регистрацию</h1>

        <table>
          <tr><td>Компания:</td>
              <td><b>{user.company_name}</b></td></tr>
          <tr><td>Контакт:</td>
              <td>{user.full_name}</td></tr>
          <tr><td>Телефон:</td>
              <td><a href="tel:{user.phone}">{user.phone or '—'}</a></td></tr>
          <tr><td>Email:</td>
              <td><a href="mailto:{user.email}">{user.email}</a></td></tr>
          <tr><td>ИНН:</td>
              <td>{user.inn or '—'}</td></tr>
          <tr><td>Город:</td>
              <td>{user.city or '—'}</td></tr>
        </table>

        <a href="{config.APP_URL}/admin"
           class="btn">Открыть админку</a>

        <div class="footer">
          {config.SHOP_NAME} · Оптовые поставки цветов
        </div>
      </div>
    </body></html>
    """

    send_email(f"👤 Новая заявка: {user.company_name}", body)


def notify_client_status_changed(order, status: str) -> None:
    """Письмо клиенту при смене статуса заказа."""
    if not order or not order.user:
        return
    user = order.user

    emoji = {
        "Подтверждён": "✅",
        "В работе": "📦",
        "Отправлен": "🚚",
        "Выполнен": "🎉",
        "Отменён": "❌",
    }.get(status, "🔄")

    text = {
        "Подтверждён": "Ваш заказ принят в работу. Мы свяжемся для уточнения доставки.",
        "В работе": "Собираем ваш заказ на складе.",
        "Отправлен": "Заказ передан в доставку.",
        "Выполнен": "Заказ выполнен. Спасибо, что выбрали Диантус!",
        "Отменён": "Заказ отменён. Если это ошибка — свяжитесь с менеджером.",
    }.get(status, f"Статус изменён на «{status}»")

    body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>{emoji} Заказ №{order.id}: {status}</h1>
        <p style="font-size:16px;">Здравствуйте, <b>{user.full_name}</b>!</p>
        <p>{text}</p>

        <table>
          <tr><td>Сумма:</td>
              <td><b>{order.total_price:.2f} ₽</b></td></tr>
          <tr><td>Позиций:</td>
              <td>{len(order.items)}</td></tr>
        </table>

        <a href="{config.APP_URL}/orders" class="btn">
            Открыть мои заказы
        </a>

        <div class="footer">
          {config.SHOP_NAME} · {config.SHOP_PHONE}
        </div>
      </div>
    </body></html>
    """
    send_email_to(
        to=user.email,
        subject=f"{emoji} Заказ №{order.id}: {status}",
        body_html=body,
    )


def notify_admin_status_changed(order, status: str) -> None:
    """Уведомление админу о смене статуса заказа (опционально)."""
    if not order:
        return

    body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>🔄 Заказ №{order.id}: статус изменён</h1>
        <p style="font-size:16px;">
          Новый статус: <b style="color:#4a6741;">{status}</b>
        </p>
        <p style="color:#8a857a;">
          Клиент: {order.user.company_name if order.user else '—'}
        </p>

        <a href="{config.APP_URL}/admin/orders/{order.id}"
           class="btn">Открыть заказ</a>
      </div>
    </body></html>
    """

    send_email(f"Заказ №{order.id}: {status}", body)


def notify_client_preorder_available(preorder, supply_item) -> None:
    """Уведомление клиенту: предзаказ поступил."""
    if not preorder or not preorder.user:
        return

    user = preorder.user
    product = supply_item.product

    body = f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>🌸 Ваш предзаказ поступил!</h1>
        <p style="font-size:16px;">Здравствуйте, <b>{user.full_name}</b>!</p>
        <p>Товар из вашего предзаказа <b>«{product.name}»</b>
           уже на складе.</p>

        <table>
          <tr><td>Количество:</td>
              <td><b>{preorder.quantity} упак.</b></td></tr>
          <tr><td>Цена:</td>
              <td><b>{supply_item.price:.2f} ₽/шт</b></td></tr>
          <tr><td>В упаковке:</td>
              <td><b>{product.package_size} шт</b></td></tr>
        </table>

        <p>Вы можете оформить заказ прямо сейчас —
           товар уже доступен в каталоге.</p>

        <a href="{config.APP_URL}/product/{supply_item.id}" class="btn">
            Перейти к товару
        </a>

        <div class="footer">
          {config.SHOP_NAME} · {config.SHOP_PHONE}
        </div>
      </div>
    </body></html>
    """

    try:
        send_email_to(
            to=user.email,
            subject=f"🌸 Предзаказ поступил: {product.name}",
            body_html=body,
        )
    except Exception as e:
        logger.warning("Ошибка email о предзаказе: %s", e)