from datetime import datetime, timedelta

import pytest

from app.models import (ApiKey, AuditLog, Claim, ClaimPhoto, ConsentLog,
                        DataRequest, Kind, LegalDocument, LegalDocumentRevision,
                        Notification, Order, OrderItem, PasswordReset, Preorder,
                        Product, SiteSetting, Supply, SupplyItem, User)
from app.services.api_keys import create_key
from scripts.clear_all_safe import clear_all_data
from tests.helpers import make_order, make_user, seed_item


def _linked_client_data(db):
    admin = make_user(db, "boss@test.ru", admin=True)
    buyer = make_user(db, "buyer@test.ru")
    item = seed_item(db, stock=20)
    active = make_order(db, buyer, item, packs=2)
    cancelled = make_order(db, buyer, item, packs=3, status="Отменён")
    claim = Claim(user_id=buyer.id, order_id=active.id,
                  product_name=item.product.name, description="Тестовая рекламация")
    db.add(claim)
    document = LegalDocument(slug="cleanup-test", title="Тест", body="Текст",
                             updated_by_id=buyer.id)
    db.add_all([claim, document])
    db.flush()
    db.add_all([
        ClaimPhoto(claim_id=claim.id, url="/static/uploads/cleanup-test.jpg"),
        Preorder(user_id=buyer.id, product_id=item.product_id,
                 supply_item_id=item.id, quantity=1),
        Notification(user_id=buyer.id, order_id=active.id, text="Тест"),
        ConsentLog(user_id=buyer.id, kind="pd", granted=True),
        PasswordReset(user_id=buyer.id, token_hash="cleanup-test",
                      expires_at=datetime.utcnow() + timedelta(hours=1)),
        DataRequest(user_id=buyer.id, handled_by_id=buyer.id,
                    email_snapshot=buyer.email, kind="export"),
        AuditLog(user_id=buyer.id, action="test"),
        SiteSetting(key="cleanup-test", value="1", updated_by_id=buyer.id),
    ])
    db.add(LegalDocumentRevision(document_id=document.id, version=1,
                                 title="Тест", body="Текст", saved_by_id=buyer.id))
    db.commit()
    key, _ = create_key(db, "Тест очистки", admin=buyer)
    return admin, buyer, item, active, cancelled, key, document


def test_clear_all_data_respects_foreign_keys_and_preserves_admin(mem_db, monkeypatch):
    removed_files = []
    monkeypatch.setattr("app.services.legal.delete_upload", removed_files.append)
    admin, buyer, item, _, _, key, document = _linked_client_data(mem_db)
    assert item.stock == 15

    result = clear_all_data(mem_db)

    assert result["stock"]["returned_packs"] == 2
    assert result["orders"] == 2
    assert result["clients"] == 1
    assert result["preorders"] == 1
    assert removed_files == ["/static/uploads/cleanup-test.jpg"]
    mem_db.expire_all()
    assert [u.id for u in mem_db.query(User).all()] == [admin.id]
    for model in (Order, OrderItem, Preorder, Claim, ClaimPhoto, ConsentLog,
                  PasswordReset, Product, SupplyItem, Supply):
        assert mem_db.query(model).count() == 0, model.__name__
    assert mem_db.query(Notification).count() == 0
    assert mem_db.query(DataRequest).one().user_id is None
    assert mem_db.query(DataRequest).one().handled_by_id is None
    assert mem_db.query(AuditLog).one().user_id is None
    assert mem_db.get(ApiKey, key.id).created_by_id is None
    assert mem_db.get(LegalDocument, document.id).updated_by_id is None
    assert mem_db.query(LegalDocumentRevision).one().saved_by_id is None
    assert mem_db.query(SiteSetting).one().updated_by_id is None
    assert mem_db.query(Kind).count() > 0


def test_clear_all_data_rolls_back_everything_on_error(mem_db, monkeypatch):
    _, buyer, item, _, _, _, _ = _linked_client_data(mem_db)

    def fail(*args):
        raise RuntimeError("Проверка отката")

    monkeypatch.setattr("scripts.clear_all_safe.purge_users", fail)
    with pytest.raises(RuntimeError, match="Проверка отката"):
        clear_all_data(mem_db)

    mem_db.expire_all()
    assert mem_db.get(User, buyer.id) is not None
    assert mem_db.query(Order).count() == 2
    assert mem_db.query(Preorder).count() == 1
    assert mem_db.query(Claim).count() == 1
    assert mem_db.get(SupplyItem, item.id).stock == 15
