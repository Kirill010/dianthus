# tests/test_data_check.py
"""scripts/data_check.py: находит задвоения и странности в данных сайта и при этом ничего не меняет."""
import importlib.util
import sys
from pathlib import Path

from app.models import SupplyItem
from tests.helpers import seed_item

_spec = importlib.util.spec_from_file_location("data_check", Path(__file__).resolve().parent.parent / "scripts" / "data_check.py")
dc = importlib.util.module_from_spec(_spec)
sys.modules["data_check"] = dc            # нужно для dataclass внутри скрипта
_spec.loader.exec_module(dc)


def titles(findings, level):
    return [f.title for f in findings if f.level == level]


def from_1c(db, item, number="00000001"):
    item.supply.external_id = f"Инвойс-{number}"
    item.supply.number = number
    db.commit()
    return item


def test_clean_catalog_has_no_problems(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза Фридом", external_id="G-1"))
    stats, findings = dc.run_checks(mem_db)
    assert stats["shelf_items"] == 1 and stats["shelf_packs"] == 10 and stats["shelf_stems"] == 250
    assert not titles(findings, "problem")


def test_two_products_with_the_same_1c_code_are_a_problem(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза А", external_id="G-1"), "00000001")
    from_1c(mem_db, seed_item(mem_db, name="Роза Б", external_id="G-1"), "00000002")
    _, findings = dc.run_checks(mem_db)
    assert "Два товара с одним и тем же кодом 1С" in titles(findings, "problem")


def test_same_position_twice_in_one_supply_is_a_problem(mem_db):
    item = from_1c(mem_db, seed_item(mem_db, name="Роза Фридом", external_id="G-1"))
    mem_db.add(SupplyItem(supply_id=item.supply_id, product_id=item.product_id, price=50, stock=3, is_active=True))
    mem_db.commit()
    _, findings = dc.run_checks(mem_db)
    assert "Одна позиция дважды в одной поставке" in titles(findings, "problem")


def test_same_name_on_two_cards_is_a_warning_not_a_problem(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза Фридом 60", external_id="G-1"), "00000001")
    from_1c(mem_db, seed_item(mem_db, name="роза  фридом 60", external_id="G-2"), "00000002")
    _, findings = dc.run_checks(mem_db)
    assert any("одинаковым названием" in t for t in titles(findings, "warn"))
    assert not titles(findings, "problem")


def test_manual_supply_on_the_shelf_is_a_warning(mem_db):
    seed_item(mem_db, name="Роза ручная")                              # без идентификатора 1С
    _, findings = dc.run_checks(mem_db)
    assert "Ручные поставки (не из 1С) выставлены на полку" in titles(findings, "warn")


def test_shelf_position_without_a_price_is_a_problem(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза без цены", external_id="G-1", price=0.0))
    _, findings = dc.run_checks(mem_db)
    assert "На полке позиции без цены" in titles(findings, "problem")


def test_last_real_send_is_not_confused_with_a_dry_run(mem_db):
    from datetime import datetime, timedelta, timezone

    from app.models import ExchangeLog
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    mem_db.add(ExchangeLog(endpoint="supplies/sync", ok=True, status_code=200, summary="инвойсов: 78, новых: 1", created_at=now - timedelta(hours=30)))
    mem_db.add(ExchangeLog(endpoint="supplies/sync", ok=True, status_code=200, summary="ПРОВЕРКА, ничего не сохранено — инвойсов: 78", created_at=now - timedelta(hours=2)))
    mem_db.add(ExchangeLog(endpoint="orders/export", ok=False, status_code=401, summary="Неверный ключ", created_at=now - timedelta(hours=1)))
    mem_db.commit()
    _, findings = dc.run_checks(mem_db, now)
    real = next(f for f in findings if f.title.startswith("Последняя настоящая отправка"))
    assert "30 ч назад" in real.title and any("только проверка" in d for d in real.details)
    failed = next(f for f in findings if f.title.startswith("Неудачных обращений 1С"))
    assert failed.level == "warn" and "401" in failed.details[0]


def test_the_check_changes_nothing(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза Фридом", external_id="G-1"))
    before = mem_db.query(SupplyItem).count()
    dc.run_checks(mem_db)
    dc.shelf_rows(mem_db)
    assert mem_db.query(SupplyItem).count() == before
    assert not mem_db.new and not mem_db.dirty and not mem_db.deleted


def test_rows_for_comparison_with_1c(mem_db):
    from_1c(mem_db, seed_item(mem_db, name="Роза Фридом", external_id="G-1", price=42.5, pack=25, stock=4), "00000624")
    rows = dc.shelf_rows(mem_db)
    assert rows == [["00000624", "", "Роза Фридом", "25", "4", "42.50", "Эквадор"]]
