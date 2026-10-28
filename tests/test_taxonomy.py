# tests/test_taxonomy.py
"""Справочники-фильтры (вид, цвет, рост, страна — отдельные таблицы): поиск и создание значений
без дублей, разбор того, что прислала 1С, порядок показа, догадки по названию."""
import pytest

from app.models import (DEFAULT_COLORS, DEFAULT_COUNTRIES, DEFAULT_HEIGHTS, DEFAULT_KINDS,
                        Color, Country, Height, Kind, Product)
from app.services import taxonomy
from app.services.taxonomy import (CLEAR, Ref, Taxonomy, TaxonomyError, describe, extract_ref,
                                   parse_height, seed_defaults, set_taxonomy)


def counts(db):
    return [db.query(m).count() for m in (Kind, Color, Height, Country)]


# ───────────── поиск и создание без дублей ─────────────

def test_the_same_name_in_any_spelling_is_one_row(mem_db):
    tax = Taxonomy(mem_db)
    first = tax.by_name("color", "Жёлтый")
    for spelling in (" жёлтый ", "ЖЁЛТЫЙ", "желтый", "Жёлтый\xa0"):
        assert tax.by_name("color", spelling) is first
    assert mem_db.query(Color).count() == 1 and first.name == "Жёлтый"
    assert tax.report() == {"color": ["Жёлтый"]}


def test_a_new_name_starts_with_a_capital_letter(mem_db):
    row = Taxonomy(mem_db).by_name("kind", "роза кустовая")
    assert row.name == "Роза кустовая"


def test_height_is_a_number_of_centimetres(mem_db):
    tax = Taxonomy(mem_db)
    row = tax.by_name("height", 60)
    for spelling in (60.0, "60", "60 см", " 60 CM ", "60,0"):
        assert tax.by_name("height", spelling) is row
    assert row.cm == 60 and row.name == "60 см" and mem_db.query(Height).count() == 1


@pytest.mark.parametrize("value", ["высокий", "5", 9, 301, 1000, "60-70", True, "6.5"])
def test_nonsense_height_is_an_error_not_a_row(mem_db, value):
    with pytest.raises(TaxonomyError):
        Taxonomy(mem_db).by_name("height", value)
    assert mem_db.query(Height).count() == 0


def test_parse_height_empty_is_none():
    assert parse_height(None) is None and parse_height("") is None and parse_height("  ") is None


def test_new_values_go_to_the_end_of_the_list(mem_db):
    tax = Taxonomy(mem_db)
    a, b, c = (tax.by_name("country", n) for n in ("Эквадор", "Кения", "Мексика"))
    assert a.sort_order < b.sort_order < c.sort_order
    assert [r.name for r in taxonomy.ordered("country", tax.rows("country"))] == ["Эквадор", "Кения", "Мексика"]
    assert [r.cm for r in taxonomy.ordered("height", [tax.by_name("height", 80), tax.by_name("height", 50)])] == [50, 80]


# ───────────── ссылки по id ─────────────

def test_resolve_by_site_id_by_1c_id_and_by_name(mem_db):
    tax = Taxonomy(mem_db)
    rose = tax.by_name("kind", "Роза")
    assert tax.resolve(Ref(kind="kind", id=rose.id)) is rose
    tax.resolve(Ref(kind="kind", name="Роза", external_id="K-1"))          # сверили справочник
    assert rose.external_id == "K-1"
    assert tax.resolve(Ref(kind="kind", external_id="K-1")) is rose
    with pytest.raises(TaxonomyError):
        tax.resolve(Ref(kind="kind", id=999))                               # такого id нет
    with pytest.raises(TaxonomyError):
        tax.resolve(Ref(kind="kind", external_id="K-404"))                  # без названия новую не создать
    assert tax.resolve(Ref(kind="kind", external_id="K-404"), create=False) is None
    assert mem_db.query(Kind).count() == 1


def test_a_name_does_not_overwrite_an_existing_1c_id(mem_db):
    tax = Taxonomy(mem_db)
    row = tax.resolve(Ref(kind="country", name="Кения", external_id="C-1"))
    tax.resolve(Ref(kind="country", name="Кения", external_id="C-2"))
    assert row.external_id == "C-1"


# ───────────── что прислала 1С ─────────────

def test_extract_ref_distinguishes_missing_empty_and_filled():
    assert extract_ref({"name": "Товар"}, "color") is None                # ключа нет — не трогаем
    assert extract_ref({"color": ""}, "color").clear is True              # пусто — убрать
    assert extract_ref({"color": None}, "color").clear is True
    assert extract_ref({"height": 0}, "height").clear is True             # 0 вместо «неизвестно»
    ref = extract_ref({"color": "Белый", "color_id": 3, "color_external_id": "C-5"}, "color")
    assert (ref.name, ref.id, ref.external_id, ref.clear) == ("Белый", 3, "C-5", False)
    only_id = extract_ref({"kind_external_id": "K-9"}, "kind")
    assert only_id.external_id == "K-9" and only_id.name is None and not only_id.clear


def test_old_field_names_are_still_understood():
    assert extract_ref({"category": "Роза"}, "kind").name == "Роза"
    assert extract_ref({"length_cm": 60}, "height").name == 60
    assert extract_ref({"height_cm": 70}, "height").name == 70
    assert extract_ref({"height": 80}, "height").name == 80


