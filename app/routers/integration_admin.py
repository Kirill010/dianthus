# app/routers/integration_admin.py
"""Админка «Интеграция с 1С»: ключи доступа, состояние обмена, журнал,
ручная загрузка файлов из 1С и памятка для программиста.

    /admin/integration                     страница
    /admin/integration/keys …              создать / отключить / удалить ключ
    /admin/integration/import              загрузить прайс-лист (CSV / Excel)
    /admin/integration/orders.csv|mark     заказы для 1С вручную
    /1c-docs, /1c-docs.md                  памятка программисту (без входа)
"""
import logging
from datetime import datetime, timezone

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Request,
                     UploadFile)
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, selectinload
from starlette.concurrency import run_in_threadpool

from ..config import config
from ..database import get_db
from ..deps import require_admin
from ..models import ApiKey, ExchangeLog, Order, Product, SupplyItem
from ..security import check_csrf
from ..services import api_keys, exchange_log, onec_brief
from ..services.audit import log_action
from ..services.catalog_files import parse_catalog_file
from ..services.catalog_sync import RowError, SyncCommitError, apply_products
from ..services.exchange_export import (ORDER_CSV_HEADER, PRODUCT_CSV_HEADER,
                                        csv_response, orders_csv_rows,
                                        orders_to_payloads, product_payload,
                                        products_csv_rows)
from ..templating import render

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_ACTIVE_KEYS = 20
ALLOWED_EXT = (".csv", ".txt", ".xlsx", ".xls")
PAGE = "/admin/integration"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _waiting_orders_query(db: Session, up_to: int = 0):
    q = db.query(Order).filter(Order.exported_at.is_(None), Order.status != "Отменён")
    if up_to > 0:
        q = q.filter(Order.id <= up_to)
    return q.order_by(Order.id)


def _page(request: Request, db: Session, admin, **extra):
    errors_only = request.query_params.get("errors") == "1"
    log_query = db.query(ExchangeLog).order_by(ExchangeLog.id.desc())
    if errors_only:
        log_query = log_query.filter(ExchangeLog.ok.is_(False))
    keys = db.query(ApiKey).order_by(ApiKey.is_active.desc(), ApiKey.id.desc()).all()
    waiting_ids = [oid for (oid,) in _waiting_orders_query(db).with_entities(Order.id).all()]
    legacy = bool(config.INTEGRATION_USER and config.INTEGRATION_PASSWORD)
    context = dict(
        user=admin,
        ov=exchange_log.overview(db, legacy_password_set=legacy),
        keys=keys,
        key_names={k.id: k.name for k in keys},
        log_rows=log_query.limit(100).all(),
        errors_only=errors_only,
        status_help=exchange_log.STATUS_HELP,
        base_url=onec_brief.base_url(),
        waiting_count=len(waiting_ids),
        waiting_up_to=max(waiting_ids) if waiting_ids else 0,
        legacy_enabled=legacy,
        import_report=request.session.pop("import_report", None),
        message=onec_brief.message_for_programmer(extra.get("new_key")),
        new_key=None, new_key_name="",
    )
    context.update(extra)
    response = render(request, "admin_integration.html", db, **context)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(PAGE, response_class=HTMLResponse)
def integration_page(request: Request, db: Session = Depends(get_db),
                     admin=Depends(require_admin)):
    return _page(request, db, admin)


# ───────────────────────── ключи ─────────────────────────

@router.post(PAGE + "/keys")
def create_key(request: Request, name: str = Form(""), allowed_ips: str = Form(""),
               db: Session = Depends(get_db), admin=Depends(require_admin),
               _csrf: None = Depends(check_csrf)):
    if api_keys.active_keys_count(db) >= MAX_ACTIVE_KEYS:
        request.session["flash"] = f"Слишком много действующих ключей (максимум {MAX_ACTIVE_KEYS}). Отключите ненужные."
        return RedirectResponse(url=PAGE, status_code=303)
    try:
        key, raw = api_keys.create_key(db, name, allowed_ips, admin)
    except ValueError as e:
        request.session["flash"] = f"❌ Адреса указаны неверно: {e}. Пример: 203.0.113.5 или 203.0.113.0/24"
        return RedirectResponse(url=PAGE, status_code=303)
    log_action(db, request, admin, "api_key_create", "api_key", key.id,
               f"Создан ключ «{key.name}» ({key.key_prefix}…)")
    # Ключ показывается ТОЛЬКО в этом ответе (не в адресе и не во flash)
    return _page(request, db, admin, new_key=raw, new_key_name=key.name)


