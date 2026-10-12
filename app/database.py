# Подключение к БД. SQLite и PostgreSQL.
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import config

if config.DATABASE_URL.startswith("sqlite"):
    engine = create_engine(config.DATABASE_URL,
                           connect_args={"check_same_thread": False})
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
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()