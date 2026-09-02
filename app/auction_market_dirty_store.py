"""Short-transaction persistence for event-driven W9 invalidation."""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.auction_market_dirty_state import (
    POLICY_VERSION,
    ComparableCurrentChange,
    InventoryGenerationDelta,
    MarketDirtyAction,
    MarketTargetState,
    bind_action_claim,
    build_inventory_generation_delta,
    optimistic_claim_allowed,
    optimistic_completion_allowed,
)
from app.auction_verified_comparable_inventory import InventoryFact, normalize_inventory_fact
from app.auction_verified_comparable_repository import (
    ComparableIngestResult,
    _aware,
    _current_values,
    _prepare_material,
)
from app.models import (
    AuctionMarketInventoryGeneration,
    AuctionMarketTargetState,
    AuctionVerifiedComparableCurrent,
    AuctionVerifiedComparableObservation,
)

MAX_BATCH = 100
_SQLITE_LOCK = threading.Lock()


class MarketDirtyStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ComparableBatchItem:
    fact: InventoryFact
    generation_signature: str
    raw_payload: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ComparableBatchResult:
    results: tuple[ComparableIngestResult, ...]
    delta: InventoryGenerationDelta | None


@dataclass(frozen=True, slots=True)
class _PreparedItem:
    item: ComparableBatchItem
    material: Mapping[str, object]
    content_hash: str


def _ingest_one(
    session: Session, prepared: _PreparedItem
) -> tuple[ComparableIngestResult, ComparableCurrentChange | None]:
    item = prepared.item
    material = prepared.material
    content_hash = prepared.content_hash
    identity = str(material["source_identity_key"])
    existing = session.scalar(
        select(AuctionVerifiedComparableObservation).where(
            AuctionVerifiedComparableObservation.source_identity_key == identity,
            AuctionVerifiedComparableObservation.content_hash == content_hash,
        )
    )
    inserted = existing is None
    if existing is None:
        existing = AuctionVerifiedComparableObservation(**material)
        session.add(existing)
        session.flush()
    current = session.get(AuctionVerifiedComparableCurrent, identity, with_for_update=True)
    old_lat = (
        float(current.latitude) if current is not None and current.latitude is not None else None
    )
    old_lon = (
        float(current.longitude) if current is not None and current.longitude is not None else None
    )
    status_priority = {"found": 0, "error": 1, "conflict": 2}
    incoming_rank = (
        _aware(item.fact.observed_at),
        item.fact.sequence_id,
        status_priority[item.fact.fact_status],
        content_hash,
    )
    current_rank = (
        (
            _aware(current.observed_at),
            current.source_sequence_id,
            status_priority[current.fact_status],
            current.content_hash,
        )
        if current is not None
        else None
    )
    effective = existing
    effective_material = material
    effective_fact = item.fact
    # Equal-rank divergent found facts deterministically publish a conflict tombstone.
    if (
        current is not None
        and current.fact_status == "found"
        and item.fact.fact_status == "found"
        and incoming_rank[:2] == current_rank[:2]
        and current.content_hash != content_hash
    ):
        conflict_fact = normalize_inventory_fact(
            {
                "sequence_id": item.fact.sequence_id,
                "source_name": item.fact.source_name,
                "source_record_id": f"rank-conflict:{item.fact.sequence_id}",
                "source_sale_id": item.fact.source_sale_id,
                "source_listing_id": item.fact.source_listing_id,
                "fact_status": "conflict",
                "price_kind": item.fact.price_kind,
                "observed_at": item.fact.observed_at,
                "provenance_refs": [
                    f"normalized-content:{current.content_hash}",
                    f"normalized-content:{content_hash}",
                ],
                "conflict_fields": ["same_rank_divergence"],
            }
        )
        effective_material, conflict_hash = _prepare_material(
            conflict_fact,
            generation_signature=item.generation_signature,
            raw_payload=None,
        )
        effective = session.scalar(
            select(AuctionVerifiedComparableObservation).where(
                AuctionVerifiedComparableObservation.source_identity_key == identity,
                AuctionVerifiedComparableObservation.content_hash == conflict_hash,
            )
        )
        if effective is None:
            effective = AuctionVerifiedComparableObservation(**effective_material)
            session.add(effective)
            session.flush()
        effective_fact = conflict_fact
        content_hash = conflict_hash
        incoming_rank = (
            _aware(effective_fact.observed_at),
            effective_fact.sequence_id,
            status_priority[effective_fact.fact_status],
            content_hash,
        )
    current_changed = current is None or (
        current.observation_id != effective.id and incoming_rank >= current_rank
    )
    if current is None:
        current = AuctionVerifiedComparableCurrent(
            **_current_values(effective_material, effective.id)
        )
        session.add(current)
    elif current_changed:
        for key, value in _current_values(effective_material, effective.id).items():
            setattr(current, key, value)
    session.flush()
    result = ComparableIngestResult(
        source_identity_key=identity,
        observation_id=int(existing.id),
        inserted=inserted,
        current_changed=current_changed,
        current_observation_id=int(current.observation_id),
        content_hash=content_hash,
    )
    change = None
    if current_changed:
        change = ComparableCurrentChange(
            source_identity_key=identity,
            old_latitude=old_lat,
            old_longitude=old_lon,
            new_latitude=float(current.latitude) if current.latitude is not None else None,
            new_longitude=float(current.longitude) if current.longitude is not None else None,
        )
    return result, change


