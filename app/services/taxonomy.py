# app/services/taxonomy.py
"""Справочники-фильтры каталога: вид, цвет, рост, страна — каждый ОТДЕЛЬНОЙ ТАБЛИЦЕЙ.

Товар хранит только ссылки (kind_id, color_id, height_id, country_id). Значения
приходят из 1С, из Excel-файла и из админки; всё, чего ещё нет в справочнике, создаётся
само (без дублей: «белый», «Белый » и «БЕЛЫЙ» — одна запись), а владелец потом
переименовывает или удаляет лишнее на странице «Справочники».

Как 1С называет значение (для каждого справочника F = kind | color | country | height):
    F_id            id записи НА САЙТЕ (число)                          — самый точный способ
    F_external_id   id записи в 1С                                      — если справочники сверены
    F               название («Белый»), объект {"id", "external_id", "name"} или, для роста, число см
Старые имена полей по-прежнему понимаются: category → kind, length_cm → height_cm.
"""
import logging
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import (Color, Country, DEFAULT_COLORS, DEFAULT_COUNTRIES, DEFAULT_HEIGHTS,
                      DEFAULT_KINDS, Height, Kind, Product)

logger = logging.getLogger(__name__)

MODELS = {"kind": Kind, "color": Color, "height": Height, "country": Country}
LABELS = {"kind": "Вид", "color": "Цвет", "height": "Рост", "country": "Страна"}
PLURALS = {"kind": "kinds", "color": "colors", "height": "heights", "country": "countries"}
# столбец товара, в котором лежит ссылка на справочник
PRODUCT_FK = {"kind": Product.kind_id, "color": Product.color_id,
              "height": Product.height_id, "country": Product.country_id}
# какие ключи JSON относятся к справочнику (первый — основной)
JSON_KEYS = {
    "kind":    ("kind", "category"),
    "color":   ("color",),
    "height":  ("height_cm", "height", "length_cm"),
    "country": ("country",),
}
MIN_HEIGHT_CM, MAX_HEIGHT_CM = 10, 300
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_HEIGHT_RE = re.compile(r"^\s*(\d{1,3})(?:[.,]0+)?\s*(?:см|cm)?\s*$", re.IGNORECASE)


class TaxonomyError(ValueError):
    """Понятная человеку причина, почему значение справочника не принято."""


class _Clear:
    """Пометка «убрать значение у товара» (1С прислала поле пустым)."""
    def __repr__(self) -> str:                                    # pragma: no cover
        return "CLEAR"


CLEAR = _Clear()
# куда в товаре кладётся запись справочника
SLOTS = {"kind": "kind_ref", "color": "color_ref", "height": "height_ref", "country": "country_ref"}


def clean_name(value: Any, maxlen: int = 100) -> str:
    s = re.sub(r"\s+", " ", str(value if value is not None else "").replace("\xa0", " ")).strip()
    return s[:maxlen]


def fold(value: Any) -> str:
    """Ключ для сравнения названий: без регистра, «ё» = «е»."""
    return clean_name(value).casefold().replace("ё", "е")


def display_name(value: Any) -> str:
    s = clean_name(value)
    return s[:1].upper() + s[1:]


