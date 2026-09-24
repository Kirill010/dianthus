"""Загрузка и оптимизация изображений (синхронные функции)."""
import uuid
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_SIZE = 5 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
THUMB_SIZE = (600, 600)
FULL_SIZE = (1600, 1600)


def _read_upload(file: UploadFile) -> bytes:
    """Синхронно читает содержимое UploadFile."""
    try:
        file.file.seek(0)
        return file.file.read()
    except Exception:
        return b""


def save_product_image(file: UploadFile) -> str:
    """
    Сохраняет одно изображение, возвращает URL ПОЛНОГО изображения.
    Создаёт рядом миниатюру {name}_thumb.webp.
    """
    if file is None or not file.filename:
        return ""

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            f"Недопустимый формат. Разрешены: {', '.join(sorted(ALLOWED_EXT))}",
        )

    content = _read_upload(file)
    if not content:
        raise HTTPException(400, "Пустой файл")
    if len(content) > MAX_SIZE:
        raise HTTPException(400, "Файл слишком большой (максимум 5 МБ)")

    try:
        img = Image.open(BytesIO(content))
        img.load()
    except Exception:
        raise HTTPException(400, "Не удалось открыть изображение")

    img = ImageOps.exif_transpose(img).convert("RGB")

    name = uuid.uuid4().hex

    # Полное изображение
    full = img.copy()
    full.thumbnail(FULL_SIZE)
    full.save(UPLOAD_DIR / f"{name}.webp", "WEBP", quality=88, method=6)

    # Миниатюра
    thumb = img.copy()
    thumb.thumbnail(THUMB_SIZE)
    thumb.save(UPLOAD_DIR / f"{name}_thumb.webp", "WEBP", quality=85, method=6)

    return f"/static/uploads/{name}.webp"


def save_upload(file: UploadFile | None) -> str:
    """Совместимость: сохраняет один файл, возвращает URL или ''."""
    if file is None or not file.filename:
        return ""
    return save_product_image(file)


def save_uploads(files: list[UploadFile] | None) -> list[str]:
    """Сохраняет несколько файлов, возвращает список URL."""
    if not files:
        return []
    urls: list[str] = []
    for f in files:
        if f is None or not f.filename:
            continue
        try:
            urls.append(save_product_image(f))
        except HTTPException:
            raise
        except Exception:
            continue
    return urls


def delete_upload(url: str) -> bool:
    """Удаляет файл по URL /static/uploads/xxx.webp (и его миниатюру)."""
    if not url or not url.startswith("/static/uploads/"):
        return False
    name = Path(url).name
    target = UPLOAD_DIR / name
    deleted = False
    try:
        if target.exists():
            target.unlink()
            deleted = True
        # Удаляем миниатюру
        stem = Path(name).stem
        thumb = UPLOAD_DIR / f"{stem}_thumb.webp"
        if thumb.exists():
            thumb.unlink()
    except OSError:
        pass
    return deleted