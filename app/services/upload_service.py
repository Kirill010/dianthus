import uuid
from io import BytesIO
from pathlib import Path
from typing import List

from fastapi import HTTPException, UploadFile
from PIL import Image, ImageOps

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_SIZE = 5 * 1024 * 1024
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
FULL_SIZE = (1600, 1600)

def _read_file(file: UploadFile) -> bytes:
    try:
        file.file.seek(0)
        return file.file.read()
    except Exception:
        return b""

def save_product_image(file: UploadFile) -> str:
    if file is None or not file.filename: return ""
    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Недопустимый формат: {ext}")

    content = _read_file(file)
    if not content: raise HTTPException(400, "Файл пустой")
    if len(content) > MAX_SIZE: raise HTTPException(400, "Файл больше 5 МБ")

    try:
        img = Image.open(BytesIO(content)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Не изображение")

    name = uuid.uuid4().hex
    img.thumbnail(FULL_SIZE)
    img.save(UPLOAD_DIR / f"{name}.webp", "WEBP", quality=88, method=6)
    return f"/static/uploads/{name}.webp"

def save_uploads(files: List[UploadFile] | None) -> List[str]:
    if not files: return []
    urls = []
    for f in files:
        if f and f.filename:
            try:
                urls.append(save_product_image(f))
            except HTTPException as e:
                print(f"[upload] skip {f.filename}: {e.detail}")
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