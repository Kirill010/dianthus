# tests/helpers.py
"""Мелкие помощники для тестов: вход, CSRF, заготовки данных."""
import re

from app.models import Order, OrderItem, Product, Supply, SupplyItem, User
from app.services.passwords import hash_password


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not m:
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert m, "CSRF-токен не найден в HTML"
    return m.group(1)


def login(client, email: str, password: str, totp: str | None = None):
    page = client.get("/login")
    data = {"email": email, "password": password, "csrf_token": csrf_from(page.text)}
    if totp is not None:
        data["totp_code"] = totp
    return client.post("/login", data=data, follow_redirects=False)


def make_user(db, email="client@test.ru", password="Client123", *, admin=False,
              approved=True, **extra) -> User:
    user = User(
        email=email,
        hashed_password=hash_password(password),
        full_name=extra.pop("full_name", "Иван Иванов"),
        company_name=extra.pop("company_name", "ООО Ромашка"),
        phone=extra.pop("phone", "+79001234567"),
        inn=extra.pop("inn", "7701234567"),
        city=extra.pop("city", "Москва"),
        is_admin=admin,
        is_approved=approved or admin,
        **extra,
    )
    db.add(user)
    db.commit()
    return user


def seed_item(db, *, name="Роза Тест", stock=10, price=50.0, pack=25,
              active=True, service=False, status="Разгружен",
              sku="", external_id="") -> SupplyItem:
    product = Product(name=name, package_size=pack, min_quantity=1,
                      country="Эквадор", length_cm=60, category="Роза Эквадор",
                      sku=sku, external_id=external_id)
    supply = Supply(country="1С" if service else "Эквадор", status=status,
                    is_service=service,
                    notes="Авто-синхронизация из 1С" if service else "")
    db.add_all([product, supply])
    db.flush()
    item = SupplyItem(supply_id=supply.id, product_id=product.id, price=price,
                      stock=stock, is_active=active)
    db.add(item)
    db.commit()
    return item


def make_order(db, user, item, *, packs=3, status="Новый", deduct=True) -> Order:
    """Заказ как после place_order: товар списан со склада."""
    pack = item.product.package_size
    subtotal = round(item.price * pack * packs, 2)
    order = Order(user_id=user.id, subtotal=subtotal, total_price=subtotal,
                  discount_percent=0, status=status)
    db.add(order)
    db.flush()
    db.add(OrderItem(order_id=order.id, product_id=item.product_id,
                     supply_item_id=item.id, product_name=item.product.name,
                     unit="упаковка", price=item.price, package_size=pack,
                     quantity=packs))
    if deduct:
        item.stock -= packs
        if item.available_stock <= 0:
            item.is_active = False
    db.commit()
    return order
