# -*- coding: utf-8 -*-
"""Собирает файлы для программиста 1С: docs/1C_PROGRAMMER_BRIEF.md (памятка) и
docs/1C_FORMATS.json (все форматы обмена одним JSON-файлом).

Исходники лежат в app/content/ (1c_brief.md и 1c_formats.json); они же показываются на сайте по
адресам /1c-docs и /1c-docs.json. Здесь в них подставляются примеры и адрес сайта, чтобы получились
готовые файлы, которые можно переслать программисту:

    python scripts/export_1c_brief.py                    # адрес https://dianthus-opt.ru
    python scripts/export_1c_brief.py https://shop.ru    # свой адрес

Запускайте после правки app/content/1c_brief.md или 1c_formats.json (тест проверит, что копии свежие).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import onec_brief  # noqa: E402

DEFAULT_URL = "https://dianthus-opt.ru"


def main(argv: list[str]) -> int:
    url = (argv[0] if argv else DEFAULT_URL).rstrip("/")
    docs = ROOT / "docs"
    brief = docs / "1C_PROGRAMMER_BRIEF.md"
    brief.write_text(onec_brief.render_brief(onec_brief.CONTENT.read_text(encoding="utf-8"), url),
                     encoding="utf-8", newline="\n")
    formats = docs / "1C_FORMATS.json"
    formats.write_text(onec_brief.FORMATS.read_text(encoding="utf-8").replace("{{URL}}", url),
                       encoding="utf-8", newline="\n")
    print(f"Готово: {brief} и {formats} (адрес сайта: {url})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
