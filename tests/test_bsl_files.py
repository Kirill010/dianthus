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