def test_a_value_may_be_an_object(mem_db):
    ref = extract_ref({"color": {"id": 2, "external_id": "C-2", "name": "Красный", "hex": "#c0392b"}}, "color")
    assert (ref.id, ref.external_id, ref.name, ref.hex) == (2, "C-2", "Красный", "#c0392b")
    h = extract_ref({"height": {"id": 4, "cm": 60}}, "height")
    assert (h.id, h.name) == (4, 60)


@pytest.mark.parametrize("item,kind", [({"kind_id": "abc"}, "kind"), ({"color": {"id": "x"}}, "color")])
def test_non_numeric_ids_are_a_clear_error(item, kind):
    with pytest.raises(TaxonomyError):
        extract_ref(item, kind)


def test_resolve_refs_returns_clear_markers_and_skips_missing(mem_db):
    tax = Taxonomy(mem_db)
    out = tax.resolve_refs({"kind": Ref(kind="kind", name="Роза"), "color": Ref(kind="color", clear=True),
                            "height": None})
    assert out["color"] is CLEAR and out["kind"].name == "Роза" and "height" not in out


def test_color_hex_is_validated():
    assert taxonomy.clean_hex("#C0392B") == "#c0392b"
    for bad in ("red", "#fff", "c0392b", "#gggggg", None, ""):
        assert taxonomy.clean_hex(bad) == ""


# ───────────── стартовый набор ─────────────

def test_seed_defaults_is_idempotent_and_keeps_the_written_order(mem_db):
    created = seed_defaults(mem_db)
    mem_db.commit()
    assert set(created) == {"kind", "color", "height", "country"}
    assert counts(mem_db) == [len(DEFAULT_KINDS), len(DEFAULT_COLORS), len(DEFAULT_HEIGHTS), len(DEFAULT_COUNTRIES)]
    shown = lambda kind, rows: [r.name for r in taxonomy.ordered(kind, rows)]
    assert shown("kind", mem_db.query(Kind).all()) == DEFAULT_KINDS
    assert shown("country", mem_db.query(Country).all()) == DEFAULT_COUNTRIES
    assert shown("color", mem_db.query(Color).all()) == [n for n, _ in DEFAULT_COLORS]
    assert mem_db.query(Color).filter(Color.name == "Белый").one().hex == "#ffffff"

    assert seed_defaults(mem_db) == {}                                      # второй раз — ничего нового
    assert counts(mem_db) == [len(DEFAULT_KINDS), len(DEFAULT_COLORS), len(DEFAULT_HEIGHTS), len(DEFAULT_COUNTRIES)]


def test_seed_puts_values_that_were_already_there_after_the_starting_set(mem_db):
    Taxonomy(mem_db).by_name("kind", "Эустома")                             # пришла из 1С раньше стартового набора
    mem_db.flush()
    seed_defaults(mem_db)
    names = [r.name for r in taxonomy.ordered("kind", mem_db.query(Kind).all())]
    assert names == DEFAULT_KINDS + ["Эустома"]


# ───────────── догадки по названию ─────────────

def test_guesses_fill_only_what_is_empty(mem_db):
    tax = Taxonomy(mem_db)
    product = Product(name="Роза белая Кения 50", package_size=25)
    tax.fill_empty_from_name(product)
    assert (product.kind_name, product.color_name, product.height_cm, product.country_name) == (
        "Роза", "Белый", 50, "Кения")
    tax.assign(product, {"country": Ref(kind="country", name="Эквадор")})
    tax.fill_empty_from_name(product)
    assert product.country_name == "Эквадор"                                # заполненное не затирается


def test_guesses_skip_what_1c_just_cleared(mem_db):
    tax = Taxonomy(mem_db)
    product = Product(name="Роза белая Кения 50", package_size=25)
    tax.fill_empty_from_name(product, skip={"color", "height"})
    assert product.color_name == "" and product.height_cm == 0 and product.country_name == "Кения"


def test_set_taxonomy_by_names_and_empty_string_clears(mem_db):
    product = Product(name="Тест", package_size=1)
    set_taxonomy(mem_db, product, kind="Роза", color="Красный", height=60, country="Эквадор")
    assert (product.kind_name, product.color_name, product.height_cm) == ("Роза", "Красный", 60)
    set_taxonomy(mem_db, product, color="", height=None)                    # None — не трогать, "" — убрать
    assert (product.color_name, product.height_cm, product.kind_name) == ("", 60, "Роза")


def test_usage_counts_and_the_catalog_names_in_the_report(mem_db):
    a, b = Product(name="А", package_size=1), Product(name="Б", package_size=1)
    set_taxonomy(mem_db, a, kind="Роза", height=60)
    set_taxonomy(mem_db, b, kind="Роза", height=70)
    mem_db.add_all([a, b])
    mem_db.commit()
    usage = taxonomy.usage_counts(mem_db)
    rose = mem_db.query(Kind).one()
    assert usage["kind"] == {rose.id: 2} and sorted(usage["height"].values()) == [1, 1] and usage["color"] == {}
    assert describe({"color": ["Белый"], "height": ["65 см"]}) == "цвет: Белый; рост: 65 см"
    assert describe({"kind": [f"К{i}" for i in range(7)]}).endswith("К4…")


def test_locking_a_product_row_uses_no_joins(mem_db):
    """На PostgreSQL «FOR UPDATE» нельзя применять к запросу с внешними соединениями. Раньше вид, цвет, рост
    и страна подгружались JOIN-ами, и загрузка фото из 1С (она блокирует строку товара) падала бы на боевой базе;
    SQLite этого не замечает, поэтому проверяем сам текст запроса."""
    sql = str(mem_db.query(Product).filter(Product.id == 1).with_for_update())
    assert "JOIN" not in sql.upper()
