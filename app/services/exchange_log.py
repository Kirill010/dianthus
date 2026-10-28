# app/services/exchange_log.py
"""Журнал обмена с 1С: кто, когда и с каким результатом обращался к сайту.

Нужен владельцу, который не читает логи сервера: на странице «Интеграция» видно,
работает ли обмен, когда последний раз приходил каталог и почему 1С получает
ошибки (неверный ключ, чужой IP, слишком большой файл и т. д.).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ApiKey, ExchangeLog, Order

logger = logging.getLogger(__name__)

LOG_KEEP_DAYS = 60
COLLAPSE_WINDOW = timedelta(hours=1)

# Метка в итоговой строке режима «Только проверить»: такой запрос ничего не
# сохраняет, поэтому не считается обновлением каталога.
DRY_RUN_MARK = "ПРОВЕРКА, ничего не сохранено"
# Адреса, которыми приходят цены и остатки: инвойсы и товары от 1С или файл из админки
CATALOG_ENDPOINTS = ("supplies/sync", "products/sync", "file-import")

# Объяснения кодов ответа — простыми словами, для владельца
STATUS_HELP = {
    400: "1С прислала данные в неправильном формате",
    401: "неверный ключ или пароль",
    403: "этот ключ нельзя использовать с этого адреса (список разрешённых IP)",
    404: "такого адреса в обмене нет (проверьте ссылку в коде 1С) или такого товара нет на сайте (для фото сначала отправьте товар: инвойс или карточку)",
    413: "слишком много данных за один раз — отправляйте их частями",
    422: "в запросе не хватает обязательных полей",
    429: "1С обращается слишком часто — подождите минуту",
    500: "ошибка на сайте — подробности в журнале сервера",
    503: "обмен не настроен: нет ни одного действующего ключа",
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def note(request, summary: str = "", **extra) -> None:
    """Обработчик сообщает журналу итог («товаров: 120, создано 3»)."""
    state = request.scope.setdefault("state", {})
    info = state.setdefault("exchange", {})
    if summary:
        info["summary"] = summary[:500]
    info.update(extra)


def record(*, method: str, path: str, status_code: int, ip: str,
           duration_ms: int, info: dict | None = None) -> None:
    """Пишет строку в журнал. Любая ошибка проглатывается: журнал не должен
    ломать обмен."""
    from ..database import SessionLocal
    info = info or {}
    endpoint = path.removeprefix("/api/1c/").strip("/")[:120] or "/"
    ok = status_code < 400
    db = SessionLocal()
    try:
        now = _now()
        summary = (info.get("summary") or "")[:500]
        if not ok:
            # одинаковые неудачи (перебор ключа, зависший скрипт) схлопываем в одну строку
            prev = (
                db.query(ExchangeLog)
                .filter(ExchangeLog.ok.is_(False), ExchangeLog.ip == ip[:45],
                        ExchangeLog.endpoint == endpoint,
                        ExchangeLog.status_code == status_code,
                        ExchangeLog.created_at >= now - COLLAPSE_WINDOW)
                .order_by(ExchangeLog.id.desc()).first()
            )
            if prev is not None:
                prev.repeat_count = (prev.repeat_count or 1) + 1
                prev.created_at = now
                prev.duration_ms = duration_ms
                if summary:
                    prev.summary = summary
                db.commit()
                return
        db.add(ExchangeLog(
            created_at=now, key_id=info.get("key_id"),
            auth_kind=(info.get("auth_kind") or "none")[:16],
            method=method[:8], endpoint=endpoint, status_code=status_code, ok=ok,
            ip=ip[:45], duration_ms=duration_ms, summary=summary, repeat_count=1,
        ))
        db.commit()
    except Exception as e:                                  # noqa: BLE001
        db.rollback()
        logger.warning("exchange_log: не удалось записать: %s", e)
    finally:
        db.close()


def prune(db: Session, days: int = LOG_KEEP_DAYS) -> int:
    n = db.query(ExchangeLog).filter(
        ExchangeLog.created_at < _now() - timedelta(days=days)
    ).delete(synchronize_session=False)
    db.commit()
    return n


def prune_job() -> None:
    """Ежесуточная чистка (вызывается планировщиком)."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        n = prune(db)
        if n:
            logger.info("Журнал обмена: удалено старых записей: %d", n)
    except Exception as e:                                  # noqa: BLE001
        db.rollback()
        logger.warning("exchange_log.prune: %s", e)
    finally:
        db.close()


