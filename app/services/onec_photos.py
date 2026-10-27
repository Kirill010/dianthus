# app/services/onec_photos.py
"""Фото товаров из 1С: POST /api/1c/products/photo.

Зачем. База 1С стоит во внутренней сети, сайт не может «сходить» за картинкой, а
в каталожной выгрузке (products/sync) умеют ездить только АДРЕСА уже опубликованных
картинок. Поэтому 1С сама присылает файл — тем же ключом, что и остальной обмен. Доступ
на сервер для этого не нужен и не должен выдаваться.

Правила (всё идемпотентно — повторная отправка ничего не ломает и не плодит файлы):
  * товар ищется как в каталоге: id из 1С → артикул → точное название; товар, которого
    ещё нет на сайте, фото не создаёт (сначала products/sync);
  * n — номер фото по порядку: 1 — главное. Номер занят — фото заменяется, следующий по
    порядку — добавляется, дальше — ошибка (чтобы не было «дыр»);
  * имя файла = товар + отпечаток содержимого: то же самое фото на том же месте не
    пишется заново («unchanged»), заменённый локальный файл удаляется с диска;
  * картинка проверяется по содержимому, перекодируется в WEBP не больше 1600×1600.
"""
import hashlib
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..models import Product
from . import upload_service
from .catalog_sync import Catalog, clean

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 15 * 1024 * 1024      # исходник из 1С; на сайт уйдёт WEBP заметно меньше
MAX_PHOTOS_PER_PRODUCT = 10


def apply_product_photo(db: Session, *, ext: str, sku: str, name: str, position: int,
                        content: bytes, dry_run: bool = False) -> dict:
    """Кладёт картинку на место `position` у товара. Возвращает ответ для 1С.

    Бросает HTTPException: 400 (нет id/sku/name, плохой номер, не картинка, «дыра» в
    номерах), 404 (такого товара на сайте нет), 500 (не удалось сохранить).
    """
    ext, sku, name = clean(ext, 64), clean(sku, 100), clean(name, 200)
    if not (ext or sku or name):
        raise HTTPException(400, "Укажите товар: id (код или GUID из 1С), sku или name.")
    if not 1 <= position <= MAX_PHOTOS_PER_PRODUCT:
        raise HTTPException(400, f"Номер фото n — от 1 до {MAX_PHOTOS_PER_PRODUCT}.")

    product = Catalog(db, [{"id": ext, "sku": sku, "name": name}]).find(ext, sku, name)
    if product is None:
        raise HTTPException(
            404, "Такого товара на сайте нет. Сначала отправьте каталог (products/sync) и "
                 "передавайте здесь тот же id.")

    # Картинку проверяем до любых записей: плохой файл не должен ничего менять.
    data, size = upload_service.encode_product_image(content, MAX_PHOTO_BYTES)

    if not dry_run:
        # строка товара блокируется: две одновременные отправки не затрут друг друга
        product = db.query(Product).filter(Product.id == product.id).with_for_update().one()

    file_name = f"p{product.id}-{hashlib.sha256(content).hexdigest()[:16]}"
    url = f"/static/uploads/{file_name}.webp"
    current = list(product.all_photos)

    old_url = ""
    if position <= len(current):
        old_url = current[position - 1]
        on_disk = (upload_service.UPLOAD_DIR / f"{file_name}.webp").exists()
        if old_url == url and on_disk:
            result, new_list = "unchanged", current          # то же фото на том же месте
        else:
            result, new_list = "saved", current[:position - 1] + [url] + current[position:]
    elif position == len(current) + 1:
        result, new_list = "saved", current + [url]
    else:
        raise HTTPException(
            400, f"Фото №{position} пока нельзя добавить: у товара {len(current)} фото. "
                 f"Добавляйте по порядку, следующее — №{len(current) + 1}.")

    if result == "saved" and not dry_run:
        target = upload_service.UPLOAD_DIR / f"{file_name}.webp"
        existed = target.exists()
        try:
            upload_service.write_upload(file_name, data)
            product.photos = new_list
            product.image_url = new_list[0]
            db.commit()
        except Exception:
            db.rollback()
            if not existed:
                try:
                    target.unlink()
                except OSError:
                    pass
            logger.exception("1С фото: не удалось сохранить фото товара id=%s", product.id)
            raise HTTPException(500, "Не удалось сохранить фото. Повторите позже.")
        # заменённый локальный файл больше никому не нужен
        if old_url and old_url != url and old_url not in new_list:
            upload_service.delete_upload(old_url)

    return {
        "status": "ok", "dry_run": bool(dry_run),
        "result": result if not (dry_run and result == "saved") else "would_save",
        "product": {"id": product.id, "external_id": product.external_id or "",
                    "sku": product.sku or "", "name": product.name},
        "position": position, "photos_total": len(new_list),
        "url": url, "width": size[0], "height": size[1],
    }
