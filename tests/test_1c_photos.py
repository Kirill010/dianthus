# tests/test_1c_photos.py
"""Фото товаров из 1С: POST /api/1c/products/photo (тело — сама картинка, ключ — как у всего обмена).

Зачем отдельный адрес: база 1С во внутренней сети, сайт не может сам скачать картинку,
а программисту нельзя выдавать доступ на сервер — он присылает файл тем же ключом."""
from io import BytesIO

from PIL import Image

from app.models import ExchangeLog, Product
from app.services import api_keys, exchange_log, onec_photos, upload_service
from tests.helpers import seed_item


def key_header(db):
    _, raw = api_keys.create_key(db, "Фото", "")
    return {"X-API-Key": raw}


def picture(fmt="PNG", size=(64, 48), color=(200, 30, 60), mode="RGB", **save_kw) -> bytes:
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, fmt, **save_kw)
    return buf.getvalue()


def product_of(db, item):
    db.expire_all()
    return db.get(Product, item.product_id)


def post_photo(web, headers, data, **params):
    params.setdefault("id", "G-1")
    return web.post("/api/1c/products/photo", params=params, content=data, headers=headers)


def files(tmp_path):
    return sorted(p.name for p in tmp_path.glob("*.webp"))


def setup(web, mem_db, tmp_path, monkeypatch, **item_kw):
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    item = seed_item(mem_db, external_id=item_kw.pop("external_id", "G-1"), **item_kw)
    return item, key_header(mem_db)


# ───────────── доступ ─────────────

def test_photo_needs_a_key(web, mem_db, tmp_path, monkeypatch):
    setup(web, mem_db, tmp_path, monkeypatch)
    assert post_photo(web, {}, picture()).status_code == 401
    assert post_photo(web, {"X-API-Key": "dnt_" + "0" * 40}, picture()).status_code == 401
    assert files(tmp_path) == []


# ───────────── основной путь ─────────────

