from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.auction_market_dirty_state import (
    MAX_BATCH,
    ComparableCurrentChange,
    MarketDirtyStateError,
    MarketTargetInput,
    MarketTargetState,
    bind_action_claim,
    build_inventory_generation_delta,
    coverage_cells,
    optimistic_claim_allowed,
    optimistic_completion_allowed,
    select_market_dirty_actions,
    target_signature,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _target(index: int = 1, **changes) -> MarketTargetInput:
    values = {
        "lot_id": f"lot-{index:04}",
        "right_type": "lease",
        "purpose_group": "camping",
        "lease_term_years": 3.0,
        "area_ha": 1.0,
        "latitude": 50.4111,
        "longitude": 80.2275,
        "access_readiness": "ready",
        "infrastructure_readiness": "ready",
        "canonical_object_id": f"land-{index}",
        "source_sale_id": str(452_000 + index),
    }
    values.update(changes)
    return MarketTargetInput(**values)


def _state(target: MarketTargetInput, generation: int = 1, **changes) -> MarketTargetState:
    values = {
        "lot_id": target.lot_id,
        "target_signature": target_signature(target),
        "coverage_cells": coverage_cells(target.latitude, target.longitude),
        "validated_generation": generation,
        "status": "ready",
    }
    values.update(changes)
    return MarketTargetState(**values)


def _delta(generation: int, lat: float | None, lon: float | None, **changes):
    change = ComparableCurrentChange(
        source_identity_key=f"source-{generation}",
        old_latitude=None,
        old_longitude=None,
        new_latitude=lat,
        new_longitude=lon,
        **changes,
    )
    return build_inventory_generation_delta(generation, [change], completed_at=NOW)


def test_nearby_inventory_change_recomputes_but_distant_only_advances_watermark() -> None:
    target = _target()
    state = _state(target)
    nearby = _delta(2, 50.412, 80.228)
    near_result = select_market_dirty_actions(
        [target], {target.lot_id: state}, [nearby], latest_generation=2, now=NOW
    )
    assert near_result.actions[0].action == "recompute"
    assert near_result.actions[0].reason == "nearby_inventory_changed"

    distant = _delta(2, 43.2, 76.8)
    distant_result = select_market_dirty_actions(
        [target], {target.lot_id: state}, [distant], latest_generation=2, now=NOW
    )
    assert distant_result.actions[0].action == "advance_watermark"
    assert distant_result.actions[0].reason == "distant_inventory_only"


def test_removal_marks_old_cell_and_unknown_change_forces_global_reconciliation() -> None:
    target = _target()
    state = _state(target)
    removed = build_inventory_generation_delta(
        2,
        [
            ComparableCurrentChange(
                source_identity_key="removed",
                old_latitude=50.4111,
                old_longitude=80.2275,
                new_latitude=None,
                new_longitude=None,
            )
        ],
        completed_at=NOW,
    )
    result = select_market_dirty_actions(
        [target], {target.lot_id: state}, [removed], latest_generation=2, now=NOW
    )
    assert result.actions[0].reason == "nearby_inventory_changed"

    unknown = _delta(2, None, None)
    assert unknown.global_reconciliation is True
    global_result = select_market_dirty_actions(
        [target], {target.lot_id: state}, [unknown], latest_generation=2, now=NOW
    )
    assert global_result.actions[0].reason == "global_reconciliation"


def test_target_fingerprint_change_recomputes_without_inventory_change() -> None:
    target = _target()
    changed = replace(target, access_readiness="partial")
    result = select_market_dirty_actions(
        [changed],
        {target.lot_id: _state(target)},
        [],
        latest_generation=1,
        now=NOW,
    )
    assert target_signature(target) != target_signature(changed)
    assert result.actions[0].reason == "target_changed"


def test_generation_gap_fails_closed_instead_of_advancing_watermark() -> None:
    target = _target()
    result = select_market_dirty_actions(
        [target],
        {target.lot_id: _state(target, generation=1)},
        [_delta(3, 43.2, 76.8)],
        latest_generation=3,
        now=NOW,
    )
    assert result.actions[0].action == "recompute"
    assert result.actions[0].reason == "generation_gap"


def test_452662_missing_coordinates_is_initial_recompute_for_insufficient_snapshot() -> None:
    target = _target(
        452662,
        latitude=None,
        longitude=None,
        access_readiness="unknown",
        infrastructure_readiness="unknown",
    )
    result = select_market_dirty_actions(
        [target], {}, [], latest_generation=0, now=NOW
    )
    assert result.actions[0].action == "recompute"
    assert result.actions[0].coverage_cells == ()
    assert result.actions[0].reason == "initial_target"


def test_due_error_retries_but_future_backoff_quiesces() -> None:
    target = _target()
    due = _state(target, status="error", next_attempt_at=NOW - timedelta(seconds=1))
    future = _state(target, status="error", next_attempt_at=NOW + timedelta(hours=1))
    due_result = select_market_dirty_actions(
        [target], {target.lot_id: due}, [], latest_generation=1, now=NOW
    )
    future_result = select_market_dirty_actions(
        [target], {target.lot_id: future}, [], latest_generation=1, now=NOW
    )
    assert due_result.actions[0].reason == "retry_due"
    assert future_result.actions == ()


def test_keyset_batch_is_capped_and_deterministic() -> None:
    targets = [_target(index) for index in range(1, MAX_BATCH + 2)]
    result = select_market_dirty_actions(
        list(reversed(targets)), {}, [], latest_generation=1, now=NOW, limit=999
    )
    assert len(result.actions) == MAX_BATCH
    assert result.has_more is True
    assert result.next_scan_cursor == "lot-0100"
    second = select_market_dirty_actions(
        targets,
        {},
        [],
        latest_generation=1,
        now=NOW,
        after_lot_id=result.next_scan_cursor,
    )
    assert [action.lot_id for action in second.actions] == ["lot-0101"]


def test_bounds_nonfinite_and_naive_timestamps_are_explicit() -> None:
    with pytest.raises(MarketDirtyStateError, match="invalid_target_coordinates"):
        coverage_cells(float("nan"), 80.0)
    with pytest.raises(MarketDirtyStateError, match="invalid_target_numeric"):
        target_signature(_target(latitude=float("inf")))
    with pytest.raises(MarketDirtyStateError, match="completed_at_not_aware"):
        build_inventory_generation_delta(1, [], completed_at=NOW.replace(tzinfo=None))


def test_sparse_source_pages_advance_scan_cursor_independent_of_dirty_actions() -> None:
    first_page = [_target(index) for index in range(1, 5_001)]
    states = {target.lot_id: _state(target) for target in first_page}
    first = select_market_dirty_actions(
        first_page,
        states,
        [],
        latest_generation=1,
        now=NOW,
        source_has_more=True,
    )
    assert first.actions == ()
    assert first.scanned_count == 5_000
    assert first.has_more is True
    assert first.next_scan_cursor == "lot-5000"

    later_dirty = _target(5_001)
    second = select_market_dirty_actions(
        [later_dirty],
        {},
        [],
        latest_generation=1,
        now=NOW,
        after_lot_id=first.next_scan_cursor,
        source_has_more=False,
    )
    assert second.actions[0].lot_id == "lot-5001"


def test_claim_ownership_and_optimistic_completion_reject_concurrent_changes() -> None:
    target = _target()
    initial = select_market_dirty_actions(
        [target], {}, [], latest_generation=1, now=NOW
    ).actions[0]
    assert optimistic_claim_allowed(
        initial,
        None,
        current_target_signature=target_signature(target),
        latest_generation=1,
        now=NOW,
    )
    assert not optimistic_claim_allowed(
        initial,
        None,
        current_target_signature=target_signature(target),
        latest_generation=2,
        now=NOW,
    )
    claimed_action = bind_action_claim(initial, "claim-1")
    processing = MarketTargetState(
        lot_id=target.lot_id,
        target_signature=claimed_action.target_signature,
        coverage_cells=claimed_action.coverage_cells,
        validated_generation=0,
        status="processing",
        claim_token="claim-1",
        claim_expires_at=NOW + timedelta(minutes=5),
        attempts=1,
    )
    assert optimistic_completion_allowed(
        claimed_action,
        processing,
        current_target_signature=target_signature(target),
        claim_token="claim-1",
    )
    assert not optimistic_completion_allowed(
        claimed_action,
        replace(processing, validated_generation=1),
        current_target_signature=target_signature(target),
        claim_token="claim-1",
    )
    assert not optimistic_claim_allowed(
        claimed_action,
        processing,
        current_target_signature=target_signature(target),
        latest_generation=1,
        now=NOW,
    )
    changed_target = replace(target, area_ha=2.0)
    assert not optimistic_completion_allowed(
        claimed_action,
        processing,
        current_target_signature=target_signature(changed_target),
        claim_token="claim-1",
    )


def test_active_claim_is_not_selected_and_expired_claim_is_recovered() -> None:
    target = _target()
    active = _state(
        target,
        status="processing",
        claim_token="active",
        claim_expires_at=NOW + timedelta(minutes=1),
    )
    expired = replace(active, claim_expires_at=NOW - timedelta(seconds=1))
    active_result = select_market_dirty_actions(
        [target], {target.lot_id: active}, [_delta(2, 50.411, 80.227)], latest_generation=2, now=NOW
    )
    expired_result = select_market_dirty_actions(
        [target],
        {target.lot_id: expired},
        [_delta(2, 50.411, 80.227)],
        latest_generation=2,
        now=NOW,
    )
    assert active_result.actions == ()
    assert expired_result.actions[0].reason == "claim_expired"


def test_duplicate_identity_changes_and_invalid_state_delta_are_rejected() -> None:
    duplicate = ComparableCurrentChange("same", None, None, 50.0, 80.0)
    with pytest.raises(MarketDirtyStateError, match="duplicate_source_identity_change"):
        build_inventory_generation_delta(1, [duplicate, duplicate], completed_at=NOW)
    target = _target()
    invalid_state = replace(_state(target), target_signature="not-a-hash")
    with pytest.raises(MarketDirtyStateError, match="invalid_target_state_identity"):
        select_market_dirty_actions(
            [target], {target.lot_id: invalid_state}, [], latest_generation=1, now=NOW
        )


def test_target_and_batch_contract_validation_is_strict() -> None:
    with pytest.raises(MarketDirtyStateError, match="lease_target_without_term"):
        target_signature(_target(lease_term_years=None))
    with pytest.raises(MarketDirtyStateError, match="incomplete_target_coordinates"):
        target_signature(_target(longitude=None))
    with pytest.raises(MarketDirtyStateError, match="invalid_batch_limit"):
        select_market_dirty_actions([], {}, [], latest_generation=0, now=NOW, limit=True)
