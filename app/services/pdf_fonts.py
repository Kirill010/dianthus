# Шрифт для PDF — правильная регистрация через @font-face + link_callback.
#
# xhtml2pdf НЕ читает file:// URI напрямую.
# Нужно использовать относительный путь + link_callback в CreatePDF.

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"

FONT_FAMILY = "DianthusFont"
FONT_CSS_PATH_REGULAR = "fonts/DejaVuSans.ttf"
FONT_CSS_PATH_BOLD = "fonts/DejaVuSans-Bold.ttf"

_font_warned = False


def _warn_fonts_missing(path: Path) -> None:
    global _font_warned
    if _font_warned:
        return
    _font_warned = True
    logger.error(
        "❌ КРИТИЧНО: PDF-шрифт не найден: %s\n"
        "   PDF-счета будут БЕЗ кириллицы (квадраты вместо букв).\n"
        "   Решение: положите DejaVuSans.ttf в %s",
        path, FONTS_DIR,
    )


def get_font_css() -> str:
    regular = FONTS_DIR / "DejaVuSans.ttf"
    bold = FONTS_DIR / "DejaVuSans-Bold.ttf"

    if not regular.exists():
        _warn_fonts_missing(regular)
        return ""

    if not bold.exists():
        logger.warning(
            "⚠️ DejaVuSans-Bold.ttf не найден — жирный шрифт "
            "будет синтезирован из обычного",
        )

    bold_path = FONT_CSS_PATH_BOLD if bold.exists() else FONT_CSS_PATH_REGULAR

    return f"""
    @font-face {{
        font-family: "{FONT_FAMILY}";
        src: url("{FONT_CSS_PATH_REGULAR}");
    }}
    @font-face {{
        font-family: "{FONT_FAMILY}";
        font-weight: bold;
        src: url("{bold_path}");
    }}
    """


def get_font_family() -> str:
    """Имя шрифта для font-family."""
    if not (FONTS_DIR / "DejaVuSans.ttf").exists():
        _warn_fonts_missing(FONTS_DIR / "DejaVuSans.ttf")
        return "Helvetica"
    return FONT_FAMILY


def link_callback(uri: str, rel: str) -> str:
    """Разрешает относительные URL. fix #6: защита от path traversal."""
    if uri.startswith("file://"):
        return uri[7:]

    relative = uri.lstrip("/")
    if relative.startswith("static/"):
        relative = relative[len("static/"):]

    static_dir = (
        Path(__file__).resolve().parent.parent.parent / "static"
    ).resolve()
    full_path = (static_dir / relative).resolve()

    try:
        full_path.relative_to(static_dir)
    except ValueError:
        logger.warning(
            "link_callback: попытка выйти за пределы static/: %s", uri,
        )
        return uri

    if full_path.exists():
        return str(full_path)

    logger.debug("link_callback: %s не найден", uri)
    return uri