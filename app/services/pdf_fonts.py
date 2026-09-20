"""Шрифт для PDF — правильная регистрация через @font-face + link_callback.

xhtml2pdf НЕ читает file:// URI напрямую.
Нужно использовать относительный путь + link_callback в CreatePDF.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Папка со шрифтами
FONTS_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"

# Имя шрифта
FONT_FAMILY = "DianthusFont"

# Относительный путь от /static/ (для CSS)
FONT_CSS_PATH_REGULAR = "fonts/DejaVuSans.ttf"
FONT_CSS_PATH_BOLD = "fonts/DejaVuSans-Bold.ttf"


def get_font_css() -> str:
    """
    CSS с @font-face. Использует ОТНОСИТЕЛЬНЫЙ путь,
    который разрешается через link_callback в pisa.CreatePDF.
    """
    regular = FONTS_DIR / "DejaVuSans.ttf"
    bold = FONTS_DIR / "DejaVuSans-Bold.ttf"

    if not regular.exists():
        logger.warning("⚠️  Шрифт не найден: %s", regular)
        return ""

    bold_path = FONT_CSS_PATH_BOLD if bold.exists() else FONT_CSS_PATH_REGULAR

    css = f"""
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
    logger.info("✅ CSS шрифта сгенерирован: %s", FONT_FAMILY)
    return css


def get_font_family() -> str:
    """Имя шрифта для font-family."""
    if not (FONTS_DIR / "DejaVuSans.ttf").exists():
        return "Helvetica"
    return FONT_FAMILY


def link_callback(uri: str, rel: str) -> str:
    """
    Разрешает относительные URL к файлам.

    xhtml2pdf вызывает эту функцию для каждого ресурса (CSS, картинки, шрифты).
    Мы должны вернуть АБСОЛЮТНЫЙ путь к файлу.

    :param uri: URI из HTML (например, "fonts/DejaVuSans.ttf")
    :param rel: относительный путь HTML-документа
    :return: абсолютный путь к файлу
    """
    # Уже абсолютный путь
    if uri.startswith("file://"):
        return uri[7:]

    # Убираем ведущий слэш
    relative = uri.lstrip("/")

    # Убираем "static/" если есть
    if relative.startswith("static/"):
        relative = relative[len("static/"):]

    # Строим путь от /app/static/
    static_dir = Path(__file__).resolve().parent.parent / "static"
    full_path = static_dir / relative

    if full_path.exists():
        logger.debug("link_callback: %s → %s", uri, full_path)
        return str(full_path)

    # Не нашли — возвращаем как есть (для встроенных ресурсов)
    logger.debug("link_callback: %s не найден, оставляем как есть", uri)
    return uri