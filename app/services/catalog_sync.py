# app/services/catalog_sync.py
"""Загрузка каталога (товары, цены, остатки) в базу сайта.

Одна и та же логика обслуживает ВСЕ способы доставки данных:
  * JSON от 1С           — POST /api/1c/products/sync
  * CSV-текст от 1С      — тот же адрес, Content-Type: text/csv
  * файл в админке       — «Интеграция» → «Загрузить файл из 1С» (CSV / Excel)

КАК УСТРОЕНА ЗАГРУЗКА (почему так). Каждая строка сначала ПРОВЕРЯЕТСЯ в
Python — длины полей обрезаются под колонки БД, числа ограничиваются, из
текста убираются управляющие символы. Раньше «мусорная» строка (название
длиннее 200 символов, символ NUL) на PostgreSQL ломала всю транзакцию, и
одна плохая строка роняла обмен целиком. Теперь плохие строки попадают в
`errors`, остальные загружаются.
"""
import ipaddress
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload

from ..models import Order, OrderItem, Product, Supply, SupplyItem
from .orders import CANCELLED
from .product_guess import guess_category, guess_country, guess_length_cm

logger = logging.getLogger(__name__)

SYNC_LOCK_KEY = 7_201_001      # ключ advisory-lock PostgreSQL для обмена
MAX_PRODUCTS_PER_SYNC = 20_000
MAX_ERRORS_IN_REPLY = 50


class RowError(ValueError):
    """Строка выгрузки не прошла проверку (в ответе попадёт в `errors`)."""


class SyncCommitError(RuntimeError):
    """База не приняла выгрузку (подробности — в журнале сервера)."""


# ═══════════════════════════════════════════════════════════
# ПРОВЕРКА И ОЧИСТКА ВХОДНЫХ ДАННЫХ
# ═══════════════════════════════════════════════════════════

# управляющие символы (NUL и т.п.): PostgreSQL не принимает \x00 в тексте
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


def clean(value, maxlen: int) -> str:
    """Текст из 1С → строка без управляющих символов, не длиннее колонки БД."""
    if value is None:
        return ""
    return _CTRL_RE.sub("", str(value)).strip()[:maxlen]


def to_number(value, label: str) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        raise RowError(f"{label}: ожидается число")
    if isinstance(value, (int, float)):
        x = float(value)
    elif isinstance(value, str):
        t = value.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
        if not t:
            return 0.0
        try:
            x = float(t)
        except ValueError:
            raise RowError(f"{label}: «{value[:20]}» — не число")
    else:
        raise RowError(f"{label}: ожидается число")
    if math.isnan(x) or math.isinf(x):
        raise RowError(f"{label}: недопустимое значение")
    return x


def bounded_int(value, label: str, lo: int, hi: int, default: int) -> int:
    """Целое в диапазоне. Пусто/0 → default; вне диапазона → ошибка строки."""
    x = to_number(value, label)
    if x == 0:
        return default
    n = int(x)
    if not (lo <= n <= hi):
        raise RowError(f"{label}: {n} вне диапазона {lo}…{hi}")
    return n


def is_safe_url(url: str) -> bool:
    """URL фото можно хранить и показывать в <img>.

    Сервер эти адреса НЕ скачивает (PDF-генератор читает только свою папку
    static), поэтому DNS не резолвим — раньше на каждый URL делался
    блокирующий gethostbyname, и выгрузка из тысяч товаров с фото
    «подвисала» на минуты. Отсекаем то, что опасно или бессмысленно для
    браузера клиента: чужие схемы, логин:пароль в адресе, localhost и
    внутренние IP-адреса.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower()
        if not host or parsed.username or parsed.password:
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return True                      # обычное доменное имя
        return not (addr.is_private or addr.is_loopback or addr.is_link_local
                    or addr.is_reserved or addr.is_multicast or addr.is_unspecified)
    except Exception:
        return False


def normalize_photos(raw) -> list:
    """Приводит поле photo/photos из 1С к списку валидных URL-ов."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    elif isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict):
                for key in ("url", "photo", "image", "link"):
                    if key in item and item[key]:
                        parts.append(str(item[key]))
                        break
            elif item is not None:
                parts.append(str(item).strip())
    else:
        return []

    result = []
    for s in parts:
        s = _CTRL_RE.sub("", s)
        if not s or len(s) > 500:          # image_url в БД — VARCHAR(500)
            continue
        if any(s.lower().startswith(bad) for bad in _BAD_SCHEMES):
            continue
        if s.startswith("/static/"):
            result.append(s)
        elif s.startswith(("http://", "https://")) and is_safe_url(s):
            result.append(s)
    return result[:20]


