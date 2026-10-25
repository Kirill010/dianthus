# app/services/preorder_rules.py
"""Сроки предзаказа: за сколько дней нужно заказывать цветы.

Правило бизнеса: срок считается от даты, к которой цветы нужны КЛИЕНТУ
(а не от даты прилёта), и зависит от страны происхождения. Заказы принимаются
каждый день, включая выходные, — последний день заказа на выходной не
переносится.

Здесь только расчёты (без обращения к сайту), поэтому их легко проверить тестами.
"""
import re
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from .site_settings import get_setting

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг",
            "пятница", "суббота", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]

# как клиенты и сотрудники называют страну → каноническое имя
SYNONYMS = {
    "эквадор": "эквадор", "ecuador": "эквадор",
    "кения": "кения", "kenya": "кения",
    "колумбия": "колумбия", "colombia": "колумбия",
    "голландия": "голландия", "нидерланды": "голландия",
    "holland": "голландия", "netherlands": "голландия",
    "китай": "китай", "china": "китай",
    "россия": "россия", "рф": "россия", "russia": "россия",
    "российская": "россия", "российские": "россия",
}


def canonical(name: str) -> str:
    key = (name or "").strip().lower().replace("ё", "е")
    return SYNONYMS.get(key, key)


def parse_lead_days(text: str) -> dict[str, int]:
    """«Эквадор=10, Колумбия=7» → {"Эквадор": 10, "Колумбия": 7} (порядок сохраняется).

    Непонятные куски пропускаются, а не ломают страницу: настройку правит человек.
    """
    rules: dict[str, int] = {}
    for part in re.split(r"[,;\n]+", text or ""):
        m = re.match(r"^\s*([^=:]+?)\s*[=:]\s*(\d{1,3})\s*(?:дн\.?|дней|день)?\s*$", part)
        if not m:
            continue
        name, days = m.group(1).strip(), int(m.group(2))
        if name and 0 <= days <= 365:
            rules[name] = days
    return rules


def lead_days(db: Session) -> dict[str, int]:
    return parse_lead_days(get_setting(db, "preorder_lead_days"))


def days_for(rules: dict[str, int], origin: str) -> int | None:
    key = canonical(origin)
    for name, days in rules.items():
        if canonical(name) == key:
            return days
    return None


def group_by_days(rules: dict[str, int]) -> list[tuple[int, list[str]]]:
    """[(10, ["Эквадор", "Голландия"]), (7, ["Колумбия"])] — сначала самые долгие."""
    groups: dict[int, list[str]] = {}
    for name, days in rules.items():
        groups.setdefault(days, []).append(name)
    return sorted(groups.items(), key=lambda kv: -kv[0])


def describe(rules: dict[str, int]) -> str:
    """Фраза для условий работы: «Эквадор, Китай — за 10 дн.; Колумбия — за 7 дн.»"""
    if not rules:
        return "сроки уточняйте у менеджера"
    return "; ".join(f"{', '.join(names)} — за {days} дн."
                     for days, names in group_by_days(rules))


def fmt_date(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year} ({WEEKDAYS[d.weekday()]})"


def last_order_date(delivery: date, days: int) -> date:
    return delivery - timedelta(days=days)


def earliest_delivery(order_date: date, days: int) -> date:
    return order_date + timedelta(days=days)


def table_for_delivery(rules: dict[str, int], delivery: date, today: date) -> list[dict]:
    """Строки «страны → последний день заказа» для выбранной даты получения."""
    rows = []
    for days, names in group_by_days(rules):
        last = last_order_date(delivery, days)
        left = (last - today).days
        rows.append({
            "origins": ", ".join(names),
            "days": days,
            "last": last,
            "last_text": fmt_date(last),
            "left": left,
            "state": "passed" if left < 0 else ("today" if left == 0 else "ok"),
            "weekend": last.weekday() >= 5,
            "earliest": earliest_delivery(today, days),
            "earliest_text": fmt_date(earliest_delivery(today, days)),
        })
    return rows


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    first_next = date(year + (month == 12), (month % 12) + 1, 1)
    d = first_next - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def peak_dates(year: int) -> list[tuple[date, str]]:
    """Общеизвестные цветочные даты (ориентир, а не данные о продажах компании)."""
    return [
        (date(year, 1, 25), "Татьянин день"),
        (date(year, 2, 14), "14 февраля"),
        (date(year, 2, 23), "23 февраля"),
        (date(year, 3, 8), "8 марта — главный пик года"),
        (date(year, 9, 1), "1 сентября"),
        (date(year, 10, 5), "День учителя"),
        (_last_weekday_of_month(year, 11, 6), "День матери"),
        (date(year, 12, 31), "Новый год"),
    ]


def upcoming_peaks(rules: dict[str, int], today: date, horizon_days: int = 200) -> list[dict]:
    end = today + timedelta(days=horizon_days)
    found = []
    for year in range(today.year, end.year + 1):
        for day, name in peak_dates(year):
            if today <= day <= end:
                found.append((day, name))
    found.sort()
    return [{
        "date": day, "date_text": fmt_date(day), "name": name,
        "in_days": (day - today).days,
        "rows": table_for_delivery(rules, day, today),
    } for day, name in found]


def parse_user_date(text: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None
