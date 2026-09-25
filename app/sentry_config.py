# app/sentry_config.py
"""Инициализация Sentry SDK."""
import logging
import os

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    """Подключает Sentry, если задан SENTRY_DSN."""
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        logger.info("SENTRY_DSN не задан — мониторинг отключён")
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("ENV", "dev"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_RATE", "0.1")),
            integrations=[FastApiIntegration()],
            send_default_pii=False,
        )
        logger.info("✅ Sentry подключён")
    except ImportError:
        logger.warning("sentry-sdk не установлен")
    except Exception as e:
        logger.error("Не удалось инициализировать Sentry: %s", e)