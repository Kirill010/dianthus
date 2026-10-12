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