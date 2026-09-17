"""
Модели БД (5 таблиц).

  User       — оптовый покупатель (или админ)
  Supply     — рейс машины из страны-производителя
  Product    — товар на складе
  Order      — заказ клиента
  OrderItem  — позиция внутри заказа

Магазин ТОЛЬКО оптовый: у товара одна цена.
"""
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, String, Text)
from sqlalchemy.orm import relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


ORDER_STATUSES = ["Новый", "Подтверждён", "В работе",
                  "Отправлен", "Выполнен", "Отменён"]

SUPPLY_STATUSES = ["Ожидается", "В пути", "Прибыл", "Разгружен"]

PRODUCT_CATEGORIES = [
    "Роза Эквадор",
    "Роза кустовая",
    "Хризантема",
    "Гвоздика",
    "Тюльпан",
    "Пион",
    "Горшечные",
    "Зелень",
    "Прочее",
]

QUANTITY_LEVELS = [
    ("available", "В наличии"),
    ("big", "Много"),
    ("medium", "Средне"),
    ("small", "Мало"),
    ("out", "Нет"),
]

QUANTITY_BIG_MIN = 200
QUANTITY_MEDIUM_MIN = 50
QUANTITY_SMALL_MIN = 1


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(120), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(120), nullable=False)
    phone = Column(String(30), default="")
    company_name = Column(String(200), nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    orders = relationship("Order", back_populates="user")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    description = Column(Text, default="")
    country = Column(String(100), default="", index=True)
    length_cm = Column(Integer, default=0)
    unit = Column(String(30), default="упаковка")
    package_size = Column(Integer, default=1)
    min_quantity = Column(Integer, default=1)
    image_url = Column(String(500), default="")
    category = Column(String(100), default="Прочее", index=True)
    created_at = Column(DateTime, default=_utcnow)

    supply_items = relationship("SupplyItem", back_populates="product",
                                cascade="all, delete-orphan")


class Supply(Base):
    __tablename__ = "supplies"
    id = Column(Integer, primary_key=True, index=True)
    country = Column(String(100), nullable=False)
    departure_date = Column(DateTime, nullable=True)
    arrival_date = Column(DateTime, nullable=True)
    status = Column(String(30), default="Ожидается")
    notes = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)

    items = relationship("SupplyItem", back_populates="supply",
                         cascade="all, delete-orphan")

    @property
    def is_active(self) -> bool:
        return self.status in ("Ожидается", "В пути", "Прибыл")

    @property
    def active_items_count(self) -> int:
        return sum(1 for i in self.items if i.is_active)


class SupplyItem(Base):
    __tablename__ = "supply_items"
    id = Column(Integer, primary_key=True, index=True)
    supply_id = Column(Integer, ForeignKey("supplies.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float, nullable=False, default=0)
    stock = Column(Integer, default=0)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)

    supply = relationship("Supply", back_populates="items")
    product = relationship("Product", back_populates="supply_items")

    @property
    def total_stems(self) -> int:
        return self.stock * (self.product.package_size if self.product else 1)

    @property
    def is_available(self) -> bool:
        return self.stock > 0 and self.is_active


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    total_price = Column(Float, default=0)
    status = Column(String(30), default="Новый")
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)

    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order",
                         cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True)
    supply_item_id = Column(Integer, ForeignKey("supply_items.id"),
                            nullable=True)
    product_name = Column(String(200), nullable=False)
    unit = Column(String(30), default="")
    price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)

    order = relationship("Order", back_populates="items")

    @property
    def subtotal(self) -> float:
        return self.price * self.quantity