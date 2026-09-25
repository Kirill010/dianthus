# Модели БД.
import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey,
                        Integer, JSON, String, Text)
from sqlalchemy.orm import relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


ORDER_STATUSES = ["Новый", "Подтверждён", "В работе",
                  "Отправлен", "Выполнен", "Отменён"]
SUPPLY_STATUSES = ["Ожидается", "В пути", "Прибыл", "Разгружен"]

PRODUCT_CATEGORIES = [
    "Роза Эквадор", "Роза кустовая", "Хризантема",
    "Гвоздика", "Тюльпан", "Пион",
    "Горшечные", "Зелень", "Прочее",
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


def _is_valid_photo_url(value) -> bool:
    if not value or not isinstance(value, str):
        return False
    s = value.strip()
    if not s:
        return False
    return s.startswith(("http://", "https://", "/static/", "static/"))


def _normalize_url(value: str) -> str:
    s = (value or "").strip()
    if not s:
        return ""
    if s.startswith("static/"):
        return "/" + s
    return s


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(120), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(120), nullable=False)
    phone = Column(String(30), default="")
    company_name = Column(String(200), nullable=False)
    inn = Column(String(12), default="")
    city = Column(String(100), default="")
    is_admin = Column(Boolean, default=False, nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    discount_percent = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    orders = relationship("Order", back_populates="user",
                          cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user",
                                 cascade="all, delete-orphan")


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    sku = Column(String(100), default="", index=True)
    description = Column(Text, default="")
    country = Column(String(100), default="", index=True)
    length_cm = Column(Integer, default=0)
    unit = Column(String(30), default="упаковка")
    package_size = Column(Integer, default=1)
    min_quantity = Column(Integer, default=1)
    image_url = Column(String(500), default="")
    photos = Column(JSON, default=list, server_default="[]")
    category = Column(String(100), default="Прочее", index=True)
    created_at = Column(DateTime, default=_utcnow)
    supply_items = relationship("SupplyItem", back_populates="product",
                                cascade="all, delete-orphan")

    @property
    def all_photos(self) -> list:
        result = []
        raw = self.photos
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = []
        if isinstance(raw, list):
            for p in raw:
                s = _normalize_url(str(p))
                if _is_valid_photo_url(s):
                    result.append(s)
        if not result:
            main = _normalize_url(self.image_url or "")
            if _is_valid_photo_url(main):
                result = [main]
        return result


class Supply(Base):
    __tablename__ = "supplies"
    id = Column(Integer, primary_key=True, index=True)
    country = Column(String(100), nullable=False)
    departure_date = Column(DateTime, nullable=True)
    arrival_date = Column(DateTime, nullable=True)
    status = Column(String(30), default="Ожидается")
    notes = Column(Text, default="")
    is_service = Column(Boolean, default=False, nullable=False)
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
    reserved_stock = Column(Integer, default=0, nullable=False)
    is_active = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    supply = relationship("Supply", back_populates="items")
    product = relationship("Product", back_populates="supply_items")

    @property
    def pack_size(self) -> int:
        if self.product and self.product.package_size:
            return max(1, self.product.package_size)
        return 1

    @property
    def total_stems(self) -> int:
        return self.stock * self.pack_size

    @property
    def available_stock(self) -> int:
        """fix #33: защита от reserved_stock > stock."""
        reserved = self.reserved_stock or 0
        if reserved > self.stock:
            # логируем, но не падаем — БД сама не должна так делать
            import logging
            logging.getLogger(__name__).warning(
                "SupplyItem #%s: reserved_stock=%s > stock=%s",
                self.id, reserved, self.stock,
            )
        return max(0, self.stock - reserved)

    @property
    def available_stems(self) -> int:
        return self.available_stock * self.pack_size

    @property
    def price_per_pack(self) -> float:
        return self.price * self.pack_size

    @property
    def is_available(self) -> bool:
        return self.available_stock > 0 and self.is_active


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    subtotal = Column(Float, default=0, nullable=False)
    discount_percent = Column(Float, default=0, nullable=False)
    total_price = Column(Float, default=0)
    status = Column(String(30), default="Новый")
    comment = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order",
                         cascade="all, delete-orphan")

    @property
    def discount_amount(self) -> float:
        return max(0.0, self.subtotal - self.total_price)


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
    package_size = Column(Integer, default=1)
    quantity = Column(Integer, default=1)
    order = relationship("Order", back_populates="items")

    @property
    def total_stems(self) -> int:
        return self.quantity * (self.package_size or 1)

    @property
    def subtotal(self) -> float:
        return self.price * (self.package_size or 1) * self.quantity


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)
    text = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User", back_populates="notifications")
    order = relationship("Order")


class Preorder(Base):
    __tablename__ = "preorders"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    supply_item_id = Column(Integer, ForeignKey("supply_items.id"), nullable=True)
    quantity = Column(Integer, nullable=False)
    is_fulfilled = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    user = relationship("User")
    product = relationship("Product")
    supply_item = relationship("SupplyItem")