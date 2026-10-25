# app/routers/integration_1c.py
"""
Обмен с 1С. Полное описание — docs/1C_PROGRAMMER_BRIEF.md и docs/1C_INTEGRATION.md.

Доступ — по КЛЮЧУ (создаётся в админке: «Интеграция»). Ключ можно передать так:
    X-API-Key: dnt_…          или        Authorization: Bearer dnt_…
    или как пароль в HTTP Basic (логин — любой): Authorization: Basic base64("1c:dnt_…")
Старый вариант «логин/пароль из .env» (INTEGRATION_USER / INTEGRATION_PASSWORD)
тоже работает, но считается устаревшим.

  1С → сайт
    POST /api/1c/products/sync      каталог, цены, остатки (JSON или CSV; опц. «полный снимок»)
    POST /api/1c/orders/status      статусы заказов (+ номер документа в 1С)
    POST /api/1c/orders/ack         «заказы получены» (чтобы не выгружать повторно)
    POST /api/1c/clients/sync       персональные скидки клиентов
  сайт → 1С
    GET  /api/1c/products/export    каталог сайта            (?format=csv — таблица)
    GET  /api/1c/orders/export      заказы (?new=1 — только ещё не забранные; ?format=csv)
    GET  /api/1c/clients/export     клиенты
  служебное
    GET  /api/1c/ping               проверка связи и ключа

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
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import (APIRouter, BackgroundTasks, Body, Depends, HTTPException,
                     Request)
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session, joinedload, selectinload
from starlette.concurrency import run_in_threadpool

from ..config import config
from ..database import get_db
from ..models import (ORDER_STATUSES, Order, Product, SupplyItem, User)
from ..security import _client_ip, check_rate_limit, safe_equal
from ..services import api_keys
from ..services.audit import log_action
from ..services.catalog_files import decode_text, parse_catalog_text
from ..services.catalog_sync import (MAX_ERRORS_IN_REPLY, MAX_PRODUCTS_PER_SYNC,
                                     RowError, SyncCommitError, apply_products,
                                     clean, to_number)
from ..services.exchange_export import (
    ORDER_CSV_HEADER, ORDER_NUMBER_PREFIX, PRODUCT_CSV_HEADER, csv_response,
    orders_csv_rows, orders_to_payloads, product_payload, products_csv_rows)
from ..services.exchange_log import note
from ..services.orders import (apply_status_change, load_order_for_update,
                               notify_client_of_status)
from ..timeutils import utc_iso

logger = logging.getLogger(__name__)

# короткие имена, которыми пользуется остальной код этого файла
_clean = clean
_num = to_number

MAX_BODY_BYTES = 40 * 1024 * 1024           # самая большая выгрузка каталога
CSV_CONTENT_TYPES = ("text/csv", "text/plain", "application/csv",
                     "application/vnd.ms-excel")


async def _rate_1c(request: Request) -> None:
    """Защита от перебора ключа: не более 120 запросов/мин с одного IP."""
    await check_rate_limit(request, "onec", max_hits=120, window=60)


router = APIRouter(
    prefix="/api/1c", tags=["1C Integration"],
    dependencies=[Depends(_rate_1c)],
)

_basic = HTTPBasic(auto_error=False)
_WWW_AUTH = {"WWW-Authenticate": 'Basic realm="Dianthus 1C"'}


# ═══════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ
# ═══════════════════════════════════════════════════════════

def _deny(request: Request, code: int, detail: str, *, headers=None,
          auth_kind: str = "none", key_id: int | None = None):
    note(request, detail, auth_kind=auth_kind, key_id=key_id)
    raise HTTPException(status_code=code, detail=detail, headers=headers)


def _verify_basic(
    request: Request,
    creds: HTTPBasicCredentials | None = Depends(_basic),
    db: Session = Depends(get_db),
) -> bool:
    """Пускает по ключу (X-API-Key / Bearer / пароль в Basic) или по старому паролю."""
    ip = _client_ip(request)

    raw = (request.headers.get("x-api-key") or "").strip()
    if not raw:
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            raw = auth[7:].strip()
    if not raw and creds is not None:
        for candidate in (creds.password or "", creds.username or ""):
            if api_keys.looks_like_key(candidate):
                raw = candidate.strip()
                break

    if raw:
        key = api_keys.find_key(db, raw)
        if key is None:
            logger.warning("1С: неверный или отключённый ключ, IP=%s", ip)
            _deny(request, 401, "Неверный или отключённый ключ.", headers=_WWW_AUTH)
        if not api_keys.ip_allowed(key, ip):
            logger.warning("1С: ключ «%s» с чужого адреса %s", key.name, ip)
            _deny(request, 403,
                  f"Этот ключ нельзя использовать с адреса {ip}.",
                  auth_kind="key", key_id=key.id)
        api_keys.touch(db, key, ip)
        note(request, auth_kind="key", key_id=key.id)
        request.state.onec_key_name = key.name
        return True

    legacy = bool(config.INTEGRATION_USER and config.INTEGRATION_PASSWORD)
    if not legacy and api_keys.active_keys_count(db) == 0:
        logger.error("1С: нет ни ключей, ни INTEGRATION_PASSWORD")
        _deny(request, 503,
              "Обмен с 1С не настроен: в админке («Интеграция») не создан ключ.")
    if creds is None:
        _deny(request, 401, "Требуется ключ доступа (X-API-Key или Basic Auth).",
              headers=_WWW_AUTH)
    if not legacy:
        _deny(request, 401, "Неверный ключ.", headers=_WWW_AUTH)

    user_ok = safe_equal(creds.username or "", config.INTEGRATION_USER)
    pass_ok = safe_equal(creds.password or "", config.INTEGRATION_PASSWORD)
    if not (user_ok and pass_ok):
        logger.warning("1С: неверные учётные данные, IP=%s", ip)
        _deny(request, 401, "Неверный логин или пароль.", headers=_WWW_AUTH)
    note(request, auth_kind="password")
    request.state.onec_key_name = "логин и пароль из .env (устарело)"
    return True


# ═══════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНОЕ
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
# 1С → САЙТ: КАТАЛОГ
# ═══════════════════════════════════════════════════════════

def _parse_json_body(body: bytes) -> dict:
    try:
        payload = json.loads(body)          # bytes: BOM и кодировку Python определит сам
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Тело запроса не разобрать как JSON "
                                 "(для таблицы укажите Content-Type: text/csv).")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Ожидается JSON-объект вида {\"products\": [...]}")
    return payload


def _apply_sync(db: Session, items: list[dict], full: bool) -> dict:
    try:
        result = apply_products(db, items, full=full,
                                reserve_unexported=config.ONEC_RESERVE_UNEXPORTED)
    except SyncCommitError:
        # подробности — только в лог сервера, не в ответ внешней системе
        raise HTTPException(500, "Ошибка БД при сохранении выгрузки. Повторите позже.")
    return {"reply": result.as_dict(), "summary": result.summary(), "result": result}


@router.post("/products/sync")
async def sync_products(
    request: Request,
    full: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Приём каталога, цен и остатков из 1С.

    Тело — JSON  {"products": [...], "full": false}  ИЛИ таблица CSV
    (Content-Type: text/csv; колонки: Артикул;Наименование;Цена;Остаток;В пач. …).
    full=1 (или "full": true) — «полный снимок»: все позиции каталога, которых НЕТ
    в этой выгрузке, снимаются с остатка (stock=0) и с полки. Включайте только
    когда присылаете ВЕСЬ каталог одним запросом.
    """
    client_ip = request.client.host if request.client else "?"
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Слишком большой запрос — отправляйте каталог частями.")

    ctype = (request.headers.get("content-type") or "").lower()
    csv_mode = (request.query_params.get("format", "").lower() == "csv"
                or ctype.startswith(CSV_CONTENT_TYPES))
    # Программист мог забыть сменить Content-Type: если тело начинается с «{» или
    # «[», это JSON, как бы он ни был подписан.
    if csv_mode and body.lstrip(b"\xef\xbb\xbf \r\n\t")[:1] in (b"{", b"["):
        csv_mode = False
    csv_info = None
    if csv_mode:
        try:
            products_data, csv_info = parse_catalog_text(
                decode_text(body),
                stock_unit=request.query_params.get("stock_unit", "stems"),
                price_unit=request.query_params.get("price_unit", "stem"),
                price_column=request.query_params.get("price_column", ""),
            )
        except RowError as e:
            raise HTTPException(400, str(e))
        full_snapshot = bool(full)
    else:
        payload = _parse_json_body(body)
        products_data = payload.get("products", [])
        if not isinstance(products_data, list):
            raise HTTPException(400, "Поле 'products' должно быть массивом")
        full_snapshot = bool(full) or payload.get("full") is True
        # Элементы не-объекты (null, числа, строки) пропускаем, а не валим весь обмен
        products_data = [it for it in products_data if isinstance(it, dict)]

    if len(products_data) > MAX_PRODUCTS_PER_SYNC:
        raise HTTPException(
            413, f"Слишком много товаров за раз (максимум "
                 f"{MAX_PRODUCTS_PER_SYNC}). Разбейте выгрузку на части."
        )
    logger.info("1С sync: ip=%s, формат=%s, товаров в запросе: %d, полный снимок: %s",
                client_ip, "csv" if csv_mode else "json", len(products_data), full_snapshot)

    if not products_data and not full_snapshot:
        note(request, "пустая выгрузка (0 товаров)")
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

    out = await run_in_threadpool(_apply_sync, db, products_data, full_snapshot)
    result = out["result"]
    note(request, out["summary"] + (", полный снимок" if full_snapshot else ""))
    logger.info(
        "1С sync: ip=%s, processed=%d, created=%d, updated=%d, "
        "deactivated=%d, errors=%d",
        client_ip, result.processed, result.created, result.updated,
        result.deactivated, len(result.errors),
    )
    reply = out["reply"]
    if csv_info is not None:
        reply["csv"] = {"columns": csv_info["columns"],
                        "skipped": csv_info["skipped"][:MAX_ERRORS_IN_REPLY]}
    return JSONResponse(reply)