def test_photo_is_saved_as_webp_and_attached_to_the_product(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    r = post_photo(web, h, picture("JPEG"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["result"] == "saved" and data["position"] == 1 and data["photos_total"] == 1
    assert data["product"]["external_id"] == "G-1" and not data["dry_run"]

    p = product_of(mem_db, item)
    assert p.photos == [data["url"]] and p.image_url == data["url"]
    assert data["url"].startswith(f"/static/uploads/p{p.id}-") and data["url"].endswith(".webp")
    saved = tmp_path / data["url"].rsplit("/", 1)[1]
    assert saved.exists() and Image.open(saved).format == "WEBP"
    assert p.all_photos == [data["url"]]                       # сайт его показывает


def test_same_photo_again_changes_nothing(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    first = post_photo(web, h, picture()).json()
    second = post_photo(web, h, picture()).json()
    assert second["result"] == "unchanged" and second["url"] == first["url"]
    assert len(files(tmp_path)) == 1
    assert product_of(mem_db, item).photos == [first["url"]]


def test_unchanged_photo_whose_file_vanished_is_written_again(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    url = post_photo(web, h, picture()).json()["url"]
    (tmp_path / url.rsplit("/", 1)[1]).unlink()                # кто-то стёр файл на диске
    again = post_photo(web, h, picture()).json()
    assert again["result"] == "saved" and len(files(tmp_path)) == 1


def test_replacing_a_position_removes_the_old_file(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    old = post_photo(web, h, picture(color=(10, 10, 10))).json()["url"]
    new = post_photo(web, h, picture(color=(240, 240, 20))).json()
    assert new["result"] == "saved" and new["url"] != old and new["photos_total"] == 1
    assert product_of(mem_db, item).photos == [new["url"]]
    assert files(tmp_path) == [new["url"].rsplit("/", 1)[1]]  # старый файл не копится


def test_next_position_is_added_and_gaps_are_refused(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    a = post_photo(web, h, picture(color=(1, 2, 3)), n=1).json()
    b = post_photo(web, h, picture(color=(9, 8, 7)), n=2).json()
    assert b["photos_total"] == 2
    p = product_of(mem_db, item)
    assert p.photos == [a["url"], b["url"]] and p.image_url == a["url"]   # главное — первое

    gap = post_photo(web, h, picture(color=(5, 5, 5)), n=4)
    assert gap.status_code == 400 and "по порядку" in gap.json()["detail"]
    assert post_photo(web, h, picture(), n=0).status_code == 400
    assert post_photo(web, h, picture(), n=onec_photos.MAX_PHOTOS_PER_PRODUCT + 1).status_code == 400
    assert product_of(mem_db, item).photos == [a["url"], b["url"]]


def test_old_single_image_and_remote_urls_are_handled(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    p = product_of(mem_db, item)
    p.photos, p.image_url = [], "https://cdn.example.com/old.jpg"      # как у товара с картинкой-ссылкой
    mem_db.commit()
    r = post_photo(web, h, picture(), n=2).json()                      # №1 — старая ссылка, №2 — новая
    assert r["photos_total"] == 2
    r = post_photo(web, h, picture(color=(0, 90, 0)), n=1).json()      # замена ссылки не трогает диск
    assert r["result"] == "saved"
    assert product_of(mem_db, item).photos[0] == r["url"] and len(files(tmp_path)) == 2


# ───────────── как находится товар ─────────────

def test_product_is_found_by_sku_then_name_and_unknown_gets_404(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch, sku="SKU-7", name="Роза Фото-Тест")
    by_sku = web.post("/api/1c/products/photo", params={"sku": "SKU-7"}, content=picture(), headers=h)
    assert by_sku.status_code == 200 and by_sku.json()["product"]["sku"] == "SKU-7"
    by_name = web.post("/api/1c/products/photo", params={"name": "Роза Фото-Тест"},
                       content=picture(color=(1, 1, 1)), headers=h)
    assert by_name.status_code == 200 and by_name.json()["position"] == 1
    missing = post_photo(web, h, picture(), id="NO-SUCH")
    assert missing.status_code == 404 and "products/sync" in missing.json()["detail"]
    assert web.post("/api/1c/products/photo", content=picture(), headers=h).status_code == 400


def test_a_photo_never_creates_a_product(web, mem_db, tmp_path, monkeypatch):
    setup(web, mem_db, tmp_path, monkeypatch)
    before = mem_db.query(Product).count()
    post_photo(web, key_header(mem_db), picture(), id="BRAND-NEW")
    mem_db.expire_all()
    assert mem_db.query(Product).count() == before and files(tmp_path) == []


# ───────────── плохие файлы ─────────────

def test_things_that_are_not_pictures_are_refused(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    for body in (b"", b"<html><script>alert(1)</script></html>", b'{"photo": "abc"}', svg,
                 b"/9j/4AAQSkZJRgABAQ==", picture("BMP"), picture("TIFF")):
        r = post_photo(web, h, body)
        assert r.status_code == 400, (body[:20], r.text)
    assert files(tmp_path) == [] and product_of(mem_db, item).photos == []


def test_oversized_file_and_huge_dimensions_are_refused(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    monkeypatch.setattr(onec_photos, "MAX_PHOTO_BYTES", 2000)
    big = web.post("/api/1c/products/photo", params={"id": "G-1"}, content=b"x" * 5000, headers=h)
    assert big.status_code == 413
    monkeypatch.setattr(onec_photos, "MAX_PHOTO_BYTES", 15 * 1024 * 1024)
    monkeypatch.setattr(upload_service, "MAX_PIXELS", 1000)               # «бомба»: картинка слишком велика
    bomb = post_photo(web, h, picture(size=(100, 100)))
    assert bomb.status_code == 400 and "размерам" in bomb.json()["detail"]
    assert files(tmp_path) == [] and product_of(mem_db, item).photos == []


def test_dry_run_checks_everything_but_saves_nothing(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    r = post_photo(web, h, picture(), dry_run=1)
    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True and data["result"] == "would_save" and data["photos_total"] == 1
    assert files(tmp_path) == [] and product_of(mem_db, item).photos == []
    assert post_photo(web, h, b"not a picture", dry_run=1).status_code == 400      # плохой файл виден и в проверке
    assert post_photo(web, h, picture()).json()["result"] == "saved"                # настоящая — как обещала проверка


# ───────────── что делается с самой картинкой ─────────────

def test_big_photo_is_scaled_down_and_phone_rotation_is_applied(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    big = post_photo(web, h, picture("JPEG", size=(2400, 1800))).json()
    assert big["width"] == 1600 and big["height"] == 1200                     # вписано в 1600×1600

    exif = Image.Exif()
    exif[0x0112] = 6                                                          # «снято на боку»: повернуть на 90°
    rotated = post_photo(web, h, picture("JPEG", size=(40, 20), exif=exif), n=2).json()
    assert (rotated["width"], rotated["height"]) == (20, 40)


def test_transparent_png_gets_a_white_background(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    url = post_photo(web, h, picture("PNG", size=(32, 32), color=(255, 0, 0, 0), mode="RGBA")).json()["url"]
    px = Image.open(tmp_path / url.rsplit("/", 1)[1]).convert("RGB").getpixel((16, 16))
    assert min(px) >= 245, px                                                  # не чёрный фон


def test_gif_and_webp_are_accepted(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    assert post_photo(web, h, picture("GIF", mode="P", color=3), n=1).status_code == 200
    assert post_photo(web, h, picture("WEBP"), n=2).status_code == 200


# ───────────── журнал обмена ─────────────

def test_failed_photo_request_leaves_its_reason_in_the_exchange_journal(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    assert post_photo(web, h, picture(), id="NO-SUCH").status_code == 404
    assert post_photo(web, h, b"not a picture").status_code == 400
    mem_db.expire_all()
    rows = mem_db.query(ExchangeLog).filter(ExchangeLog.endpoint == "products/photo").order_by(ExchangeLog.id).all()
    assert [r.status_code for r in rows] == [404, 400] and not any(r.ok for r in rows)
    assert "Такого товара на сайте нет" in rows[0].summary and "Не изображение" in rows[1].summary
    assert "товара нет на сайте" in exchange_log.STATUS_HELP[404]


def test_photo_requests_are_logged_but_do_not_count_as_a_catalog_update(web, mem_db, tmp_path, monkeypatch):
    item, h = setup(web, mem_db, tmp_path, monkeypatch)
    post_photo(web, h, picture())
    mem_db.expire_all()
    row = mem_db.query(ExchangeLog).filter(ExchangeLog.endpoint == "products/photo").one()
    assert row.ok and "№1" in row.summary and "сохранено" in row.summary
    assert exchange_log.last_catalog_update(mem_db) is None                    # строка «цены обновлены» не врёт


def test_admin_photo_upload_still_works_after_the_refactoring(tmp_path, monkeypatch):
    """Общая проверка картинок теперь одна на админку и на 1С: админская загрузка ведёт себя как раньше."""
    import pytest
    from fastapi import HTTPException, UploadFile
    monkeypatch.setattr(upload_service, "UPLOAD_DIR", tmp_path)
    ok = UploadFile(file=BytesIO(picture("JPEG")), filename="rose.JPG")
    url = upload_service.save_product_image(ok)
    assert url.startswith("/static/uploads/") and (tmp_path / url.rsplit("/", 1)[1]).exists()
    for name, data in (("x.exe", picture()), ("x.jpg", b"not an image"), ("x.png", b"")):
        with pytest.raises(HTTPException):
            upload_service.save_product_image(UploadFile(file=BytesIO(data), filename=name))
