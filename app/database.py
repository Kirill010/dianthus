# Подключение к БД. SQLite и PostgreSQL.
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import config

if config.DATABASE_URL.startswith("sqlite"):
    def _sqlite_pragmas(dbapi_conn, _conn_record):
        """WAL + busy_timeout для SQLite.

        WAL позволяет читать во время записи и уменьшает вероятность
        SQLITE_BUSY при списании остатков; busy_timeout ждёт освобождения
        блокировки до 5 секунд вместо мгновенной ошибки «database is locked».
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    engine = create_engine(
        config.DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
    from sqlalchemy import event as _sa_event
    _sa_event.listen(engine, "connect", _sqlite_pragmas)
else:
    # Пул на КАЖДЫЙ воркер: воркеров × (pool_size + max_overflow) должно быть
    # меньше max_connections в PostgreSQL (в compose — 100).
    engine = create_engine(
        config.DATABASE_URL,
        pool_pre_ping=True,
        pool_size=int(os.getenv("DB_POOL_SIZE", "6")),
        max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "9")),
        pool_recycle=1800,
    )

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False,
)
Base = declarative_base()


def get_db():
    """Зависимость FastAPI: отдаёт сессию БД на один запрос.

    Движок берётся из SessionLocal.bind, а не из engine напрямую —
    так тесты могут подменить БД через SessionLocal.configure(bind=...),
    не трогая dependency_overrides. В продакшене поведение не меняется.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()