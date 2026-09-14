"""Загрузка и оптимизация изображений (сохраняем в WebP)."""
import io
import logging
import uuid
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps


logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent  # → app/
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_SIZE = 5 * 1024 * 1024      # 5 МБ
MAX_DIM = 1200
CHUNK_SIZE = 64 * 1024          # 64 КБ


def _read_limited(file: UploadFile, limit: int) -> bytes | None:
    """Читает файл чанками, не давая загрузить больше limit байт."""
    data = bytearray()
    while True:
        chunk = file.file.read(CHUNK_SIZE)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            return None
    return bytes(data)


def save_upload(file: UploadFile | None) -> str:
    """
    Сохраняет картинку и возвращает URL.
    Возвращает пустую строку при любой ошибке.
    """
    if not file or not file.filename:
        return ""

    if Path(file.filename).suffix.lower() not in ALLOWED_EXT:
        logger.warning("Отклонён файл с расширением %s", file.filename)
        return ""

    contents = _read_limited(file, MAX_SIZE)
    if contents is None:
        logger.warning("Файл превысил лимит %d байт: %s",
                       MAX_SIZE, file.filename)
        return ""
    if not contents:
        return ""

    try:
        img = Image.open(io.BytesIO(contents))
        img = ImageOps.exif_transpose(img)

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")

        if max(img.size) > MAX_DIM:
            img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

        filename = f"{uuid.uuid4().hex}.webp"
        img.save(UPLOAD_DIR / filename, "WEBP", quality=85, optimize=True)
        return f"/static/uploads/{filename}"
    except Exception as e:
        logger.exception("Не удалось обработать изображение: %s", e)
        return ""


def delete_upload(url: str) -> None:
    """Удаляет файл из uploads/, если url на него указывает."""
    if not url or not url.startswith("/static/uploads/"):
        return
    filename = Path(url).name
    if not filename or ".." in filename or "/" in filename:
        return
    path = UPLOAD_DIR / filename
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("Не удалось удалить файл %s: %s", path, e)