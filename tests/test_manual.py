# tests/test_manual.py
"""Руководство «Диантус: сайт и 1С — просто» (docs/РУКОВОДСТВО.md) и его сборщик (scripts/build_manual.py).

Проверяем то, что ломается незаметно: пропавшие картинки, «склеенные» нумерованные списки (пункты вместо 1, 2, 3
превращаются в 11, 12, 13), оборванные таблицы, недосказанная разметка — и главное: что руководство не расходится с
настоящим меню обработки 1С и с правилами сайта (размеры фото, статусы заказов)."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "РУКОВОДСТВО.md"
IMAGES = ROOT / "docs" / "manual_img"
BSL = ROOT / "docs" / "1c_kit" / "ObmenSSaitom.bsl"

_spec = importlib.util.spec_from_file_location("build_manual", ROOT / "scripts" / "build_manual.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def _text() -> str:
    return MANUAL.read_text(encoding="utf-8")


def test_every_image_in_the_manual_exists_and_every_image_file_is_used():
    used = set(re.findall(r"!\[[^\]]*\]\((manual_img/[^)]+)\)", _text()))
    assert used, "в руководстве нет ни одной картинки"
    for rel in used:
        assert (ROOT / "docs" / rel).exists(), f"нет файла {rel}"
    on_disk = {f"manual_img/{p.name}" for p in IMAGES.iterdir() if p.is_file()}
    assert on_disk - used == set(), f"картинки не используются в тексте: {sorted(on_disk - used)}"


def test_numbered_lists_run_in_order_and_do_not_glue_together():
    """Список «1…10» + пустая строка + «3…» склеился бы в один и показал бы 11, 12, 13."""
    prev = None
    for n, line in enumerate(_text().split("\n"), 1):
        m = re.match(r"^(\d+)\.\s", line)
        if m:
            num = int(m.group(1))
            assert prev is None or num == prev + 1 or num == 1, f"строка {n}: «{line[:50]}» после {prev}"
            prev = num
        elif line.strip() and not line.startswith(" ") and not re.match(r"^[*-]\s", line):
            prev = None


def test_manual_converts_to_clean_html():
    doc = build.convert(_text())
    assert doc.title.startswith("Диантус: сайт и 1С") and doc.subtitle
    html = build.render(doc)
    visible = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    visible = re.sub(r"data:[^\"']+", "", visible)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert "**" not in visible, "остались незакрытые «**»"
    assert "<!--" not in html
    ids = re.findall(r' id="(s\d+)"', html)
    assert len(ids) == len(set(ids)) and len(ids) >= 20
    # у каждой таблицы во всех строках одинаковое число ячеек
    for table in re.findall(r"<table>.*?</table>", html, flags=re.S):
        widths = {len(re.findall(r"<t[hd][ >]", row)) for row in re.findall(r"<tr>.*?</tr>", table, flags=re.S)}
        assert len(widths) == 1, f"таблица с разным числом колонок: {widths}"


def test_manual_is_short_and_organised_in_cards_for_every_role():
    heads = [t for _lvl, _sid, t in build.convert(_text()).toc]
    for needle in ("Часть 0. Главное за три минуты", "Часть 1. Оператор 1С", "Часть 2. Менеджер",
                   "Часть 3. Владелец", "Часть 4. Покупателю", "Приложение"):
        assert any(h.startswith(needle) for h in heads), needle
    for card in ("Как открыть обмен с сайтом", "Пришла поставка", "Заказ с сайта", "Фото товаров",
                 "Новый клиент", "Заказ клиента", "Что осталось до «100 из 100»", "Почта на Яндексе",
                 "Яндекс Вебмастер", "Юрист и Роскомнадзор", "Спорные товары", "Администратор 1С",
                 "Копия базы сайта вне сервера"):
        assert any(card in h for h in heads), f"нет карточки «{card}»"
    # не возвращаем 60-страничный том: около 20–30 карточек, каждая на одну задачу
    assert 20 <= len(heads) <= 40, len(heads)


def test_manual_describes_the_real_menu_of_the_1c_processor():
    """Названия пунктов меню в руководстве = названиям в исходнике обработки (иначе человек не найдёт пункт)."""
    text = _text()
    bsl = BSL.read_text(encoding="utf-8")
    main = re.findall(r'Список\.Добавить\("(поставка|заказы|фото|связь|доп)", "(\d)\. ([^ ]+(?: [^-]+?)?) - ', bsl)
    assert [m[0] for m in main] == ["поставка", "заказы", "фото", "связь", "доп"], main
    for _code, number, title in main:
        assert f"«{number}. {title.strip()}»" in text, f"в руководстве нет пункта «{number}. {title.strip()}»"
    # подменю «Заказы с сайта»
    for caption in ("Показать новые заказы с сайта", "Заказ оформлен в 1С", "Сменить статус заказа на сайте"):
        assert caption in bsl and caption in text, caption
    # сборка, которая видна в заголовке меню, названа в руководстве
    version = re.search(r'Возврат "([^"]+)";', bsl[bsl.index("Функция ВерсияОбработки"):]).group(1)
    assert f"сборка {version.split(':')[0]}" in text, version
    # вердикты отчётов, которые оператор должен искать глазами, действительно есть в обработке
    for phrase in ("ВСЁ В ПОРЯДКЕ: сайт отвечает, ключ принят", "ИТОГ: проверка прошла, ошибок нет",
                   "ИТОГ: поставка отправлена на сайт", "ПРОБЛЕМА: сайт не отвечает с этого компьютера",
                   "ПРОБЛЕМА: сайт отвечает, но ключ не принят"):
        assert phrase in bsl and phrase.split(":")[0] in text, phrase
    assert "Первая настройка" in text and "не нажимайте" in text.lower()


def test_photo_rules_in_the_manual_match_the_site_code():
    from app.config import config
    from app.services import onec_photos, upload_service

    text = _text()
    assert f"до {onec_photos.MAX_PHOTO_BYTES // (1024 * 1024)} МБ через 1С" in text
    assert f"до {config.MAX_UPLOAD_SIZE // (1024 * 1024)} МБ, если загружаете через админку" in text
    assert f"Больше {upload_service.FULL_SIZE[0]} сайт сам уменьшит" in text
    assert f"до {onec_photos.MAX_PHOTOS_PER_PRODUCT} на один товар" in text
    assert f"Больше {upload_service.MAX_PIXELS // 1_000_000} мегапикселей сайт не примет" in text


def test_order_statuses_in_the_manual_match_the_site():
    from app.models import ORDER_STATUSES

    table = _text()[_text().index("| Статус | Когда ставить |"):]
    for status in ORDER_STATUSES:
        assert f"**{status}**" in table, f"в таблице статусов нет «{status}»"
    assert "→".join(["Новый", "Подтверждён", "В работе", "Отправлен", "Выполнен"]).replace("→", " → ") in _text()


def test_disputed_products_table_lists_twelve_names():
    text = _text()
    block = text[text.index("| Товар в 1С | Папка (сайт показывает) | Карточка | Мой вывод |"):]
    rows = []
    for line in block.split("\n")[2:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    assert len(rows) == 12


def test_inline_markup_lists_code_blocks_and_image_width_hints_of_the_converter():
    doc = build.convert("# Т\n\nПодзаголовок\n\n## Раздел\n\nТекст **жирный**, *курсив*, `код`, [ссылка](/help).\n\n"
                        "1. Один\n   * вложенный\n2. Два\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n> **Важно.** Осторожно.\n\n"
                        "```\nстрока <1>\n  строка 2\n```\n")
    html = "".join(doc.parts)
    assert "<strong>жирный</strong>" in html and "<em>курсив</em>" in html and "<code>код</code>" in html
    assert '<a href="/help">ссылка</a>' in html
    assert "<ol><li>Один<ul><li>вложенный</li></ul></li><li>Два</li></ol>" in html
    assert "<table>" in html and 'class="callout important"' in html
    assert "<pre>строка &lt;1&gt;\n  строка 2</pre>" in html
    # список, который начинается не с 1, сохраняет свой номер (и счёт кружков в PDF)
    third = "".join(build.convert("Текст\n\n3. Третий\n4. Четвёртый\n").parts)
    assert '<ol start="3" style="counter-reset: step 2">' in third
    # подсказка ширины в подписи картинки: ![Подпись {w=60}](путь)
    figure = "".join(build.convert("# Т\n\nП\n\n![Меню {w=60}](manual_img/c-menu-main.png)\n").parts)
    assert 'style="width:60%"' in figure and "<figcaption>Меню</figcaption>" in figure
