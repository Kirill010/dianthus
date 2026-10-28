# app/services/product_guess.py
"""Подсказки для товаров, о которых 1С прислала только название.

В учёте цветов страна и длина стебля часто «зашиты» в название:
«Роза Фридом 60», «Роза Кения 40с», «Гвоздика Мондиал 70 см». Если полей
country / length_cm / category в выгрузке нет, сайт пробует понять их по названию.
Правило осторожное: нет уверенности — возвращаем пустое значение (0, "").
Подсказка применяется только к ПУСТЫМ полям товара и никогда не перезаписывает
то, что прислала 1С или заполнил менеджер.
"""
import re

# (начало слова в названии, как называем страну на сайте)
_COUNTRY_STEMS = (
    ("эквадор", "Эквадор"),
    ("колумб", "Колумбия"),
    ("кения", "Кения"),
    ("кений", "Кения"),
    ("голланд", "Голландия"),
    ("нидерланд", "Голландия"),
    ("росси", "Россия"),
    ("эфиоп", "Эфиопия"),
    ("израил", "Израиль"),
    ("китай", "Китай"),
    ("итали", "Италия"),
    ("турци", "Турция"),
)

# (признак в названии, категория)
_CATEGORY_RULES = (
    ("кустов", "Роза кустовая"),
    ("хризантем", "Хризантема"),
    ("гвоздик", "Гвоздика"),
    ("тюльпан", "Тюльпан"),
    ("пион", "Пион"),
    ("горшеч", "Горшечные"),
    ("зелен", "Зелень"),
)

_NUMBER = re.compile(r"(?<![\d.,/\-])(\d{2,3})(?![\d.,/\-])")
# после такого слова число — не длина стебля, а штуки/граммы/объём
_NOT_LENGTH_AFTER = re.compile(r"^\s*(шт|уп|пач|гр?\b|кг|мл|л\b|%|штук|пучк)", re.IGNORECASE)
_NOT_LENGTH_BEFORE = re.compile(r"(х|x|\*|№|#)\s*$", re.IGNORECASE)

MIN_LENGTH_CM, MAX_LENGTH_CM = 20, 150


def guess_length_cm(name: str) -> int:
    """«Роза Фридом 60 см» → 60; «Роза Кения 40с» → 40; нет уверенности → 0."""
    text = (name or "").lower()
    candidates = []
    for m in _NUMBER.finditer(text):
        value = int(m.group(1))
        if not (MIN_LENGTH_CM <= value <= MAX_LENGTH_CM):
            continue
        if _NOT_LENGTH_AFTER.match(text[m.end():m.end() + 8]):
            continue
        if _NOT_LENGTH_BEFORE.search(text[:m.start()]):
            continue
        candidates.append(value)
    return candidates[-1] if candidates else 0


def guess_country(name: str) -> str:
    """«Роза Кения 40» → «Кения»; не нашли → пустая строка."""
    text = (name or "").lower()
    for stem, country in _COUNTRY_STEMS:
        if re.search(r"(?<![а-яё])" + re.escape(stem), text):
            return country
    return ""


def guess_category(name: str, country: str = "") -> str:
    """Категория для витрины по названию; нет уверенности → пустая строка."""
    text = (name or "").lower()
    for stem, category in _CATEGORY_RULES:
        if stem in text:
            return category
    if re.search(r"(?<![а-яё])роз", text):
        return "Роза Эквадор" if (country or guess_country(name)) == "Эквадор" else "Роза"
    return ""
