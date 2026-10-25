# app/services/api_keys.py
"""Ключи доступа для 1С («API-ключи»).

Зачем. Раньше обмен работал по одному логину/паролю из файла .env — менять его
мог только тот, у кого есть доступ к серверу. Теперь владелец сам создаёт ключ в
админке («Интеграция»), отдаёт его программисту 1С, а когда программист
закончил работу или ключ засветился — одним нажатием отключает.

Как устроено.
  * ключ выглядит как  dnt_3f9a…  (dnt_ + 40 символов 0-9a-f, 160 бит случайности);
  * в базе хранится только SHA-256 от ключа: даже при утечке базы ключи не
    восстановить (ключ случайный и длинный, поэтому соль и медленный хэш не нужны);
  * целиком ключ показывается ОДИН раз — при создании;
  * ключ можно привязать к IP-адресам сервера 1С — тогда с другого адреса он не сработает.
"""
import hashlib
import ipaddress
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import ApiKey

KEY_PREFIX = "dnt_"
KEY_RE = re.compile(r"^dnt_[0-9a-f]{40}$")
LAST_USED_UPDATE_EVERY = timedelta(seconds=60)      # не пишем в базу на каждый запрос


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_key(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def looks_like_key(value: str) -> bool:
    return bool(KEY_RE.match((value or "").strip()))


def parse_ip_list(text: str) -> list[str]:
    """«1.2.3.4, 10.0.0.0/24» → ['1.2.3.4', '10.0.0.0/24']. Бросает ValueError при мусоре."""
    result = []
    for part in re.split(r"[\s,;]+", (text or "").strip()):
        if not part:
            continue
        try:
            net = ipaddress.ip_network(part, strict=False)
        except ValueError:
            raise ValueError(f"«{part[:40]}» — это не IP-адрес")
        result.append(str(net.network_address) if net.num_addresses == 1 else str(net))
    return result


def ip_allowed(key: ApiKey, ip: str) -> bool:
    """Пустой список = можно с любого адреса."""
    allowed = [p for p in re.split(r"[\s,;]+", key.allowed_ips or "") if p]
    if not allowed:
        return True
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    for item in allowed:
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def create_key(db: Session, name: str, allowed_ips: str = "", admin=None) -> tuple[ApiKey, str]:
    """Создаёт ключ. Возвращает (запись, ключ целиком) — ключ больше нигде не хранится."""
    raw = KEY_PREFIX + secrets.token_hex(20)
    key = ApiKey(
        name=(name or "").strip()[:100] or "Ключ для 1С",
        key_prefix=raw[:8],
        key_hash=hash_key(raw),
        allowed_ips=", ".join(parse_ip_list(allowed_ips)),
        created_by_id=getattr(admin, "id", None),
    )
    db.add(key)
    db.commit()
    return key, raw


def find_key(db: Session, raw: str) -> ApiKey | None:
    """Действующий ключ по его значению (или None)."""
    raw = (raw or "").strip()
    if not looks_like_key(raw):
        return None
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(raw)).first()
    if key is None or not key.is_active:
        return None
    return key


def touch(db: Session, key: ApiKey, ip: str) -> None:
    """Отмечает, что ключ использовали (не чаще раза в минуту)."""
    now = _now()
    if key.last_used_at and now - key.last_used_at < LAST_USED_UPDATE_EVERY \
            and key.last_used_ip == ip[:45]:
        return
    key.last_used_at = now
    key.last_used_ip = ip[:45]
    db.commit()


def active_keys_count(db: Session) -> int:
    return db.query(ApiKey).filter(ApiKey.is_active.is_(True)).count()
