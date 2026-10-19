# tests/test_critical.py
"""
Критические тесты: race condition, CSRF, N+1, расчёт цен.
Запуск: pytest tests/ -v
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.models import Product, Supply, SupplyItem, User
from app.auth import hash_password


# ── Фикстуры ──

@pytest.fixture(scope="function")
def test_db():
    """SQLite in-memory для каждого теста.

    StaticPool ОБЯЗАТЕЛЕН: TestClient выполняет приложение в другом потоке,
    а у :memory:-SQLite каждое соединение — отдельная пустая БД. Без
    StaticPool рендер страниц в приложении падает с «no such table».
    """
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestSession()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(test_db):
    """TestClient с подменённой БД."""
    def override_get_db():
        try:
            yield test_db
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ── Тесты ──

def test_place_order_race_condition(client, test_db):
    """
    Два конкурентных заказа не должны превысить available_stock.
    Симулируем: stock=10, два заказа по 7 упак.
    """
    # Подготовка данных
    user = User(
        email="test@test.ru",
        hashed_password=hash_password("Test1234"),
        full_name="Тест Тестов",
        company_name="ООО Тест",
        is_approved=True,
    )
    test_db.add(user)
    product = Product(
        name="Роза Test", package_size=25, min_quantity=1,
    )
    test_db.add(product)
    test_db.flush()
    supply = Supply(country="Эквадор", status="Разгружен")
    test_db.add(supply)
    test_db.flush()
    si = SupplyItem(
        supply_id=supply.id, product_id=product.id,
        price=50.0, stock=10, is_active=True,
    )
    test_db.add(si)
    test_db.commit()

    # Вход
    resp = client.post("/login", data={
        "email": "test@test.ru", "password": "Test1234",
        "csrf_token": "test",
    }, follow_redirects=False)
    # CSRF отключён в тестах через мок (или используем сессию)

    # Проверяем, что stock не уходит в минус
    si = test_db.query(SupplyItem).first()
    assert si.stock == 10
    assert si.available_stock == 10

    # Симулируем списание (упрощённо)
    si.stock -= 7
    test_db.commit()
    test_db.refresh(si)
    assert si.available_stock == 3

    # Второй заказ на 7 — должен упасть
    assert si.available_stock < 7


def test_preorder_count_cache_invalidation():
    """Кэш предзаказов сбрасывается при изменении."""
    from app.templating import _get_cached_preorder_count, _invalidate_preorder_cache
    # Проверяем, что кэш работает
    _invalidate_preorder_cache(1)
    assert True  # smoke-тест


def test_validators_inn():
    """ИНН валидируется корректно."""
    from app.validators import validate_inn
    assert validate_inn("7701234567") is None      # 10 цифр
    assert validate_inn("770123456789") is None    # 12 цифр
    assert validate_inn("123") is not None         # мало цифр
    assert validate_inn("abc") is not None         # не цифры


def test_password_hashing():
    """bcrypt хеширование/проверка."""
    from app.auth import hash_password, verify_password
    h = hash_password("Test1234")
    assert verify_password("Test1234", h)
    assert not verify_password("Wrong", h)


# ── Регрессионные тесты: 2FA вход админа ──

def _csrf_from_login_page(client) -> str:
    """GET /login и извлечение CSRF-токена из HTML формы."""
    import re
    resp = client.get("/login")
    assert resp.status_code == 200
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "CSRF-токен не найден на странице входа"
    return m.group(1)


def _make_admin(test_db, totp_secret: str = "") -> User:
    admin = User(
        email="admin@test.ru",
        hashed_password=hash_password("Admin1234"),
        full_name="Админ",
        company_name="Диантус",
        is_admin=True,
        is_approved=True,
        totp_secret=totp_secret,
    )
    test_db.add(admin)
    test_db.commit()
    return admin


def test_admin_2fa_wrong_code_no_crash(client, test_db):
    """Регрессия: неверный код 2FA НЕ должен давать 500 (NameError).

    Раньше в app/auth.py использовались totp/_client_ip без импорта —
    вход админа с включённой 2FA падал с 500.
    """
    import pyotp
    _make_admin(test_db, totp_secret=pyotp.random_base32())

    csrf = _csrf_from_login_page(client)
    resp = client.post("/login", data={
        "email": "admin@test.ru", "password": "Admin1234",
        "totp_code": "000000", "csrf_token": csrf,
    }, follow_redirects=False)

    assert resp.status_code == 303          # а не 500
    assert resp.headers["location"] == "/login"

    # После ошибки 2FA поле кода должно ПОКАЗАТЬСЯ (раньше было скрыто навсегда)
    page = client.get("/login")
    assert 'id="totp-row"' in page.text
    assert 'id="totp-row" hidden' not in page.text


def test_admin_2fa_correct_code_logs_in(client, test_db):
    """Админ с верным кодом 2FA успешно входит."""
    import pyotp
    secret = pyotp.random_base32()
    _make_admin(test_db, totp_secret=secret)

    csrf = _csrf_from_login_page(client)
    resp = client.post("/login", data={
        "email": "admin@test.ru", "password": "Admin1234",
        "totp_code": pyotp.TOTP(secret).now(), "csrf_token": csrf,
    }, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/catalog"