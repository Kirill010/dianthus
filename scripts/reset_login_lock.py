# Снять блокировку входа (rate-limit бан) для email или всех аккаунтов.
# Использование:
#   docker compose exec app python scripts/reset_login_lock.py EMAIL
#   docker compose exec app python scripts/reset_login_lock.py --all
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    if len(sys.argv) != 2:
        print("Использование: python scripts/reset_login_lock.py EMAIL|--all")
        return 2

    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        print("❌ REDIS_URL не задан (запускайте внутри контейнера app)")
        return 1

    import redis  # из зависимостей приложения

    r = redis.from_url(redis_url, decode_responses=True)
    try:
        r.ping()
    except Exception as e:
        print(f"❌ Redis недоступен: {e}")
        return 1

    target = sys.argv[1].strip().lower()
    if target == "--all":
        keys = []
        for pattern in ("ll:*", "lf:*"):
            keys.extend(r.scan_iter(match=pattern, count=500))
        if keys:
            r.delete(*keys)
        print(f"✅ Снято блокировок/счётчиков: {len(keys)}")
        return 0

    deleted = 0
    for key in (f"ll:{target}", f"lf:{target}"):
        deleted += r.delete(key)
    if deleted:
        print(f"✅ Бан снят для {target} (удалено ключей: {deleted})")
    else:
        print(f"ℹ️ Ключей для {target} нет — блокировки не было")
    return 0


if __name__ == "__main__":
    sys.exit(main())
