# app/routers/integration_1c.py
"""
Обмен с 1С. Полное описание — docs/1C_PROGRAMMER_BRIEF.md и docs/1C_INTEGRATION.md.

Доступ — по КЛЮЧУ (создаётся в админке: «Интеграция»). Ключ можно передать так:
    X-API-Key: dnt_…          или        Authorization: Bearer dnt_…
    или как пароль в HTTP Basic (логин — любой): Authorization: Basic base64("1c:dnt_…")
Старый вариант «логин/пароль из .env» (INTEGRATION_USER / INTEGRATION_PASSWORD)
тоже работает, но считается устаревшим.

  1С → сайт
    POST /api/1c/supplies/sync      ПОСТАВКИ (инвойсы): остатки и цены по партиям (основной путь)
    POST /api/1c/products/sync      товары-справочник (и, если нужно, остаток «без поставки»)
    POST /api/1c/products/photo     фото товара (тело — сама картинка; ?id=…&n=1)
    POST /api/1c/orders/status      статусы заказов (+ номер документа в 1С, комментарий)
    POST /api/1c/orders/ack         «заказы получены» (чтобы не выгружать повторно)
    POST /api/1c/clients/sync       id контрагента в 1С и скидка клиента
  сайт → 1С
    GET  /api/1c/orders/export      заказы (?new=1 — только ещё не забранные; ?format=csv)
    GET  /api/1c/products/export    товары-справочник        (?format=csv — таблица)
    GET  /api/1c/clients/export     клиенты (контрагенты)
    GET  /api/1c/dictionaries       справочники: виды, цвета, рост, страны (только чтение)
  служебное
    GET  /api/1c/ping               проверка связи и ключа

Сайт работает ПО ПОСТАВКАМ: каждая строка поставки — позиция каталога со своей ценой и своим
остатком, клиент заказывает конкретную позицию конкретной поставки, а в заказе для 1С у каждой
строки указана поставка (`supply`) — по ней 1С списывает товар из нужной партии.

Одни и те же короткие названия во всём обмене: price — цена за одну ШТУКУ (стебель), ₽;
quantity — количество в ШТУКАХ; sum — сумма; total — итог. Ни упаковок, ни «price_per_stem» в
обмене больше нет: штук в упаковке — package_size, сайт продаёт целыми упаковками сам.

У каждой сущности есть id. Товар: id, sku (артикул), barcode (штрихкод), name. Контрагент: id,
name, inn, email, address, phone. Заказ: id (на сайте), number («САЙТ-57»), date.
В выгрузке С САЙТА `id` — номер на сайте, а идентификатор 1С лежит в `external_id`
(пусто, пока 1С его не сообщила). Все форматы и примеры: app/content/1c_formats.json
(открыт на сайте по адресу /1c-docs.json).
"""
import json
import logging
import re
from datetime import datetime, timezone

from fastapi import (APIRouter, BackgroundTasks, Body, Depends, HTTPException,
                     Query, Request)
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from ..config import config
from ..database import get_db
from ..models import ORDER_STATUSES, Order, Product, User
from ..security import _client_ip, check_rate_limit, safe_equal
from ..services import api_keys, onec_photos, taxonomy
from ..services.audit import log_action
from ..services.catalog_files import decode_text, parse_catalog_text
from ..services.catalog_sync import (MAX_ERRORS_IN_REPLY, MAX_PRODUCTS_PER_SYNC,
                                     RowError, SyncCommitError, apply_products,
                                     clean, to_number)
from ..services.exchange_export import (
    ORDER_CSV_HEADER, ORDER_NUMBER_PREFIX, PRODUCT_CSV_HEADER, counterparty_payload,
    csv_response, orders_csv_rows, orders_to_payloads, product_payload, products_csv_rows)
from ..services.exchange_log import DRY_RUN_MARK, note
from ..services.orders import (apply_status_change, load_order_for_update,
                               normalize_status, notify_client_of_status)
from ..services.supply_sync import MAX_SUPPLIES_PER_SYNC, apply_supplies
from ..timeutils import fmt_local, utc_iso

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


def _apply_sync(db: Session, items: list[dict], full: bool, dry_run: bool = False) -> dict:
    try:
        result = apply_products(db, items, full=full,
                                reserve_unexported=config.ONEC_RESERVE_UNEXPORTED,
                                dry_run=dry_run)
    except SyncCommitError:
        # подробности — только в лог сервера, не в ответ внешней системе
        raise HTTPException(500, "Ошибка БД при сохранении выгрузки. Повторите позже.")
    return {"reply": result.as_dict(), "summary": result.summary(), "result": result}


