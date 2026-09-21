# Валидация пользовательских данных.
import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_NAME_RE = re.compile(r"^[А-Яа-яЁёA-Za-z\- .]{2,120}$")


def validate_email(email: str) -> str | None:
    email = (email or "").strip().lower()
    if not email:
        return "Укажите email"
    if len(email) > 120:
        return "Email слишком длинный"
    if not _EMAIL_RE.match(email):
        return "Неверный формат email. Пример: ivan@company.ru"
    return None


def validate_password(password: str) -> str | None:
    if not password:
        return "Укажите пароль"
    if len(password) < 8:
        return "Пароль минимум 8 символов"
    if len(password.encode("utf-8")) > 72:
        return "Пароль слишком длинный (макс. 72 байта)"
    if not any(c.isalpha() for c in password):
        return "Пароль должен содержать букву"
    if not any(c.isdigit() for c in password):
        return "Пароль должен содержать цифру"
    return None


def validate_full_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите ФИО"
    if len(name) < 2:
        return "ФИО слишком короткое"
    if len(name) > 120:
        return "ФИО слишком длинное"
    if not _NAME_RE.match(name):
        return "ФИО: только буквы, пробел, дефис, точка"
    return None


def validate_phone(phone: str) -> str | None:
    phone = (phone or "").strip()
    if not phone:
        return "Укажите телефон"
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    else:
        return "Телефон: 11 цифр. Пример +7 (999) 123-45-67"
    if len(set(digits)) == 1:
        return "Телефон не может состоять из одинаковых цифр"
    return None


def validate_company_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите название компании"
    if len(name) < 2:
        return "Название слишком короткое"
    if len(name) > 200:
        return "Название слишком длинное"
    return None


def validate_inn(inn: str, required: bool = True) -> str | None:
    # ИНН: 10 цифр (юрлицо) или 12 цифр (ИП). :param required: если False — пустое значение допустимо.
    inn = (inn or "").strip()
    if not inn:
        return "Укажите ИНН" if required else None
    if not inn.isdigit():
        return "ИНН должен содержать только цифры"
    if len(inn) not in (10, 12):
        return "ИНН: 10 цифр для юрлица или 12 для ИП"
    return None


def validate_city(city: str, required: bool = True) -> str | None:
    # Город. :param required: если False — пустое значение допустимо.
    city = (city or "").strip()
    if not city:
        return "Укажите город" if required else None
    if len(city) < 2:
        return "Название города слишком короткое"
    if len(city) > 100:
        return "Название города слишком длинное"
    return None


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
        return f"{label} минимум 1"
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