# Модели БД.
import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Index,
                        Integer, JSON, String, Text)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from .database import Base
from .services.pricing import line_total, money


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


ORDER_STATUSES = ["Новый", "Подтверждён", "В работе",
                  "Отправлен", "Выполнен", "Отменён"]
SUPPLY_STATUSES = ["Ожидается", "В пути", "Прибыл", "Разгружен"]

# Что кладётся в справочники при первом запуске (дальше их ведёт владелец и 1С).
# Фильтры каталога — «вид», «цвет», «рост», «страна» — это ОТДЕЛЬНЫЕ ТАБЛИЦЫ
# (kinds, colors, heights, countries); товар ссылается на них по id.
DEFAULT_KINDS = [
    "Роза", "Роза кустовая", "Хризантема", "Гвоздика", "Тюльпан", "Пион",
    "Горшечные", "Зелень", "Прочее",
]
# (название, цвет кружка в фильтре)
DEFAULT_COLORS = [
    ("Белый", "#ffffff"), ("Кремовый", "#f5ecd2"), ("Жёлтый", "#f4d03f"),
    ("Оранжевый", "#f08a24"), ("Персиковый", "#f7b896"), ("Розовый", "#f1a7c1"),
    ("Красный", "#c0392b"), ("Бордовый", "#7b1e2b"), ("Сиреневый", "#b39ddb"),
    ("Фиолетовый", "#6c3483"), ("Голубой", "#8ecae6"), ("Зелёный", "#52a35a"),
    ("Микс", ""),
]
DEFAULT_HEIGHTS = [40, 50, 60, 70, 80, 90, 100]
DEFAULT_COUNTRIES = [
    "Эквадор", "Колумбия", "Кения", "Голландия", "Россия", "Эфиопия", "Израиль",
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
    # Контрагент для 1С: адрес и идентификатор в 1С (пусто, пока 1С не сообщила его)
    address = Column(String(300), default="")
    external_id = Column(String(64), default="", index=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_approved = Column(Boolean, default=False, nullable=False)
    discount_percent = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    orders = relationship("Order", back_populates="user",
                          cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user",
                                 cascade="all, delete-orphan")
    totp_secret = Column(String(64), default="", nullable=False)
    # Когда клиент дал согласие на обработку персональных данных (152-ФЗ),
    # какой редакции документа и с какого IP. Полная история — в consent_log.
    consent_at = Column(DateTime, nullable=True)
    consent_version = Column(String(32), default="")
    consent_ip = Column(String(45), default="")
    # Отдельное, необязательное согласие на рекламу (ст. 18 закона «О рекламе»):
    # без него клиенту нельзя звонить и писать с предложениями — только по заказам.
    marketing_consent = Column(Boolean, default=False, nullable=False)
    marketing_consent_at = Column(DateTime, nullable=True)
    # Сотрудник без полных прав: заказы, клиенты, рекламации (но не ключи 1С,
    # не юридические тексты, не справочник и поставки)
    is_manager = Column(Boolean, default=False, nullable=False)
    # Внутренняя заметка менеджеров о клиенте (клиент её не видит)
    staff_note = Column(Text, default="")
    last_login_at = Column(DateTime, nullable=True)
    # Когда данные клиента обезличены по его запросу (удаление/отзыв согласия)
    anonymized_at = Column(DateTime, nullable=True)

    @property
    def is_staff(self) -> bool:
        return bool(self.is_admin or self.is_manager)


# ═══════════════════════════════════════════════════════════
# Справочники-фильтры: каждый — отдельная таблица с id
# ═══════════════════════════════════════════════════════════

class _DictionaryMixin:
    """Общие поля справочника: id на сайте, id в 1С, порядок показа."""
    id = Column(Integer, primary_key=True, index=True)
    external_id = Column(String(64), default="", index=True)      # идентификатор в 1С
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class Kind(_DictionaryMixin, Base):
    """Вид цветка: роза, хризантема, гвоздика…"""
    __tablename__ = "kinds"
    name = Column(String(100), nullable=False, unique=True)


class Color(_DictionaryMixin, Base):
    """Цвет: белый, жёлтый… (hex — цвет кружка в фильтре, необязательно)."""
    __tablename__ = "colors"
    name = Column(String(100), nullable=False, unique=True)
    hex = Column(String(7), default="")


class Height(_DictionaryMixin, Base):
    """Рост (длина стебля), см: 50, 60, 70, 80…"""
    __tablename__ = "heights"
    cm = Column(Integer, nullable=False, unique=True)

    @property
    def name(self) -> str:
        return f"{self.cm} см"


class Country(_DictionaryMixin, Base):
    """Страна происхождения."""
    __tablename__ = "countries"
    name = Column(String(100), nullable=False, unique=True)


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    sku = Column(String(100), default="", index=True)
    # Штрихкод (EAN-13 и т. п.): по нему товар тоже находится при обмене
    barcode = Column(String(64), default="", index=True)
    # Идентификатор товара В 1С (GUID или код номенклатуры) — поле "external_id"
    # в JSON-обмене. По нему товары сопоставляются при синхронизации, поэтому
    # переименование в 1С больше не создаёт дубль. Пусто = заведён вручную.
    external_id = Column(String(64), default="", index=True)
    description = Column(Text, default="")
    unit = Column(String(30), default="упаковка")
    package_size = Column(Integer, default=1)
    min_quantity = Column(Integer, default=1)
    image_url = Column(String(500), default="")
    photos = Column(
        JSON().with_variant(JSONB(), "postgresql"),
        default=list,
        server_default="[]",
        nullable=False,
    )
    # Классификация — ссылки на справочники (kinds, colors, heights, countries).
    # Раньше здесь лежали текстовые country / category / length_cm: колонки в
    # старых базах остаются, но код их больше не читает (значения переносит
    # миграция «taxonomy_v1»).
    kind_id = Column(Integer, ForeignKey("kinds.id"), nullable=True, index=True)
    color_id = Column(Integer, ForeignKey("colors.id"), nullable=True, index=True)
    height_id = Column(Integer, ForeignKey("heights.id"), nullable=True, index=True)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=True, index=True)
    # selectin, а не joined: с «joined» запрос с блокировкой строки товара (FOR UPDATE) на
    # PostgreSQL падал («нельзя применить к внешнему соединению»), а каждая выборка товаров
    # тянула четыре лишних JOIN.
    kind_ref = relationship("Kind", lazy="selectin")
    color_ref = relationship("Color", lazy="selectin")
    height_ref = relationship("Height", lazy="selectin")
    country_ref = relationship("Country", lazy="selectin")
    created_at = Column(DateTime, default=_utcnow)
    supply_items = relationship("SupplyItem", back_populates="product",
                                cascade="all, delete-orphan")

    # Названия из справочников для шаблонов («Роза», «Красный», 60, «Эквадор»);
    # пусто, если значение не указано. Менять значения — через app.services.taxonomy.
    @property
    def kind_name(self) -> str:
        return self.kind_ref.name if self.kind_ref is not None else ""

    @property
    def color_name(self) -> str:
        return self.color_ref.name if self.color_ref is not None else ""

    @property
    def height_cm(self) -> int:
        return self.height_ref.cm if self.height_ref is not None else 0

    @property
    def country_name(self) -> str:
        return self.country_ref.name if self.country_ref is not None else ""

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
    city = Column(String(100), default="")
    departure_date = Column(DateTime, nullable=True)
    arrival_date = Column(DateTime, nullable=True)
    status = Column(String(30), default="Ожидается")
    notes = Column(Text, default="")
    is_service = Column(Boolean, default=False, nullable=False)
    # Поставка, которую ведёт 1С (инвойс «в пути»): номер/идентификатор документа
    # в 1С. Пусто — поставка заведена вручную. Такие поставки обновляются только
    # обменом, не разгружаются автоматически и вручную (остатки ведёт 1С).
    external_id = Column(String(64), default="", index=True)
    # Номер инвойса для людей («00000624»); у ручной поставки пусто
    number = Column(String(64), default="")
    external_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    items = relationship("SupplyItem", back_populates="supply",
                         cascade="all, delete-orphan")

    @property
    def is_from_1c(self) -> bool:
        return bool(self.external_id)

    @property
    def invoice_no(self) -> str:
        """Номер инвойса в 1С («00000624»); у ручной поставки пусто."""
        return (self.number or self.external_id or "") if self.external_id else ""

    @property
    def label(self) -> str:
        """«№ 00000624 от 29.10.2026» — как поставка названа в 1С; у ручной поставки пусто."""
        if not self.external_id:
            return ""
        # у «в пути» дата — плановая: если она уже прошла (машина задерживается), её не показываем
        in_transit = self.status in ("Ожидается", "В пути")
        show_date = bool(self.arrival_date) and (not in_transit or self.arrival_known)
        date = f" от {self.arrival_date.strftime('%d.%m.%Y')}" if show_date else ""
        return f"№ {self.invoice_no}{date}"

    @property
    def arrival_known(self) -> bool:
        """Дата прибытия есть и ещё не прошла. Машина, которая задерживается, свою дату «теряет»:
        в 1С документ «в пути» остаётся с плановой датой, пока его не оприходуют."""
        from .timeutils import shop_tz
        return bool(self.arrival_date) and self.arrival_date.date() >= datetime.now(shop_tz()).date()

    @property
    def arrival_text(self) -> str:
        """Дата прибытия для клиентов: «29.10.2026» или «дата уточняется» (нет даты или она прошла)."""
        return self.arrival_date.strftime("%d.%m.%Y") if self.arrival_known else "дата уточняется"

    @property
    def is_active(self) -> bool:
        return self.status in ("Ожидается", "В пути", "Прибыл")

    @property
    def active_items_count(self) -> int:
        return sum(1 for i in self.items if i.is_active)

    @property
    def location(self) -> str:
        country = (self.country or "").strip()
        city = (self.city or "").strip()
        if country and city:
            return f"{country}, {city}"
        return country or city or ""


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
    supply = relationship("Supply", back_populates="items", lazy="selectin")
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
        reserved = self.reserved_stock or 0
        if reserved > self.stock:
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
    # Обмен с 1С: когда 1С забрала заказ (NULL = ещё не забирала) и номер
    # документа «Заказ клиента» в самой 1С.
    exported_at = Column(DateTime, nullable=True, index=True)
    onec_number = Column(String(64), default="")
    # Одноразовый ключ формы оформления. Уникальный индекс гарантирует, что
    # двойная отправка формы (два быстрых клика, повтор запроса на плохой
    # сети) создаст только ОДИН заказ — вторая вставка упадёт на уникальности.
    order_token = Column(String(64), nullable=True)
    # К какой дате клиенту нужны цветы (необязательно). От неё считаются
    # сроки предзаказа и планируется сборка.
    desired_date = Column(DateTime, nullable=True)
    # Как клиент получит заказ: pickup — самовывоз, delivery — доставка по адресу
    delivery_method = Column(String(16), default="")
    delivery_address = Column(String(500), default="")
    # Внутренняя заметка сотрудников (клиент её не видит)
    staff_note = Column(Text, default="")
    __table_args__ = (Index("uq_orders_order_token", "order_token", unique=True),)
    user = relationship("User", back_populates="orders")
    items = relationship("OrderItem", back_populates="order",
                         cascade="all, delete-orphan")

    @property
    def discount_amount(self) -> float:
        return money(max(0.0, (self.subtotal or 0) - (self.total_price or 0)))


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
    # позиция поставки, из которой собран товар: по ней видно, из какой партии (инвойса) брать
    supply_item = relationship("SupplyItem", lazy="selectin")

    @property
    def supply_label(self) -> str:
        """«№ 00000624 от 29.10.2026» — из какой поставки 1С взят товар (иначе пусто)."""
        si = self.supply_item
        return si.supply.label if si is not None and si.supply is not None else ""

    @property
    def total_stems(self) -> int:
        return self.quantity * (self.package_size or 1)

    @property
    def subtotal(self) -> float:
        return line_total(self.price, self.package_size, self.quantity)


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


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)   # order_status, user_approve...
    entity = Column(String(64), default="")                    # order / user / product
    entity_id = Column(Integer, nullable=True)
    details = Column(Text, default="")                          # было → стало
    ip = Column(String(45), default="")
    created_at = Column(DateTime, default=_utcnow, index=True)
    user = relationship("User")