@router.post(PAGE + "/keys/{key_id}/toggle")
def toggle_key(key_id: int, request: Request, db: Session = Depends(get_db),
               admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Ключ не найден")
    key.is_active = not key.is_active
    db.commit()
    log_action(db, request, admin, "api_key_toggle", "api_key", key.id,
               f"Ключ «{key.name}» {'включён' if key.is_active else 'отключён'}")
    request.session["flash"] = (f"Ключ «{key.name}» снова действует" if key.is_active
                                else f"Ключ «{key.name}» отключён — 1С с ним больше не войдёт")
    return RedirectResponse(url=PAGE, status_code=303)


@router.post(PAGE + "/keys/{key_id}/delete")
def delete_key(key_id: int, request: Request, db: Session = Depends(get_db),
               admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    key = db.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(404, "Ключ не найден")
    if key.is_active:
        request.session["flash"] = "Сначала отключите ключ, потом его можно удалить"
        return RedirectResponse(url=PAGE, status_code=303)
    name = key.name
    db.query(ExchangeLog).filter(ExchangeLog.key_id == key.id).update(
        {ExchangeLog.key_id: None}, synchronize_session=False)
    db.delete(key)
    db.commit()
    log_action(db, request, admin, "api_key_delete", "api_key", key_id, f"Удалён ключ «{name}»")
    request.session["flash"] = f"Ключ «{name}» удалён"
    return RedirectResponse(url=PAGE, status_code=303)


# ───────────────────────── загрузка файла из 1С ─────────────────────────

async def _read_upload(file: UploadFile) -> bytes:
    content = bytearray()
    try:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_IMPORT_BYTES:
                raise ValueError("too big")
    finally:
        await file.close()
    return bytes(content)


@router.post(PAGE + "/import")
async def import_catalog_file(
    request: Request,
    file: UploadFile = File(...),
    stock_unit: str = Form("stems"),
    price_unit: str = Form("stem"),
    price_column: str = Form(""),
    full: str = Form(""),
    db: Session = Depends(get_db), admin=Depends(require_admin),
    _csrf: None = Depends(check_csrf),
):
    """Ручная загрузка прайс-листа (CSV / Excel) — те же правила, что у обмена по сети."""
    def back(message: str):
        request.session["flash"] = message
        return RedirectResponse(url=PAGE + "#import", status_code=303)

    filename = file.filename or ""
    if not filename.lower().endswith(ALLOWED_EXT):
        return back("Нужен файл .xlsx, .xls или .csv")
    try:
        content = await _read_upload(file)
    except ValueError:
        return back("Файл слишком большой (больше 10 МБ)")
    if not content:
        return back("Файл пустой")

    try:
        rows, info = await run_in_threadpool(
            parse_catalog_file, content, filename,
            stock_unit="packs" if stock_unit == "packs" else "stems",
            price_unit="pack" if price_unit == "pack" else "stem",
            price_column=price_column.strip()[:80],
        )
    except RowError as e:
        return back(f"❌ {e}")
    except Exception as e:                                   # noqa: BLE001
        logger.exception("Не удалось разобрать файл %s: %s", filename, e)
        return back("❌ Не удалось прочитать файл. Сохраните его из 1С как Excel (.xlsx) или CSV.")
    if not rows:
        return back("В файле не нашлось ни одного товара с ценой.")

    try:
        result = await run_in_threadpool(
            apply_products, db, rows, full=bool(full),
            reserve_unexported=config.ONEC_RESERVE_UNEXPORTED)
    except SyncCommitError:
        return back("❌ База данных не приняла файл. Попробуйте ещё раз или обратитесь к администратору.")

    summary = result.summary() + (", полный каталог" if full else "")
    exchange_log.record(method="POST", path="/api/1c/file-import", status_code=200,
                        ip=(request.client.host if request.client else ""), duration_ms=0,
                        info={"auth_kind": "file", "summary": f"файл «{filename[:60]}»: {summary}"})
    log_action(db, request, admin, "catalog_file_import", "catalog", None,
               f"Файл «{filename[:80]}»: {summary}")
    request.session["import_report"] = {
        "filename": filename, "summary": summary,
        "errors": (result.errors + info["skipped"])[:30],
        "errors_total": len(result.errors) + len(info["skipped"]),
        "warnings": result.warnings,
        "columns": info["columns"],
        "other_price_columns": info["other_price_columns"],
        "full": bool(full),
    }
    request.session["flash"] = f"Файл загружен: {summary}"
    return RedirectResponse(url=PAGE + "#import", status_code=303)


# ───────────────────────── заказы для 1С вручную ─────────────────────────

@router.get(PAGE + "/orders.csv")
def orders_csv(up_to: int = 0, encoding: str = "utf-8",
               db: Session = Depends(get_db), admin=Depends(require_admin)):
    orders = (_waiting_orders_query(db, up_to)
              .options(selectinload(Order.items), selectinload(Order.user)).all())
    return csv_response(ORDER_CSV_HEADER, orders_csv_rows(orders_to_payloads(db, orders)),
                        "orders-for-1c.csv", encoding)


@router.post(PAGE + "/orders/mark")
def orders_mark(request: Request, up_to: int = Form(0), db: Session = Depends(get_db),
                admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    """«Я загрузил эти заказы в 1С»: больше не показывать их как ожидающие."""
    if up_to <= 0:
        request.session["flash"] = "Нет заказов для отметки"
        return RedirectResponse(url=PAGE + "#orders", status_code=303)
    now = _now()
    marked = (db.query(Order)
              .filter(Order.exported_at.is_(None), Order.id <= up_to)
              .update({Order.exported_at: now}, synchronize_session=False))
    db.commit()
    log_action(db, request, admin, "orders_mark_exported", "order", None,
               f"Отмечено как переданные в 1С: {marked} (до № {up_to})")
    request.session["flash"] = f"Отмечено как переданные в 1С: {marked}"
    return RedirectResponse(url=PAGE + "#orders", status_code=303)


@router.get(PAGE + "/products.csv")
def products_csv(encoding: str = "utf-8", db: Session = Depends(get_db),
                 admin=Depends(require_admin)):
    from sqlalchemy.orm import joinedload
    rows: dict[int, dict] = {}
    for item in (db.query(SupplyItem).options(joinedload(SupplyItem.product))
                 .filter(SupplyItem.is_active.is_(True)).order_by(SupplyItem.id.desc()).all()):
        if item.product and item.product.id not in rows:
            rows[item.product.id] = product_payload(item.product, item)
    for product in db.query(Product).order_by(Product.id).all():
        rows.setdefault(product.id, product_payload(product, None))
    ordered = [rows[k] for k in sorted(rows)]
    return csv_response(PRODUCT_CSV_HEADER, products_csv_rows(ordered),
                        "site-products.csv", encoding)


# ───────────────────────── памятка программисту ─────────────────────────

@router.get("/1c-docs", response_class=HTMLResponse)
def brief_page(request: Request, db: Session = Depends(get_db)):
    response = render(request, "onec_brief.html", db, body_html=onec_brief.brief_html())
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@router.get("/1c-docs.md")
def brief_markdown():
    return Response(
        content=onec_brief.brief_markdown(),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="1c-obmen-pamyatka.md"',
                 "X-Robots-Tag": "noindex, nofollow"},
    )