@router.get("/ping")
def ping(request: Request, _auth: bool = Depends(_verify_basic)):
    return {
        "status": "ok",
        "service": "dianthus-1c",
        "env": config.ENV,
        "auth": True,
        "auth_name": getattr(request.state, "onec_key_name", ""),
        "server_time_utc": utc_iso(datetime.now(timezone.utc)),
        "reserve_unexported": config.ONEC_RESERVE_UNEXPORTED,
        "formats": ["json", "csv"],
    }


# ═══════════════════════════════════════════════════════════
# САЙТ → 1С: КАТАЛОГ
# ═══════════════════════════════════════════════════════════

@router.get("/products/export")
def export_products_to_1c(
    request: Request,
    all: int = 0,
    format: str = "json",
    encoding: str = "utf-8",
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Каталог сайта для 1С — одна запись на товар.

        GET /api/1c/products/export          — товары, которые сейчас на полке
        GET /api/1c/products/export?all=1    — весь справочник (включая отсутствующие)
        GET /api/1c/products/export?format=csv[&encoding=cp1251] — то же таблицей

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
            rows[product.id] = product_payload(product, item)

    if all:
        for product in db.query(Product).order_by(Product.id).all():
            rows.setdefault(product.id, product_payload(product, None))

    products = [rows[pid] for pid in sorted(rows)]
    note(request, f"выгружено товаров: {len(products)}")
    if format.lower() == "csv":
        return csv_response(PRODUCT_CSV_HEADER, products_csv_rows(products),
                            "products.csv", encoding)
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


@router.get("/orders/export")
def export_orders_to_1c(
    request: Request,
    since: str = "",
    status: str = "",
    new: int = 0,
    after_id: int = 0,
    limit: int = ORDERS_MAX_PER_CALL,
    format: str = "json",
    encoding: str = "utf-8",
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
    format=csv — таблица «одна строка = одна позиция заказа» (для старых версий 1С).
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

    payloads = orders_to_payloads(db, orders)
    note(request, f"выгружено заказов: {len(orders)}" + (" (только новые)" if new else ""))

    if format.lower() == "csv":
        return csv_response(ORDER_CSV_HEADER, orders_csv_rows(payloads),
                            "orders.csv", encoding)

    return JSONResponse({
        "count": len(orders),
        "has_more": len(orders) == limit,
        "last_id": orders[-1].id if orders else after_id,
        "orders": payloads,
    })


# ═══════════════════════════════════════════════════════════
# 1С → САЙТ: ПОДТВЕРЖДЕНИЕ ПОЛУЧЕНИЯ И СТАТУСЫ ЗАКАЗОВ
# ═══════════════════════════════════════════════════════════

def _orders_list(payload: dict | None, max_items: int = 1000) -> list:
    payload = payload or {}
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
    request: Request,
    payload: dict | None = Body(default=None),
    numbers: str = "",
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """1С подтверждает: «эти заказы получены и заведены».

    Тело: {"orders": [{"number": "САЙТ-57", "onec_number": "00000000123"}]}
    (можно и просто {"numbers": ["САЙТ-57", "САЙТ-58"]}).
    Для старых 1С без JSON — адрес вида  POST /api/1c/orders/ack?numbers=САЙТ-57,САЙТ-58
    Повторное подтверждение безопасно.
    """
    if numbers.strip():
        payload = {"numbers": [n.strip() for n in numbers.split(",") if n.strip()]}
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
    note(request, f"подтверждено заказов: {acked}" + (f", ошибок: {len(errors)}" if errors else ""))
    return {"status": "ok", "acked": acked,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}


@router.post("/orders/status")
def update_orders_status(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict | None = Body(default=None),
    number: str = "",
    status: str = "",
    onec_number: str = "",
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """1С меняет статусы заказов сайта (и сообщает номер своего документа).

    Тело: {"orders": [{"number": "САЙТ-57", "status": "Подтверждён",
                       "onec_number": "00000000123"}]}
    Для старых 1С без JSON — один заказ в адресе:
        POST /api/1c/orders/status?number=САЙТ-57&status=Подтверждён&onec_number=123
    Допустимые статусы: Новый, Подтверждён, В работе, Отправлен, Выполнен,
    Отменён. Отмена возвращает товар на склад; клиент получает уведомление
    и письмо — как при смене статуса менеджером в админке.
    """
    if number.strip():
        payload = {"orders": [{"number": number.strip(), "status": status,
                               "onec_number": onec_number}]}
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

    note(request, f"статусов изменено: {updated}, без изменений: {unchanged}"
                  + (f", ошибок: {len(errors)}" if errors else ""))
    return {"status": "ok", "updated": updated, "unchanged": unchanged,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}


# ═══════════════════════════════════════════════════════════
# КЛИЕНТЫ: СКИДКИ
# ═══════════════════════════════════════════════════════════

@router.get("/clients/export")
def export_clients_to_1c(
    request: Request,
    after_id: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Клиенты сайта (без сотрудников) — для сопоставления с контрагентами 1С."""
    limit = max(1, min(int(limit), 1000))
    users = (
        db.query(User)
        .filter(User.is_admin.is_(False), User.is_manager.is_(False),
                User.anonymized_at.is_(None), User.id > max(0, after_id))
        .order_by(User.id)
        .limit(limit)
        .all()
    )
    note(request, f"выгружено клиентов: {len(users)}")
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
    request: Request,
    payload: dict,
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

    note(request, f"скидок изменено: {updated}, без изменений: {unchanged}"
                  + (f", ошибок: {len(errors)}" if errors else ""))
    return {"status": "ok", "updated": updated, "unchanged": unchanged,
            "errors": errors[:MAX_ERRORS_IN_REPLY]}
