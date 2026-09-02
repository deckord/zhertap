from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auction_market_dirty_state import (
    POLICY_VERSION,
    MarketTargetInput,
    build_inventory_generation_delta,
    coverage_cells,
    target_signature,
)
from app.auction_market_dirty_worker import recompute_market_dirty_page
from app.db import Base
from app.models import (
    AuctionLot,
    AuctionMarketInventoryGeneration,
    AuctionMarketScanCursor,
    AuctionMarketTargetState,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite3'}")
    Base.metadata.create_all(
        engine,
        tables=[
            AuctionLot.__table__,
            AuctionMarketInventoryGeneration.__table__,
            AuctionMarketTargetState.__table__,
            AuctionMarketScanCursor.__table__,
        ],
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _target(lot_id: str) -> MarketTargetInput:
    return MarketTargetInput(
        lot_id=lot_id,
        right_type="lease",
        purpose_group="camping",
        lease_term_years=3,
        area_ha=1,
        latitude=50.411,
        longitude=80.227,
        access_readiness="ready",
        infrastructure_readiness="partial",
        canonical_object_id=f"parcel-{lot_id}",
        source_sale_id=lot_id,
    )


@dataclass(frozen=True)
class _Result:
    changed: bool
    status: str = "ok"


def test_generation_scan_recomputes_once_then_durable_cursor_quiesces(
    tmp_path, monkeypatch
) -> None:
    import app.auction_market_dirty_worker as worker

    factory = _factory(tmp_path)
    delta = build_inventory_generation_delta(1, [], completed_at=NOW)
    with factory() as session, session.begin():
        session.add_all(
            [
                AuctionLot(
                    id=f"lot-{index}",
                    source="e-qazyna",
                    source_lot_id=str(index),
                    object_type="land",
                    title=f"Lot {index}",
                    source_url=f"https://example/{index}",
                    active=True,
                )
                for index in range(2)
            ]
        )
        session.add(
            AuctionMarketInventoryGeneration(
                generation=1,
                generation_signature=delta.generation_signature,
                changed_cells_json="[]",
                global_reconciliation=True,
                changed_identity_count=0,
                policy_version=delta.policy_version,
                completed_at=NOW,
            )
        )
    monkeypatch.setattr(
        worker,
        "load_global_market_target_inputs",
        lambda _f, lots: [_target(lot) for lot in lots],
    )
    calls = []
    monkeypatch.setattr(
        worker,
        "recompute_market_from_global_inventory",
        lambda _f, lot, **_k: calls.append(lot) or _Result(changed=False),
    )
    first = recompute_market_dirty_page(factory, limit=1, now=NOW)
    second = recompute_market_dirty_page(factory, limit=1, now=NOW)
    third = recompute_market_dirty_page(factory, limit=1, now=NOW)
    assert first.has_more is True
    assert second.has_more is False
    assert third.status == "quiescent"
    assert calls == ["lot-0", "lot-1"]
    with factory() as session:
        states = list(session.query(AuctionMarketTargetState).all())
        assert {state.status for state in states} == {"ready"}


def test_due_queue_pages_independently_without_corrupting_main_cursor(
    tmp_path, monkeypatch
) -> None:
    import app.auction_market_dirty_worker as worker

    factory = _factory(tmp_path)
    targets = [_target(f"lot-{index:02}") for index in range(30)]
    with factory() as session, session.begin():
        for target in targets:
            session.add(
                AuctionLot(
                    id=target.lot_id,
                    source="e-qazyna",
                    source_lot_id=target.lot_id,
                    object_type="land",
                    title=target.lot_id,
                    source_url=f"https://example/{target.lot_id}",
                    active=True,
                )
            )
            session.add(
                AuctionMarketTargetState(
                    lot_id=target.lot_id,
                    target_signature=target_signature(target),
                    coverage_cells_json=json.dumps(
                        list(coverage_cells(target.latitude, target.longitude))
                    ),
                    validated_generation=0,
                    status="error",
                    attempts=1,
                    next_attempt_at=NOW,
                    policy_version=POLICY_VERSION,
                    updated_at=NOW,
                )
            )
        session.add(
            AuctionMarketScanCursor(
                policy_version=POLICY_VERSION,
                scan_cursor_lot_id=None,
                high_water_lot_id="lot-29",
                latest_generation=0,
                updated_at=NOW,
            )
        )
    monkeypatch.setattr(
        worker,
        "load_global_market_target_inputs",
        lambda _f, ids: [_target(item) for item in ids],
    )
    calls = []
    monkeypatch.setattr(
        worker,
        "recompute_market_from_global_inventory",
        lambda _f, lot, **_k: calls.append(lot) or _Result(False),
    )
    first = recompute_market_dirty_page(factory, limit=25, now=NOW)
    second = recompute_market_dirty_page(factory, limit=25, now=NOW)
    assert first.has_more is True
    assert second.has_more is False
    assert len(calls) == 30
    with factory() as session:
        cursor = session.get(AuctionMarketScanCursor, POLICY_VERSION)
        assert cursor.scan_cursor_lot_id is None
        assert cursor.high_water_lot_id == "lot-29"


def test_generation_committed_during_cpu_forces_immediate_restart(tmp_path, monkeypatch) -> None:
    import app.auction_market_dirty_worker as worker

    factory = _factory(tmp_path)
    first_delta = build_inventory_generation_delta(1, [], completed_at=NOW)
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-race",
                source="e-qazyna",
                source_lot_id="race",
                object_type="land",
                title="race",
                source_url="https://example/race",
                active=True,
            )
        )
        session.add(
            AuctionMarketInventoryGeneration(
                generation=1,
                generation_signature=first_delta.generation_signature,
                changed_cells_json="[]",
                global_reconciliation=True,
                changed_identity_count=0,
                policy_version=POLICY_VERSION,
                completed_at=NOW,
            )
        )
    monkeypatch.setattr(
        worker, "load_global_market_target_inputs", lambda _f, ids: [_target(i) for i in ids]
    )

    def race(_factory, _lot, **_kwargs):
        second_delta = build_inventory_generation_delta(2, [], completed_at=NOW)
        with factory() as session, session.begin():
            session.add(
                AuctionMarketInventoryGeneration(
                    generation=2,
                    generation_signature=second_delta.generation_signature,
                    changed_cells_json="[]",
                    global_reconciliation=True,
                    changed_identity_count=0,
                    policy_version=POLICY_VERSION,
                    completed_at=NOW,
                )
            )
        return _Result(False)

    monkeypatch.setattr(worker, "recompute_market_from_global_inventory", race)
    result = recompute_market_dirty_page(factory, limit=25, now=NOW)
    assert result.has_more is True
    assert result.next_scan_cursor is None
    with factory() as session:
        assert session.get(AuctionMarketScanCursor, POLICY_VERSION) is None


