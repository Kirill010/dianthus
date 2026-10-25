# -*- coding: utf-8 -*-
"""Собирает файл docs/1C_PROGRAMMER_BRIEF.md — памятку программисту 1С.

Исходник памятки лежит в app/content/1c_brief.md (он же показывается на сайте по
адресу /1c-docs). Здесь в него подставляется адрес сайта, чтобы получился готовый
файл, который можно переслать программисту:

    python scripts/export_1c_brief.py                    # адрес https://dianthus-opt.ru
    python scripts/export_1c_brief.py https://shop.ru    # свой адрес

Запускайте после правки app/content/1c_brief.md (тест проверит, что копия свежая).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://dianthus-opt.ru"


def main(argv: list[str]) -> int:
    url = (argv[0] if argv else DEFAULT_URL).rstrip("/")
    source = (ROOT / "app" / "content" / "1c_brief.md").read_text(encoding="utf-8")
    target = ROOT / "docs" / "1C_PROGRAMMER_BRIEF.md"
    target.write_text(source.replace("{{URL}}", url), encoding="utf-8", newline="\n")
    print(f"Готово: {target} (адрес сайта: {url})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
