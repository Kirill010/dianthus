# app/services/totp.py
"""TOTP (Google Authenticator / Яндекс.Ключ) для админов."""
import logging
import pyotp

logger = logging.getLogger(__name__)


def new_secret() -> str:
    return pyotp.random_base32()


def verify(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    # valid_window=1 — допускаем соседний 30-секундный интервал (рассинхрон часов)
    return pyotp.TOTP(secret).verify(code, valid_window=1)


def provisioning_url(secret: str, email: str, issuer: str = "Диантус") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=issuer)


def qr_data_uri(data: str) -> str:
    """QR-код для otpauth://-ссылки как data:URI (PNG, base64).

    Библиотека qrcode опциональна: если её нет, вернём пустую строку,
    и страница покажет секрет текстом для ручного ввода.
    """
    try:
        import base64
        from io import BytesIO

        import qrcode

        img = qrcode.make(data)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        logger.warning("QR для 2FA не сгенерирован: %s", e)
        return ""