# app/routers/seo_pages.py
"""Страницы-разделы /optom/<slug> («розы оптом», «хризантемы оптом»…): открыты всем, без входа.
Содержимое и правила — в services/seo_pages.py."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import help_page as help_content, seo_pages
from ..templating import render

router = APIRouter()


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request, db: Session = Depends(get_db)):
    """«Как заказать»: пошаговая инструкция и частые вопросы (тексты — в services/help_page.py)."""
    return render(
        request, "help.html", db,
        steps=help_content.STEPS, faq=help_content.FAQ,
        breadcrumbs=[("Главная", "/"), ("Как заказать", "/help")],
    )


@router.get("/optom/{section}", response_class=HTMLResponse)
def seo_section(section: str, request: Request, db: Session = Depends(get_db)):
    page = seo_pages.BY_SLUG.get(section)
    if page is None:
        raise HTTPException(status_code=404, detail="Такой страницы нет.")
    data = seo_pages.collect(db, page)
    return render(
        request, "seo_page.html", db,
        section=page, data=data, faq=seo_pages.faq(page, data),
        countries_text=seo_pages.join_words(data["countries"]),
        other_sections=[s for s in seo_pages.SECTIONS if s.slug != page.slug],
        breadcrumbs=[("Главная", "/"), ("Каталог", "/catalog"), (page.h1, f"/optom/{page.slug}")],
    )
