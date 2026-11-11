#!/usr/bin/env python
"""Собирает «Пакет для юриста» в один PDF: письмо с вопросами (docs/legal/ПИСЬМО_ЮРИСТУ.md) + внутренние образцы
документов (docs/legal/*.md) как приложения B–G.

    python scripts/build_lawyer_packet.py --pdf "D:/Dianthus/3-documents/Для юриста/Пакет для юриста.pdf"

Тексты, опубликованные на сайте (политика, согласия, оферта), в PDF не входят: их печатают с живого сайта отдельными файлами
(так их видят клиенты). Только стандартная библиотека + сборщик руководства (scripts/build_manual.py)."""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGAL = ROOT / "docs" / "legal"

_spec = importlib.util.spec_from_file_location("build_manual", ROOT / "scripts" / "build_manual.py")
bm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bm)

APPENDICES = [
    ("B", "Образец приказа об ответственном за обработку персональных данных", "order_responsible_pd.md"),
    ("C", "Положение об обработке и защите персональных данных", "pd_regulation.md"),
    ("D", "Обязательство сотрудника о неразглашении персональных данных", "employee_pd_obligation.md"),
    ("E", "Условие о поручении обработки данных для договоров", "processor_clause.md"),
    ("F", "План действий при утечке данных", "incident_plan.md"),
    ("G", "Уведомление в Роскомнадзор: готовые ответы для формы", "rkn_notification_guide.md"),
]


def appendix_text(letter: str, title: str, filename: str) -> str:
    """Файл-образец без своего заголовка первого уровня; остальные заголовки — третьего уровня (в оглавление не идут)."""
    lines = (LEGAL / filename).read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    out = [f"## Приложение {letter}. {title}", ""]
    skipped_title = False
    for line in lines:
        if not skipped_title and line.startswith("# "):
            skipped_title = True
            continue
        out.append(re.sub(r"^#{1,6}\s", "### ", line))
    return "\n".join(out).strip() + "\n"


def build_markdown() -> str:
    text = (LEGAL / "ПИСЬМО_ЮРИСТУ.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    body = "\n\n".join(appendix_text(*a) for a in APPENDICES)
    return text.replace("@@APPENDICES@@", body)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True, help="куда положить PDF")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "_build"), help="куда положить промежуточный HTML")
    args = ap.parse_args()

    doc = bm.convert(build_markdown(), LEGAL)
    bm.CSS = bm.CSS.replace("Диантус: сайт и 1С · стр. ", "Пакет для юриста · ООО «Диантус» · стр. ")
    html_text = bm.render(doc)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "ПАКЕТ_ЮРИСТУ.html"
    html_path.write_text(html_text, encoding="utf-8")
    bm.make_pdf(html_path, Path(args.pdf))
    print(f"PDF: {args.pdf} ({Path(args.pdf).stat().st_size // 1024} КБ), разделов в оглавлении: {len(doc.toc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
