# -*- coding: utf-8 -*-
"""Демо-данные для РАЗРАБОТКИ: товары с картинками, поставки, клиент, заказ.

    python seed_demo.py            # наполнить пустую базу
    python seed_demo.py --force    # добавить, даже если товары уже есть

Создаёт:
  * админа      admin@example.ru   / Admin12345   (если в .env нет ADMIN_EMAIL)
  * клиента     client@example.ru  / Client12345  (скидка 5 %)
  * 12 товаров в поставке «Эквадор» (в каталоге) и 3 в поставке «В пути»
    (их можно заказать как предзаказ)
  * один тестовый заказ клиента
  * картинки-заглушки в app/static/uploads/ (рисуются Pillow, без интернета)

На боевой базе (ENV=prod) скрипт отказывается работать.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageDraw  # noqa: E402

from app.bootstrap import ensure_default_admin  # noqa: E402
from app.config import config  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.migrations import prepare_database  # noqa: E402
from app import models  # noqa: E402
from app.services.passwords import hash_password  # noqa: E402
from app.services.taxonomy import Taxonomy, set_taxonomy  # noqa: E402

UPLOADS = Path(__file__).resolve().parent / "app" / "static" / "uploads"

# название, цвет картинки (RGB), цвет, страна, рост, вид, шт/упак., цена/шт, упаковок
SHELF = [
    ("Роза Freedom 60",       (190, 30, 45),   "Красный",   "Эквадор",  60, "Роза",          25, 95.0, 120),
    ("Роза Explorer 60",      (140, 20, 40),   "Бордовый",  "Эквадор",  60, "Роза",          25, 105.0, 80),
    ("Роза Pink Floyd 60",    (235, 120, 170), "Розовый",   "Эквадор",  60, "Роза",          25, 132.0, 45),
    ("Роза Playa Blanca 60",  (245, 240, 230), "Кремовый",  "Эквадор",  60, "Роза",          25, 105.0, 60),
    ("Роза Mondial 70",       (250, 250, 245), "Белый",     "Эквадор",  70, "Роза",          25, 118.0, 30),
    ("Роза кустовая Bombastic", (240, 170, 190), "Розовый", "Эквадор",  50, "Роза кустовая", 10, 160.0, 20),
    ("Хризантема Бигуди",     (240, 215, 90),  "Жёлтый",    "Колумбия", 70, "Хризантема",    10, 85.0, 40),
    ("Гвоздика микс",         (220, 90, 120),  "Микс",      "Колумбия", 55, "Гвоздика",      20, 45.0, 200),
    ("Тюльпан Strong Gold",   (245, 190, 40),  "Жёлтый",    "Голландия", 40, "Тюльпан",      50, 38.0, 90),
    ("Пион Sarah Bernhardt",  (245, 180, 200), "Розовый",   "Голландия", 55, "Пион",         10, 310.0, 6),
    ("Эвкалипт Цинерея",      (130, 160, 140), "Зелёный",   "Колумбия", 80, "Зелень",        10, 70.0, 3),
    ("Гортензия белая",       (230, 235, 245), "Белый",     "Эквадор",  60, "Прочее",        10, 240.0, 0),
]
INCOMING = [
    ("Роза Avalanche 60",     (252, 252, 250), "Белый",     "Эквадор",  60, "Роза",          25, 110.0, 150),
    ("Роза Orange Crush 60",  (240, 120, 40),  "Оранжевый", "Эквадор",  60, "Роза",          25, 125.0, 70),
    ("Лилия восточная",       (250, 200, 215), "Розовый",   "Голландия", 80, "Прочее",       10, 190.0, 40),
]


def draw_photo(name: str, color: tuple, slug: str) -> str:
    """Мягкая картинка-заглушка: градиент и «бутон» нужного цвета."""
    size = 640
    img = Image.new("RGB", (size, size), (245, 239, 228))
    d = ImageDraw.Draw(img)
    for y in range(size):                      # фон-градиент
        shade = int(245 - 28 * y / size)
        d.line([(0, y), (size, y)], fill=(shade, shade - 4, shade - 14))
    cx, cy = size // 2, size // 2 + 20
    for r, k in ((230, 0.78), (185, 0.9), (140, 1.0), (95, 1.08), (50, 1.16)):
        c = tuple(min(255, int(v * k)) for v in color)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=c)
    d.rectangle([cx - 6, cy + 200, cx + 6, size], fill=(70, 110, 70))
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = UPLOADS / f"demo_{slug}.webp"
    img.save(path, "WEBP", quality=82)
    return f"/static/uploads/{path.name}"


def main(force: bool = False) -> int:
    if config.ENV == "prod":
        print("Отказ: ENV=prod. Демо-данные нельзя создавать на боевой базе.")
        return 1

    prepare_database(ensure_default_admin)
    db = SessionLocal()
    try:
        if db.query(models.Product).count() > 0 and not force:
            print("Товары уже есть — пропускаю (добавить всё равно: --force).")
            return 0

        if not db.query(models.User).filter(models.User.is_admin.is_(True)).count():
            db.add(models.User(
                email="admin@example.ru", hashed_password=hash_password("Admin12345"),
                full_name="Администратор", company_name="Диантус",
                is_admin=True, is_approved=True))
        client = db.query(models.User).filter(
            models.User.email == "client@example.ru").first()
        if client is None:
            client = models.User(
                email="client@example.ru", hashed_password=hash_password("Client12345"),
                full_name="Иван Петров", company_name="Салон «Ромашка»",
                phone="+7 900 123-45-67", inn="7701234567", city="Москва",
                is_approved=True, discount_percent=5)
            db.add(client)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        shipped = models.Supply(country="Эквадор", city="Кито", status="Разгружен",
                                departure_date=now - timedelta(days=4),
                                arrival_date=now - timedelta(days=1),
                                notes="Демо-поставка")
        incoming = models.Supply(country="Эквадор", city="Кито", status="В пути",
                                 departure_date=now - timedelta(days=1),
                                 arrival_date=now + timedelta(days=3),
                                 notes="Демо: машина в пути")
        db.add_all([shipped, incoming])
        db.flush()

        tax = Taxonomy(db)

        def add(rows, supply, active):
            items = []
            for i, (name, rgb, color, country, height, kind, pack, price, packs) in enumerate(rows):
                photo = draw_photo(name, rgb, f"{supply.id}_{i}")
                p = models.Product(
                    name=name, sku=f"DEMO-{supply.id}-{i + 1:02d}",
                    barcode=f"46000000{supply.id:02d}{i + 1:02d}",
                    package_size=pack, min_quantity=1, unit="упаковка",
                    image_url=photo, photos=[photo],
                    description=f"{name} — свежесрезанные цветы. Длина стебля {height} см.")
                set_taxonomy(db, p, kind=kind, color=color, height=height, country=country, tax=tax)
                db.add(p)
                db.flush()
                item = models.SupplyItem(supply_id=supply.id, product_id=p.id,
                                         price=price, stock=packs,
                                         is_active=active and packs > 0)
                db.add(item)
                items.append(item)
            return items

        shelf_items = add(SHELF, shipped, True)
        add(INCOMING, incoming, False)
        db.flush()

        first = shelf_items[0]                  # тестовый заказ клиента на 2 упаковки
        packs = 2
        first.stock -= packs
        subtotal = round(first.price * 25 * packs, 2)
        order = models.Order(user_id=client.id, subtotal=subtotal,
                             discount_percent=5, total_price=round(subtotal * 0.95, 2),
                             status="Новый", comment="Демо-заказ")
        db.add(order)
        db.flush()
        db.add(models.OrderItem(order_id=order.id, product_id=first.product_id,
                                supply_item_id=first.id, product_name="Роза Freedom 60",
                                unit="упаковка", price=first.price, package_size=25,
                                quantity=packs))
        db.commit()
        print(f"Готово: {len(SHELF)} товаров в каталоге, {len(INCOMING)} в пути, "
              f"клиент client@example.ru / Client12345, админ admin@example.ru / Admin12345")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
