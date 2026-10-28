# tests/test_taxonomy_migration.py
"""Обновление базы СО СТАРОЙ СХЕМОЙ: у товара были текстовые country / category / length_cm,
теперь вид, цвет, рост и страна — отдельные таблицы-справочники. Миграция добавляет колонки,
переносит значения и кладёт стартовый набор справочников; повторный запуск ничего не меняет."""
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.migrations import prepare_database
from app.models import Color, Country, Height, Kind, Product

OLD_PRODUCTS = """
CREATE TABLE products (
    id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL, sku VARCHAR(100), external_id VARCHAR(64),
    description TEXT, country VARCHAR(100), length_cm INTEGER, unit VARCHAR(30), package_size INTEGER,
    min_quantity INTEGER, image_url VARCHAR(500), photos TEXT DEFAULT '[]', category VARCHAR(100),
    created_at DATETIME
)"""

OLD_ROWS = [
    # (name, country, length_cm, category)
    ("Роза Фридом 60", "Эквадор", 60, "Роза Эквадор"),       # «Роза Эквадор» = вид «Роза» + страна «Эквадор»
    ("Роза Эксплорер 70", "эквадор ", 70, "Роза Эквадор"),   # то же, но страна записана иначе: дубля быть не должно
    ("Гвоздика микс", "", 0, "Гвоздика"),                    # ни страны, ни роста
    ("Хризантема Бигуди", "Колумбия", 70, "Хризантема"),
    ("Что-то ещё", "Кения", 40, "Прочее"),
    ("Странный рост", "", 999, ""),                          # 999 см — не рост: пропускаем, товар не теряем
]


@pytest.fixture
def old_db(tmp_path):
    """Файл SQLite в схеме старой версии, SessionLocal на время теста смотрит в него."""
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(OLD_PRODUCTS))
        for i, (name, country, length, category) in enumerate(OLD_ROWS, start=1):
            conn.execute(text(
                "INSERT INTO products (id, name, sku, external_id, description, country, length_cm, unit, "
                "package_size, min_quantity, image_url, photos, category) "
                "VALUES (:i, :n, '', '', '', :c, :l, 'упаковка', 25, 1, '', '[]', :k)"),
                {"i": i, "n": name, "c": country, "l": length, "k": category})
    original = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=engine)
    try:
        yield engine
    finally:
        SessionLocal.configure(bind=original)
        engine.dispose()


def by_name(db, name):
    return db.query(Product).filter(Product.name == name).one()


def test_old_database_is_upgraded_and_values_move_into_the_dictionaries(old_db):
    prepare_database()

    cols = {c["name"] for c in inspect(old_db).get_columns("products")}
    assert {"barcode", "kind_id", "color_id", "height_id", "country_id"} <= cols
    assert {"country", "category", "length_cm"} <= cols              # старые колонки не трогаем
    user_cols = {c["name"] for c in inspect(old_db).get_columns("users")}
    assert {"address", "external_id"} <= user_cols

    with Session(old_db) as db:
        a, b = by_name(db, "Роза Фридом 60"), by_name(db, "Роза Эксплорер 70")
        assert (a.kind_ref.name, a.country_ref.name, a.height_ref.cm) == ("Роза", "Эквадор", 60)
        assert (b.kind_ref.name, b.country_ref.name, b.height_ref.cm) == ("Роза", "Эквадор", 70)
        assert a.country_id == b.country_id                          # «Эквадор» и «эквадор » — одна запись

        g = by_name(db, "Гвоздика микс")
        assert g.kind_ref.name == "Гвоздика" and g.country_ref is None and g.height_ref is None
        assert by_name(db, "Хризантема Бигуди").country_ref.name == "Колумбия"
        assert by_name(db, "Что-то ещё").kind_ref.name == "Прочее"
        odd = by_name(db, "Странный рост")
        assert odd.height_ref is None and odd.kind_ref is None       # ничего не выдумываем

        # справочники: стартовый набор + то, что нашлось в товарах; без дублей
        assert db.query(Kind).filter(Kind.name == "Роза Эквадор").count() == 0
        assert {"Роза", "Гвоздика", "Хризантема", "Тюльпан", "Пион", "Зелень"} <= {k.name for k in db.query(Kind)}
        assert {"Белый", "Жёлтый", "Красный", "Розовый"} <= {c.name for c in db.query(Color)}
        assert db.query(Color).filter(Color.name == "Белый").one().hex == "#ffffff"
        heights = [h.cm for h in db.query(Height).order_by(Height.cm)]
        assert heights == sorted(set(heights)) and {40, 50, 60, 70, 80} <= set(heights)
        assert db.query(Country).filter(Country.name == "Эквадор").count() == 1
        assert {"Эквадор", "Колумбия", "Кения"} <= {c.name for c in db.query(Country)}


def test_second_run_changes_nothing_and_does_not_repeat_the_move(old_db):
    prepare_database()

    def snapshot():
        with Session(old_db) as db:
            return (db.query(Kind).count(), db.query(Color).count(), db.query(Height).count(),
                    db.query(Country).count(),
                    sorted((p.name, p.kind_id, p.country_id, p.height_id) for p in db.query(Product)))
    first = snapshot()

    prepare_database()
    assert snapshot() == first
    with old_db.connect() as conn:
        done = conn.execute(text("SELECT count(*) FROM _migrations WHERE name = 'taxonomy_v1'")).scalar()
    assert done == 1


def test_values_edited_after_the_move_are_not_overwritten_by_a_rerun(old_db):
    """Владелец поправил вид товара вручную; перезапуск сайта не возвращает старое значение."""
    prepare_database()
    with Session(old_db) as db:
        p = by_name(db, "Гвоздика микс")
        p.kind_ref = db.query(Kind).filter(Kind.name == "Тюльпан").one()
        db.commit()
    prepare_database()
    with Session(old_db) as db:
        assert by_name(db, "Гвоздика микс").kind_ref.name == "Тюльпан"


def test_a_failed_move_is_retried_on_the_next_start(old_db, monkeypatch):
    from app.services import taxonomy
    real = taxonomy.migrate_legacy_products
    monkeypatch.setattr(taxonomy, "migrate_legacy_products", lambda db: (_ for _ in ()).throw(RuntimeError("сбой")))
    prepare_database()                                              # перенос упал, но сайт запустился
    with old_db.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM _migrations WHERE name = 'taxonomy_v1'")).scalar() == 0
    monkeypatch.setattr(taxonomy, "migrate_legacy_products", real)
    prepare_database()                                              # следующий запуск доделал работу
    with Session(old_db) as db:
        assert by_name(db, "Роза Фридом 60").kind_ref.name == "Роза"


def test_fresh_database_gets_the_defaults_without_legacy_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", connect_args={"check_same_thread": False})
    original = SessionLocal.kw["bind"]
    SessionLocal.configure(bind=engine)
    try:
        prepare_database()
        cols = {c["name"] for c in inspect(engine).get_columns("products")}
        assert "country" not in cols and "category" not in cols and "length_cm" not in cols
        with Session(engine) as db:
            assert db.query(Kind).count() >= 9 and db.query(Color).count() >= 12
            assert db.query(Height).count() >= 7 and db.query(Country).count() >= 5
    finally:
        SessionLocal.configure(bind=original)
        engine.dispose()
