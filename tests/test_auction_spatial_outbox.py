from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auction_spatial_outbox import (
    OUTBOX_LEASE,
    claim_spatial_decision_signals,
    dispatch_spatial_decision_outbox,
    mark_spatial_signal_dispatched,
    mark_spatial_signal_failed,
)
from app.models import (
    AuctionLot,
    AuctionSpatialDecisionSignal,
    Base,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _session(*, signal_count: int = 1) -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    with session.begin():
        session.add(
            AuctionLot(
                id="lot-1",
                source="e-qazyna",
                source_lot_id="452662",
                title="lot",
                source_url="https://example.kz/452662",
            )
        )
        session.add_all(
            AuctionSpatialDecisionSignal(
                lot_id="lot-1",
                manifest_hash=f"{index:064x}",
                manifest_watermark=index,
                status="pending",
                attempts=0,
                created_at=NOW,
            )
            for index in range(1, signal_count + 1)
        )
    return session


def _engine(*, signal_count: int = 1):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-1",
                source="e-qazyna",
                source_lot_id="452662",
                title="lot",
                source_url="https://example.kz/452662",
            )
        )
        session.add_all(
            AuctionSpatialDecisionSignal(
                lot_id="lot-1",
                manifest_hash=f"{index:064x}",
                manifest_watermark=index,
                status="pending",
                attempts=0,
                created_at=NOW,
            )
            for index in range(1, signal_count + 1)
        )
    return engine


def test_claim_is_bounded_and_lock_busy_is_not_reselected() -> None:
    session = _session(signal_count=3)
    first = claim_spatial_decision_signals(session, checked_at=NOW, limit=2)
    assert len(first.claims) == 2
    assert first.has_more is True
    assert all(claim.lease_until == NOW + OUTBOX_LEASE for claim in first.claims)

    second = claim_spatial_decision_signals(session, checked_at=NOW, limit=2)
    assert [claim.manifest_watermark for claim in second.claims] == [3]
    assert second.has_more is False


def test_broker_success_is_required_before_dispatched() -> None:
    session = _session()
    claim = claim_spatial_decision_signals(session, checked_at=NOW, limit=1).claims[0]
    row = session.get(AuctionSpatialDecisionSignal, claim.signal_id)
    assert row.status == "failed"
    assert row.dispatched_at is None
    session.rollback()

    assert mark_spatial_signal_dispatched(session, claim, dispatched_at=NOW) is True
    row = session.get(AuctionSpatialDecisionSignal, claim.signal_id)
    assert row.status == "dispatched"
    assert row.dispatched_at is not None
    session.rollback()


def test_failure_backoff_and_expired_lease_recovery_without_sleep() -> None:
    session = _session()
    claim = claim_spatial_decision_signals(session, checked_at=NOW, limit=1).claims[0]
    delay = mark_spatial_signal_failed(session, claim, failed_at=NOW)
    assert delay is not None and 2 <= delay <= 3
    assert not claim_spatial_decision_signals(
        session, checked_at=NOW + timedelta(seconds=1), limit=1
    ).claims

    recovered = claim_spatial_decision_signals(
        session, checked_at=NOW + timedelta(seconds=4), limit=1
    ).claims[0]
    assert recovered.signal_id == claim.signal_id
    assert recovered.attempt == 2


def test_stale_claim_cannot_ack_newer_lease() -> None:
    session = _session()
    old = claim_spatial_decision_signals(session, checked_at=NOW, limit=1).claims[0]
    newer = claim_spatial_decision_signals(
        session, checked_at=NOW + OUTBOX_LEASE + timedelta(seconds=1), limit=1
    ).claims[0]
    assert newer.signal_id == old.signal_id
    assert mark_spatial_signal_dispatched(session, old, dispatched_at=NOW) is False
    assert mark_spatial_signal_dispatched(session, newer, dispatched_at=NOW) is True


def test_at_least_once_duplicate_after_broker_crash_is_idempotent_by_watermark() -> None:
    session = _session()
    first = claim_spatial_decision_signals(session, checked_at=NOW, limit=1).claims[0]
    # Simulates broker acceptance followed by worker death before acknowledgement.
    replay = claim_spatial_decision_signals(
        session, checked_at=NOW + OUTBOX_LEASE + timedelta(seconds=1), limit=1
    ).claims[0]
    assert (replay.lot_id, replay.manifest_watermark) == (
        first.lot_id,
        first.manifest_watermark,
    )
    assert mark_spatial_signal_dispatched(session, replay, dispatched_at=NOW) is True
    assert len(
        session.scalars(
            select(AuctionSpatialDecisionSignal).where(
                AuctionSpatialDecisionSignal.lot_id == "lot-1",
                AuctionSpatialDecisionSignal.manifest_watermark == 1,
            )
        ).all()
    ) == 1


def test_dispatcher_enqueues_outside_transaction_then_acknowledges() -> None:
    engine = _engine(signal_count=2)
    def factory() -> Session:
        return Session(engine, expire_on_commit=False)

    observed: list[tuple[int, ...]] = []

    def enqueue(claims) -> None:
        observed.append(tuple(claim.manifest_watermark for claim in claims))
        with factory() as independent:
            assert independent.in_transaction() is False

    report = dispatch_spatial_decision_outbox(
        factory, enqueue, checked_at=NOW, limit=10
    )
    assert observed == [(1, 2)]
    assert report.dispatched == 2
    with factory() as session:
        statuses = session.scalars(
            select(AuctionSpatialDecisionSignal.status).order_by(
                AuctionSpatialDecisionSignal.id
            )
        ).all()
    assert statuses == ["dispatched", "dispatched"]


def test_dispatcher_broker_failure_is_deferred_without_sleep() -> None:
    engine = _engine()
    def factory() -> Session:
        return Session(engine, expire_on_commit=False)


    def unavailable(_claims) -> None:
        raise ConnectionError("broker unavailable")

    report = dispatch_spatial_decision_outbox(
        factory, unavailable, checked_at=NOW, limit=10
    )
    assert report.dispatched == 0
    assert report.failed == 1
    assert report.retry_after_seconds is not None
    with factory() as session:
        row = session.scalar(select(AuctionSpatialDecisionSignal))
        assert row.status == "failed"
        assert row.next_attempt_at is not None
