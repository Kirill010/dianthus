# tests/test_compare_1c_site.py
"""scripts/compare_1c_site.py: построчная сверка отчёта 1С «Сверка» со строками полки сайта."""
import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location("compare_1c_site", Path(__file__).resolve().parent.parent / "scripts" / "compare_1c_site.py")
cmp = importlib.util.module_from_spec(_spec)
sys.modules["compare_1c_site"] = cmp
_spec.loader.exec_module(cmp)


def line(invoice, name, price, stems, pack, country="Эквадор", status="received"):
    """Строка отчёта «Сверка»: поля инвойса || поля строки (индексы те же, что в обработке)."""
    left = f"{invoice} | 02.05.26 | Инвойс срезка эквадор | Диантус ООО | ECUANROS S.A. | USD |  | да | {status}"
    right = [name, "Группа", "Роза", "60", "10", "", "", "", "", "", "", country, "", "", "",
             f"{price:.2f}".replace(".", ","), str(stems), str(stems), country, "дерево групп 1С", pack, "из строки", "Путь", country]
    return left + " || " + " | ".join(right)


def site(*rows):
    return "\n".join("\t".join(map(str, r)) for r in rows)


def test_equal_data_gives_no_differences():
    report = cmp.parse_report(line("00000001", "Роза Фридом", 42.5, 300, "25"))
    result = cmp.compare(report, cmp.parse_site(site(["00000001", "", "Роза Фридом", 25, 12, "42.50", "Эквадор"])))
    assert not (result["missing"] or result["extra"] or result["qty"] or result["price"] or result["pack"] or result["country"])
    assert result["packs_1c"] == result["packs_site"] == 12


def test_guessed_pack_with_a_question_mark_is_read_as_a_number():
    report = cmp.parse_report(line("00000001", "Роза", 10, 100, "10?"))
    assert report[0].pack == 10


def test_sales_after_the_last_send_show_up_as_quantity_difference_only():
    report = cmp.parse_report(line("00000001", "Роза Фридом", 42.5, 100, "10"))        # в 1С осталось 10 пачек
    result = cmp.compare(report, cmp.parse_site(site(["00000001", "", "Роза Фридом", 10, 14, "42.50", "Эквадор"])))
    assert [(e, s) for _, e, s in result["qty"]] == [(10, 14)]
    assert not (result["price"] or result["pack"] or result["country"])


def test_item_missing_on_site_and_extra_on_site_and_rename_are_told_apart():
    report = cmp.parse_report("\n".join([
        line("00000001", "Кротон 12", 50, 100, "10"),            # переименован в 1С
        line("00000001", "Новая роза", 30, 200, "10"),           # на сайте её нет
    ]))
    result = cmp.compare(report, cmp.parse_site(site(
        ["00000001", "", "Кротон", 10, 10, "50.00", "Эквадор"],
        ["00000001", "", "Старая роза", 10, 3, "20.00", "Эквадор"],      # в 1С её уже нет
    )))
    assert [m[1] for m in result["missing"]] == ["новая роза"]
    assert [x[1] for x in result["extra"]] == ["старая роза"]
    assert len(result["renamed"]) == 1


def test_lines_below_one_pack_and_in_transit_are_not_expected_on_the_site():
    report = cmp.parse_report("\n".join([
        line("00000001", "Остаток меньше пачки", 10, 7, "10"),
        line("00000002", "В пути", 10, 100, "10", status="in_transit"),
    ]))
    result = cmp.compare(report, {})
    assert result["expect"] == {} and not result["missing"]
