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

# Почта: у разработчика в .env могут стоять НАСТОЯЩИЕ SMTP-данные. Пустые
# значения здесь важны — load_dotenv() не перезаписывает уже заданные
# переменные, и тесты (оформление заказа, одобрение клиента и т.п.) иначе
# рассылали бы реальные письма.
for _smtp in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_TO"):
    os.environ[_smtp] = ""

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

# ── Общие фикстуры для новых тестов ─────────────────────────────

@pytest.fixture(autouse=True)
def _reset_in_memory_counters():
    """Счётчики лимитов и блокировок живут в памяти процесса — между тестами
    их нужно сбрасывать, иначе серия входов из разных тестов упрётся в лимит
    «10 попыток в минуту» и тесты начнут падать случайным образом."""
    from app import security
    security._rate_buckets.clear()
    security._login_fails_mem.clear()
    security._login_locks_mem.clear()
    security._used_once.clear()
    yield


@pytest.fixture
def mem_db():
    """Чистая SQLite в памяти на один тест (одно соединение на все потоки)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # В боевой PostgreSQL внешние ключи СТРОГИЕ, а SQLite по умолчанию их не
    # проверяет — поэтому удаление записи, на которую ещё кто-то ссылается,
    # «работало» в тестах и падало в проде. Включаем проверку и здесь.
    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=eng)
    session = sessionmaker(
        bind=eng, autocommit=False, autoflush=False, expire_on_commit=False
    )()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=eng)
        eng.dispose()


@pytest.fixture
def web(mem_db):
    """TestClient, у которого ВСЁ приложение (роутеры, обработчики ошибок,
    планировщик) работает с БД из mem_db: перепривязывается SessionLocal."""
    from app.database import SessionLocal

    original = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=mem_db.get_bind())
    try:
        with TestClient(app) as c:
            yield c
    finally:
        SessionLocal.configure(bind=original)
