"""Bounded worker orchestration for event-driven global W9 refreshes."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from app.auction_eqazyna_verified_sales import (
    load_global_market_target_inputs,
    recompute_market_from_global_inventory,
)
from app.auction_market_dirty_state import (
    MAX_DELTAS,
    POLICY_VERSION,
    InventoryGenerationDelta,
    MarketTargetState,
    select_market_dirty_actions,
    target_signature,
)
from app.auction_market_dirty_store import (
    claim_market_action,
    complete_market_action,
    fail_market_action,
)
from app.models import (
    AuctionLot,
    AuctionMarketInventoryGeneration,
    AuctionMarketScanCursor,
    AuctionMarketTargetState,
)

_SQLITE_CURSOR_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class MarketDirtyWorkerResult:
    status: str
    scanned: int
    recomputed: int
    advanced: int
    changed: int
    errors: int
    has_more: bool
    next_scan_cursor: str | None
    latest_generation: int


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _state(row: AuctionMarketTargetState) -> MarketTargetState:
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


def _delta(row: AuctionMarketInventoryGeneration) -> InventoryGenerationDelta:
    return InventoryGenerationDelta(
        generation=int(row.generation),
        generation_signature=row.generation_signature,
        changed_cells=tuple(json.loads(row.changed_cells_json)),
        global_reconciliation=bool(row.global_reconciliation),
        changed_identity_count=int(row.changed_identity_count),
        completed_at=_aware(row.completed_at),
        policy_version=row.policy_version,
    )


def recompute_market_dirty_page(
    session_factory: Callable[[], Session],
    *,
    limit: int = 25,
    now: datetime | None = None,
) -> MarketDirtyWorkerResult:
    """Scan one durable source page, then claim/compute each action outside read tx."""
    checked = now or datetime.now(UTC)
    bounded = max(1, min(int(limit), 100))
    with session_factory() as session:
        latest = int(
            session.scalar(select(func.max(AuctionMarketInventoryGeneration.generation))) or 0
        )
        cursor = session.get(AuctionMarketScanCursor, POLICY_VERSION)
        max_active = session.scalar(
            select(func.max(AuctionLot.id)).where(
                AuctionLot.active.is_(True), AuctionLot.object_type == "land"
            )
        )
        previous_cursor = cursor.scan_cursor_lot_id if cursor else None
        previous_generation = int(cursor.latest_generation) if cursor else -1
        completed_high_water = cursor.high_water_lot_id if cursor else None
        due_lot_ids: list[str] = []
        if previous_generation != latest:
            after = None
            high_water = max_active
        elif previous_cursor is None:
            if max_active is None or max_active == completed_high_water:
                due_lot_ids = list(
                    session.scalars(
                        select(AuctionMarketTargetState.lot_id)
                        .join(AuctionLot, AuctionLot.id == AuctionMarketTargetState.lot_id)
                        .where(
                            AuctionLot.active.is_(True),
                            AuctionLot.object_type == "land",
                            or_(
                                AuctionMarketTargetState.status == "pending",
                                and_(
                                    AuctionMarketTargetState.status == "error",
                                    or_(
                                        AuctionMarketTargetState.next_attempt_at.is_(None),
                                        AuctionMarketTargetState.next_attempt_at <= checked,
                                    ),
                                ),
                                and_(
                                    AuctionMarketTargetState.status == "processing",
                                    AuctionMarketTargetState.claim_expires_at <= checked,
                                ),
                            ),
                        )
                        .order_by(AuctionMarketTargetState.lot_id.asc())
                        .limit(bounded + 1)
                    )
                )
                if not due_lot_ids:
                    return MarketDirtyWorkerResult("quiescent", 0, 0, 0, 0, 0, False, None, latest)
                after = None
                high_water = completed_high_water
            else:
                after = completed_high_water
                high_water = max_active
        else:
            after = previous_cursor
            high_water = completed_high_water
        if due_lot_ids:
            lot_ids = due_lot_ids
        else:
            conditions = [AuctionLot.active.is_(True), AuctionLot.object_type == "land"]
            if after is not None:
                conditions.append(AuctionLot.id > after)
            if high_water is not None:
                conditions.append(AuctionLot.id <= high_water)
            lot_ids = list(
                session.scalars(
                    select(AuctionLot.id)
                    .where(*conditions)
                    .order_by(AuctionLot.id.asc())
                    .limit(bounded + 1)
                )
            )
        source_has_more = len(lot_ids) > bounded
        lot_ids = lot_ids[:bounded]
        state_rows = list(
            session.scalars(
                select(AuctionMarketTargetState).where(AuctionMarketTargetState.lot_id.in_(lot_ids))
            )
        )
        delta_rows = list(
            session.scalars(
                select(AuctionMarketInventoryGeneration)
                .order_by(AuctionMarketInventoryGeneration.generation.desc())
                .limit(MAX_DELTAS)
            )
        )
    # Target normalization and all W9 CPU run with no read transaction held.
    targets = load_global_market_target_inputs(session_factory, lot_ids)
    states = {row.lot_id: _state(row) for row in state_rows}
    batch = select_market_dirty_actions(
        targets,
        states,
        [_delta(row) for row in reversed(delta_rows)],
        latest_generation=latest,
        now=checked,
        after_lot_id=after,
        source_has_more=source_has_more,
        limit=bounded,
    )
    recomputed = advanced = changed = errors = 0
    targets_by_id = {target.lot_id: target for target in targets}
    for action in batch.actions:
        signature = target_signature(targets_by_id[action.lot_id])
        claimed = claim_market_action(
            session_factory,
            action,
            current_target_signature=signature,
            latest_generation=latest,
            now=checked,
            ttl_seconds=360,
        )
        if claimed is None:
            continue
        try:
            if action.action == "recompute":
                result = recompute_market_from_global_inventory(
                    session_factory,
                    action.lot_id,
                    observed_at=checked,
                    expected_target_signature=signature,
                )
                recomputed += 1
                if result.status == "ok":
                    completion_status = "ready"
                elif result.status == "insufficient_data":
                    completion_status = "insufficient"
                else:
                    raise ValueError("unexpected_market_result_status")
            else:
                advanced += 1
                completion_status = states[action.lot_id].status
                if completion_status not in {"ready", "insufficient"}:
                    completion_status = "insufficient"
            current_signature = target_signature(
                load_global_market_target_inputs(session_factory, [action.lot_id])[0]
            )
            completed = complete_market_action(
                session_factory,
                claimed,
                current_target_signature=current_signature,
                status=completion_status,
                now=checked,
            )
            if not completed:
                errors += 1
                fail_market_action(
                    session_factory,
                    claimed,
                    current_target_signature=signature,
                    now=checked,
                )
            elif action.action == "recompute":
                changed += int(result.changed)
        except Exception:
            errors += 1
            fail_market_action(
                session_factory,
                claimed,
                current_target_signature=signature,
                now=checked,
            )
    next_cursor = batch.next_scan_cursor if batch.has_more else None
    generation_advanced = False
    with session_factory() as probe:
        sqlite = probe.get_bind().dialect.name == "sqlite"
    cursor_lock = _SQLITE_CURSOR_LOCK if sqlite else None
    if cursor_lock:
        cursor_lock.acquire()
    try:
        with session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext('auction-market-scan-cursor'))")
                )
            current_latest = int(
                session.scalar(select(func.max(AuctionMarketInventoryGeneration.generation))) or 0
            )
            generation_advanced = current_latest != latest
            if current_latest == latest and not due_lot_ids:
                cursor = session.get(AuctionMarketScanCursor, POLICY_VERSION, with_for_update=True)
                if cursor is None:
                    cursor = AuctionMarketScanCursor(
                        policy_version=POLICY_VERSION,
                        scan_cursor_lot_id=next_cursor,
                        high_water_lot_id=high_water,
                        latest_generation=latest,
                        updated_at=checked,
                    )
                    session.add(cursor)
                elif cursor.latest_generation in {previous_generation, latest} and (
                    cursor.scan_cursor_lot_id == previous_cursor or previous_generation != latest
                ):
                    cursor.scan_cursor_lot_id = next_cursor
                    cursor.high_water_lot_id = high_water
                    cursor.latest_generation = latest
                    cursor.updated_at = checked
    finally:
        if cursor_lock:
            cursor_lock.release()
    return MarketDirtyWorkerResult(
        "ok" if not errors else "partial",
        batch.scanned_count,
        recomputed,
        advanced,
        changed,
        errors,
        batch.has_more or generation_advanced or errors > 0,
        next_cursor if not generation_advanced else None,
        latest,
    )
