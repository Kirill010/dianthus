# app/routers/integration_1c.py
"""
Обмен с 1С (HTTP Basic + лимит запросов). Полное описание — docs/1C_INTEGRATION.md.

  1С → сайт
    POST /api/1c/products/sync      каталог, цены, остатки (опц. «полный снимок»)
    POST /api/1c/orders/status      статусы заказов (+ номер документа в 1С)
    POST /api/1c/orders/ack         «заказы получены» (чтобы не выгружать повторно)
    POST /api/1c/clients/sync       персональные скидки клиентов
  сайт → 1С
    GET  /api/1c/products/export    каталог сайта
    GET  /api/1c/orders/export      заказы (?new=1 — только ещё не забранные)
    GET  /api/1c/clients/export     клиенты
  служебное
    GET  /api/1c/ping               проверка связи и логина/пароля

Формат товара (одинаковый в обе стороны):

    {"id": 1, "external_id": "ROSA-FREEDOM-60", "sku": "ROSA-FREEDOM-60",
     "name": "Роза Freedom 60 см", "description": "Классическая красная роза",
     "country": "Эквадор", "length_cm": 60, "unit": "упаковка",
     "package_size": 25, "min_quantity": 1, "category": "Роза Эквадор",
     "price_per_stem": 42.5, "stock_packs": 120,
     "photos": ["https://dianthus-opt.ru/static/uploads/abc.webp"]}

ВНИМАНИЕ по единицам: price_per_stem — цена за ШТ (стебель),
stock_packs — остаток в УПАКОВКАХ, package_size — штук в упаковке.
Если в 1С остаток ведётся в штуках — пришлите stock_stems (в упаковки
сайт переведёт сам: stock_stems // package_size).

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
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload, selectinload

from ..config import config
from ..database import get_db
from ..models import (ORDER_STATUSES, Order, OrderItem, Product, Supply,
                      SupplyItem, User)
from ..security import check_rate_limit, safe_equal
from ..services.audit import log_action
from ..services.orders import (CANCELLED, apply_status_change,
                               load_order_for_update, notify_client_of_status)
from ..timeutils import local_iso, utc_iso

logger = logging.getLogger(__name__)


async def _rate_1c(request: Request) -> None:
    """Защита Basic Auth от перебора пароля: не более 120 запросов/мин с IP."""
    await check_rate_limit(request, "onec", max_hits=120, window=60)


router = APIRouter(
    prefix="/api/1c", tags=["1C Integration"],
    dependencies=[Depends(_rate_1c)],
)

_basic = HTTPBasic(auto_error=False)

SYNC_LOCK_KEY = 7_201_001      # ключ advisory-lock PostgreSQL для обмена
MAX_PRODUCTS_PER_SYNC = 20_000
MAX_ERRORS_IN_REPLY = 50
ORDER_NUMBER_PREFIX = "САЙТ-"


# ═══════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════════════════

def _verify_basic(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
) -> bool:
    auth_configured = bool(
        config.INTEGRATION_USER and config.INTEGRATION_PASSWORD
    )

    if not auth_configured:
        logger.error("1С: INTEGRATION_USER/PASSWORD не заданы")
        raise HTTPException(
            status_code=503,
            detail="1С-интеграция не настроена на сервере (INTEGRATION_PASSWORD).",
        )

    if creds is None:
        raise HTTPException(
            status_code=401,
            detail="Требуется авторизация Basic Auth.",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )

    user_ok = safe_equal(creds.username or "", config.INTEGRATION_USER)
    pass_ok = safe_equal(creds.password or "", config.INTEGRATION_PASSWORD)
    if not (user_ok and pass_ok):
        logger.warning(
            "1С: неверные учётные данные, IP=%s",
            request.client.host if request.client else "?",
        )
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль.",
            headers={"WWW-Authenticate": 'Basic realm="Dianthus 1C"'},
        )
    return True


# ═══════════════════════════════════════════════════════════
# ПРОВЕРКА И ОЧИСТКА ВХОДНЫХ ДАННЫХ
# ═══════════════════════════════════════════════════════════

class RowError(ValueError):
    """Строка выгрузки не прошла проверку (в ответе попадёт в `errors`)."""


# управляющие символы (NUL и т.п.): PostgreSQL не принимает \x00 в тексте
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BAD_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


def _clean(value, maxlen: int) -> str:
    """Текст из 1С → строка без управляющих символов, не длиннее колонки БД."""
    if value is None:
        return ""
    return _CTRL_RE.sub("", str(value)).strip()[:maxlen]


def _num(value, label: str) -> float:
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


def _bounded_int(value, label: str, lo: int, hi: int, default: int) -> int:
    """Целое в диапазоне. Пусто/0 → default; вне диапазона → ошибка строки."""
    x = _num(value, label)
    if x == 0:
        return default
    n = int(x)
    if not (lo <= n <= hi):
        raise RowError(f"{label}: {n} вне диапазона {lo}…{hi}")
    return n


def _is_safe_url(url: str) -> bool:
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


def _normalize_photos(raw) -> list:
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
        elif s.startswith(("http://", "https://")) and _is_safe_url(s):
            result.append(s)
    return result[:20]


def _extract_photos_from_item(item: dict) -> list:
    for key in ("photos", "photo", "image_url"):
        value = item.get(key)
        if value:
            photos = _normalize_photos(value)
            if photos:
                return photos
    return []


def _ext_id_of(item: dict) -> str:
    """Идентификатор товара в 1С: поле id / external_id / guid / code.

    Именно по нему товар сопоставляется при обмене (приоритет выше sku и
    названия), поэтому переименование номенклатуры в 1С не создаёт дубль.
    """
    for key in ("id", "external_id", "guid", "code"):
        value = item.get(key)
        if value not in (None, ""):
            return _clean(value, 64)
    return ""


def _parse_order_id(item) -> int:
    """Номер заказа из {"number": "САЙТ-57"} или {"id": 57} или просто 57."""
    if isinstance(item, dict):
        raw = item.get("number")
        if raw in (None, ""):
            raw = item.get("id")
    else:
        raw = item
    if isinstance(raw, bool) or raw in (None, ""):
        raise RowError("не указан номер заказа")
    if isinstance(raw, int):
        return raw
    m = re.search(r"(\d+)\s*$", str(raw))
    if not m:
        raise RowError(f"не удалось определить номер заказа из «{str(raw)[:30]}»")
    return int(m.group(1))


def _lock_exchange(db: Session) -> None:
    """Одновременно может идти только один обмен каталогом.

    Если 1С повторила запрос, не дождавшись ответа, два синка иначе
    создали бы дубли товаров и служебной поставки. Блокировка снимается
    сама при commit/rollback; на SQLite (разработка) это не нужно.
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": SYNC_LOCK_KEY})


