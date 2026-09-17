"""
Скачивает Bootstrap и Font Awesome в app/static/vendor/.

Запуск:
    python download_vendor.py

Если что-то не скачается — откройте ссылку в браузере и сохраните файл вручную.
"""
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ Нужен пакет requests. Установите: pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
VENDOR = BASE_DIR / "app" / "static" / "vendor"

# (URL, куда сохранить относительно app/static/vendor/)
FILES = [
    ("https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css",
     "bootstrap.min.css"),
    ("https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js",
     "bootstrap.bundle.min.js"),
    ("https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css",
     "fontawesome/css/all.min.css"),
    ("https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-solid-900.woff2",
     "fontawesome/webfonts/fa-solid-900.woff2"),
    ("https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-regular-400.woff2",
     "fontawesome/webfonts/fa-regular-400.woff2"),
    ("https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/webfonts/fa-brands-400.woff2",
     "fontawesome/webfonts/fa-brands-400.woff2"),
]


def download(url: str, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"⬇  {url}")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        print(f"   ✅ {len(r.content):,} байт → {dest.relative_to(BASE_DIR)}")
        return True
    except Exception as e:
        print(f"   ❌ ошибка: {e}")
        return False


def main() -> int:
    print(f"📁 Куда: {VENDOR}\n")
    ok = fail = 0
    for url, rel in FILES:
        if download(url, VENDOR / rel):
            ok += 1
        else:
            fail += 1
    print(f"\n✅ Успешно: {ok}, ❌ Ошибок: {fail}")
    if fail:
        print("\n⚠️  Часть файлов не скачалась — их CDN блокируется вашей сетью.")
        print("   Откройте ссылки в браузере (или через мобильный интернет),")
        print("   сохраните файлы вручную в папки, указанные выше.")
        return 1
    print("\n🎉 Готово. Теперь перезапустите сервер: python run.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())