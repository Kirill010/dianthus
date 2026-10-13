# tests/conftest.py
"""
Pytest-фикстуры. ВАЖНО: переменные окружения должны быть
установлены ДО первого импорта app.*
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Тестовое окружение (до импорта app) ──────────────────
os.environ["ENV"] = "test"
os.environ["DATABASE_URL"] = "sqlite:///./test_dianthus.db"
os.environ["SECRET_KEY"] = "test-secret-key-min-32-characters-abcdefgh"
os.environ["ADMIN_EMAIL"] = ""
os.environ["ADMIN_PASSWORD"] = ""
os.environ["REDIS_URL"] = ""
os.environ["SENTRY_DSN"] = ""

# ── 1С-обмен ─────────────────────────────────────────────
# Логин/пароль для эндпоинтов /api/1c/*. Без них приложение отдаёт 503
# («интеграция не настроена»), и тесты обмена не смогли бы работать.
os.environ["INTEGRATION_USER"] = "1c_dianthus"
os.environ["INTEGRATION_PASSWORD"] = "test-1c-pass"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app import models  # noqa: F401 — регистрация всех таблиц в Base.metadata
from app.main import app


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    """Глобальная SQLite-БД для lifespan-задач (планировщик, админ).

    ВАЖНО: lifespan приложения (TestClient(app)) выполняет
    Base.metadata.create_all() и авто-миграции на НАСТОЯЩЕМ engine,
    поэтому его тоже нужно подготовить.
    """
    Base.metadata.create_all(bind=engine)
    yield
    engine.dispose()
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(f"./test_dianthus.db{suffix}")
        except FileNotFoundError:
            pass


@pytest.fixture
def client():
    """TestClient с активированным lifespan."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def anon_client():
    """TestClient без аутентификации (синоним client)."""
    with TestClient(app) as c:
        yield c