def _ensure_1c_supply(db: Session) -> Supply:
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


class _Catalog:
    """Поиск существующих товаров: id из 1С → sku → название."""

    def __init__(self, db: Session, rows: list[dict]):
        self.by_ext: dict[str, Product] = {}
        self.by_sku: dict[str, Product] = {}
        self.by_name: dict[str, Product] = {}

        exts = sorted({_ext_id_of(r) for r in rows} - {""})
        skus = sorted({_clean(r.get("sku"), 100) for r in rows} - {""})
        names = sorted({_clean(r.get("name"), 200) for r in rows} - {""})
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


# ═══════════════════════════════════════════════════════════
# 1С → САЙТ: КАТАЛОГ
# ═══════════════════════════════════════════════════════════

def _parse_product_row(item: dict) -> dict:
    """Проверка и очистка одной строки каталога. Бросает RowError."""
    name = _clean(item.get("name"), 200)
    if not name:
        raise RowError("нет name")

    price = _num(item.get("price_per_stem"), "price_per_stem")
    if not (0 <= price <= 10_000_000):
        raise RowError(f"недопустимая цена {price}")

    row = {
        "ext": _ext_id_of(item),
        "sku": _clean(item.get("sku"), 100),
        "name": name,
        "price": round(price, 2),
        "photos": _extract_photos_from_item(item),
        # необязательные поля: None = «не передано, не трогать»
        "description": (_clean(item.get("description"), 5000)
                        if "description" in item else None),
        "country": (_clean(item.get("country"), 100)
                    if "country" in item else None),
        "category": ((_clean(item.get("category"), 100) or None)
                     if "category" in item else None),
        "length_cm": None, "package_size": None, "min_quantity": None,
        "stock_packs": None, "stock_stems": None,
    }
    if "length_cm" in item:
        row["length_cm"] = int(_num(item["length_cm"], "length_cm"))
        if not (0 <= row["length_cm"] <= 500):
            raise RowError(f"length_cm {row['length_cm']} вне диапазона 0…500")
    if "package_size" in item:
        row["package_size"] = _bounded_int(
            item["package_size"], "package_size", 1, 10_000, 1)
    if "min_quantity" in item:
        row["min_quantity"] = _bounded_int(
            item["min_quantity"], "min_quantity", 1, 10_000, 1)

    if item.get("stock_packs") not in (None, ""):
        n = int(_num(item["stock_packs"], "stock_packs"))
        if not (0 <= n <= 1_000_000):
            raise RowError(f"недопустимый остаток {n}")
        row["stock_packs"] = n
    elif item.get("stock_stems") not in (None, ""):
        n = int(_num(item["stock_stems"], "stock_stems"))
        if not (0 <= n <= 100_000_000):
            raise RowError(f"недопустимый остаток {n} шт")
        row["stock_stems"] = n
    else:
        row["stock_packs"] = 0
    return row


