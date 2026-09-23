# Автоматическая разгрузка поставок по расписанию.
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .database import SessionLocal
from .models import Supply, SupplyItem

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="Europe/Moscow")


def do_unload(db, supply: Supply) -> int:
    """
    Единая функция разгрузки поставки.
    Деактивирует старые партии тех же товаров,
    активирует текущую поставку.
    Возвращает количество разгруженных товаров.
    """
    product_ids = [i.product_id for i in supply.items]

    if product_ids:
        old = (
            db.query(SupplyItem)
            .filter(
                SupplyItem.product_id.in_(product_ids),
                SupplyItem.supply_id != supply.id,
                SupplyItem.is_active.is_(True),
            )
            .all()
        )
        for o in old:
            o.is_active = False
            o.stock = 0

    for item in supply.items:
        item.is_active = True

    supply.status = "Разгружен"
    return len(supply.items)


def auto_unload_overdue() -> None:
    """
    Ищет поставки с arrival_date <= сейчас и статусом
    «Ожидается»/«В пути» → разгружает их.
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        overdue = (
            db.query(Supply)
            .filter(
                Supply.status.in_(["Ожидается", "В пути"]),
                Supply.arrival_date.isnot(None),
                Supply.arrival_date <= now,
            )
            .all()
        )
        for s in overdue:
            count = do_unload(db, s)
            logger.info(
                "⏰ Авторазгрузка поставки №%d (%d товаров)", s.id, count
            )
        if overdue:
            db.commit()
    except Exception as e:
        db.rollback()
        logger.exception("Ошибка авторазгрузки: %s", e)
    finally:
        db.close()


def start_scheduler() -> None:
    # Проверка каждые 5 минут. Первый запуск — сразу при старте.
    scheduler.add_job(
        auto_unload_overdue,
        "interval",
        minutes=5,
        id="auto_unload",
        replace_existing=True,
        next_run_time=datetime.utcnow(),
    )
    scheduler.start()
    logger.info("⏰ Планировщик авторазгрузки запущен")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)