def ingest_verified_comparable_batch(
    session_factory: Callable[[], Session],
    items: Sequence[ComparableBatchItem],
    *,
    completed_at: datetime,
) -> ComparableBatchResult:
    """Commit one provider page and at most one monotonic generation atomically."""
    if not 0 < len(items) <= MAX_BATCH:
        raise MarketDirtyStoreError("invalid_batch_size")
    identities = [item.fact for item in items]
    # Preparing happens before the DB transaction and validates duplicate identities.
    prepared_items: list[_PreparedItem] = []
    for item in items:
        material, content_hash = _prepare_material(
            item.fact,
            generation_signature=item.generation_signature,
            raw_payload=item.raw_payload,
        )
        prepared_items.append(_PreparedItem(item, material, content_hash))
    prepared_keys = [str(prepared.material["source_identity_key"]) for prepared in prepared_items]
    if len(prepared_keys) != len(set(prepared_keys)):
        raise MarketDirtyStoreError("duplicate_source_identity_in_batch")
    del identities
    with session_factory() as probe:
        sqlite = probe.get_bind().dialect.name == "sqlite"
    lock = _SQLITE_LOCK if sqlite else None
    if lock:
        lock.acquire()
    try:
        with session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('auction-market-generation'))")
                )
            results: list[ComparableIngestResult] = []
            changes: list[ComparableCurrentChange] = []
            for prepared in prepared_items:
                result, change = _ingest_one(session, prepared)
                results.append(result)
                if change is not None:
                    changes.append(change)
            delta = None
            if changes:
                latest = (
                    session.scalar(select(func.max(AuctionMarketInventoryGeneration.generation)))
                    or 0
                )
                delta = build_inventory_generation_delta(
                    int(latest) + 1, changes, completed_at=completed_at
                )
                session.add(
                    AuctionMarketInventoryGeneration(
                        generation=delta.generation,
                        generation_signature=delta.generation_signature,
                        changed_cells_json=json.dumps(
                            list(delta.changed_cells), separators=(",", ":")
                        ),
                        global_reconciliation=delta.global_reconciliation,
                        changed_identity_count=delta.changed_identity_count,
                        policy_version=delta.policy_version,
                        completed_at=delta.completed_at,
                    )
                )
            session.flush()
        return ComparableBatchResult(tuple(results), delta)
    finally:
        if lock:
            lock.release()


def _state_from_row(row: AuctionMarketTargetState) -> MarketTargetState:
    return MarketTargetState(
        lot_id=row.lot_id,
        target_signature=row.target_signature,
        coverage_cells=tuple(json.loads(row.coverage_cells_json)),
        validated_generation=int(row.validated_generation),
        status=row.status,  # type: ignore[arg-type]
        claim_token=row.claim_token,
        claim_expires_at=_aware(row.claim_expires_at) if row.claim_expires_at else None,
        attempts=int(row.attempts),
        next_attempt_at=_aware(row.next_attempt_at) if row.next_attempt_at else None,
        policy_version=row.policy_version,
    )


