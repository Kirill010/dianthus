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


def current_bind():
    """Текущий движок/соединение, к которому привязан SessionLocal.

    Тесты (tests/test_1c_integration.py) подменяют БД через
    SessionLocal.configure(bind=...). Все, кто выполняет DDL или
    прямые SQL-запросы (lifespan, авто-миграции, health-check),
    ОБЯЗАНЫ брать движок отсюда, иначе миграция и запросы уходят
    в разные базы («no such table» в тестах). В продакшене
    SessionLocal никогда не перепривязывается — возвращается engine.
    """
    return SessionLocal.kw["bind"]


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

def check_cyrillic_search() -> bool:
    """Проверяет, что PostgreSQL сравнивает кириллицу без учёта регистра.

    Поиск по каталогу построен на ILIKE. Если база создана с локалью «C»,
    ILIKE работает только для латиницы: «роза» не найдёт «Роза Freedom» —
    и ошибки при этом нет, просто пустая выдача. Лучше сказать об этом в логе
    при старте, чем искать причину по жалобам клиентов.
    """
    if current_bind().dialect.name != "postgresql":
        return True
    import logging
    from sqlalchemy import text
    log = logging.getLogger(__name__)
    try:
        with current_bind().connect() as conn:
            ok = bool(conn.execute(text("SELECT 'Роза' ILIKE 'роза'")).scalar())
    except Exception as e:                                   # noqa: BLE001
        log.warning("Проверка поиска по кириллице не выполнена: %s", e)
        return True
    if not ok:
        log.error(
            "❌ PostgreSQL не различает регистр кириллицы (ILIKE 'Роза' vs 'роза'). "
            "Поиск по русским названиям будет работать только при точном "
            "совпадении регистра. Создайте базу с локалью en_US.UTF-8/ru_RU.UTF-8 "
            "или ICU-сортировкой (см. docs/GUIDE_FROM_SCRATCH.md, раздел 10)."
        )
    return ok
