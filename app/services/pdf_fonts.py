"""Регистрация шрифта с поддержкой кириллицы для PDF.

xhtml2pdf по умолчанию использует Helvetica — в ней НЕТ русских букв.
Поэтому мы подключаем DejaVu Sans (есть везде, поддерживает кириллицу).
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Папка для наших шрифтов
FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
FONTS_DIR.mkdir(parents=True, exist_ok=True)

# Имя шрифта, под которым будем использовать в HTML/CSS
FONT_NAME = "DejaVuSans"
FONT_BOLD = "DejaVuSans-Bold"

_font_registered = False


def _find_dejavu_files() -> dict | None:
    """
    Ищет файлы DejaVu Sans в системе или в папке проекта.

    Возвращает:
        {
            "regular": Path,
            "bold": Path,
        }
        или None, если файлы не найдены.
    """
    # Кандидаты — файлы DejaVu в проекте (приоритет)
    candidates = {
        "regular": [
            FONTS_DIR / "DejaVuSans.ttf",
            FONTS_DIR / "DejaVuSans-Regular.ttf",
        ],
        "bold": [
            FONTS_DIR / "DejaVuSans-Bold.ttf",
        ],
    }

    # Системные пути (Linux)
    system_paths = [
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/dejavu"),
        Path("/usr/local/share/fonts"),
    ]
    for sp in system_paths:
        if sp.exists():
            candidates["regular"].append(sp / "DejaVuSans.ttf")
            candidates["bold"].append(sp / "DejaVuSans-Bold.ttf")

    # Системные пути (Windows)
    win_paths = [
        Path("C:/Windows/Fonts"),
        Path.home() / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts",
    ]
    for wp in win_paths:
        if wp.exists():
            candidates["regular"].append(wp / "DejaVuSans.ttf")
            candidates["regular"].append(wp / "arial.ttf")  # fallback
            candidates["bold"].append(wp / "DejaVuSans-Bold.ttf")
            candidates["bold"].append(wp / "arialbd.ttf")

    # Ищем первый существующий файл
    result = {}
    for kind, paths in candidates.items():
        for p in paths:
            if p.exists():
                result[kind] = p
                break
        else:
            logger.warning("Не найден файл шрифта: %s", kind)
            return None

    return result


def register_pdf_fonts() -> str:
    """
    Регистрирует шрифт DejaVu Sans в xhtml2pdf.

    Возвращает имя шрифта (использовать в CSS как font-family).
    Если шрифт не найден — возвращает "Helvetica" (будет с квадратиками).
    """
    global _font_registered
    if _font_registered:
        return FONT_NAME

    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        logger.error("reportlab не установлен")
        return "Helvetica"

    files = _find_dejavu_files()
    if not files:
        logger.warning(
            "⚠️  DejaVu Sans не найден. "
            "Скачай его и положи в app/static/fonts/. "
            "Или установи: pip install fonttools "
            "и запусти: python download_fonts.py"
        )
        return "Helvetica"

    try:
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(files["regular"])))
        pdfmetrics.registerFont(TTFont(FONT_BOLD, str(files["bold"])))

        # Регистрируем семейство (regular + bold)
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily(
            FONT_NAME,
            normal=FONT_NAME,
            bold=FONT_BOLD,
            italic=FONT_NAME,
            boldItalic=FONT_BOLD,
        )

        _font_registered = True
        logger.info("✅ Шрифт DejaVu Sans зарегистрирован для PDF")
        return FONT_NAME

    except Exception as e:
        logger.exception("Ошибка регистрации шрифта: %s", e)
        return "Helvetica"