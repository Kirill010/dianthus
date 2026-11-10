# tests/test_build_epf.py
"""Готовая обработка 1С (.epf) лежит в репозитории рядом с исходным текстом и должна быть собрана ровно из него."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "docs" / "1c_kit"
EPF = KIT / "ОбменССайтом.epf"
BSL = KIT / "ObmenSSaitom.bsl"


def _tool():
    spec = importlib.util.spec_from_file_location("build_epf", ROOT / "scripts" / "build_epf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_epf_is_built_from_the_committed_source():
    tool = _tool()
    same = tool.read_module_text(str(EPF)) == tool.normalize(BSL.read_bytes())
    assert same, ("ОбменССайтом.epf собран из другого текста, чем ObmenSSaitom.bsl. Пересоберите: "
                  "python scripts/build_epf.py docs/1c_kit/ОбменССайтом.epf docs/1c_kit/ObmenSSaitom.bsl "
                  "docs/1c_kit/ОбменССайтом.epf")


def test_build_replaces_only_the_module_text(tmp_path):
    tool = _tool()
    source = "// проверка\r\nПроцедура Тест()\r\nКонецПроцедуры\r\n".encode("utf-8")
    src = tmp_path / "m.bsl"
    src.write_bytes(source)
    out = tmp_path / "out.epf"
    tool.build(str(EPF), str(src), str(out))
    assert tool.read_module_text(str(out)) == tool.normalize(source)
    # структура контейнера та же: те же служебные записи, что и в шаблоне
    before = [e["name"] for e in tool.parse_container(EPF.read_bytes())["entries"]]
    after = [e["name"] for e in tool.parse_container(out.read_bytes())["entries"]]
    assert before == after
