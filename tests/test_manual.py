# tests/test_manual.py
"""Руководство по сайту (docs/РУКОВОДСТВО.md) и его сборщик (scripts/build_manual.py).

Проверяем то, что ломается незаметно: пропавшие картинки, «склеенные» нумерованные списки (пункты вместо 1, 2, 3
превращаются в 11, 12, 13), оборванные таблицы и недосказанная разметка."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "РУКОВОДСТВО.md"

_spec = importlib.util.spec_from_file_location("build_manual", ROOT / "scripts" / "build_manual.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def test_every_image_in_the_manual_exists_and_every_screenshot_is_used():
    text = MANUAL.read_text(encoding="utf-8")
    used = set(re.findall(r"!\[[^\]]*\]\((manual_img/[^)]+)\)", text))
    assert used, "в руководстве нет ни одной картинки"
    for rel in used:
        assert (ROOT / "docs" / rel).exists(), f"нет файла {rel}"
    on_disk = {f"manual_img/{p.name}" for p in (ROOT / "docs" / "manual_img").glob("*.webp")}
    assert on_disk - used == set(), f"снимки не используются в тексте: {sorted(on_disk - used)}"


def test_numbered_lists_run_in_order_and_do_not_glue_together():
    """Список «1…10» + пустая строка + «3…» склеился бы в один и показал бы 11, 12, 13."""
    prev = None
    for n, line in enumerate(MANUAL.read_text(encoding="utf-8").split("\n"), 1):
        m = re.match(r"^(\d+)\.\s", line)
        if m:
            num = int(m.group(1))
            assert prev is None or num == prev + 1 or num == 1, f"строка {n}: «{line[:50]}» после {prev}"
            prev = num
        elif line.strip() and not line.startswith(" ") and not re.match(r"^[*-]\s", line):
            prev = None


def test_manual_converts_to_clean_html():
    doc = build.convert(MANUAL.read_text(encoding="utf-8"))
    assert doc.title.startswith("Руководство по сайту") and doc.subtitle
    html = build.render(doc)
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    visible = re.sub(r"data:[^\"']+", "", visible)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert "**" not in visible, "остались незакрытые «**»"
    assert "<!--" not in html and "КОНЕЦ-ЧАСТИ" not in html and "КОНЕЦ-ДОКУМЕНТА" not in html
    ids = re.findall(r' id="(s\d+)"', html)
    assert len(ids) == len(set(ids)) and len(ids) >= 40
    # у каждой таблицы во всех строках одинаковое число ячеек
    for table in re.findall(r"<table>.*?</table>", html, flags=re.S):
        widths = {len(re.findall(r"<t[hd][ >]", row)) for row in re.findall(r"<tr>.*?</tr>", table, flags=re.S)}
        assert len(widths) == 1, f"таблица с разным числом колонок: {widths}"


def test_parts_cover_all_roles_and_appendices():
    heads = [t for _lvl, _sid, t in build.convert(MANUAL.read_text(encoding="utf-8")).toc]
    for needle in ("Часть 1. Покупателю", "Часть 2. Менеджеру", "Часть 3. Владельцу", "Часть 4. Оператору 1С",
                   "Часть 5. Склад", "Часть 6. Если что-то пошло не так", "Приложение. Адреса страниц"):
        assert any(h.startswith(needle) for h in heads), needle
    assert not any("Приложение Б" in h for h in heads)               # колонки журнала 1С убраны по просьбе владельца


def test_every_menu_item_of_the_processor_is_described_in_the_manual():
    """В руководстве есть раздел по каждому из 21 пункта меню обработки 1С (и названия сверены с исходником)."""
    text = MANUAL.read_text(encoding="utf-8")
    bsl = (ROOT / "docs" / "1c_kit" / "ObmenSSaitom.bsl").read_text(encoding="utf-8")
    items = re.findall(r'Список\.Добавить\("(\d+)", "(\d+)\.', bsl)
    assert sorted(int(n) for n, _ in items) == list(range(1, 22))      # в обработке ровно пункты 1–21
    covered = set()
    for m in re.finditer(r"^### Пункты? ([\d, ]+)\.", text, flags=re.M):
        covered |= {int(x) for x in re.findall(r"\d+", m.group(1))}
    assert covered == set(range(1, 22)), sorted(set(range(1, 22)) - covered)
    version = re.search(r'Возврат "([^"]+)";', bsl[bsl.index("Функция ВерсияОбработки"):]).group(1)
    assert f"сборка {version}" in text                                  # номер сборки в руководстве = в исходнике


def test_inline_markup_and_lists_of_the_converter():
    doc = build.convert("# Т\n\nПодзаголовок\n\n## Раздел\n\nТекст **жирный**, *курсив*, `код`, [ссылка](/help).\n\n"
                        "1. Один\n   * вложенный\n2. Два\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> **Важно.** Осторожно.\n")
    html = "".join(doc.parts)
    assert "<strong>жирный</strong>" in html and "<em>курсив</em>" in html and "<code>код</code>" in html
    assert '<a href="/help">ссылка</a>' in html
    assert "<ol><li>Один<ul><li>вложенный</li></ul></li><li>Два</li></ol>" in html
    assert "<table>" in html and 'class="callout important"' in html
    # список, который начинается не с 1, сохраняет свой номер
    assert '<ol start="3">' in "".join(build.convert("Текст\n\n3. Третий\n4. Четвёртый\n").parts)
