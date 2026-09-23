"""
Планировщик фоновых задач.
Lock-файл — в /run/dianthus (systemd RuntimeDirectory),
чтобы работал при PrivateTmp=true.
"""
import fcntl
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

LOCK_FILE = os.getenv("SCHEDULER_LOCK", "/run/dianthus/scheduler.lock")

_scheduler: AsyncIOScheduler | None = None
_lock_fd: int | None = None


def _ensure_lock_dir() -> None:
    d = os.path.dirname(LOCK_FILE)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            logger.warning("Не создал %s: %s", d, e)


def _acquire_scheduler_lock() -> bool:
    global _lock_fd
    _ensure_lock_dir()
    try:
        _lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            os.ftruncate(_lock_fd, 0)
            os.write(_lock_fd, f"pid={os.getpid()}\n".encode())
        except OSError:
            pass
        logger.info("🔒 Lock планировщика: %s", LOCK_FILE)
        return True
    except BlockingIOError:
        logger.info("ℹ️ Планировщик уже запущен в другом воркере")
        if _lock_fd is not None:
            os.close(_lock_fd)
            _lock_fd = None
        return False
    except OSError as e:
        logger.warning("Lock недоступен (%s) — запускаю без блокировки", e)
        return True


def _release_scheduler_lock() -> None:
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        finally:
            _lock_fd = None


def do_unload(db: Session, supply) -> int:
    """Разгружает поставку + вычитает предзаказы."""
    from .models import SupplyItem, Preorder
    from sqlalchemy import func

    db.query(SupplyItem).filter(
        SupplyItem.is_active.is_(True)
    ).update({SupplyItem.is_active: False}, synchronize_session=False)

    for item in supply.items:
        if item.stock <= 0:
            continue
        preordered = (
            db.query(func.coalesce(func.sum(Preorder.quantity), 0))
            .filter(
                Preorder.product_id == item.product_id,
                Preorder.is_fulfilled.is_(False),
            )
            .scalar()
        )
        if preordered > 0:
            item.stock = max(0, item.stock - preordered)
            db.query(Preorder).filter(
                Preorder.product_id == item.product_id,
                Preorder.is_fulfilled.is_(False),
            ).update({Preorder.is_fulfilled: True},
                     synchronize_session=False)
            logger.info(
                "📦 '%s': зарезервировано %d упак. под предзаказы",
                item.product.name, preordered,
            )

    activated = 0
    for item in supply.items:
        if item.stock > 0:
            item.is_active = True
            activated += 1

    supply.status = "Разгружен"
    logger.info("📦 Поставка №%d: активировано %d", supply.id, activated)
    return activated


async def auto_unload_overdue() -> None:
    logger.info("⏰ auto_unload_overdue: старт")
    from .database import SessionLocal
    from .models import Supply

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        overdue = (
            db.query(Supply)
            .filter(
                Supply.status.in_(["В пути", "Прибыл"]),
                Supply.arrival_date.isnot(None),
                Supply.arrival_date <= now,
            )
            .all()
        )
        for supply in overdue:
            try:
                do_unload(db, supply)
            except Exception as e:
                logger.error("Разгрузка №%d: %s", supply.id, e)
        db.commit()
        logger.info("✅ auto_unload_overdue: %d", len(overdue))
    except Exception as e:
        db.rollback()
        logger.exception("auto_unload_overdue: %s", e)
    finally:
        db.close()


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