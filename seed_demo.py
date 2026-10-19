# -*- coding: utf-8 -*-
"""Временный скрипт: наполняет dev-БД демо-товарами."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.database import SessionLocal, Base, engine
from app import models

Base.metadata.create_all(bind=engine)

db = SessionLocal()

if db.query(models.Product).count() > 0:
    print("Уже есть товары, пропускаю")
    sys.exit(0)

supply = models.Supply(
    country="Эквадор",
    city="Кито",
    departure_date=datetime.utcnow() - timedelta(days=2),
    arrival_date=datetime.utcnow() + timedelta(days=3),
    status="В пути",
    notes="Еженедельная поставка роз",
)
db.add(supply)
db.flush()

PRODUCTS = [
    # name, image, country, length, category, package, price, stock
    ("Роза Венделла 60", "Роза Венделла 60.jpg", "Эквадор", 60, "Розы", 25, 95, 1200),
    ("Роза Эсперансе 50", "Роза Эсперансе 50 Tessa.jpg", "Эквадор", 50, "Розы", 25, 88, 800),
    ("Роза Пинк Флоид 60", "пинк флоид 60.jpg", "Эквадор", 60, "Розы", 25, 132, 450),
    ("Роза Плая Бланка 60", "плая бланка 60.jpg", "Эквадор", 60, "Розы", 25, 105, 600),
    ("Роза Мондиал 60", "мондиал 60.jpg", "Эквадор", 60, "Розы", 25, 98, 1500),
    ("Роза Проуд 50", "проуд 50.jpg", "Эквадор", 50, "Розы", 20, 90, 300),
    ("Роза Эксплорер 60", "эксплорер 60.jpg", "Эквадор", 60, "Розы", 25, 110, 90),
    ("Гвоздика микс", "Гвоздика микс.jpg", "Колумбия", 55, "Гвоздики", 20, 45, 2000),
    ("Вайолет Хилл 60", "Вайолет Хилл 60.jpg", "Голландия", 60, "Хризантемы", 10, 160, 40),
]

for name, img, country, length, cat, pack, price, stock in PRODUCTS:
    p = models.Product(
        name=name,
        country=country,
        length_cm=length,
        category=cat,
        package_size=pack,
        min_quantity=1,
        unit="упаковка",
        image_url=f"/static/{img}",
        photos=[f"/static/{img}"],
        description=f"{name} — свежесрезанные цветы премиум-качества. Длина стебля {length} см.",
    )
    db.add(p)
    db.flush()
    db.add(models.SupplyItem(
        supply_id=supply.id,
        product_id=p.id,
        price=price,
        stock=stock,
        reserved_stock=0,
        is_active=True,
    ))

db.commit()
print(f"OK: {len(PRODUCTS)} товаров, поставка #{supply.id}")
db.close()