def last_catalog_update(db: Session) -> ExchangeLog | None:
    """Последняя УДАЧНАЯ загрузка цен и остатков (инвойсы или товары от 1С, либо файл) —
    без «Только проверить» и без неудачных попыток. От неё зависит строка
    «Цены и остатки обновлены …»."""
    return (db.query(ExchangeLog)
            .filter(ExchangeLog.ok.is_(True),
                    ExchangeLog.endpoint.in_(CATALOG_ENDPOINTS),
                    ~func.coalesce(ExchangeLog.summary, "").contains(DRY_RUN_MARK))
            .order_by(ExchangeLog.id.desc()).first())


def waiting_orders(db: Session) -> int:
    """Заказы, которые 1С ещё не забрала (и которые не отменены)."""
    return db.query(Order).filter(Order.exported_at.is_(None),
                                  Order.status != "Отменён").count()


def overview(db: Session, legacy_password_set: bool) -> dict:
    """Сводка для верхней плашки страницы «Интеграция»."""
    now = _now()
    day_ago = now - timedelta(hours=24)
    keys = db.query(ApiKey).filter(ApiKey.is_active.is_(True)).count()
    total_rows = db.query(func.count(ExchangeLog.id)).scalar() or 0
    last = db.query(ExchangeLog).order_by(ExchangeLog.id.desc()).first()
    last_ok = (db.query(ExchangeLog).filter(ExchangeLog.ok.is_(True))
               .order_by(ExchangeLog.id.desc()).first())
    last_sync = last_catalog_update(db)
    ok_24h = db.query(func.count(ExchangeLog.id)).filter(
        ExchangeLog.ok.is_(True), ExchangeLog.created_at >= day_ago).scalar() or 0
    err_24h = db.query(func.coalesce(func.sum(ExchangeLog.repeat_count), 0)).filter(
        ExchangeLog.ok.is_(False), ExchangeLog.created_at >= day_ago).scalar() or 0

    if keys == 0 and not legacy_password_set:
        state, message = "no_keys", ("Обмен выключен: ключ ещё не создан. "
                                     "Создайте ключ ниже и передайте его программисту 1С.")
    elif total_rows == 0 and keys:
        state, message = "never", ("Ключ создан, но 1С ещё ни разу не обращалась к сайту. "
                                   "Это нормально, пока программист не закончил настройку.")
    elif total_rows == 0:
        state, message = "never", ("1С ещё ни разу не обращалась к сайту. Сейчас вход возможен "
                                   "только по старому паролю из .env — создайте ключ: он "
                                   "удобнее и безопаснее.")
    elif last_ok is not None and last_ok.created_at >= day_ago and (last is None or last.ok):
        state, message = "ok", "Обмен работает: 1С обращалась к сайту за последние сутки без ошибок."
    elif last is not None and not last.ok and last.created_at >= day_ago:
        reason = STATUS_HELP.get(last.status_code, f"код {last.status_code}")
        state, message = "failing", f"Последнее обращение 1С завершилось ошибкой: {reason}."
    else:
        state, message = "idle", "1С давно не обращалась к сайту (больше суток)."
    return {
        "state": state, "message": message, "keys": keys,
        "last": last, "last_ok": last_ok, "last_sync": last_sync,
        "ok_24h": ok_24h, "err_24h": int(err_24h),
        "waiting_orders": waiting_orders(db),
    }
