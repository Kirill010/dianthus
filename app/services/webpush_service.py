# app/services/webpush_service.py
"""WebPush-уведомления для клиентов."""
import json
import logging
import os

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.getenv("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS_EMAIL = os.getenv("VAPID_CLAIMS_EMAIL", "mailto:admin@dianthus.ru")


def send_webpush(subscription_info: dict, title: str, body: str,
                 url: str = "/") -> bool:
    """Отправляет WebPush-уведомление. Не бросает исключений."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        logger.debug("VAPID ключи не заданы — WebPush пропущен")
        return False

    try:
        from pywebpush import webpush, WebPushException

        payload = json.dumps({
            "title": title,
            "body": body,
            "url": url,
        })
        webpush(
            subscription_info=subscription_info,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
        )
        return True
    except ImportError:
        logger.warning("pywebpush не установлен")
        return False
    except Exception as e:
        logger.warning("WebPush ошибка: %s", e)
        return False