def _duplicates_with_manual_supplies(db: Session) -> int:
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


def _pending_packs(db: Session) -> dict[int, int]:
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


@router.post("/products/sync")
def sync_products(
    payload: dict,
    request: Request,
    full: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Приём каталога, цен и остатков из 1С.

    full=1 (или "full": true в теле) — «полный снимок»: все позиции каталога,
    которых НЕТ в этой выгрузке, снимаются с остатка (stock=0) и с полки.
    Без этого товар, удалённый или распроданный в 1С, висел бы на сайте со
    старым остатком вечно. Включайте только когда присылаете ВЕСЬ каталог
    одним запросом.
    """
    client_ip = request.client.host if request.client else "?"

    products_data = payload.get("products", [])
    if not isinstance(products_data, list):
        raise HTTPException(400, "Поле 'products' должно быть массивом")
    if len(products_data) > MAX_PRODUCTS_PER_SYNC:
        raise HTTPException(
            413, f"Слишком много товаров за раз (максимум "
                 f"{MAX_PRODUCTS_PER_SYNC}). Разбейте выгрузку на части."
        )
    full_snapshot = bool(full) or payload.get("full") is True
    # Элементы не-объекты (null, числа, строки) пропускаем, а не валим весь обмен
    products_data = [it for it in products_data if isinstance(it, dict)]

    logger.info("1С sync: ip=%s, товаров в запросе: %d, полный снимок: %s",
                client_ip, len(products_data), full_snapshot)

    if not products_data and not full_snapshot:
        return JSONResponse({
            "status": "ok", "processed": 0, "created": 0, "updated": 0,
            "deactivated": 0, "errors": [], "supply_id": None,
        })
    if not products_data and full_snapshot:
        # Пустой «полный снимок» обнулил бы весь каталог — почти наверняка
        # это ошибка выгрузки на стороне 1С, а не желание закрыть магазин.
        raise HTTPException(
            400, "Полный снимок без товаров не принимается "
                 "(он снял бы с продажи весь каталог).")

    _lock_exchange(db)
    supply = _ensure_1c_supply(db)
    catalog = _Catalog(db, products_data)

    # Позиции служебной партии: один запрос вместо запроса на каждую строку.
    # joinedload отдаёт те же объекты Product, что нашёл каталог выше.
    existing_items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.supply_id == supply.id)
        .all()
    )
    item_of: dict[Product, SupplyItem] = {si.product: si for si in existing_items}
    pending = _pending_packs(db) if config.ONEC_RESERVE_UNEXPORTED else {}

    processed = created = updated = 0
    errors: list[str] = []
    seen: set[int] = set()            # id(Product) — что пришло в этой выгрузке

    for idx, item in enumerate(products_data, start=1):
        try:
            row = _parse_product_row(item)
        except RowError as e:
            label = _clean(item.get("name"), 60) or "?"
            errors.append(f"#{idx} ({label}): {e}")
            # при «полном снимке» не обнуляем товар, чья строка пришла битой
            prot = catalog.find(_ext_id_of(item), _clean(item.get("sku"), 100),
                                _clean(item.get("name"), 200))
            if prot is not None:
                seen.add(id(prot))
            continue

        product = catalog.find(row["ext"], row["sku"], row["name"])
        photos = row["photos"]
        main_image = photos[0] if photos else ""

        if product is None:
            product = Product(
                sku=row["sku"], external_id=row["ext"], name=row["name"],
                description=row["description"] or "",
                country=row["country"] or "",
                length_cm=row["length_cm"] or 0,
                unit="упаковка",
                package_size=row["package_size"] or 1,
                min_quantity=row["min_quantity"] or 1,
                category=row["category"] or "Прочее",
                image_url=main_image, photos=photos,
            )
            db.add(product)
            catalog.add(product)
            created += 1
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
            catalog.add(product)
            updated += 1
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
        processed += 1

    deactivated = 0
    if full_snapshot:
        for product, si in item_of.items():
            if id(product) in seen:
                continue
            if (si.stock or 0) > 0 or si.is_active:
                si.stock = 0
                si.is_active = False
                deactivated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("1С: ошибка коммита")
        # подробности — только в лог сервера, не в ответ внешней системе
        raise HTTPException(500, "Ошибка БД при сохранении выгрузки. Повторите позже.")

    duplicates = _duplicates_with_manual_supplies(db)
    warnings: list[str] = []
    if duplicates:
        warnings.append(
            f"{duplicates} тов. одновременно лежат на полке из 1С и из ручной "
            f"поставки — в каталоге они видны дважды. Снимите ручные партии с "
            f"полок (Админка → Поставки → «Снять с полок»).")
        logger.warning("1С sync: %s", warnings[0])

    logger.info(
        "1С sync: ip=%s, processed=%d, created=%d, updated=%d, "
        "deactivated=%d, errors=%d",
        client_ip, processed, created, updated, deactivated, len(errors),
    )
    return JSONResponse({
        "status": "ok",
        "processed": processed,
        "created": created,
        "updated": updated,
        "deactivated": deactivated,
        "errors": errors[:MAX_ERRORS_IN_REPLY],
        "errors_total": len(errors),
        "warnings": warnings,
        "supply_id": supply.id,
    })


@router.get("/ping")
def ping(_auth: bool = Depends(_verify_basic)):
    return {
        "status": "ok",
        "service": "dianthus-1c",
        "env": config.ENV,
        "auth": bool(config.INTEGRATION_PASSWORD),
        "server_time_utc": utc_iso(datetime.now(timezone.utc)),
        "reserve_unexported": config.ONEC_RESERVE_UNEXPORTED,
    }


# ═══════════════════════════════════════════════════════════
# САЙТ → 1С: КАТАЛОГ
# ═══════════════════════════════════════════════════════════

def _product_payload(product: Product, item: SupplyItem | None) -> dict:
    """Одна запись каталога в формате обмена с 1С.

    Формат тот же, что принимает POST /api/1c/products/sync: выгрузку можно
    поправить в 1С и отправить обратно без преобразований.
    """
    return {
        # id — идентификатор товара НА САЙТЕ
        "id": product.id,
        # external_id — идентификатор товара В 1С (то, что 1С присылает в "id")
        "external_id": product.external_id or "",
        "sku": product.sku or "",
        "name": product.name,
        "description": product.description or "",
        "country": product.country or "",
        "length_cm": product.length_cm or 0,
        "unit": product.unit or "упаковка",
        "package_size": product.package_size or 1,
        "min_quantity": product.min_quantity or 1,
        "category": product.category or "Прочее",
        # Цена за ШТ (стебель), остаток — в УПАКОВКАХ
        "price_per_stem": float(item.price) if item else 0.0,
        "stock_packs": int(item.stock) if item else 0,
        "reserved_packs": int(item.reserved_stock or 0) if item else 0,
        "available_packs": int(item.available_stock) if item else 0,
        "photos": product.all_photos,
        "supply_item_id": item.id if item else None,
    }


@router.get("/products/export")
def export_products_to_1c(
    all: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Каталог сайта для 1С — одна запись на товар.

        GET /api/1c/products/export          — товары, которые сейчас на полке
        GET /api/1c/products/export?all=1    — весь справочник (включая отсутствующие)

    Цена и остаток берутся из активной партии поставки.
    """
    rows: dict[int, dict] = {}
    items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.is_active.is_(True))
        .order_by(SupplyItem.id.desc())
        .all()
    )
    for item in items:
        product = item.product
        if product and product.id not in rows:
            rows[product.id] = _product_payload(product, item)

    if all:
        for product in db.query(Product).order_by(Product.id).all():
            rows.setdefault(product.id, _product_payload(product, None))

    products = [rows[pid] for pid in sorted(rows)]
    return JSONResponse({"count": len(products), "products": products})


