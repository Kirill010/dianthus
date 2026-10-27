import logging
import os
import uuid
from io import BytesIO
from pathlib import Path
from typing import List

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

from ..config import config

logger = logging.getLogger(__name__)

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_SIZE = config.MAX_UPLOAD_SIZE
# Маленький по размеру файл может «распаковаться» в гигантскую картинку
# (decompression bomb) и съесть всю память. Лимит в пикселях проверяем сами:
# Pillow по MAX_IMAGE_PIXELS только предупреждает и отказывает лишь при
# ДВОЙНОМ превышении, то есть настоящий потолок был бы вдвое выше заявленного.
MAX_PIXELS = 40_000_000          # ≈ 7000×5700
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Что на самом деле лежит в файле (расширению и заголовку запроса не верим).
# MPO — это обычный JPEG с приставкой: так Pillow называет часть снимков с телефонов.
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "GIF"}
FULL_SIZE = (1600, 1600)

def _read_file(file: UploadFile) -> bytes:
    try:
        file.file.seek(0)
        return file.file.read()
    except Exception:
        return b""

def encode_product_image(content: bytes, max_size: int | None = None) -> tuple[bytes, tuple[int, int]]:
    """Проверяет картинку и готовит её к показу на сайте: WEBP не больше 1600×1600.

    Возвращает (байты WEBP, (ширина, высота)). Бросает HTTPException(400) с понятным
    текстом: пустой файл, слишком большой, не картинка, неподходящий формат, гигантские
    размеры. Прозрачные PNG кладутся на белый фон (раньше фон становился чёрным).
    """
    if not content:
        raise HTTPException(400, "Файл пустой")
    limit = max_size or MAX_SIZE
    if len(content) > limit:
        raise HTTPException(400, f"Файл больше {limit // (1024 * 1024)} МБ")

    try:
        img = Image.open(BytesIO(content))          # читает только заголовок
        fmt = img.format
        width, height = img.size
    except Exception:
        raise HTTPException(400, "Не изображение")
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(400, f"Недопустимый формат картинки: {fmt or '?'} (нужен JPEG, PNG, WEBP или GIF)")
    if width * height > MAX_PIXELS:
        raise HTTPException(400, "Изображение слишком большое по размерам")
    try:
        # fix #49: применяем ориентацию по EXIF
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            img = flat
        else:
            img = img.convert("RGB")
    except Exception:
        raise HTTPException(400, "Не изображение")

    img.thumbnail(FULL_SIZE)
    out = BytesIO()
    img.save(out, "WEBP", quality=88, method=6)
    return out.getvalue(), img.size

def write_upload(name: str, data: bytes) -> str:
    """Кладёт готовый WEBP в папку загрузок под именем <name>.webp и возвращает адрес.
    Запись через временный файл: nginx не отдаст «наполовину записанную» картинку."""
    target = UPLOAD_DIR / f"{name}.webp"
    tmp = UPLOAD_DIR / f".{name}.tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return f"/static/uploads/{name}.webp"

def save_product_image(file: UploadFile, max_size: int | None = None) -> str:
    if file is None or not file.filename:
        return ""
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Недопустимый формат: {ext}")

    content = _read_file(file)
    data, _size = encode_product_image(content, max_size)
    return write_upload(uuid.uuid4().hex, data)

def save_uploads(files: List[UploadFile] | None, max_size: int | None = None,
                 skipped: list | None = None) -> List[str]:
    """Сохраняет картинки. Непринятые файлы не роняют загрузку: они попадают
    в лог и (если передан список skipped) — в него, чтобы сказать человеку."""
    if not files:
        return []
    urls = []
    for f in files:
        if f and f.filename:
            try:
                urls.append(save_product_image(f, max_size=max_size))
            except HTTPException as e:
                # Раньше здесь был print() — в проде он уходил в никуда,
                # и причина «фото не загрузилось» терялась.
                logger.warning("Загрузка %s пропущена: %s", f.filename, e.detail)
                if skipped is not None:
                    skipped.append(f"{f.filename[:40]}: {e.detail}")
    return urls

def delete_upload(url: str) -> bool:
    if not url or not url.startswith("/static/uploads/"): return False
    name = Path(url).name
    target = UPLOAD_DIR / name
    try:
        if target.exists(): target.unlink()
        thumb = UPLOAD_DIR / f"{Path(name).stem}_thumb.webp"
        if thumb.exists(): thumb.unlink()
        return True
    except OSError:
        return False