def extract_photos(item: dict) -> list:
    for key in ("photos", "photo", "image_url"):
        value = item.get(key)
        if value:
            photos = normalize_photos(value)
            if photos:
                return photos
    return []


def ext_id_of(item: dict) -> str:
    """Идентификатор товара в 1С: поле id / external_id / guid / code.

    Именно по нему товар сопоставляется при обмене (приоритет выше sku и
    названия), поэтому переименование номенклатуры в 1С не создаёт дубль.
    """
    for key in ("id", "external_id", "guid", "code"):
        value = item.get(key)
        if value not in (None, ""):
            return clean(value, 64)
    return ""


def parse_product_row(item: dict) -> dict:
    """Проверка и очистка одной строки каталога. Бросает RowError."""
    name = clean(item.get("name"), 200)
    if not name:
        raise RowError("нет name")

    price = to_number(item.get("price_per_stem"), "price_per_stem")
    if not (0 <= price <= 10_000_000):
        raise RowError(f"недопустимая цена {price}")

    row = {
        "ext": ext_id_of(item),
        "sku": clean(item.get("sku"), 100),
        "name": name,
        "price": round(price, 2),
        "photos": extract_photos(item),
        # необязательные поля: None = «не передано, не трогать»
        "description": (clean(item.get("description"), 5000)
                        if "description" in item else None),
        "country": (clean(item.get("country"), 100)
                    if "country" in item else None),
        "category": ((clean(item.get("category"), 100) or None)
                     if "category" in item else None),
        "length_cm": None, "package_size": None, "min_quantity": None,
        "stock_packs": None, "stock_stems": None,
    }
    if "length_cm" in item:
        row["length_cm"] = int(to_number(item["length_cm"], "length_cm"))
        if not (0 <= row["length_cm"] <= 500):
            raise RowError(f"length_cm {row['length_cm']} вне диапазона 0…500")
    if "package_size" in item:
        row["package_size"] = bounded_int(
            item["package_size"], "package_size", 1, 10_000, 1)
    # подсказка для НОВЫХ товаров, когда в файле нет размера упаковки
    row["package_size_hint"] = None
    if item.get("package_size_hint"):
        try:
            row["package_size_hint"] = bounded_int(
                item["package_size_hint"], "package_size_hint", 1, 10_000, 1)
        except RowError:
            pass
    if "min_quantity" in item:
        row["min_quantity"] = bounded_int(
            item["min_quantity"], "min_quantity", 1, 10_000, 1)

    if item.get("stock_packs") not in (None, ""):
        n = int(to_number(item["stock_packs"], "stock_packs"))
        if not (0 <= n <= 1_000_000):
            raise RowError(f"недопустимый остаток {n}")
        row["stock_packs"] = n
    elif item.get("stock_stems") not in (None, ""):
        n = int(to_number(item["stock_stems"], "stock_stems"))
        if not (0 <= n <= 100_000_000):
            raise RowError(f"недопустимый остаток {n} шт")
        row["stock_stems"] = n
    else:
        row["stock_packs"] = 0
    return row


