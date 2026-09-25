"""
Планировщик фоновых задач.
Lock-файл — в /run/dianthus (systemd RuntimeDirectory).
"""
import fcntl
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, joinedload, selectinload

from .models import SupplyItem, Preorder, Notification, Product

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
        logger.info(
            "🔒 Lock планировщика получен: %s (pid=%d)",
            LOCK_FILE, os.getpid(),
        )
        return True
    except BlockingIOError:
        logger.info("ℹ️ Планировщик уже запущен в другом воркере")
        if _lock_fd is not None:
            os.close(_lock_fd)
            _lock_fd = None
        return False
    except OSError as e:
        logger.warning(
            "Lock недоступен (%s) — запускаю без блокировки. "
            "Проверьте RuntimeDirectory=dianthus в systemd.",
            e,
        )
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
    
    supply_items = (
        db.query(SupplyItem)
        .options(joinedload(SupplyItem.product))
        .filter(SupplyItem.supply_id == supply.id)
        .all()
    )

    active_count = (
        db.query(SupplyItem)
        .filter(SupplyItem.is_active.is_(True))
        .count()
    )
    if active_count > 0:
        db.query(SupplyItem).filter(
            SupplyItem.is_active.is_(True)
        ).update(
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
        <p style="font-size:16px;">Здравствуйте, <b>{user.full_name}</b>!</p>
        <p>Товар из вашего предзаказа <b>«{product.name}»</b>
           уже на складе.</p>
        <a href="{config.APP_URL}/preorders" class="btn">
            Открыть мои предзаказы
        </a>
        <div class="footer">{config.SHOP_NAME}</div>
      </div>
    </body></html>
    """


async def auto_unload_overdue() -> None:
    logger.info("⏰ auto_unload_overdue: старт")
    from .database import SessionLocal
    from .models import Supply, SupplyItem

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