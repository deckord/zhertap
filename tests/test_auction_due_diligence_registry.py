from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auction_due_diligence import (
    create_due_diligence_request,
    list_due_diligence_requests,
    record_manual_check_request,
    update_due_diligence_request,
)
from app.models import Account, AuctionLot, Base


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_request_registry_is_owner_scoped_and_tracks_response_deadline() -> None:
    session = _session()
    account_a = Account(id="account-a", phone="+70000000001")
    account_b = Account(id="account-b", phone="+70000000002")
    lot = AuctionLot(
        id="lot-1",
        source="e-qazyna",
        source_lot_id="source-42",
        auction_number="A-42",
        title="Земельный лот",
        source_url="https://example.test/lot/42",
        region="г. Астана",
    )
    session.add_all([account_a, account_b, lot])
    session.flush()

    due_at = datetime.now(UTC) + timedelta(days=10)
    request = create_due_diligence_request(
        session,
        account_id=account_a.id,
        lot_id=lot.id,
        check_code="access",
        response_due_at=due_at,
    )
    session.commit()

    assert len(list_due_diligence_requests(session, account_id=account_a.id, lot_id=lot.id)) == 1
    assert list_due_diligence_requests(session, account_id=account_b.id, lot_id=lot.id) == []

    update_due_diligence_request(
        session,
        account_id=account_a.id,
        request_id=request.id,
        status="sent",
        external_reference="OUT-42",
        submitted_at=datetime.now(UTC),
    )
    session.commit()
    assert request.status == "sent"
    assert request.external_reference == "OUT-42"

    try:
        update_due_diligence_request(
            session,
            account_id=account_b.id,
            request_id=request.id,
            status="verified",
        )
    except ValueError as exc:
        assert str(exc) == "request_not_found"
    else:
        raise AssertionError("cross-account request update must be denied")


def test_request_registry_rejects_invalid_status() -> None:
    session = _session()
    account = Account(id="account-a", phone="+70000000001")
    lot = AuctionLot(
        id="lot-1",
        source="e-qazyna",
        source_lot_id="source-42",
        title="Земельный лот",
        source_url="https://example.test/lot/42",
    )
    session.add_all([account, lot])
    session.flush()
    request = create_due_diligence_request(
        session,
        account_id=account.id,
        lot_id=lot.id,
        check_code="flood",
    )

    try:
        update_due_diligence_request(
            session,
            account_id=account.id,
            request_id=request.id,
            status="made_up",
        )
    except ValueError as exc:
        assert str(exc) == "invalid_request_status"
    else:
        raise AssertionError("invalid status must be rejected")


def test_manual_check_journal_is_idempotent_and_maps_state_machine() -> None:
    session = _session()
    account = Account(id="account-a", phone="+700****0001")
    lot = AuctionLot(
        id="lot-1",
        source="e-qazyna",
        source_lot_id="source-42",
        title="Земельный лот",
        source_url="https://example.test/lot/42",
    )
    session.add_all([account, lot])
    session.flush()

    first = record_manual_check_request(
        session,
        account_id=account.id,
        lot_id=lot.id,
        check_code="electricity",
        check_status="in_progress",
        note="Запрос отправлен владельцу сети",
    )
    second = record_manual_check_request(
        session,
        account_id=account.id,
        lot_id=lot.id,
        check_code="electricity",
        check_status="done",
        note="Ответ получен, техническая возможность подтверждена",
        has_attachment=True,
    )
    session.commit()

    assert first.id == second.id
    assert second.status == "received"
    assert second.received_at is not None
    assert second.response_summary.startswith("Ответ получен")
    assert len(list_due_diligence_requests(session, account_id=account.id, lot_id=lot.id)) == 1
