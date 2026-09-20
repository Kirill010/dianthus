"""Скачивает шрифты DejaVu Sans для PDF (поддержка кириллицы).

Запуск:  python download_fonts.py
"""
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

# Прямые ссылки на TTF с GitHub (DejaVu — свободная лицензия)
FILES = [
    (
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/"
        "ttf/DejaVuSans.ttf",
        "DejaVuSans.ttf",
    ),
    (
        "https://github.com/dejavu-fonts/dejavu-fonts/raw/master/"
        "ttf/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
    ),
]


def download(url: str, dest: Path) -> bool:
    try:
        print(f"⬇  {url}")
        r = requests.get(url, timeout=60, allow_redirects=True)
        r.raise_for_status()
        dest.write_bytes(r.content)
        size_kb = len(r.content) / 1024
        print(f"   ✅ {size_kb:.0f} КБ → {dest.name}")
        return True
    except Exception as e:
        print(f"   ❌ {e}")
        return False


def main() -> int:
    print(f"📁 Куда: {FONTS_DIR}\n")
    ok = fail = 0
    for url, name in FILES:
        if download(url, FONTS_DIR / name):
            ok += 1
        else:
            fail += 1

    print(f"\n✅ Успешно: {ok}, ❌ Ошибок: {fail}")
    if fail == 0:
        print("\n🎉 Шрифты установлены!")
        print("   Теперь перезапусти сайт: python run.py")
        print("   И попробуй скачать PDF-счёт — русский текст будет отображаться.")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())