# ═══════════════════════════════════════════════════════════
# Согласия, юридические документы, запросы на удаление данных (152-ФЗ)
# ═══════════════════════════════════════════════════════════

class ConsentLog(Base):
    """Неизменяемый журнал согласий и отзывов: кто, когда, на какую редакцию
    документа и с какого адреса. Нужен, чтобы доказать согласие проверяющим."""
    __tablename__ = "consent_log"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    kind = Column(String(16), nullable=False)          # pd | marketing
    granted = Column(Boolean, nullable=False)          # True — дал, False — отозвал
    doc_version = Column(String(32), default="")
    source = Column(String(40), default="")            # registration | profile | admin
    ip = Column(String(45), default="")
    user_agent = Column(String(255), default="")
    created_at = Column(DateTime, default=_utcnow, index=True)
    user = relationship("User")


class LegalDocument(Base):
    """Юридический текст сайта (политика, согласия, условия) — правится в админке."""
    __tablename__ = "legal_documents"
    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(40), unique=True, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default="")
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime, default=_utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    revisions = relationship("LegalDocumentRevision", back_populates="document",
                             cascade="all, delete-orphan",
                             order_by="LegalDocumentRevision.version.desc()")


class LegalDocumentRevision(Base):
    """Прежние редакции: по версии, на которую согласился клиент, всегда можно
    восстановить, какой текст он видел."""
    __tablename__ = "legal_document_revisions"
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("legal_documents.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    title = Column(String(200), default="")
    body = Column(Text, default="")
    saved_at = Column(DateTime, default=_utcnow)
    saved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    document = relationship("LegalDocument", back_populates="revisions")


class SiteSetting(Base):
    """Настройки, которые владелец меняет в админке, а не в файле .env
    (условия оплаты, сроки предзаказа, отметки юридического списка дел)."""
    __tablename__ = "site_settings"
    key = Column(String(80), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=_utcnow)
    updated_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)


