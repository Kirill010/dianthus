import logging
from ..models import AuditLog

logger = logging.getLogger(__name__)


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