def test_target_changed_after_cpu_releases_claim_and_never_counts_changed(
    tmp_path, monkeypatch
) -> None:
    import app.auction_market_dirty_worker as worker

    factory = _factory(tmp_path)
    delta = build_inventory_generation_delta(1, [], completed_at=NOW)
    with factory() as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-mutated",
                source="e-qazyna",
                source_lot_id="mutated",
                object_type="land",
                title="mutated",
                source_url="https://example/mutated",
                active=True,
            )
        )
        session.add(
            AuctionMarketInventoryGeneration(
                generation=1,
                generation_signature=delta.generation_signature,
                changed_cells_json="[]",
                global_reconciliation=True,
                changed_identity_count=0,
                policy_version=POLICY_VERSION,
                completed_at=NOW,
            )
        )
    reads = 0

    def target_reads(_factory, ids):
        nonlocal reads
        reads += 1
        target = _target(ids[0])
        return [target if reads == 1 else replace(target, area_ha=2)]

    monkeypatch.setattr(worker, "load_global_market_target_inputs", target_reads)
    monkeypatch.setattr(
        worker,
        "recompute_market_from_global_inventory",
        lambda *_args, **_kwargs: _Result(True),
    )
    result = recompute_market_dirty_page(factory, limit=25, now=NOW)
    assert result.changed == 0
    assert result.errors == 1
    assert result.has_more is True
    with factory() as session:
        state = session.get(AuctionMarketTargetState, "lot-mutated")
        assert state.status == "error"
        assert state.claim_token is None
