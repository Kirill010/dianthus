"""Загрузка и оптимизация изображений — СИНХРОННЫЕ функции."""
import uuid
from io import BytesIO
from pathlib import Path

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_SIZE = 5 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THUMB_SIZE = (600, 600)
FULL_SIZE = (1600, 1600)


def _read_file(file: UploadFile) -> bytes:
    """Синхронно читает UploadFile (не использует async)."""
    try:
        file.file.seek(0)
        return file.file.read()
    except Exception:
        return b""


def save_product_image(file: UploadFile) -> str:
    """
    Сохраняет одно изображение и возвращает URL ПОЛНОГО изображения.
    Также создаёт миниатюру {name}_thumb.webp.
    """
    if file is None or not file.filename:
        return ""

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            400,
            f"Недопустимый формат. Разрешены: {', '.join(sorted(ALLOWED_EXT))}",
        )

    content = _read_file(file)
    if not content:
        raise HTTPException(400, f"Файл «{file.filename}» пустой")
    if len(content) > MAX_SIZE:
        raise HTTPException(
            400, f"«{file.filename}» больше 5 МБ"
        )

    try:
        img = Image.open(BytesIO(content))
        img.load()
    except Exception:
        raise HTTPException(
            400, f"«{file.filename}» — не изображение"
        )

    img = ImageOps.exif_transpose(img).convert("RGB")

    name = uuid.uuid4().hex

    # Полное
    full = img.copy()
    full.thumbnail(FULL_SIZE)
    full.save(UPLOAD_DIR / f"{name}.webp", "WEBP", quality=88, method=6)

    # Миниатюра
    thumb = img.copy()
    thumb.thumbnail(THUMB_SIZE)
    thumb.save(UPLOAD_DIR / f"{name}_thumb.webp", "WEBP", quality=85, method=6)

    return f"/static/uploads/{name}.webp"


# ── Обёртки, которые ждёт admin.py ──────────────────────────

def save_upload(file: UploadFile | None) -> str:
    """Один файл → URL или пустая строка."""
    if file is None or not file.filename:
        return ""
    return save_product_image(file)


def save_uploads(files: list[UploadFile] | None) -> list[str]:
    """Список файлов → список URL. Ошибка одного файла не роняет остальные."""
    if not files:
        return []
    urls: list[str] = []
    for f in files:
        if f is None or not f.filename:
            continue
        try:
            urls.append(save_product_image(f))
        except HTTPException as e:
            # пропускаем битый файл, продолжаем остальные
            print(f"[upload] skip {f.filename}: {e.detail}")
            continue
    return urls


def delete_upload(url: str) -> bool:
    """Удаляет файл + его миниатюру по URL /static/uploads/xxx.webp."""
    if not url or not url.startswith("/static/uploads/"):
        return False
    name = Path(url).name
    target = UPLOAD_DIR / name
    deleted = False
    try:
        if target.exists():
            target.unlink()
            deleted = True
        stem = Path(name).stem
        thumb = UPLOAD_DIR / f"{stem}_thumb.webp"
        if thumb.exists():
            thumb.unlink()
    except OSError:
        pass
    return deleted