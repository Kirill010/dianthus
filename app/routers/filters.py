# app/routers/filters.py
"""Страница «Фильтры каталога» (только владелец): виды, цвета, рост и страны — те самые таблицы,
по которым клиенты фильтруют каталог. Новые значения появляются сами (из 1С, из файла с товарами),
здесь их можно добавить, переименовать, раскрасить и удалить, если они нигде не используются."""
import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_admin
from ..models import Product
from ..security import check_csrf
from ..services import taxonomy
from ..services.audit import log_action
from ..templating import render

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/filters")

TITLES = {"kind": "Вид цветка", "color": "Цвет", "height": "Рост, см", "country": "Страна"}


def _model(kind: str):
    model = taxonomy.MODELS.get(kind)
    if model is None:
        raise HTTPException(404, "Такого фильтра нет")
    return model


def _done(request: Request, text: str, kind: str) -> RedirectResponse:
    request.session["flash"] = text
    return RedirectResponse(url=f"/admin/filters#{kind}", status_code=303)


@router.get("", response_class=HTMLResponse)
def filters_page(request: Request, db: Session = Depends(get_db), admin=Depends(require_admin)):
    usage = taxonomy.usage_counts(db)
    sections = [{"kind": kind, "title": TITLES[kind], "usage": usage[kind],
                 "rows": taxonomy.ordered(kind, db.query(model).all())}
                for kind, model in taxonomy.MODELS.items()]
    return render(request, "admin_filters.html", db, user=admin, sections=sections)


@router.post("/{kind}/add")
def add_value(kind: str, request: Request, name: str = Form(""), swatch: str = Form(""),
              db: Session = Depends(get_db), admin=Depends(require_admin),
              _csrf: None = Depends(check_csrf)):
    _model(kind)
    label = taxonomy.LABELS[kind]
    tax = taxonomy.Taxonomy(db)
    try:
        ref = taxonomy.Ref(kind=kind, name=name, hex=taxonomy.clean_hex(swatch))
        if ref.is_empty:
            raise taxonomy.TaxonomyError(f"{label}: укажите значение")
        if tax.resolve(ref, create=False) is not None:
            db.rollback()
            return _done(request, f"{label}: «{taxonomy.clean_name(name)}» уже есть в списке", kind)
        row = tax.resolve(ref)
        db.commit()
    except taxonomy.TaxonomyError as e:
        db.rollback()
        return _done(request, str(e), kind)
    except IntegrityError:
        db.rollback()
        return _done(request, f"{label}: такое значение уже есть в списке", kind)
    log_action(db, request, admin, "filter_add", kind, row.id, f"{label}: добавлено «{row.name}»")
    return _done(request, f"Добавлено: {label.lower()} «{row.name}»", kind)


@router.post("/{kind}/{row_id:int}/edit")
def edit_value(kind: str, row_id: int, request: Request, name: str = Form(""), swatch: str = Form(""),
               db: Session = Depends(get_db), admin=Depends(require_admin),
               _csrf: None = Depends(check_csrf)):
    model = _model(kind)
    label = taxonomy.LABELS[kind]
    row = db.get(model, row_id)
    if row is None:
        return _done(request, f"{label}: запись не найдена (возможно, её уже удалили)", kind)
    old = row.name
    try:
        if kind == "height":
            cm = taxonomy.parse_height(name)
            if cm is None:
                raise taxonomy.TaxonomyError("Рост: укажите число сантиметров, например 60")
            if db.query(model).filter(model.cm == cm, model.id != row.id).first():
                raise taxonomy.TaxonomyError(f"Рост {cm} см уже есть в списке")
            row.cm = cm
        else:
            new_name = taxonomy.display_name(name)
            if not new_name:
                raise taxonomy.TaxonomyError(f"{label}: название не может быть пустым")
            key = taxonomy.fold(new_name)
            if any(r.id != row.id and taxonomy.fold(r.name) == key for r in db.query(model).all()):
                raise taxonomy.TaxonomyError(f"{label}: «{new_name}» уже есть в списке")
            row.name = new_name
            if kind == "color":
                new_hex = taxonomy.clean_hex(swatch)
                # палитра всегда присылает какой-то цвет: «белый» у записи без кружка — не правка
                if new_hex and not (new_hex == "#ffffff" and not row.hex):
                    row.hex = new_hex
        changed = db.is_modified(row)
        db.commit()
    except taxonomy.TaxonomyError as e:
        db.rollback()
        return _done(request, str(e), kind)
    except IntegrityError:
        db.rollback()
        return _done(request, f"{label}: такое значение уже есть в списке", kind)
    if not changed:
        return _done(request, "Без изменений", kind)
    log_action(db, request, admin, "filter_edit", kind, row.id, f"{label}: «{old}» → «{row.name}»")
    return _done(request, f"Сохранено: {label.lower()} «{row.name}»", kind)


@router.post("/{kind}/{row_id:int}/delete")
def delete_value(kind: str, row_id: int, request: Request, db: Session = Depends(get_db),
                 admin=Depends(require_admin), _csrf: None = Depends(check_csrf)):
    model = _model(kind)
    label = taxonomy.LABELS[kind]
    row = db.get(model, row_id)
    if row is None:
        return _done(request, f"{label}: запись уже удалена", kind)
    used = db.query(Product).filter(taxonomy.PRODUCT_FK[kind] == row.id).count()
    if used:
        return _done(request, f"«{row.name}» нельзя удалить: оно указано у товаров ({used}). "
                              f"Сначала смените значение у этих товаров или переименуйте запись.", kind)
    name = row.name
    db.delete(row)
    db.commit()
    log_action(db, request, admin, "filter_delete", kind, row_id, f"{label}: удалено «{name}»")
    return _done(request, f"Удалено: {label.lower()} «{name}»", kind)
