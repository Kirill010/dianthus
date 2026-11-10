# tests/test_bsl_files.py
"""Файлы для 1С (docs/1c_kit/*.bsl) в тесте не запустить: нужна платформа 1С. Но опечатки
ловятся и без неё: два скрипта проверяют парность блоков, скобок и кавычек, неопределённые
имена и число аргументов у собственных процедур. Тест не даёт случайно сломать файлы при правке
(в живой 1С они ещё не запускались, поэтому каждая лишняя ошибка там была бы потерей времени
владельца)."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "docs" / "1c_kit").glob("*.bsl"))
SCRIPTS = ("bsl_lint_structure.py", "bsl_lint_names.py")

BAD_SAMPLE = """Перем Глобальная;

Процедура Тест1(А, Б = 1)
	Результат = А + НеОпределённаяПеременная;
	Тест2(1, 2, 3);
	Возврат 5;
КонецПроцедуры

Функция Тест2(Х)
	Для Каждого Эл Из Х Цикл
		Сообщить(Эл);
	КонецЦикла;
	Возврат;
КонецФункции

Процедура Тест3()
	Значение = Тест1(1);
	Если Значение = 1 Тогда
		Сообщить("нет закрытия"
КонецПроцедуры
"""


def run(script: str, path: Path):
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "scripts" / script), str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_both_1c_files_are_present():
    assert [f.name for f in FILES] == ["ObmenSSaitom.bsl", "Proverka.bsl"]


def test_1c_exchange_has_full_snapshot_and_order_safety_guards():
    source = FILES[0].read_text(encoding="utf-8")
    assert 'Возврат Нас("cap_stock") = "1" И ПустаяСтрока(Нас("invoice_kinds"))' in source
    assert 'ГДЕ (Д.Проведен Или Д.Дата >= &От)' in source
    assert 'Инвойсы.Количество() > 200' in source
    assert '"""full"":true' in source and '"""allow_empty"":true' in source
    assert 'Инвойсы.Количество() = 0 И СтатИнвойсов.Прочитано = 0' in source
    assert 'СтатИнвойсов.Пустых = СтатИнвойсов.Пустых + 1' in source
    assert '/api/1c/orders/ack?numbers=' not in source
    assert 'Подтверждайте каждый заказ отдельно' in source
    assert 'Автоматическое подтверждение заказов запрещено' in source
    assert 'ДействиеЗаказСтатус();' in source
    assert '/api/1c/orders/status' in source


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("bsl", FILES, ids=lambda p: p.name)
def test_1c_file_has_no_typos(script, bsl):
    r = run(script, bsl)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_checkers_really_fail_on_broken_code(script, tmp_path):
    """Проверка проверяльщиков: на заведомо сломанном тексте они обязаны ругаться,
    иначе зелёный тест выше ничего не значит."""
    bad = tmp_path / "bad.bsl"
    bad.write_text(BAD_SAMPLE, encoding="utf-8")
    r = run(script, bad)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ПРОБЛЕМА" in r.stdout


def test_names_checker_counts_structure_keys_and_values(tmp_path):
    """В «Новый Структура("A, B", 1)» не хватает значения: платформа не ругается, но поле останется пустым."""
    bad = tmp_path / "bad.bsl"
    bad.write_text('Процедура Т()\n\tС = Новый Структура("А, Б, В", 1, 2);\nКонецПроцедуры\n', encoding="utf-8")
    r = run("bsl_lint_names.py", bad)
    assert r.returncode == 1 and "ключей 3, значений 2" in r.stdout, r.stdout + r.stderr

    good = tmp_path / "good.bsl"
    good.write_text('Процедура Т()\n\tС = Новый Структура("А, Б",\n\t\t1, Окр(2, 1));\n\tД = Новый Структура("Х");\nКонецПроцедуры\n',
                    encoding="utf-8")
    r = run("bsl_lint_names.py", good)
    assert r.returncode == 0, r.stdout + r.stderr


def test_names_checker_rejects_method_call_on_new(tmp_path):
    """Вторая настоящая ошибка из живой 1С (2026-11-08): «Новый Файл(Путь).Существует()» в режиме
    совместимости 8.2 не компилируется («Ожидается ключевое слово Тогда»)."""
    bad = tmp_path / "bad.bsl"
    bad.write_text("Процедура Т(Путь)\n\tЕсли Новый Файл(Путь).Существует() Тогда\n\t\tСообщить(1);\n\tКонецЕсли;\nКонецПроцедуры\n",
                   encoding="utf-8")
    r = run("bsl_lint_names.py", bad)
    assert r.returncode == 1 and "Новый" in r.stdout, r.stdout + r.stderr

    good = tmp_path / "good.bsl"
    good.write_text("Процедура Т(Путь)\n\tОбъектФайл = Новый Файл(Путь);\n\tЕсли ОбъектФайл.Существует() Тогда\n\t\tСообщить(1);\n\tКонецЕсли;\nКонецПроцедуры\n",
                    encoding="utf-8")
    r = run("bsl_lint_names.py", good)
    assert r.returncode == 0, r.stdout + r.stderr


def test_names_checker_knows_that_system_info_needs_new(tmp_path):
    """Настоящая ошибка из живой 1С (первый запуск, 2026-11-08): СистемнаяИнформация - это ТИП,
    без «Новый» у неё нет свойств, платформа отвечает «Переменная не определена»."""
    bad = tmp_path / "bad.bsl"
    bad.write_text("Процедура Т()\n\tСообщить(СистемнаяИнформация.ВерсияПриложения);\nКонецПроцедуры\n", encoding="utf-8")
    r = run("bsl_lint_names.py", bad)
    assert r.returncode == 1 and "СистемнаяИнформация" in r.stdout, r.stdout + r.stderr

    good = tmp_path / "good.bsl"
    good.write_text(
        "Процедура Т()\n\tСисИнфо = Новый СистемнаяИнформация;\n\tСообщить(СисИнфо.ВерсияПриложения);\nКонецПроцедуры\n",
        encoding="utf-8")
    r = run("bsl_lint_names.py", good)
    assert r.returncode == 0, r.stdout + r.stderr
