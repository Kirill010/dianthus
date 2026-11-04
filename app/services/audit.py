import logging
from datetime import datetime, timedelta, timezone

from ..models import AuditLog

logger = logging.getLogger(__name__)

# Журнал действий хранится 12 месяцев — так сказано в политике обработки персональных данных
AUDIT_KEEP_DAYS = 365


def prune_job(days: int = AUDIT_KEEP_DAYS) -> int:
    """Ежесуточная чистка журнала действий сотрудников (вызывается планировщиком)."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        n = db.query(AuditLog).filter(AuditLog.created_at < cutoff).delete(synchronize_session=False)
        db.commit()
        if n:
            logger.info("Журнал действий: удалено записей старше %d дней: %d", days, n)
        return n
    except Exception as e:                     # noqa: BLE001
        db.rollback()
        logger.warning("audit_log.prune: %s", e)
        return 0
    finally:
        db.close()


def log_action(db, request, admin, action: str, entity: str = "",
               entity_id: int | None = None, details: str = "") -> None:
    """Пишет действие админа в журнал. Ошибка записи не должна ломать операцию."""
    try:
        db.add(AuditLog(
            user_id=getattr(admin, "id", None),
            action=action[:64],
            entity=entity[:64],
            entity_id=entity_id,
            details=details[:2000],
            ip=(request.client.host if request.client else "")[:45],
        ))
        db.commit()
    except Exception as e:                     # noqa: BLE001
        db.rollback()
        logger.warning("audit_log: %s", e)