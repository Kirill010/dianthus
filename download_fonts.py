# Скачивает шрифты DejaVu Sans (Regular + Bold) для PDF.
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ Установи: pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "app" / "static" / "fonts"
FONTS_DIR.mkdir(parents=True, exist_ok=True)

# Прямые ссылки на TTF-файлы через CDN jsDelivr
FILES = {
    "DejaVuSans.ttf":
        "https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf":
        "https://cdn.jsdelivr.net/npm/dejavu-fonts-ttf@2.37.3/ttf/DejaVuSans-Bold.ttf",
}


def download(url: str, dest: Path) -> bool:
    try:
        print(f"⬇  {dest.name}")
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        dest.write_bytes(r.content)
        size_kb = len(r.content) / 1024
        print(f"   ✅ {size_kb:.0f} КБ")
        return True
    except Exception as e:
        print(f"   ❌ {e}")
        return False


def main() -> int:
    print(f"📁 Куда: {FONTS_DIR}\n")
    ok = fail = 0
    for name, url in FILES.items():
        if download(url, FONTS_DIR / name):
            ok += 1
        else:
            fail += 1

    print(f"\n✅ Успешно: {ok}, ❌ Ошибок: {fail}")
    if fail == 0:
        print("\n🎉 Шрифты установлены! Перезапусти сайт: python run.py")
        print("   Затем проверь PDF-счёт — русский текст будет чётким.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())