# ═══════════════════════════════════════════════════════════
# САЙТ → 1С: ЗАКАЗЫ
# ═══════════════════════════════════════════════════════════

ORDERS_MAX_PER_CALL = 200


def _parse_since(since: str) -> datetime:
    try:
        dt = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "since — дата в формате ISO 8601")
    if dt.tzinfo is not None:                 # в БД время хранится в UTC без пояса
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _order_payload(o: Order, skus: dict) -> dict:
    user = o.user
    return {
        "id": o.id,
        "number": f"{ORDER_NUMBER_PREFIX}{o.id}",
        # created_at — как раньше (UTC без пояса). Для разбора надёжнее
        # created_at_utc (с «+00:00») или created_at_local (с поясом магазина).
        "created_at": o.created_at.isoformat(),
        "created_at_utc": utc_iso(o.created_at),
        "created_at_local": local_iso(o.created_at),
        "status": o.status,
        "comment": o.comment or "",
        "exported": o.exported_at is not None,
        "onec_number": o.onec_number or "",
        "subtotal": o.subtotal,
        "discount_percent": o.discount_percent,
        "total": o.total_price,
        "buyer": {
            "company": user.company_name if user else "",
            "contact": user.full_name if user else "",
            "inn": (user.inn if user else "") or "",
            "phone": (user.phone if user else "") or "",
            "email": (user.email if user else "") or "",
            "city": (user.city if user else "") or "",
        },
        "items": [
            {
                "sku": skus.get(i.product_id, ""),
                "name": i.product_name,
                "quantity_packs": i.quantity,
                "package_size": i.package_size or 1,
                "stems": i.total_stems,
                "price_per_stem": i.price,
                "sum": i.subtotal,
            }
            for i in o.items
        ],
    }