@router.post("/products/sync")
async def sync_products(
    request: Request,
    full: int = 0,
    dry_run: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Приём товаров (справочник) из 1С; при желании — ещё и остаток «без поставки».

    Тело — JSON  {"products": [...], "full": false}  ИЛИ таблица CSV
    (Content-Type: text/csv; колонки: Артикул;Наименование;Цена;Остаток;В пач. …).
    Строка без price и quantity — просто карточка товара: цену и остаток она не трогает
    (остатки и цены по поставкам приходят в supplies/sync). Строка с price и quantity
    выставляет остаток «без поставки» (отдельная служебная партия).
    full=1 (или "full": true) — «полный снимок»: все позиции «без поставки», которых НЕТ
    в этой выгрузке, снимаются с остатка и с полки. Включайте только
    когда присылаете ВЕСЬ каталог одним запросом.
    dry_run=1 — «проверка без сохранения»: ответ тот же (сколько будет создано,
    обновлено, снято с полки, какие строки с ошибками), но в базе ничего не меняется.
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
            "status": "ok", "dry_run": bool(dry_run), "processed": 0, "created": 0,
            "updated": 0, "deactivated": 0, "errors": [], "supply_id": None,
        })
    if not products_data and full_snapshot:
        # Пустой «полный снимок» обнулил бы весь каталог — почти наверняка
        # это ошибка выгрузки на стороне 1С, а не желание закрыть магазин.
        raise HTTPException(
            400, "Полный снимок без товаров не принимается "
                 "(он снял бы с продажи весь каталог).")

    out = await run_in_threadpool(_apply_sync, db, products_data, full_snapshot, bool(dry_run))
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


@router.post("/supplies/sync")
async def sync_supplies(
    request: Request,
    full: int = 0,
    dry_run: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Поставки (инвойсы) из 1С: остатки и цены ПО ПОСТАВКАМ.

    Тело: {"supplies": [{"id": "00000622", "number": "00000622", "date": "2026-10-29",
                         "country": "Эквадор", "supplier": "ECUANROS S.A",
                         "status": "in_transit",
                         "items": [{"id": "…", "sku": "…", "name": "Роза Фридом 60",
                                    "package_size": 25, "price": 42.5,
                                    "quantity": 3000}]}],
           "full": false}
    status: in_transit (ещё в пути: предзаказ) | received (оприходован: товар на полке,
    quantity — ОСТАТОК партии в штуках) | cancelled.
    Повторная отправка ничего не дублирует: поставка ищется по id инвойса.
    full=1 (или "full": true) — в списке ВСЕ поставки, которые ещё в пути или имеют остаток;
    остальные поставки из 1С закрываются. dry_run=1 — проверка без сохранения.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Слишком большой запрос — отправляйте инвойсы частями.")
    payload = _parse_json_body(body)
    supplies = payload.get("supplies")
    if not isinstance(supplies, list):
        raise HTTPException(400, "Поле 'supplies' должно быть массивом")
    if len(supplies) > MAX_SUPPLIES_PER_SYNC:
        raise HTTPException(413, f"Слишком много инвойсов за раз (максимум {MAX_SUPPLIES_PER_SYNC}).")
    full_list = bool(full) or payload.get("full") is True
    try:
        result = await run_in_threadpool(
            apply_supplies, db, supplies, dry_run=bool(dry_run), full=full_list,
            reserve_unexported=config.ONEC_RESERVE_UNEXPORTED)
    except SyncCommitError:
        raise HTTPException(500, "Ошибка БД при сохранении инвойсов. Повторите позже.")
    note(request, result.summary() + (", полный список" if full_list else ""))
    return JSONResponse(result.as_dict())


async def _read_limited(request: Request, limit: int, what: str) -> bytes:
    """Читает тело запроса, но не больше limit байт: огромный файл не должен целиком
    оказаться в памяти. Сначала смотрим заявленный размер, потом считаем по ходу."""
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"{what} больше {limit // (1024 * 1024)} МБ.")
    chunks, total = [], 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, f"{what} больше {limit // (1024 * 1024)} МБ.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/products/photo")
