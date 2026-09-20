"""Скачивает шрифты DejaVu Sans для PDF (поддержка кириллицы).

Запуск:  python download_fonts.py
"""
import io
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌ Установи: pip install requests")
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent
FONTS_DIR = BASE_DIR / "app" / "static" / "fonts"
FONTS_DIR.mkdir(parents=True, exist_ok=True)

# Архив с официального SourceForge (содержит все TTF-файлы DejaVu Sans)
# Ссылка проверена и работает.
ARCHIVE_URL = (
    "https://master.dl.sourceforge.net/project/dejavu/dejavu/2.37/"
    "dejavu-sans-ttf-2.37.zip?viasf=1"
)

# Какие файлы достаем из архива (путь внутри архива)
NEEDED_FILES = {
    "dejavu-sans-ttf-2.37/ttf/DejaVuSans.ttf": "DejaVuSans.ttf",
    "dejavu-sans-ttf-2.37/ttf/DejaVuSans-Bold.ttf": "DejaVuSans-Bold.ttf",
}


def download_and_extract() -> bool:
    """Скачивает архив и извлекает TTF-файлы в FONTS_DIR."""
    print(f"⬇  Скачиваю архив: {ARCHIVE_URL}")
    try:
        r = requests.get(ARCHIVE_URL, timeout=120, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        print(f"   ❌ Не удалось скачать архив: {e}")
        return False

    print(f"   ✅ Архив скачан ({len(r.content) / 1024:.0f} КБ)")
    print("📦 Извлекаю шрифты...")

    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            # Проверяем, какие файлы есть в архиве
            all_names = z.namelist()

            extracted = 0
            for inner_path, target_name in NEEDED_FILES.items():
                # Ищем файл (иногда путь внутри архива может отличаться)
                found = None
                for name in all_names:
                    if name.endswith(Path(inner_path).name):
                        found = name
                        break

                if not found:
                    print(f"   ⚠️  Файл {target_name} не найден в архиве")
                    continue

                data = z.read(found)
                target_path = FONTS_DIR / target_name
                target_path.write_bytes(data)
                print(f"   ✅ {target_name} ({len(data) / 1024:.0f} КБ)")
                extracted += 1

            if extracted == 0:
                print("   ❌ Ничего не извлечено. Возможно, структура архива изменилась.")
                return False

        return True

    except zipfile.BadZipFile:
        print("   ❌ Скачанный файл не является ZIP-архивом.")
        print("      Возможно, SourceForge вернул страницу-заглушку.")
        return False
    except Exception as e:
        print(f"   ❌ Ошибка при извлечении: {e}")
        return False


def main() -> int:
    print(f"📁 Куда: {FONTS_DIR}\n")

    if download_and_extract():
        print("\n🎉 Шрифты установлены!")
        print("   Теперь перезапусти сайт: python run.py")
        print("   И попробуй скачать PDF-счёт — русский текст будет отображаться.")
        return 0
    else:
        print("\n❌ Не удалось установить шрифты автоматически.")
        print("\n💡 Что делать:")
        print("   1. Скачай вручную: " + ARCHIVE_URL)
        print("   2. Распакуй архив")
        print("   3. Найди файлы DejaVuSans.ttf и DejaVuSans-Bold.ttf")
        print(f"   4. Положи их в папку: {FONTS_DIR}")
        return 1


if __name__ == "__main__":
    sys.exit(main())