@router.get("/orders/export")
def export_orders_to_1c(
    since: str = "",
    status: str = "",
    new: int = 0,
    after_id: int = 0,
    limit: int = ORDERS_MAX_PER_CALL,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Заказы сайта для загрузки в 1С (документ «Заказ клиента»).

    Рекомендуемый способ — «забрал и подтвердил»:
        1) GET  /api/1c/orders/export?new=1   → заказы, которые 1С ещё не забирала
        2) создать документы в 1С
        3) POST /api/1c/orders/ack            → подтвердить получение
    Не нужно помнить «время последней выгрузки», и ни один заказ не потеряется:
    пока не подтверждён — он будет приходить снова (1С отсеивает по «САЙТ-<id>»).

    Остальные фильтры: since (ISO 8601; со смещением или без — тогда UTC),
    status, after_id (id больше указанного), limit (до 200).
    """
    try:
        limit = max(1, min(int(limit), ORDERS_MAX_PER_CALL))
    except (TypeError, ValueError):
        limit = ORDERS_MAX_PER_CALL

    query = (
        db.query(Order)
        .options(selectinload(Order.items), selectinload(Order.user))
        .order_by(Order.id.asc())
    )
    if new:
        query = query.filter(Order.exported_at.is_(None))
    if after_id > 0:
        query = query.filter(Order.id > after_id)
    if since:
        query = query.filter(Order.created_at >= _parse_since(since))
    if status:
        if status not in ORDER_STATUSES:
            raise HTTPException(400, "Неизвестный статус заказа")
        query = query.filter(Order.status == status)

    orders = query.limit(limit).all()

    # SKU подтягиваем одним запросом — без N+1
    product_ids = {i.product_id for o in orders for i in o.items if i.product_id}
    skus = (
        dict(db.query(Product.id, Product.sku)
             .filter(Product.id.in_(product_ids)).all())
        if product_ids else {}
    )

    return JSONResponse({
        "count": len(orders),
        "has_more": len(orders) == limit,
        "last_id": orders[-1].id if orders else after_id,
        "orders": [_order_payload(o, skus) for o in orders],
    })


# ═══════════════════════════════════════════════════════════
# 1С → САЙТ: ПОДТВЕРЖДЕНИЕ ПОЛУЧЕНИЯ И СТАТУСЫ ЗАКАЗОВ
# ═══════════════════════════════════════════════════════════

def _orders_list(payload: dict, max_items: int = 1000) -> list:
    orders = payload.get("orders")
    if orders is None and isinstance(payload.get("numbers"), list):
        orders = payload["numbers"]
    if not isinstance(orders, list):
        raise HTTPException(400, "Поле 'orders' должно быть массивом")
    if len(orders) > max_items:
        raise HTTPException(413, f"Слишком много заказов за раз (максимум {max_items})")
    return orders


@router.post("/orders/ack")
def ack_orders(
    payload: dict,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """1С подтверждает: «эти заказы получены и заведены».

    Тело: {"orders": [{"number": "САЙТ-57", "onec_number": "00000000123"}]}
    (можно и просто {"numbers": ["САЙТ-57", "САЙТ-58"]}).
    Повторное подтверждение безопасно.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    acked = 0
    errors: list[str] = []
    for item in _orders_list(payload):
        try:
            order_id = _parse_order_id(item)
        except RowError as e:
            errors.append(str(e))
            continue
        order = db.query(Order).filter(Order.id == order_id).first()
        if order is None:
            errors.append(f"{ORDER_NUMBER_PREFIX}{order_id}: заказ не найден")
            continue
        if order.exported_at is None:
            order.exported_at = now
        if isinstance(item, dict) and item.get("onec_number"):
            order.onec_number = _clean(item["onec_number"], 64)
        acked += 1
    db.commit()
    return {"status": "ok", "acked": acked,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}