async def upload_product_photo(
    request: Request,
    product_id: str = Query("", alias="id"),
    barcode: str = "",
    sku: str = "",
    name: str = "",
    n: int = 1,
    dry_run: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Фото товара из 1С. Тело запроса — САМА картинка (двоичные данные JPEG/PNG/WEBP),
    не JSON и не base64. Товар — в адресе: ?id=<id товара в 1С, как в products/sync>
    (или barcode, sku, name). n — номер фото по порядку (1 — главное): занятый номер
    заменяется, следующий — добавляется. dry_run=1 — проверить, ничего не сохраняя.
    Подробности — в памятке (раздел «Фото товаров»). Доступа на сервер для этого не нужно.
    """
    try:
        body = await _read_limited(request, onec_photos.MAX_PHOTO_BYTES, "Файл")
        result = await run_in_threadpool(
            onec_photos.apply_product_photo, db, ext=product_id, barcode=barcode, sku=sku,
            name=name, position=n, content=body, dry_run=bool(dry_run))
    except HTTPException as e:
        note(request, f"фото: {e.detail}")      # причина видна в журнале обмена в админке
        raise
    verdict = {"saved": "сохранено", "unchanged": "без изменений",
               "would_save": "проверка: было бы сохранено"}[result["result"]]
    note(request, f"фото «{result['product']['name'][:60]}» №{result['position']}: {verdict}")
    return JSONResponse(result)


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


@router.get("/dictionaries")
def export_dictionaries(
    request: Request,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Справочники сайта, по которым фильтруется каталог: виды, цвета, рост, страны.

    У каждой записи: id (на сайте), external_id (в 1С, пусто, если не сверяли), name;
    у роста ещё cm, у цвета ещё hex. Только чтение: новые значения создаются сами, когда
    приходят в товарах (products/sync), а править список может владелец на сайте.
    """
    data = taxonomy.dictionaries_payload(db)
    note(request, "справочники: " + ", ".join(f"{k} {len(v)}" for k, v in data.items()))
    return JSONResponse(data)


# ═══════════════════════════════════════════════════════════
# САЙТ → 1С: КАТАЛОГ
# ═══════════════════════════════════════════════════════════

@router.get("/products/export")
def export_products_to_1c(
    request: Request,
    format: str = "json",
    encoding: str = "utf-8",
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Справочник товаров сайта для 1С — одна запись на товар (без цен и остатков:
    они живут в поставках).

        GET /api/1c/products/export
        GET /api/1c/products/export?format=csv[&encoding=cp1251] — то же таблицей

    Поля те же, что принимает products/sync; `id` — номер на сайте, id из 1С — в `external_id`.
    """
    products = [product_payload(p) for p in db.query(Product).order_by(Product.id).all()]
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

    Тело: {"orders": [{"number": "САЙТ-57", "status": "Отгружен",
                       "comment": "Передан в доставку", "onec_number": "00000000123"}]}
    Для старых 1С без JSON — один заказ в адресе:
        POST /api/1c/orders/status?number=САЙТ-57&status=Подтверждён&onec_number=123
    Статусы: Новый, Подтверждён, В работе, Отправлен, Выполнен, Отменён; понимаются и
    привычные названия («Отгружен» = Отправлен, «Доставлен» = Выполнен, «В обработке» = В работе).
    Отмена возвращает товар на склад; клиент получает уведомление и письмо — как при смене
    статуса менеджером в админке. comment (необязательно) сохраняется в заметке сотрудников
    к заказу — клиент его не видит.
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
        except RowError as e:
            errors.append(str(e))
            continue
        label = f"{ORDER_NUMBER_PREFIX}{order_id}"
        if not isinstance(item, dict) or not item.get("status"):
            errors.append(f"{label}: не указан status")
            continue
        new_status = normalize_status(item["status"])
        if new_status is None:
            errors.append(f"{label}: неизвестный статус «{_clean(item['status'], 30)}» "
                          f"(нужен один из: {', '.join(ORDER_STATUSES)})")
            continue

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
        remark = _clean(item.get("comment"), 500)
        if remark:
            order.staff_note = ((order.staff_note or "") + ("\n" if order.staff_note else "")
                                + f"1С, {fmt_local(now)}: {remark}")[-2000:]
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
    """Клиенты сайта (без сотрудников) — для сопоставления с контрагентами 1С.

    Контрагент: id (на сайте), external_id (в 1С, пусто, пока 1С не сообщила), name, inn,
    email, address, phone — те же поля, что в заказе (`counterparty`), и discount_percent.
    """
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
        "clients": [{**counterparty_payload(u), "discount_percent": u.discount_percent or 0}
                    for u in users],
    })


def _find_clients(db: Session, site_id: int | None, email: str, ext: str, inn: str) -> list[User]:
    """Клиент ищется по id на сайте (как в выгрузке clients/export); нет такого — по email;
    нет и такого — по id в 1С; нет и такого — по ИНН. Администраторов по id в 1С и ИНН не ищем
    (по id сайта и email находим, чтобы честно отказать)."""
    if site_id:
        found = db.query(User).filter(User.id == site_id).all()
        if found:
            return found
    if email:
        found = db.query(User).filter(User.email == email).all()
        if found:
            return found
    for column, value in ((User.external_id, ext), (User.inn, inn)):
        if value:
            found = db.query(User).filter(column == value, User.is_admin.is_(False)).all()
            if found:
                return found
    return []


@router.post("/clients/sync")
def sync_clients(
    request: Request,
    payload: dict,
    dry_run: int = 0,
    db: Session = Depends(get_db),
    _auth: bool = Depends(_verify_basic),
):
    """Скидки и идентификатор контрагента из 1С. Меняются ТОЛЬКО discount_percent
    и external_id (id контрагента в 1С); адрес — только если на сайте он пуст.

    Тело: {"clients": [{"id": 8, "external_id": "000001234", "name": "ООО Ромашка",
                        "inn": "7701234567", "email": "shop@test.ru",
                        "address": "Москва, ул. Цветочная, 1", "phone": "+79001234567",
                        "discount_percent": 5},
                       {"inn": "7701234567", "discount_percent": 7.5}]}
    То есть можно отправить обратно запись из clients/export (count, has_more и last_id
    игнорируются). `id` — ЧИСЛО: номер клиента на сайте (как в выгрузке); `id` строкой —
    код контрагента в 1С (то же, что external_id). Клиент ищется по id на сайте; не нашёлся —
    по email; не нашёлся — по external_id; не нашёлся и так — по ИНН (должен быть ровно один
    клиент). Если id, email и inn в строке указывают на разных клиентов — строка с ошибкой.
    discount_percent можно не присылать, если присылается external_id (тогда запоминается
    только он). Название, ИНН, email и телефон 1С не меняет.
    Новых клиентов и права доступа 1С не создаёт и не меняет: регистрацию и
    одобрение делает менеджер на сайте.
    dry_run=1 — «проверка без сохранения»: ответ тот же, но данные не меняются.
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
        ext = _clean(c.get("external_id"), 64)
        raw_id = c.get("id")
        site_id = None
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            site_id = raw_id                 # число — номер клиента на сайте (как в clients/export)
        elif not ext:
            ext = _clean(raw_id, 64)         # строка — код контрагента в 1С (как `id` в products/sync)
        pct = None
        try:
            if "discount_percent" in c:
                pct = _num(c.get("discount_percent"), "discount_percent")
                if not (0 <= pct <= 100):
                    raise RowError(f"discount_percent {pct} вне диапазона 0…100")
            elif not ext:
                raise RowError("нет discount_percent")
            if not (site_id or email or ext or inn):
                raise RowError("нужен id, email, external_id или inn")
            matches = _find_clients(db, site_id, email, ext, inn)
            if not matches:
                raise RowError("клиент не найден")
            if len(matches) > 1:
                raise RowError("найдено несколько клиентов — укажите id или email")
        except RowError as e:
            errors.append(f"#{idx} ({email or ext or inn or site_id or '?'}): {e}")
            continue

        user = matches[0]
        if user.is_admin:
            errors.append(f"#{idx} ({user.email}): у администратора данные не меняются")
            continue
        if site_id and user.id == site_id and (
                (email and (user.email or "").lower() != email)
                or (inn and user.inn and user.inn != inn)):
            errors.append(f"#{idx} ({email or inn}): id {site_id} — это другой клиент "
                          f"({user.email}); проверьте id, email и inn в строке")
            continue

        changes = []
        old = user.discount_percent or 0
        if pct is not None:
            pct = round(pct, 2)
            if abs(old - pct) >= 0.005:
                user.discount_percent = pct
                changes.append(f"скидка {old:.1f}% → {pct:.1f}%")
        if ext and (user.external_id or "") != ext:
            changes.append(f"id в 1С «{user.external_id or '—'}» → «{ext}»")
            user.external_id = ext
        address = _clean(c.get("address"), 300)
        if address and not (user.address or "").strip():
            user.address = address
            changes.append("адрес из 1С")
        if not changes:
            unchanged += 1
            continue
        if not dry_run:
            db.commit()
            log_action(db, request, None, "user_discount_1c", "user", user.id,
                       f"1С: {user.email}: " + "; ".join(changes))
        updated += 1

    if dry_run:
        db.rollback()                      # проверка: ничего не сохраняем
    note(request, (f"{DRY_RUN_MARK} — " if dry_run else "")
                  + f"клиентов изменено: {updated}, без изменений: {unchanged}"
                  + (f", ошибок: {len(errors)}" if errors else ""))
    return {"status": "ok", "dry_run": bool(dry_run), "updated": updated,
            "unchanged": unchanged, "errors": errors[:MAX_ERRORS_IN_REPLY]}