class PasswordReset(Base):
    """Одноразовая ссылка «забыли пароль». В базе — только хэш токена."""
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=_utcnow)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    ip = Column(String(45), default="")


DATA_REQUEST_KINDS = {"delete": "Удаление данных / отзыв согласия", "export": "Выгрузка данных"}
DATA_REQUEST_STATUSES = ["Новая", "Выполнена", "Отклонена"]


class DataRequest(Base):
    """Обращение клиента о его персональных данных: закон даёт на ответ 10 рабочих дней."""
    __tablename__ = "data_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    email_snapshot = Column(String(120), default="")   # кто обращался — даже после обезличивания
    kind = Column(String(16), nullable=False)          # delete | export
    status = Column(String(16), nullable=False, default="Новая", index=True)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow, index=True)
    handled_at = Column(DateTime, nullable=True)
    handled_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    user = relationship("User", foreign_keys=[user_id])


# ═══════════════════════════════════════════════════════════
# Обмен с 1С: ключи доступа и журнал
# ═══════════════════════════════════════════════════════════

class ApiKey(Base):
    """Ключ доступа для 1С. В базе — только хэш: сам ключ показывается один раз."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    key_prefix = Column(String(16), default="")        # первые символы — чтобы узнать ключ в списке
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    allowed_ips = Column(String(500), default="")      # пусто = с любого адреса
    created_at = Column(DateTime, default=_utcnow)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    last_used_ip = Column(String(45), default="")
    is_active = Column(Boolean, default=True, nullable=False)


class ExchangeLog(Base):
    """Журнал обращений 1С к сайту: владелец видит, работает ли обмен."""
    __tablename__ = "exchange_log"
    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=True)
    auth_kind = Column(String(16), default="")         # key | password | none
    method = Column(String(8), default="")
    endpoint = Column(String(120), default="", index=True)
    status_code = Column(Integer, default=0)
    ok = Column(Boolean, default=False, index=True)
    ip = Column(String(45), default="")
    duration_ms = Column(Integer, default=0)
    summary = Column(String(500), default="")
    repeat_count = Column(Integer, default=1)          # одинаковые неудачи за час схлопываются


# ═══════════════════════════════════════════════════════════
# Рекламации (жалобы на качество)
# ═══════════════════════════════════════════════════════════

CLAIM_STATUSES = ["Новая", "В работе", "Решена", "Отклонена"]
CLAIM_URGENCY = ["обычная", "высокая"]


class Claim(Base):
    """Жалоба клиента на качество. Поля повторяют карточку рекламации владельца."""
    __tablename__ = "claims"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    product_name = Column(String(200), default="")
    country = Column(String(100), default="")
    affected_qty = Column(Integer, default=0)           # сколько испорчено
    total_qty = Column(Integer, default=0)              # из скольких
    unit = Column(String(20), default="шт")
    received_at = Column(DateTime, nullable=True)       # когда получили товар
    opened_at = Column(DateTime, nullable=True)         # когда вскрыли коробку
    storage_notes = Column(Text, default="")            # вода, температура, как хранили
    description = Column(Text, default="")              # слова клиента как есть
    expectation = Column(Text, default="")              # чего хочет клиент
    status = Column(String(20), nullable=False, default="Новая", index=True)
    urgency = Column(String(16), default="обычная")
    staff_comment = Column(Text, default="")            # внутреннее; предположения помечать
    owner_decision = Column(Text, default="")           # решение владельца
    reply_to_client = Column(Text, default="")          # ответ, который видит клиент
    created_at = Column(DateTime, default=_utcnow, index=True)
    updated_at = Column(DateTime, default=_utcnow)
    resolved_at = Column(DateTime, nullable=True)
    user = relationship("User")
    order = relationship("Order")
    photos = relationship("ClaimPhoto", back_populates="claim",
                          cascade="all, delete-orphan",
                          order_by="ClaimPhoto.id")

    @property
    def number(self) -> str:
        return f"РЕК-{self.id:04d}" if self.id else "РЕК-—"

    @property
    def is_late(self) -> bool:
        """С момента получения прошло больше суток — по правилам не отказываем,
        а передаём владельцу с пометкой."""
        if not (self.received_at and self.created_at):
            return False
        return (self.created_at - self.received_at).total_seconds() > 24 * 3600


class ClaimPhoto(Base):
    __tablename__ = "claim_photos"
    id = Column(Integer, primary_key=True, index=True)
    claim_id = Column(Integer, ForeignKey("claims.id"), nullable=False, index=True)
    url = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=_utcnow)
    claim = relationship("Claim", back_populates="photos")