def parse_height(value: Any) -> int | None:
    """60, 60.0, «60», «60 см» → 60. Пусто → None. Бессмыслица → TaxonomyError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise TaxonomyError("Рост: ожидается число сантиметров")
    if isinstance(value, (int, float)):
        cm = int(round(value))
    else:
        m = _HEIGHT_RE.match(str(value))
        if not m:
            raise TaxonomyError(f"Рост: не понял «{clean_name(value, 20)}» (нужно число сантиметров, например 60)")
        cm = int(m.group(1))
    if not (MIN_HEIGHT_CM <= cm <= MAX_HEIGHT_CM):
        raise TaxonomyError(f"Рост: {cm} см вне диапазона {MIN_HEIGHT_CM}…{MAX_HEIGHT_CM}")
    return cm


def clean_hex(value: Any) -> str:
    s = str(value or "").strip()
    return s.lower() if _HEX_RE.match(s) else ""


# ═══════════════════════════════════════════════════════════
# Что прислали про значение справочника
# ═══════════════════════════════════════════════════════════

@dataclass
class Ref:
    """Ссылка на значение справочника, как её прислала 1С/файл/форма."""
    kind: str                       # kind | color | height | country
    id: int | None = None           # id на сайте
    external_id: str = ""           # id в 1С
    name: Any = None                # название (для роста — число или текст)
    hex: str = ""
    clear: bool = False             # явно пусто → убрать значение у товара

    @property
    def is_empty(self) -> bool:
        return self.id is None and not self.external_id and not clean_name(self.name)


def extract_ref(item: dict, kind: str) -> "Ref | None":
    """Достаёт из строки товара всё про справочник `kind`. None — поле не присылали вовсе
    (значит, у существующего товара оставляем как есть); Ref(clear=True) — прислали пустое."""
    keys = JSON_KEYS[kind]
    main = keys[0]
    raw_id = item.get(f"{main}_id")
    raw_ext = item.get(f"{main}_external_id")
    present = [k for k in keys if k in item]
    if not present and raw_id is None and raw_ext is None:
        return None

    ref = Ref(kind=kind)
    if raw_id not in (None, ""):
        try:
            ref.id = int(raw_id)
        except (TypeError, ValueError):
            raise TaxonomyError(f"{LABELS[kind]}: {main}_id должен быть числом, а не «{clean_name(raw_id, 20)}»")
    if raw_ext not in (None, ""):
        ref.external_id = clean_name(raw_ext, 64)

    for key in present:
        value = item.get(key)
        if isinstance(value, dict):
            if value.get("id") not in (None, "") and ref.id is None:
                try:
                    ref.id = int(value["id"])
                except (TypeError, ValueError):
                    raise TaxonomyError(f"{LABELS[kind]}: id должен быть числом")
            if value.get("external_id") not in (None, "") and not ref.external_id:
                ref.external_id = clean_name(value["external_id"], 64)
            name = value.get("cm") if kind == "height" and value.get("cm") not in (None, "") else value.get("name")
            if name not in (None, "") and ref.name in (None, ""):
                ref.name = name
            if kind == "color":
                ref.hex = clean_hex(value.get("hex"))
        elif value not in (None, "") and ref.name in (None, ""):
            ref.name = value
    if kind == "color" and not ref.hex:
        ref.hex = clean_hex(item.get("color_hex"))
    if kind == "height" and isinstance(ref.name, (int, float, str)) and str(ref.name).strip() in ("0", "0.0"):
        ref.name = None                                  # 1С часто шлёт 0 вместо «рост неизвестен»
    if ref.is_empty:
        ref.clear = True
    return ref


# ═══════════════════════════════════════════════════════════
# Справочники в памяти на время одной операции
# ═══════════════════════════════════════════════════════════

class Taxonomy:
    """Все четыре справочника, загруженные один раз (в них десятки строк): поиск без
    лишних запросов, создание недостающих записей, отчёт о созданном."""

    def __init__(self, db: Session):
        self.db = db
        self._by_id: dict[str, dict[int, Any]] = {}
        self._by_ext: dict[str, dict[str, Any]] = {}
        self._by_key: dict[str, dict[Any, Any]] = {}
        self.created: dict[str, list[str]] = {k: [] for k in MODELS}
        for kind, model in MODELS.items():
            rows = db.query(model).all()
            self._by_id[kind] = {r.id: r for r in rows}
            self._by_ext[kind] = {r.external_id: r for r in rows if r.external_id}
            self._by_key[kind] = {self._key(kind, r): r for r in rows}

    @staticmethod
    def _key(kind: str, row_or_value) -> Any:
        if kind == "height":
            return row_or_value.cm if hasattr(row_or_value, "cm") else parse_height(row_or_value)
        value = row_or_value.name if hasattr(row_or_value, "name") else row_or_value
        return fold(value)

    def rows(self, kind: str) -> list:
        return list(self._by_id[kind].values())

    def resolve(self, ref: Ref, *, create: bool = True):
        """Запись справочника по ссылке. Ничего не указано → None. Нашли по id, затем по
        id из 1С, затем по названию; нет — создаём (если create) и запоминаем в отчёте."""
        kind = ref.kind
        label = LABELS[kind]
        if ref.id is not None:
            row = self._by_id[kind].get(ref.id)
            if row is None:
                raise TaxonomyError(f"{label}: на сайте нет записи с id={ref.id}")
            return row
        if ref.external_id:
            row = self._by_ext[kind].get(ref.external_id)
            if row is not None:
                return row
        key = self._key(kind, ref.name) if ref.name not in (None, "") else None
        if key not in (None, ""):
            row = self._by_key[kind].get(key)
            if row is not None:
                if ref.external_id and not row.external_id:       # сверяем справочники: запоминаем id 1С
                    row.external_id = ref.external_id
                    self._by_ext[kind][ref.external_id] = row
                if kind == "color" and ref.hex and not row.hex:
                    row.hex = ref.hex
                return row
        if not create:
            return None
        if key in (None, ""):
            if ref.external_id:
                raise TaxonomyError(f"{label}: для новой записи (id в 1С {ref.external_id}) нужно ещё и название")
            return None
        return self._create(kind, ref, key)

    def _create(self, kind: str, ref: Ref, key):
        model = MODELS[kind]
        if kind == "height":
            row = model(cm=key, external_id=ref.external_id)
            shown = f"{key} см"
        else:
            shown = display_name(ref.name)
            if not shown:
                raise TaxonomyError(f"{LABELS[kind]}: пустое название")
            row = model(name=shown, external_id=ref.external_id)
            if kind == "color":
                row.hex = ref.hex
            # новое значение встаёт в конец списка
            row.sort_order = max((r.sort_order or 0 for r in self._by_id[kind].values()), default=0) + 1
        self.db.add(row)
        self.db.flush()
        self._by_id[kind][row.id] = row
        if ref.external_id:
            self._by_ext[kind][ref.external_id] = row
        self._by_key[kind][key] = row
        self.created[kind].append(shown)
        return row

    def by_name(self, kind: str, name, *, create: bool = True):
        """Короткий путь: найти/создать запись по одному названию."""
        if name in (None, ""):
            return None
        return self.resolve(Ref(kind=kind, name=name), create=create)

    def resolve_refs(self, refs: dict, *, create: bool = True) -> dict:
        """{kind: Ref | None} → {kind: запись | CLEAR}. Что не присылали (None) или не нашлось
        без права создавать — в результат не попадает. Сам товар здесь не трогается: ошибка
        в одном значении не оставит товар наполовину заполненным."""
        out = {}
        for kind, ref in refs.items():
            if ref is None:
                continue
            if ref.clear:
                out[kind] = CLEAR
                continue
            row = self.resolve(ref, create=create)
            if row is not None:
                out[kind] = row
        return out

    @staticmethod
    def apply(product: Product, resolved: dict) -> None:
        for kind, value in resolved.items():
            setattr(product, SLOTS[kind], None if value is CLEAR else value)

    def assign(self, product: Product, refs: dict, *, create: bool = True) -> None:
        """resolve_refs + apply одним шагом."""
        self.apply(product, self.resolve_refs(refs, create=create))

    def fill_empty_from_name(self, product: Product, skip: "set[str] | frozenset[str]" = frozenset()) -> None:
        """Пустые страна, рост, вид и цвет у товара — угадываем по названию («Роза Кения 40с»).
        Никогда не затирает заполненное, неуверенно — оставляет пустым. skip — справочники,
        которые 1С только что явно очистила: их не угадываем."""
        from .product_guess import guess_category, guess_color, guess_country, guess_length_cm
        name = product.name or ""
        if product.country_ref is None and "country" not in skip:
            country = guess_country(name)
            if country:
                product.country_ref = self.by_name("country", country)
        if product.height_ref is None and "height" not in skip:
            cm = guess_length_cm(name)
            if cm:
                try:
                    product.height_ref = self.by_name("height", cm)
                except TaxonomyError:
                    pass
        if product.kind_ref is None and "kind" not in skip:
            kind = guess_category(name)
            if kind:
                product.kind_ref = self.by_name("kind", kind)
        if product.color_ref is None and "color" not in skip:
            color = guess_color(name)
            if color:
                product.color_ref = self.by_name("color", color)

    def report(self) -> dict:
        """{kind: [«что создали»]} — только непустое; для ответа 1С и журнала."""
        return {k: v for k, v in self.created.items() if v}


def describe(report: dict) -> str:
    """{"color": ["Белый"], "height": ["65 см"]} → «цвет: Белый; рост: 65 см» (журнал, сообщения)."""
    return "; ".join(f"{LABELS[k].lower()}: {', '.join(v[:5])}{'…' if len(v) > 5 else ''}"
                     for k, v in report.items() if v)


def set_taxonomy(db: Session, product: Product, *, kind=None, color=None, height=None, country=None,
                 tax: "Taxonomy | None" = None) -> Taxonomy:
    """Удобный способ положить значения в товар по названиям: set_taxonomy(db, p, kind="Роза",
    height=60). None — не трогать; пустая строка — убрать значение."""
    tax = tax or Taxonomy(db)
    refs = {}
    for name, value in (("kind", kind), ("color", color), ("height", height), ("country", country)):
        if value is None:
            continue
        refs[name] = Ref(kind=name, clear=True) if value == "" else Ref(kind=name, name=value)
    tax.assign(product, refs)
    return tax


# ═══════════════════════════════════════════════════════════
# Первый запуск и перенос старых данных
# ═══════════════════════════════════════════════════════════

def seed_defaults(db: Session) -> dict:
    """Кладёт в справочники стартовый набор (то, чего ещё нет — по названию). Безопасно повторять."""
    tax = Taxonomy(db)
    for name in DEFAULT_KINDS:
        tax.by_name("kind", name)
    for name, hex_ in DEFAULT_COLORS:
        tax.resolve(Ref(kind="color", name=name, hex=hex_ or ""))
    for cm in DEFAULT_HEIGHTS:
        tax.by_name("height", cm)
    for name in DEFAULT_COUNTRIES:
        tax.by_name("country", name)
    # порядок показа: сначала стартовый набор в том порядке, как он записан, затем остальное
    for kind, names in (("kind", DEFAULT_KINDS), ("color", [n for n, _hex in DEFAULT_COLORS]),
                        ("country", DEFAULT_COUNTRIES)):
        position = {fold(n): i for i, n in enumerate(names, start=1)}
        for i, row in enumerate(ordered(kind, tax.rows(kind)), start=len(names) + 1):
            row.sort_order = position.get(fold(row.name), i)
    db.flush()
    return tax.report()


def apply_legacy_values(tax: Taxonomy, product: Product, *, country: Any = None,
                        category: Any = None, length_cm: Any = None) -> None:
    """Старые текстовые поля товара → справочники (только там, где ссылка ещё пуста).
    Категория «Роза Эквадор» — это на самом деле вид «Роза» + страна «Эквадор»."""
    country_name = clean_name(country)
    kind_name = clean_name(category)
    if kind_name.casefold() == "роза эквадор":
        kind_name = "Роза"
        country_name = country_name or "Эквадор"
    if country_name and product.country_ref is None:
        product.country_ref = tax.by_name("country", country_name)
    if kind_name and product.kind_ref is None:
        product.kind_ref = tax.by_name("kind", kind_name)
    try:
        cm = parse_height(length_cm) if length_cm not in (None, "", 0) else None
    except TaxonomyError:
        cm = None
    if cm and product.height_ref is None:
        product.height_ref = tax.by_name("height", cm)


def migrate_legacy_products(db: Session) -> int:
    """Один раз при обновлении: берёт из колонок products.country / category / length_cm
    значения и превращает их в записи справочников. Возвращает число обработанных товаров."""
    from sqlalchemy import inspect, text
    cols = {c["name"] for c in inspect(db.get_bind()).get_columns("products")}
    legacy = [c for c in ("country", "category", "length_cm") if c in cols]
    if not legacy:
        return 0
    rows = db.execute(text(f"SELECT id, {', '.join(legacy)} FROM products")).mappings().all()
    tax = Taxonomy(db)
    done = 0
    for row in rows:
        product = db.get(Product, row["id"])
        if product is None:
            continue
        apply_legacy_values(tax, product, country=row.get("country"),
                            category=row.get("category"), length_cm=row.get("length_cm"))
        done += 1
    db.flush()
    return done


# ═══════════════════════════════════════════════════════════
# Для обмена с 1С и для админки
# ═══════════════════════════════════════════════════════════

def row_payload(kind: str, row) -> dict | None:
    """Запись справочника для JSON: {"id", "external_id", "name", …}."""
    if row is None:
        return None
    data = {"id": row.id, "external_id": row.external_id or "", "name": row.name}
    if kind == "height":
        data["cm"] = row.cm
    if kind == "color":
        data["hex"] = row.hex or ""
    return data


def ordered(kind: str, rows: list) -> list:
    if kind == "height":
        return sorted(rows, key=lambda r: r.cm)
    return sorted(rows, key=lambda r: (r.sort_order or 0, fold(r.name)))


def choices(db: Session) -> dict:
    """Записи всех справочников в порядке показа: {kind: [строки]} — для выпадающих списков."""
    return {kind: ordered(kind, db.query(model).all()) for kind, model in MODELS.items()}


def dictionaries_payload(db: Session) -> dict:
    """Все справочники для 1С: {"kinds": [...], "colors": [...], "heights": [...], "countries": [...]}."""
    return {PLURALS[kind]: [row_payload(kind, r) for r in ordered(kind, db.query(model).all())]
            for kind, model in MODELS.items()}


def usage_counts(db: Session) -> dict[str, dict[int, int]]:
    """{kind: {id записи: сколько товаров на неё ссылается}} — для страницы «Фильтры каталога»."""
    return {kind: dict(db.query(col, func.count(Product.id)).filter(col.isnot(None)).group_by(col).all())
            for kind, col in PRODUCT_FK.items()}
