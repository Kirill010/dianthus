"""
Валидация пользовательских данных.

Каждая функция возвращает строку с ошибкой или None, если всё ок.
"""
import re


# ═══════ EMAIL ═══════════════════════════════════════════════

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def validate_email(email: str) -> str | None:
    email = (email or "").strip().lower()
    if not email:
        return "Укажите email"
    if len(email) > 120:
        return "Email слишком длинный (максимум 120 символов)"
    if not _EMAIL_RE.match(email):
        return "Неверный формат email. Пример: ivan@company.ru"
    return None


# ═══════ ПАРОЛЬ ══════════════════════════════════════════════

def validate_password(password: str) -> str | None:
    if not password:
        return "Укажите пароль"
    if len(password) < 8:
        return "Пароль должен быть минимум 8 символов"
    # bcrypt принимает максимум 72 БАЙТА (не символа!)
    if len(password.encode("utf-8")) > 72:
        return "Пароль слишком длинный (максимум 72 байта)"
    if not any(c.isalpha() for c in password):
        return "Пароль должен содержать хотя бы одну букву"
    if not any(c.isdigit() for c in password):
        return "Пароль должен содержать хотя бы одну цифру"
    return None


# ═══════ ФИО ═════════════════════════════════════════════════

_NAME_RE = re.compile(r"^[А-Яа-яЁёA-Za-z\- ]{2,120}$")


def validate_full_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите ФИО контактного лица"
    if len(name) < 2:
        return "ФИО слишком короткое"
    if len(name) > 120:
        return "ФИО слишком длинное (максимум 120 символов)"
    if not _NAME_RE.match(name):
        return "ФИО может содержать только буквы, пробел и дефис"
    return None


# ═══════ ТЕЛЕФОН ═════════════════════════════════════════════

def validate_phone(phone: str) -> str | None:
    """Российский телефон: 11 цифр, начиная с 7 или 8."""
    phone = (phone or "").strip()
    if not phone:
        return "Укажите телефон"

    digits = re.sub(r"\D", "", phone)

    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return "Телефон должен содержать 11 цифр. Пример: +7 (999) 123-45-67"

    if len(set(digits)) == 1:
        return "Телефон не может состоять из одинаковых цифр"

    return None


# ═══════ КОМПАНИЯ ════════════════════════════════════════════

def validate_company_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите название компании"
    if len(name) < 2:
        return "Название компании слишком короткое"
    if len(name) > 200:
        return "Название компании слишком длинное (максимум 200 символов)"
    return None


# ═══════ ЧИСЛА (товары) ══════════════════════════════════════

def validate_price(value: float | None) -> str | None:
    if value is None:
        return "Укажите цену"
    if value < 0:
        return "Цена не может быть отрицательной"
    if value > 10_000_000:
        return "Цена слишком большая"
    return None


def validate_stock(value: int | None) -> str | None:
    if value is None:
        return "Укажите остаток"
    if value < 0:
        return "Остаток не может быть отрицательным"
    if value > 1_000_000:
        return "Остаток слишком большой"
    return None


def validate_positive_int(value: int | None, label: str) -> str | None:
    if value is None:
        return f"Укажите {label}"
    if value < 1:
        return f"{label} должен быть не меньше 1"
    if value > 10_000:
        return f"{label} слишком большой"
    return None


def validate_country(value: str) -> str | None:
    value = (value or "").strip()
    if not value:
        return "Укажите страну"
    if len(value) > 100:
        return "Название страны слишком длинное"
    return None