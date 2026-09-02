from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app.auction_market_dirty_state import (
    MarketTargetInput,
    select_market_dirty_actions,
    target_signature,
)
from app.auction_market_dirty_store import (
    ComparableBatchItem,
    claim_market_action,
    complete_market_action,
    fail_market_action,
    ingest_verified_comparable_batch,
)
from app.auction_verified_comparable_inventory import normalize_inventory_fact
from app.db import Base
from app.models import (
    AuctionLot,
    AuctionMarketInventoryGeneration,
    AuctionMarketTargetState,
    AuctionVerifiedComparableCurrent,
    AuctionVerifiedComparableObservation,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dirty.sqlite3'}")
    Base.metadata.create_all(
        engine,
        tables=[
            AuctionLot.__table__,
            AuctionVerifiedComparableObservation.__table__,
            AuctionVerifiedComparableCurrent.__table__,
            AuctionMarketInventoryGeneration.__table__,
            AuctionMarketTargetState.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _fact(index: int):
    return normalize_inventory_fact(
        {
            "sequence_id": index,
            "source_name": "e-qazyna",
            "source_record_id": f"record-{index}",
            "source_sale_id": f"sale-{index}",
            "source_url": f"https://example/{index}",
            "object_id": f"parcel-{index}",
            "fact_status": "found",
            "price_kind": "verified_sale",
            "verification_status": "verified",
            "verification_ref": f"protocol:{index}",
            "right_type": "lease",
            "purpose_group": "camping",
            "lease_term_years": 3,
            "area_ha": 1,
            "price_kzt": 10_000_000 + index,
            "latitude": 50.411 + index / 10_000,
            "longitude": 80.227,
            "access_readiness": "ready",
            "infrastructure_readiness": "partial",
            "event_at": NOW - timedelta(days=2),
            "observed_at": NOW - timedelta(minutes=index),
            "title": f"Sale {index}",
            "locality": "Semey",
            "provenance_refs": [f"protocol:{index}"],
            "conflict_fields": [],
        }
    )


def _item(index: int) -> ComparableBatchItem:
    return ComparableBatchItem(_fact(index), f"{index:064x}")


def test_provider_page_commits_one_generation_and_noop_page_does_not_churn(tmp_path) -> None:
    factory = _factory(tmp_path)
    first = ingest_verified_comparable_batch(factory, [_item(1), _item(2)], completed_at=NOW)
    second = ingest_verified_comparable_batch(factory, [_item(1), _item(2)], completed_at=NOW)
    assert first.delta is not None
    assert first.delta.generation == 1
    assert first.delta.changed_identity_count == 2
    assert second.delta is None
    with factory() as session:
        assert session.scalar(select(func.count(AuctionMarketInventoryGeneration.generation))) == 1
        assert (
            session.scalar(select(func.count(AuctionVerifiedComparableCurrent.observation_id))) == 2
        )


def test_same_rank_divergence_is_fail_closed_as_current_conflict(tmp_path) -> None:
    factory = _factory(tmp_path)
    first = _item(1)
    divergent = ComparableBatchItem(
        replace(first.fact, price_kzt=first.fact.price_kzt + 1), "f" * 64
    )
    ingest_verified_comparable_batch(factory, [first], completed_at=NOW)
    result = ingest_verified_comparable_batch(factory, [divergent], completed_at=NOW)
    assert result.delta is not None
    with factory() as session:
        current = session.scalar(select(AuctionVerifiedComparableCurrent))
        assert current.fact_status == "conflict"
        assert current.conflicts_json == '["same_rank_divergence"]'


def test_batch_100_prepares_normal_material_before_transaction(tmp_path, monkeypatch) -> None:
    import app.auction_market_dirty_store as store

    factory = _factory(tmp_path)
    transaction_depth = 0

    def began(*_args):
        nonlocal transaction_depth
        transaction_depth += 1

    def ended(*_args):
        nonlocal transaction_depth
        transaction_depth = max(0, transaction_depth - 1)

    event.listen(factory.class_, "after_begin", began)
    event.listen(factory.class_, "after_transaction_end", ended)
    original = store._prepare_material
    calls = 0

    def guarded_prepare(*args, **kwargs):
        nonlocal calls
        assert transaction_depth == 0
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "_prepare_material", guarded_prepare)
    result = ingest_verified_comparable_batch(
        factory, [_item(index) for index in range(1, 101)], completed_at=NOW
    )
    assert calls == 100
    assert result.delta is not None
    assert result.delta.changed_identity_count == 100


def test_claim_and_completion_use_signature_generation_and_token_guards(tmp_path) -> None:
    factory = _factory(tmp_path)
    lot = AuctionLot(
        id="lot-1",
        source="e-qazyna",
        source_lot_id="1",
        object_type="land",
        title="Lot",
        source_url="https://example/lot/1",
        active=True,
    )
    with factory() as session, session.begin():
        session.add(lot)
    target = MarketTargetInput(
        lot_id="lot-1",
        right_type="lease",
        purpose_group="camping",
        lease_term_years=3,
        area_ha=1,
        latitude=50.411,
        longitude=80.227,
        access_readiness="ready",
        infrastructure_readiness="partial",
        canonical_object_id="parcel-1",
        source_sale_id="1",
    )
    action = select_market_dirty_actions([target], {}, [], latest_generation=0, now=NOW).actions[0]
    claimed = claim_market_action(
        factory,
        action,
        current_target_signature=target_signature(target),
        latest_generation=0,
        now=NOW,
    )
    assert claimed is not None
    forged = replace(claimed, expected_claim_token="wrong")
    assert not complete_market_action(
        factory,
        forged,
        current_target_signature=target_signature(target),
        status="insufficient",
        now=NOW,
    )
    assert complete_market_action(
        factory,
        claimed,
        current_target_signature=target_signature(target),
        status="insufficient",
        now=NOW,
    )
    with factory() as session:
        state = session.get(AuctionMarketTargetState, "lot-1")
        assert state.status == "insufficient"
        assert state.claim_token is None
        assert state.attempts == 0


def test_target_change_claim_updates_material_and_failure_has_guarded_backoff(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-2",
                source="e-qazyna",
                source_lot_id="2",
                object_type="land",
                title="Lot 2",
                source_url="https://example/lot/2",
                active=True,
            )
        )
    old_target = MarketTargetInput(
        lot_id="lot-2",
        right_type="lease",
        purpose_group="camping",
        lease_term_years=3,
        area_ha=1,
        latitude=50.411,
        longitude=80.227,
        access_readiness="ready",
        infrastructure_readiness="partial",
        canonical_object_id="parcel-2",
        source_sale_id="2",
    )
    initial = select_market_dirty_actions(
        [old_target], {}, [], latest_generation=0, now=NOW
    ).actions[0]
    first_claim = claim_market_action(
        factory,
        initial,
        current_target_signature=target_signature(old_target),
        latest_generation=0,
        now=NOW,
    )
    assert first_claim is not None
    assert complete_market_action(
        factory,
        first_claim,
        current_target_signature=target_signature(old_target),
        status="ready",
        now=NOW,
    )
    changed = replace(old_target, area_ha=2)
    with factory() as session:
        row = session.get(AuctionMarketTargetState, "lot-2")
        old_state = replace(
            _state_for_test(row),
        )
    action = select_market_dirty_actions(
        [changed], {"lot-2": old_state}, [], latest_generation=0, now=NOW
    ).actions[0]
    claimed = claim_market_action(
        factory,
        action,
        current_target_signature=target_signature(changed),
        latest_generation=0,
        now=NOW,
    )
    assert claimed is not None
    assert not fail_market_action(
        factory,
        replace(claimed, expected_claim_token="forged"),
        current_target_signature=target_signature(changed),
        now=NOW,
    )
    assert fail_market_action(
        factory,
        claimed,
        current_target_signature=target_signature(changed),
        now=NOW,
    )
    with factory() as session:
        row = session.get(AuctionMarketTargetState, "lot-2")
        assert row.target_signature == target_signature(changed)
        assert row.status == "error"
        assert row.claim_token is None
        assert row.next_attempt_at is not None


def test_concurrent_missing_state_claim_has_one_owner(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-race",
                source="e-qazyna",
                source_lot_id="race",
                object_type="land",
                title="Race",
                source_url="https://example/lot/race",
                active=True,
            )
        )
    target = MarketTargetInput(
        lot_id="lot-race",
        right_type="lease",
        purpose_group="camping",
        lease_term_years=3,
        area_ha=1,
        latitude=50.411,
        longitude=80.227,
        access_readiness="ready",
        infrastructure_readiness="partial",
        canonical_object_id="parcel-race",
        source_sale_id="race",
    )
    action = select_market_dirty_actions([target], {}, [], latest_generation=0, now=NOW).actions[0]

    def claim():
        return claim_market_action(
            factory,
            action,
            current_target_signature=target_signature(target),
            latest_generation=0,
            now=NOW,
            ttl_seconds=360,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1
    with factory() as session:
        row = session.get(AuctionMarketTargetState, "lot-race")
        expires = row.claim_expires_at.replace(tzinfo=UTC)
        assert (expires - NOW).total_seconds() == 360


def _state_for_test(row: AuctionMarketTargetState):
    from app.auction_market_dirty_state import MarketTargetState

    return MarketTargetState(
        lot_id=row.lot_id,
        target_signature=row.target_signature,
        coverage_cells=tuple(__import__("json").loads(row.coverage_cells_json)),
        validated_generation=row.validated_generation,
        status=row.status,
        claim_token=row.claim_token,
        claim_expires_at=row.claim_expires_at,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        policy_version=row.policy_version,
    )