# ═══════════════════════════════════════════════════════════
# ПОИСК И СЛУЖЕБНАЯ ПОСТАВКА
# ═══════════════════════════════════════════════════════════

def lock_exchange(db: Session) -> None:
    """Одновременно может идти только один обмен каталогом.

    Если 1С повторила запрос, не дождавшись ответа, два синка иначе
    создали бы дубли товаров и служебной поставки. Блокировка снимается
    сама при commit/rollback; на SQLite (разработка) это не нужно.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SYNC_LOCK_KEY})


def ensure_service_supply(db: Session) -> Supply:
    supply = (
        db.query(Supply)
        .filter(Supply.is_service.is_(True))
        .order_by(Supply.id)
        .first()
    )
    if not supply:
        # Базы, созданные до появления колонки is_service, держат служебную
        # партию как обычную (is_service=FALSE по умолчанию). Находим её по
        # пометке в заметках и «повышаем» — иначе завели бы вторую партию, и
        # каждый товар в каталоге продублировался бы.
        supply = (
            db.query(Supply)
            .filter(Supply.notes.like("%Авто-синхронизация из 1С%"))
            .order_by(Supply.id)
            .first()
        )
        if supply is not None:
            supply.is_service = True
    if not supply:
        supply = Supply(
            country="1С",
            status="Ожидается",
            arrival_date=datetime.now(timezone.utc).replace(tzinfo=None),
            notes="Авто-синхронизация из 1С",
            is_service=True,
        )
        db.add(supply)
        db.flush()
        logger.info("1С: создана служебная поставка №%d", supply.id)
    return supply


def _chunks(seq: list, size: int = 500):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class Catalog:
    """Поиск существующих товаров: id из 1С → sku → название."""

    def __init__(self, db: Session, rows: list[dict]):
        self.by_ext: dict[str, Product] = {}
        self.by_sku: dict[str, Product] = {}
        self.by_name: dict[str, Product] = {}

        exts = sorted({ext_id_of(r) for r in rows} - {""})
        skus = sorted({clean(r.get("sku"), 100) for r in rows} - {""})
        names = sorted({clean(r.get("name"), 200) for r in rows} - {""})
        # порциями: у SQLite лимит на число параметров запроса
        for column, values in ((Product.external_id, exts),
                               (Product.sku, skus), (Product.name, names)):
            for part in _chunks(values):
                for p in db.query(Product).filter(column.in_(part)).all():
                    self._index(p)

    def _index(self, p: Product) -> None:
        if p.external_id:
            self.by_ext.setdefault(p.external_id, p)
        if p.sku:
            self.by_sku.setdefault(p.sku, p)
        self.by_name.setdefault(p.name, p)

    def add(self, p: Product) -> None:
        self._index(p)

    def find(self, ext: str, sku: str, name: str) -> Product | None:
        # 1) точное совпадение по id из 1С
        if ext and ext in self.by_ext:
            return self.by_ext[ext]
        # 2) по артикулу / 3) по названию — но только если это не ДРУГОЙ товар
        #    1С: иначе два разных товара с одним названием склеились бы в один
        for cand in (self.by_sku.get(sku) if sku else None,
                     self.by_name.get(name) if name else None):
            if cand and (not ext or not cand.external_id
                         or cand.external_id == ext):
                return cand
        return None


def new_product(row: dict, main_image: str = "", photos: list | None = None) -> Product:
    """Новый товар из разобранной строки; страну, длину и категорию, которых
    1С не прислала, пробуем понять по названию («Роза Кения 40» → Кения, 40 см)."""
    country = row["country"] or guess_country(row["name"])
    return Product(
        sku=row["sku"], external_id=row["ext"], name=row["name"],
        description=row["description"] or "",
        country=country,
        length_cm=row["length_cm"] or guess_length_cm(row["name"]),
        unit="упаковка",
        package_size=row["package_size"] or row.get("package_size_hint") or 1,
        min_quantity=row["min_quantity"] or 1,
        category=row["category"] or guess_category(row["name"], country) or "Прочее",
        image_url=main_image, photos=photos or [],
    )


def fill_empty_from_name(product: Product, row: dict) -> None:
    """У существующего товара заполняем только ПУСТЫЕ поля и только если 1С
    их не присылала; всё, что уже есть (от 1С или от менеджера), не трогаем."""
    if not product.country and row["country"] is None:
        product.country = guess_country(product.name)
    if not product.length_cm and row["length_cm"] is None:
        product.length_cm = guess_length_cm(product.name)
    if (not product.category or product.category == "Прочее") and not row["category"]:
        product.category = (guess_category(product.name, product.country or "")
                            or product.category or "Прочее")


def duplicates_with_manual_supplies(db: Session) -> int:
    """Сколько товаров одновременно лежит на полке и из 1С, и из ручной поставки.

    Каталог показывает каждую позицию склада отдельной карточкой, поэтому такой
    товар виден дважды (возможно, с разными ценами). Это бывает при переходе на
    1С, пока старые ручные партии ещё не сняты с полок.
    """
    from sqlalchemy.orm import aliased
    svc_item, man_item = aliased(SupplyItem), aliased(SupplyItem)
    svc, man = aliased(Supply), aliased(Supply)
    return (
        db.query(func.count(func.distinct(svc_item.product_id)))
        .select_from(svc_item)
        .join(svc, svc.id == svc_item.supply_id)
        .join(man_item, man_item.product_id == svc_item.product_id)
        .join(man, man.id == man_item.supply_id)
        .filter(svc.is_service.is_(True), svc_item.is_active.is_(True),
                man.is_service.is_(False), man_item.is_active.is_(True))
        .scalar()
    ) or 0


def pending_packs(db: Session) -> dict[int, int]:
    """Упаковки из заказов, которые 1С ещё НЕ забрала (exported_at IS NULL)."""
    rows = (
        db.query(OrderItem.supply_item_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.exported_at.is_(None),
                Order.status != CANCELLED,
                OrderItem.supply_item_id.isnot(None))
        .group_by(OrderItem.supply_item_id)
        .all()
    )
    return {sid: int(total or 0) for sid, total in rows}


# ═══════════════════════════════════════════════════════════
# ПРИМЕНЕНИЕ ВЫГРУЗКИ
# ═══════════════════════════════════════════════════════════

@dataclass
class SyncResult:
    processed: int = 0
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    supply_id: int | None = None
    dry_run: bool = False

    def as_dict(self) -> dict:
        return {
            "status": "ok",
            "dry_run": self.dry_run,
            "processed": self.processed,
            "created": self.created,
            "updated": self.updated,
            "deactivated": self.deactivated,
            "errors": self.errors[:MAX_ERRORS_IN_REPLY],
            "errors_total": len(self.errors),
            "warnings": self.warnings,
            "supply_id": self.supply_id,
        }

    def summary(self) -> str:
        text = (f"товаров: {self.processed}, новых: {self.created}, "
                f"обновлено: {self.updated}")
        if self.deactivated:
            text += f", снято с полки: {self.deactivated}"
        if self.errors:
            text += f", ошибок в строках: {len(self.errors)}"
        if self.dry_run:
            text = "ПРОВЕРКА, ничего не сохранено — " + text
        return text


def apply_products(db: Session, items: list[dict], *, full: bool,
                   reserve_unexported: bool = False,
                   dry_run: bool = False) -> SyncResult:
    """Применяет разобранную выгрузку к базе. Делает commit.

    full=True — «полный снимок»: все позиции каталога, которых НЕТ в выгрузке,
    снимаются с остатка (stock=0) и с полки. Включайте, только когда присылаете
    ВЕСЬ каталог одним запросом.

    dry_run=True — «проверка без сохранения»: всё считается точно так же (сколько
    товаров создастся и обновится, какие строки с ошибками, сколько снимется с
    полки), но в конце транзакция откатывается и в базе ничего не меняется.
    """
    lock_exchange(db)
    supply = ensure_service_supply(db)
    catalog = Catalog(db, items)

    # Позиции служебной партии: один запрос вместо запроса на каждую строку.
    # joinedload отдаёт те же объекты Product, что нашёл каталог выше.
    existing_items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.supply_id == supply.id)
        .all()
    )
    item_of: dict[Product, SupplyItem] = {si.product: si for si in existing_items}
    pending = pending_packs(db) if reserve_unexported else {}

    result = SyncResult(supply_id=supply.id)
    seen: set[int] = set()            # id(Product) — что пришло в этой выгрузке

    for idx, item in enumerate(items, start=1):
        try:
            row = parse_product_row(item)
        except RowError as e:
            label = clean(item.get("name"), 60) or "?"
            result.errors.append(f"#{idx} ({label}): {e}")
            # при «полном снимке» не обнуляем товар, чья строка пришла битой
            prot = catalog.find(ext_id_of(item), clean(item.get("sku"), 100),
                                clean(item.get("name"), 200))
            if prot is not None:
                seen.add(id(prot))
            continue

        product = catalog.find(row["ext"], row["sku"], row["name"])
        photos = row["photos"]
        main_image = photos[0] if photos else ""

        if product is None:
            product = new_product(row, main_image, photos)
            db.add(product)
            catalog.add(product)
            result.created += 1
        else:
            product.sku = row["sku"] or product.sku
            if row["ext"]:
                product.external_id = row["ext"]
            product.name = row["name"]
            if row["description"] is not None:
                product.description = row["description"]
            if row["country"] is not None:
                product.country = row["country"]
            if row["length_cm"] is not None:
                product.length_cm = row["length_cm"]
            if row["package_size"] is not None:
                product.package_size = row["package_size"]
            if row["min_quantity"] is not None:
                product.min_quantity = row["min_quantity"]
            if row["category"]:
                product.category = row["category"]
            if photos:
                product.photos = photos
                product.image_url = main_image
            fill_empty_from_name(product, row)
            catalog.add(product)
            result.updated += 1
        seen.add(id(product))

        pack = max(1, product.package_size or 1)
        if row["stock_packs"] is not None:
            stock = row["stock_packs"]
        else:
            stock = row["stock_stems"] // pack          # неполную упаковку не продаём

        si = item_of.get(product)
        if si is not None and pending:
            stock = max(0, stock - pending.get(si.id, 0))
        if si is None:
            si = SupplyItem(supply=supply, product=product, price=row["price"],
                            stock=stock, is_active=stock > 0)
            db.add(si)
            item_of[product] = si
        else:
            si.price = row["price"]
            si.stock = stock
            si.is_active = stock > 0
        result.processed += 1

    if full:
        for product, si in item_of.items():
            if id(product) in seen:
                continue
            if (si.stock or 0) > 0 or si.is_active:
                si.stock = 0
                si.is_active = False
                result.deactivated += 1

    if dry_run:
        # откатываем всё, что насчитали: товары, партию, остатки и блокировку
        db.rollback()
        result.dry_run = True
        result.supply_id = None
        return result

    try:
        db.commit()
    except Exception as e:                                  # noqa: BLE001
        db.rollback()
        logger.exception("1С: ошибка коммита")
        raise SyncCommitError(str(e)) from e

    duplicates = duplicates_with_manual_supplies(db)
    if duplicates:
        result.warnings.append(
            f"{duplicates} тов. одновременно лежат на полке из 1С и из ручной "
            f"поставки — в каталоге они видны дважды. Снимите ручные партии с "
            f"полок (Админка → Поставки → «Снять с полок»).")
        logger.warning("1С sync: %s", result.warnings[0])
    return result
