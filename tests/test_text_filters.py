"""Фильтры шаблонов для описаний страниц: падеж страны и число с существительным."""
from app.templating import _from_country, _plural


def test_plural_russian_forms():
    forms = ("позиция", "позиция".replace("я", "и"), "позиций")
    assert [_plural(n, *forms) for n in (1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 101, 111, 253, 0)] == [
        "позиция", "позиции", "позиции", "позиций", "позиций", "позиций", "позиций",
        "позиция", "позиции", "позиций", "позиция", "позиций", "позиции", "позиций",
    ]


def test_plural_bad_value_falls_back_to_many():
    assert _plural(None, "страна", "страны", "стран") == "стран"
    assert _plural("не число", "страна", "страны", "стран") == "стран"


def test_from_country_genitive():
    assert _from_country("Кения") == "Кении"
    assert _from_country("Эквадор") == "Эквадора"
    assert _from_country("Голландия") == "Голландии"
    assert _from_country("Россия") == "России"
    assert _from_country("Израиль") == "Израиля"
    assert _from_country("  кения ") == "Кении"


def test_from_country_unknown_name_is_kept_as_is():
    assert _from_country("ЮАР") == "ЮАР"
    assert _from_country("") == ""
    assert _from_country(None) == ""
