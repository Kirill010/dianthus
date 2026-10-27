"""
Планировщик фоновых задач.
Lock-файл — в /run/dianthus (systemd RuntimeDirectory).
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from html import escape

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload, selectinload

from .locks import acquire_file_lock, lock_path, release_file_lock
from .models import SupplyItem, Supply, Preorder, Notification, Product

logger = logging.getLogger(__name__)

LOCK_FILE = lock_path("SCHEDULER_LOCK", "scheduler.lock")

_scheduler: AsyncIOScheduler | None = None
_lock_fd: int | None = None


def _acquire_scheduler_lock() -> bool:
    """True — планировщик наш. False — он уже работает в другом воркере.

    Сама блокировка — в app/locks.py: fcntl на Linux, msvcrt на Windows.
    Если механизм блокировки недоступен, работаем без неё (лучше дубль
    задачи раз в час, чем отсутствие планировщика).
    """
    global _lock_fd
    _lock_fd = acquire_file_lock(LOCK_FILE, blocking=False)
    if _lock_fd is None:
        logger.info("ℹ️ Планировщик уже запущен в другом воркере")
        return False
    logger.info(
        "🔒 Lock планировщика получен: %s (pid=%d)", LOCK_FILE, os.getpid()
    )
    return True


def _release_scheduler_lock() -> None:
    global _lock_fd
    release_file_lock(_lock_fd)
    _lock_fd = None


def do_unload(db: Session, supply) -> int:
    # Служебная поставка 1С — это фактически «живой» каталог: её разгрузка
    # сняла бы с полок остатки всего сайта. Такие поставки обновляются
    # только синхронизацией с 1С.
    if getattr(supply, "is_service", False):
        logger.warning(
            "do_unload: поставка №%s служебная (1С) — пропускаю",
            getattr(supply, "id", "?"),
        )
        return 0
    # Инвойс из 1С («товары в пути»): остатки на полке ведёт 1С, а разгрузка
    # показала бы тот же товар ещё раз.
    if getattr(supply, "external_id", ""):
        logger.warning(
            "do_unload: поставка №%s ведётся из 1С (инвойс %s) — пропускаю",
            getattr(supply, "id", "?"), supply.external_id,
        )
        return 0

    supply_items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.supply_id == supply.id)
        .all()
    )

    # «Старые партии» — это активные позиции ОБЫЧНЫХ поставок. Служебную
    # партию 1С (весь живой каталог, который ведёт обмен) не трогаем: раньше
    # ручная разгрузка любой поставки снимала с полок и её, и клиенты видели
    # пустой каталог до ближайшей выгрузки из 1С.
    regular_supplies = db.query(Supply.id).filter(Supply.is_service.is_(False))
    old_active = db.query(SupplyItem).filter(
        SupplyItem.is_active.is_(True),
        SupplyItem.supply_id.in_(regular_supplies),
    )
    active_count = old_active.count()
    if active_count > 0:
        old_active.update(
            {SupplyItem.is_active: False, SupplyItem.reserved_stock: 0},
            synchronize_session=False,
        )
        logger.info("📦 Деактивировано %d старых партий", active_count)

    activated = 0
    batch_emails: list[dict] = []

    product_ids = [i.product_id for i in supply_items if i.stock > 0]
    preorders_by_product: dict[int, list] = {}
    if product_ids:
        all_preorders = (
            db.query(Preorder)
            .options(joinedload(Preorder.user))
            .filter(
                Preorder.product_id.in_(product_ids),
                Preorder.is_fulfilled.is_(False),
            )
            .all()
        )
        for p in all_preorders:
            preorders_by_product.setdefault(p.product_id, []).append(p)

    for item in supply_items:
        if item.stock <= 0:
            continue
        preorders = preorders_by_product.get(item.product_id, [])
        reserved = sum(p.quantity for p in preorders)

        if reserved > 0:
            actual_reserve = min(reserved, item.stock)
            item.reserved_stock = actual_reserve

            for preorder in preorders:
                preorder.supply_item_id = item.id
                preorder.is_fulfilled = True

                db.add(Notification(
                    user_id=preorder.user_id,
                    text=(
                        f"🌸 Предзаказ поступил: "
                        f"«{item.product.name}» — {preorder.quantity} упак. "
                        f"Перейдите в «Мои предзаказы», чтобы оформить заказ."
                    ),
                ))

                if preorder.user and preorder.user.email:
                    body = _build_preorder_email(preorder, item)
                    batch_emails.append({
                        "to": preorder.user.email,
                        "subject": f"🌸 Предзаказ поступил: {item.product.name}",
                        "body_html": body,
                    })

        if item.available_stock > 0:
            item.is_active = True
            activated += 1

    supply.status = "Разгружен"

    try:
        from .templating import _invalidate_preorder_cache
        _invalidate_preorder_cache()
    except Exception:
        pass

    if batch_emails:
        from .services.notifier import send_batch_emails
        sent = send_batch_emails(batch_emails)
        logger.info("📧 Batch отправлено: %d/%d", sent, len(batch_emails))

    logger.info("📦 Поставка №%d: активировано %d", supply.id, activated)
    return activated


def _build_preorder_email(preorder, item) -> str:
    from .services.notifier import _BASE_STYLE
    from .config import config
    user = preorder.user
    product = item.product
    return f"""
    <!DOCTYPE html>
    <html><head><meta charset="UTF-8">{_BASE_STYLE}</head>
    <body>
      <div class="card">
        <h1>🌸 Ваш предзаказ поступил!</h1>
        <p style="font-size:16px;">Здравствуйте, <b>{escape(user.full_name or '')}</b>!</p>
        <p>Товар из вашего предзаказа <b>«{escape(product.name or '')}»</b>
           уже на складе.</p>
        <a href="{config.APP_URL}/preorders" class="btn">
            Открыть мои предзаказы
        </a>
        <div class="footer">{config.SHOP_NAME}</div>
      </div>
    </body></html>
    """


async def auto_unload_overdue() -> None:
    # Работа с БД и рассылка писем синхронные и могут длиться секунды.
    # В потоке они не блокируют event loop, который обслуживает запросы.
    await asyncio.to_thread(_auto_unload_overdue_sync)


def _auto_unload_overdue_sync() -> None:
    logger.info("⏰ auto_unload_overdue: старт")
    from .database import SessionLocal

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        overdue = (
            db.query(Supply)
            .options(
                selectinload(Supply.items).selectinload(SupplyItem.product)
            )
            .filter(
                Supply.status.in_(["В пути", "Прибыл"]),
                Supply.arrival_date.isnot(None),
                Supply.arrival_date <= now,
                Supply.is_service.is_(False),   # fix #20
                # инвойсы из 1С не разгружаем: остатки ведёт 1С
                or_(Supply.external_id == "", Supply.external_id.is_(None)),
            )
            .all()
        )
        for supply in overdue:
            try:
                # savepoint: сбой на одной поставке откатывает только её,
                # а не оставляет «половину» разгрузки в общей транзакции
                with db.begin_nested():
                    do_unload(db, supply)
            except Exception as e:
                logger.exception("Разгрузка №%d: %s", supply.id, e)
        db.commit()
        logger.info("✅ auto_unload_overdue: %d", len(overdue))
    except Exception as e:
        db.rollback()
        logger.exception("auto_unload_overdue: %s", e)
    finally:
        db.close()


async def _prune_exchange_log() -> None:
    from .services.exchange_log import prune_job
    await asyncio.to_thread(prune_job)


async def _close_stale_1c_supplies() -> None:
    from .services.supply_sync import close_stale_job
    await asyncio.to_thread(close_stale_job)


def start_scheduler() -> None:
    global _scheduler
    if not _acquire_scheduler_lock():
        return
    try:
        _scheduler = AsyncIOScheduler()
        _scheduler.add_job(
            auto_unload_overdue,
            trigger=CronTrigger(minute=0),
            id="auto_unload_overdue",
            replace_existing=True,
            max_instances=1,
        )
        # Раз в сутки чистим журнал обмена с 1С (храним 60 дней)
        _scheduler.add_job(
            _prune_exchange_log,
            trigger=CronTrigger(hour=3, minute=30),
            id="prune_exchange_log",
            replace_existing=True,
            max_instances=1,
        )
        # Раз в сутки закрываем «забытые» инвойсы из 1С (1С 21+ день их не присылала)
        _scheduler.add_job(
            _close_stale_1c_supplies,
            trigger=CronTrigger(hour=4, minute=10),
            id="close_stale_1c_supplies",
            replace_existing=True,
            max_instances=1,
        )
        _scheduler.start()
        logger.info("⏰ Планировщик запущен")
    except Exception as e:
        logger.exception("Не удалось запустить планировщик: %s", e)
        _release_scheduler_lock()


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("🛑 Планировщик остановлен")
    _release_scheduler_lock()