@router.post("/orders/status")
def update_orders_status(
    payload: dict,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """1С меняет статусы заказов сайта (и сообщает номер своего документа).

    Тело: {"orders": [{"number": "САЙТ-57", "status": "Подтверждён",
                       "onec_number": "00000000123"}]}
    Допустимые статусы: Новый, Подтверждён, В работе, Отправлен, Выполнен,
    Отменён. Отмена возвращает товар на склад; клиент получает уведомление
    и письмо — как при смене статуса менеджером в админке.
    """
    updated = unchanged = 0
    errors: list[str] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    for item in _orders_list(payload):
        try:
            order_id = _parse_order_id(item)
            if not isinstance(item, dict) or not item.get("status"):
                raise RowError("не указан status")
            new_status = _clean(item["status"], 30)
        except RowError as e:
            errors.append(str(e))
            continue
        label = f"{ORDER_NUMBER_PREFIX}{order_id}"

        order = load_order_for_update(db, order_id)
        if order is None:
            db.rollback()
            errors.append(f"{label}: заказ не найден")
            continue

        result = apply_status_change(db, order, new_status)
        if result.error:
            db.rollback()
            errors.append(f"{label}: {result.error}")
            continue

        if order.exported_at is None:        # раз 1С шлёт статус — заказ у неё есть
            order.exported_at = now
        if item.get("onec_number"):
            order.onec_number = _clean(item["onec_number"], 64)
        db.commit()

        if result.changed:
            updated += 1
            details = f"1С: {result.old} → {result.new}"
            if result.stock_note:
                details += f" ({result.stock_note})"
            log_action(db, request, None, "order_status_1c", "order",
                       order.id, details)
            notify_client_of_status(db, order, new_status, background_tasks)
        else:
            unchanged += 1

    return {"status": "ok", "updated": updated, "unchanged": unchanged,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}


# ═══════════════════════════════════════════════════════════
# КЛИЕНТЫ: СКИДКИ
# ═══════════════════════════════════════════════════════════

@router.get("/clients/export")
def export_clients_to_1c(
    after_id: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Клиенты сайта (без администраторов) — для сопоставления с контрагентами 1С."""
    limit = max(1, min(int(limit), 1000))
    users = (
        db.query(User)
        .filter(User.is_admin.is_(False), User.id > max(0, after_id))
        .order_by(User.id)
        .limit(limit)
        .all()
    )
    return JSONResponse({
        "count": len(users),
        "has_more": len(users) == limit,
        "last_id": users[-1].id if users else after_id,
        "clients": [
            {
                "id": u.id,
                "email": u.email,
                "company": u.company_name,
                "contact": u.full_name,
                "inn": u.inn or "",
                "phone": u.phone or "",
                "city": u.city or "",
                "discount_percent": u.discount_percent or 0,
                "is_approved": bool(u.is_approved),
                "created_at_utc": utc_iso(u.created_at),
            }
            for u in users
        ],
    })


@router.post("/clients/sync")
def sync_clients(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Персональные скидки из 1С. Меняется ТОЛЬКО discount_percent.

    Тело: {"clients": [{"email": "shop@test.ru", "discount_percent": 5},
                       {"inn": "7701234567", "discount_percent": 7.5}]}
    Клиент ищется по email; если email не указан — по ИНН (должен быть
    ровно один клиент с таким ИНН). Новых клиентов и права доступа 1С не
    создаёт и не меняет: регистрацию и одобрение делает менеджер на сайте.
    """
    clients = payload.get("clients")
    if not isinstance(clients, list):
        raise HTTPException(400, "Поле 'clients' должно быть массивом")
    if len(clients) > 5000:
        raise HTTPException(413, "Слишком много клиентов за раз (максимум 5000)")

    updated = unchanged = 0
    errors: list[str] = []
    for idx, c in enumerate(clients, start=1):
        if not isinstance(c, dict):
            continue
        email = _clean(c.get("email"), 120).lower()
        inn = _clean(c.get("inn"), 12)
        try:
            if "discount_percent" not in c:
                raise RowError("нет discount_percent")
            pct = _num(c.get("discount_percent"), "discount_percent")
            if not (0 <= pct <= 100):
                raise RowError(f"discount_percent {pct} вне диапазона 0…100")
            if email:
                matches = db.query(User).filter(User.email == email).all()
            elif inn:
                matches = db.query(User).filter(
                    User.inn == inn, User.is_admin.is_(False)).all()
            else:
                raise RowError("нужен email или inn")
            if not matches:
                raise RowError("клиент не найден")
            if len(matches) > 1:
                raise RowError("по ИНН найдено несколько клиентов — укажите email")
        except RowError as e:
            errors.append(f"#{idx} ({email or inn or '?'}): {e}")
            continue

        user = matches[0]
        if user.is_admin:
            errors.append(f"#{idx} ({user.email}): у администратора скидка не меняется")
            continue
        pct = round(pct, 2)
        if abs((user.discount_percent or 0) - pct) < 0.005:
            unchanged += 1
            continue
        old = user.discount_percent or 0
        user.discount_percent = pct
        db.commit()
        log_action(db, request, None, "user_discount_1c", "user", user.id,
                   f"1С: {user.email}: скидка {old:.1f}% → {pct:.1f}%")
        updated += 1

    return {"status": "ok", "updated": updated, "unchanged": unchanged,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}