def claim_market_action(
    session_factory: Callable[[], Session],
    action: MarketDirtyAction,
    *,
    current_target_signature: str,
    latest_generation: int,
    now: datetime,
    ttl_seconds: int = 360,
) -> MarketDirtyAction | None:
    """Short optimistic claim; zero matched rows means the scan must be repeated."""
    token = uuid.uuid4().hex
    expires = now + timedelta(seconds=max(30, min(ttl_seconds, 600)))
    with session_factory() as probe:
        sqlite = probe.get_bind().dialect.name == "sqlite"
    lock = _SQLITE_LOCK if sqlite else None
    if lock:
        lock.acquire()
    try:
        with session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:lot_id))"),
                    {"lot_id": f"auction-market-target:{action.lot_id}"},
                )
            row = session.get(AuctionMarketTargetState, action.lot_id, with_for_update=True)
            state = _state_from_row(row) if row else None
            if not optimistic_claim_allowed(
                action,
                state,
                current_target_signature=current_target_signature,
                latest_generation=latest_generation,
                now=now,
            ):
                return None
            if row is None:
                row = AuctionMarketTargetState(
                    lot_id=action.lot_id,
                    target_signature=action.target_signature,
                    coverage_cells_json=json.dumps(
                        list(action.coverage_cells), separators=(",", ":")
                    ),
                    validated_generation=action.expected_validated_generation,
                    status="processing",
                    claim_token=token,
                    claim_expires_at=expires,
                    attempts=1,
                    policy_version=POLICY_VERSION,
                    updated_at=now,
                )
                session.add(row)
            else:
                if row.attempts >= 10_000:
                    raise MarketDirtyStoreError("target_attempt_limit_reached")
                row.target_signature = action.target_signature
                row.coverage_cells_json = json.dumps(
                    list(action.coverage_cells), separators=(",", ":")
                )
                row.policy_version = POLICY_VERSION
                row.status = "processing"
                row.claim_token = token
                row.claim_expires_at = expires
                row.attempts += 1
                row.updated_at = now
            session.flush()
        return bind_action_claim(action, token)
    finally:
        if lock:
            lock.release()


def complete_market_action(
    session_factory: Callable[[], Session],
    action: MarketDirtyAction,
    *,
    current_target_signature: str,
    status: str,
    now: datetime,
) -> bool:
    if status not in {"ready", "insufficient"} or action.expected_claim_token is None:
        raise MarketDirtyStoreError("invalid_completion")
    with session_factory() as session, session.begin():
        row = session.get(AuctionMarketTargetState, action.lot_id, with_for_update=True)
        if row is None or not optimistic_completion_allowed(
            action,
            _state_from_row(row),
            current_target_signature=current_target_signature,
            claim_token=action.expected_claim_token,
        ):
            return False
        result = session.execute(
            update(AuctionMarketTargetState)
            .where(
                AuctionMarketTargetState.lot_id == action.lot_id,
                AuctionMarketTargetState.claim_token == action.expected_claim_token,
                AuctionMarketTargetState.target_signature == action.target_signature,
                AuctionMarketTargetState.validated_generation
                == action.expected_validated_generation,
            )
            .values(
                status=status,
                validated_generation=action.through_generation,
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=None,
                attempts=0,
                updated_at=now,
            )
        )
        return result.rowcount == 1


def fail_market_action(
    session_factory: Callable[[], Session],
    action: MarketDirtyAction,
    *,
    current_target_signature: str,
    now: datetime,
) -> bool:
    """Release an owned claim and schedule bounded exponential retry."""
    token = action.expected_claim_token
    if token is None:
        raise MarketDirtyStoreError("failure_without_claim")
    with session_factory() as session, session.begin():
        row = session.get(AuctionMarketTargetState, action.lot_id, with_for_update=True)
        if row is None or not optimistic_completion_allowed(
            action,
            _state_from_row(row),
            current_target_signature=current_target_signature,
            claim_token=token,
        ):
            return False
        delay = min(3600, 30 * (2 ** min(max(row.attempts - 1, 0), 7)))
        result = session.execute(
            update(AuctionMarketTargetState)
            .where(
                AuctionMarketTargetState.lot_id == action.lot_id,
                AuctionMarketTargetState.claim_token == token,
                AuctionMarketTargetState.target_signature == action.target_signature,
                AuctionMarketTargetState.validated_generation
                == action.expected_validated_generation,
            )
            .values(
                status="error",
                claim_token=None,
                claim_expires_at=None,
                next_attempt_at=now + timedelta(seconds=delay),
                updated_at=now,
            )
        )
        return result.rowcount == 1
