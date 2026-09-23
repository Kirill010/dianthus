"""
Планировщик фоновых задач для проекта Диантус.

Две роли:
1. Автоматическая разгрузка просроченных поставок (каждый час)
2. Функция do_unload() — ручная разгрузка из админки

При запуске с несколькими воркерами Uvicorn каждый воркер
пытается запустить свой планировщик. Чтобы этого избежать,
используется файловая блокировка fcntl:
только первый воркер захватывает lock и запускает задачи.
"""

import fcntl
import logging
import os
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

LOCK_FILE = "/tmp/dianthus_scheduler.lock"

_scheduler: AsyncIOScheduler | None = None
_lock_fd: int | None = None


# ═══════════════════════════════════════════════════════════
# ФАЙЛОВАЯ БЛОКИРОВКА
# ═══════════════════════════════════════════════════════════

def _acquire_scheduler_lock() -> bool:
    """
    Пытается захватить файловую блокировку.

    Возвращает:
        True — блокировка наша, можно запускать планировщик
        False — другой воркер уже запустил, пропускаем

    fcntl.flock с LOCK_EX | LOCK_NB — эксклюзивная неблокирующая
    блокировка. Если занято — сразу BlockingIOError.
    """
    global _lock_fd

    try:
        _lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info("🔒 Файловая блокировка планировщика захвачена")
        return True
    except BlockingIOError:
        logger.info(
            "ℹ️ Планировщик уже запущен в другом воркере — пропускаем"
        )
        if _lock_fd is not None:
            os.close(_lock_fd)
            _lock_fd = None
        return False


def _release_scheduler_lock() -> None:
    """Освобождает файловую блокировку при остановке приложения."""
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
            logger.info("🔓 Файловая блокировка планировщика освобождена")
        except OSError:
            pass
        finally:
            _lock_fd = None


# ═══════════════════════════════════════════════════════════
# РАЗГРУЗКА ПОСТАВКИ (используется и в админке, и в задаче)
# ═══════════════════════════════════════════════════════════

def do_unload(db: Session, supply) -> int:
    """
    Разгружает поставку:
    1. Снимает с полок ВСЕ активные позиции
    2. Вычитает предзаказы из остатков НОВОЙ поставки
    3. Активирует позиции этой поставки с остатком > 0
    4. Ставит статус «Разгружен»
    """
    from .models import SupplyItem, Preorder
    from sqlalchemy import func

    # 1. Снимаем с полок всё, что было активно
    db.query(SupplyItem).filter(
        SupplyItem.is_active.is_(True)
    ).update({SupplyItem.is_active: False}, synchronize_session=False)

    # 2. Вычитаем предзаказы из остатков НОВОЙ поставки
    for item in supply.items:
        if item.stock <= 0:
            continue

        # Сколько всего НЕВЫПОЛНЕННЫХ предзаказов на этот ТОВАР
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
            # Помечаем как выполненные, чтобы не вычитать повторно
            db.query(Preorder).filter(
                Preorder.product_id == item.product_id,
                Preorder.is_fulfilled.is_(False),
            ).update({Preorder.is_fulfilled: True},
                     synchronize_session=False)
            logger.info(
                "📦 Товар '%s' (ID %d): зарезервировано %d упак. "
                "под предзаказы. Остаток: %d упак.",
                item.product.name, item.id, preordered, item.stock,
            )

    # 3. Активируем позиции этой поставки с остатком > 0
    activated = 0
    for item in supply.items:
        if item.stock > 0:
            item.is_active = True
            activated += 1

    # 4. Статус
    supply.status = "Разгружен"
    logger.info("📦 Разгружена поставка №%d: активировано %d позиций",
                supply.id, activated)
    return activated

# ═══════════════════════════════════════════════════════════
# ФОНОВЫЕ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════

async def auto_unload_overdue() -> None:
    """
    Каждый час: авторазгрузка поставок, у которых
    arrival_date уже наступил, статус «В пути» или «Прибыл»,
    но их ещё не разгрузили.
    """
    logger.info("⏰ auto_unload_overdue: старт")
    from .database import SessionLocal
    from .models import Supply

    db = SessionLocal()
    try:
        now = datetime.utcnow()
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
                logger.error(
                    "Ошибка разгрузки поставки №%d: %s", supply.id, e
                )
        db.commit()
        logger.info("✅ auto_unload_overdue: разгружено %d", len(overdue))
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка в auto_unload_overdue: %s", e)
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════
# ЗАПУСК / ОСТАНОВКА
# ═══════════════════════════════════════════════════════════

def start_scheduler() -> None:
    """
    Запускает планировщик, если удалось захватить блокировку.
    Вызывается из lifespan FastAPI.
    """
    global _scheduler

    if not _acquire_scheduler_lock():
        return

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(
        auto_unload_overdue,
        trigger=CronTrigger(minute=0),
        id="auto_unload_overdue",
        name="Авторазгрузка просроченных поставок",
        replace_existing=True,
        max_instances=1,
    )
    _scheduler.start()
    logger.info("⏰ Планировщик запущен (в этом воркере)")


def stop_scheduler() -> None:
    """Останавливает планировщик и освобождает блокировку."""
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("🛑 Планировщик остановлен")
    _release_scheduler_lock()