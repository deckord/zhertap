"""Pure design contract for event-driven global-market W9 invalidation.

The future store persists generation deltas and one state row per target.  This
module only computes deterministic cells, target fingerprints and bounded actions;
it performs no database or network I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

KZ_LAT_MIN = 40.0
KZ_LAT_MAX = 56.0
KZ_LON_MIN = 46.0
KZ_LON_MAX = 88.0
CELL_DEGREES = 0.05
RADIUS_KM = 5.0
MAX_CELLS_PER_TARGET = 64
MAX_CHANGED_CELLS = 2_000
MAX_CHANGES = 1_000
MAX_TARGETS = 5_000
MAX_DELTAS = 200
MAX_BATCH = 100
POLICY_VERSION = "market-dirty-state/2026.1"
ATOMIC_GENERATION_COMMIT_CONTRACT = (
    "One database transaction under the inventory-generation advisory lock must: "
    "insert immutable observations, update every authoritative current pointer, derive the "
    "old+new cell changes from those exact locked rows, insert the next monotonic generation "
    "and its cell delta, then commit. Pointer mutation without its delta is forbidden."
)
TARGET_PROFILES = {
    "camping",
    "hospitality",
    "residential",
    "roadside",
    "warehouse",
    "industrial",
    "retail",
    "agriculture",
    "services",
    "data_center",
    "other",
    "unknown",
}


class MarketDirtyStateError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ComparableCurrentChange:
    source_identity_key: str
    old_latitude: float | None
    old_longitude: float | None
    new_latitude: float | None
    new_longitude: float | None
    current_changed: bool = True


@dataclass(frozen=True, slots=True)
class InventoryGenerationDelta:
    generation: int
    generation_signature: str
    changed_cells: tuple[str, ...]
    global_reconciliation: bool
    changed_identity_count: int
    completed_at: datetime
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True, slots=True)
class MarketTargetInput:
    lot_id: str
    right_type: str | None
    purpose_group: str | None
    lease_term_years: float | None
    area_ha: float | None
    latitude: float | None
    longitude: float | None
    access_readiness: str
    infrastructure_readiness: str
    canonical_object_id: str | None
    source_sale_id: str | None


@dataclass(frozen=True, slots=True)
class MarketTargetState:
    lot_id: str
    target_signature: str
    coverage_cells: tuple[str, ...]
    validated_generation: int
    status: Literal["ready", "insufficient", "error", "pending", "processing"]
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    attempts: int = 0
    next_attempt_at: datetime | None = None
    policy_version: str = POLICY_VERSION


@dataclass(frozen=True, slots=True)
class MarketDirtyAction:
    lot_id: str
    action: Literal["recompute", "advance_watermark"]
    reason: str
    target_signature: str
    coverage_cells: tuple[str, ...]
    through_generation: int
    expected_target_signature: str | None
    expected_validated_generation: int
    expected_claim_token: str | None


@dataclass(frozen=True, slots=True)
class MarketDirtyBatch:
    actions: tuple[MarketDirtyAction, ...]
    next_scan_cursor: str | None
    has_more: bool
    scanned_count: int
    latest_generation: int
    policy_version: str = POLICY_VERSION


def _finite(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def _aware(value: datetime) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _cell(latitude: float, longitude: float) -> str:
    lat_index = math.floor((latitude - KZ_LAT_MIN) / CELL_DEGREES)
    lon_index = math.floor((longitude - KZ_LON_MIN) / CELL_DEGREES)
    return f"{lat_index}:{lon_index}"


def coverage_cells(latitude: float, longitude: float) -> tuple[str, ...]:
    """Cover the full 5km bbox; false positives are allowed, false negatives are not."""
    lat = _finite(latitude, KZ_LAT_MIN, KZ_LAT_MAX)
    lon = _finite(longitude, KZ_LON_MIN, KZ_LON_MAX)
    if lat is None or lon is None:
        raise MarketDirtyStateError("invalid_target_coordinates")
    lat_delta = RADIUS_KM / 110.574
    lon_delta = RADIUS_KM / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    lat_min = max(KZ_LAT_MIN, lat - lat_delta)
    lat_max = min(KZ_LAT_MAX, lat + lat_delta)
    lon_min = max(KZ_LON_MIN, lon - lon_delta)
    lon_max = min(KZ_LON_MAX, lon + lon_delta)
    lat_start = math.floor((lat_min - KZ_LAT_MIN) / CELL_DEGREES)
    lat_end = math.floor((lat_max - KZ_LAT_MIN) / CELL_DEGREES)
    lon_start = math.floor((lon_min - KZ_LON_MIN) / CELL_DEGREES)
    lon_end = math.floor((lon_max - KZ_LON_MIN) / CELL_DEGREES)
    cells = tuple(
        f"{lat_index}:{lon_index}"
        for lat_index in range(lat_start, lat_end + 1)
        for lon_index in range(lon_start, lon_end + 1)
    )
    if len(cells) > MAX_CELLS_PER_TARGET:
        raise MarketDirtyStateError("target_cell_coverage_exceeds_bound")
    return tuple(sorted(cells))


def build_inventory_generation_delta(
    generation: int,
    changes: Sequence[ComparableCurrentChange],
    *,
    completed_at: datetime,
) -> InventoryGenerationDelta:
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise MarketDirtyStateError("invalid_generation")
    if not _aware(completed_at):
        raise MarketDirtyStateError("completed_at_not_aware")
    if len(changes) > MAX_CHANGES:
        raise MarketDirtyStateError("too_many_generation_changes")
    cells: set[str] = set()
    global_reconciliation = False
    identities: set[str] = set()
    generation_rows: list[tuple[object, ...]] = []
    for change in changes:
        if not change.current_changed:
            continue
        if not isinstance(change.source_identity_key, str) or not 0 < len(
            change.source_identity_key
        ) <= 128:
            raise MarketDirtyStateError("invalid_source_identity")
        if change.source_identity_key in identities:
            raise MarketDirtyStateError("duplicate_source_identity_change")
        identities.add(change.source_identity_key)
        coordinates_found = False
        for latitude, longitude in (
            (change.old_latitude, change.old_longitude),
            (change.new_latitude, change.new_longitude),
        ):
            if latitude is None and longitude is None:
                continue
            lat = _finite(latitude, KZ_LAT_MIN, KZ_LAT_MAX)
            lon = _finite(longitude, KZ_LON_MIN, KZ_LON_MAX)
            if lat is None or lon is None:
                global_reconciliation = True
                continue
            coordinates_found = True
            cells.add(_cell(lat, lon))
        if not coordinates_found:
            global_reconciliation = True
        generation_rows.append(
            (
                change.source_identity_key,
                _finite(change.old_latitude, KZ_LAT_MIN, KZ_LAT_MAX),
                _finite(change.old_longitude, KZ_LON_MIN, KZ_LON_MAX),
                _finite(change.new_latitude, KZ_LAT_MIN, KZ_LAT_MAX),
                _finite(change.new_longitude, KZ_LON_MIN, KZ_LON_MAX),
            )
        )
    if len(cells) > MAX_CHANGED_CELLS:
        global_reconciliation = True
        cells = set()
    generation_rows.sort(key=lambda item: str(item[0]))
    encoded = json.dumps(
        {
            "generation": generation,
            "rows": generation_rows,
            "global": global_reconciliation,
            "policy": POLICY_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return InventoryGenerationDelta(
        generation=generation,
        generation_signature=hashlib.sha256(encoded.encode()).hexdigest(),
        changed_cells=tuple(sorted(cells)),
        global_reconciliation=global_reconciliation,
        changed_identity_count=len(identities),
        completed_at=completed_at,
    )


def target_signature(target: MarketTargetInput) -> str:
    if not isinstance(target.lot_id, str) or not 0 < len(target.lot_id) <= 36:
        raise MarketDirtyStateError("invalid_lot_id")
    if target.right_type not in {None, "ownership", "lease"}:
        raise MarketDirtyStateError("invalid_target_right")
    if target.right_type == "lease" and target.lease_term_years is None:
        raise MarketDirtyStateError("lease_target_without_term")
    if target.right_type != "lease" and target.lease_term_years is not None:
        raise MarketDirtyStateError("nonlease_target_with_term")
    if target.purpose_group not in TARGET_PROFILES:
        raise MarketDirtyStateError("invalid_target_purpose")
    for value, limit, label in (
        (target.canonical_object_id, 128, "object"),
        (target.source_sale_id, 128, "sale"),
    ):
        if value is not None and (not isinstance(value, str) or not 0 < len(value) <= limit):
            raise MarketDirtyStateError(f"invalid_target_{label}")
    for value, low, high in (
        (target.area_ha, 0.0001, 1_000_000),
        (target.lease_term_years, 0.01, 99),
        (target.latitude, KZ_LAT_MIN, KZ_LAT_MAX),
        (target.longitude, KZ_LON_MIN, KZ_LON_MAX),
    ):
        if value is not None and _finite(value, low, high) is None:
            raise MarketDirtyStateError("invalid_target_numeric")
    if (target.latitude is None) != (target.longitude is None):
        raise MarketDirtyStateError("incomplete_target_coordinates")
    if target.access_readiness not in {"none", "partial", "ready", "unknown"} or (
        target.infrastructure_readiness not in {"none", "partial", "ready", "unknown"}
    ):
        raise MarketDirtyStateError("invalid_target_readiness")
    payload = asdict(target)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _valid_hash(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _validate_cell(value: str) -> bool:
    match = re.fullmatch(r"(\d+):(\d+)", value)
    if match is None:
        return False
    lat_index, lon_index = (int(item) for item in match.groups())
    return 0 <= lat_index <= 320 and 0 <= lon_index <= 840


def _validate_state(
    lot_id: str, state: MarketTargetState, *, latest_generation: int
) -> None:
    if state.lot_id != lot_id or not _valid_hash(state.target_signature):
        raise MarketDirtyStateError("invalid_target_state_identity")
    if state.validated_generation < 0 or state.validated_generation > latest_generation:
        raise MarketDirtyStateError("invalid_target_state_generation")
    if state.attempts < 0 or state.attempts > 10_000:
        raise MarketDirtyStateError("invalid_target_state_attempts")
    if state.status not in {"ready", "insufficient", "error", "pending", "processing"}:
        raise MarketDirtyStateError("invalid_target_state_status")
    if len(state.coverage_cells) > MAX_CELLS_PER_TARGET or any(
        not _validate_cell(cell) for cell in state.coverage_cells
    ):
        raise MarketDirtyStateError("invalid_target_state_cells")
    if tuple(sorted(set(state.coverage_cells))) != state.coverage_cells:
        raise MarketDirtyStateError("noncanonical_target_state_cells")
    if state.claim_token is not None and (
        not isinstance(state.claim_token, str) or not 0 < len(state.claim_token) <= 64
    ):
        raise MarketDirtyStateError("invalid_target_state_claim")
    for timestamp in (state.claim_expires_at, state.next_attempt_at):
        if timestamp is not None and not _aware(timestamp):
            raise MarketDirtyStateError("target_state_timestamp_not_aware")
    if state.status == "processing" and (
        state.claim_token is None or state.claim_expires_at is None
    ):
        raise MarketDirtyStateError("processing_state_without_claim")


def _validate_delta(delta: InventoryGenerationDelta, *, latest_generation: int) -> None:
    if not 0 < delta.generation <= latest_generation or not _valid_hash(
        delta.generation_signature
    ):
        raise MarketDirtyStateError("invalid_generation_delta")
    if delta.policy_version != POLICY_VERSION or not _aware(delta.completed_at):
        raise MarketDirtyStateError("invalid_generation_delta_metadata")
    if delta.changed_identity_count < 0 or delta.changed_identity_count > MAX_CHANGES:
        raise MarketDirtyStateError("invalid_generation_delta_count")
    if len(delta.changed_cells) > MAX_CHANGED_CELLS or any(
        not _validate_cell(cell) for cell in delta.changed_cells
    ):
        raise MarketDirtyStateError("invalid_generation_delta_cells")
    if tuple(sorted(set(delta.changed_cells))) != delta.changed_cells:
        raise MarketDirtyStateError("noncanonical_generation_delta_cells")


def optimistic_completion_allowed(
    action: MarketDirtyAction,
    current_state: MarketTargetState,
    *,
    current_target_signature: str,
    claim_token: str,
) -> bool:
    """Guard the store's UPDATE ... WHERE signature/generation/policy/claim predicate."""
    return (
        current_state.lot_id == action.lot_id
        and current_state.status == "processing"
        and current_state.policy_version == POLICY_VERSION
        and current_state.target_signature == current_target_signature
        and current_target_signature == action.target_signature
        and current_state.validated_generation == action.expected_validated_generation
        and current_state.claim_token == claim_token
        and current_state.claim_token == action.expected_claim_token
    )


def optimistic_claim_allowed(
    action: MarketDirtyAction,
    current_state: MarketTargetState | None,
    *,
    current_target_signature: str,
    latest_generation: int,
    now: datetime,
) -> bool:
    """Guard the short claim transaction before assigning a new claim token.

    The store must compare all fields in its UPDATE/INSERT predicate.  A concurrent
    target edit, generation commit, watermark advance, or live claim forces a fresh
    scan instead of acknowledging stale work.
    """
    if not _aware(now) or latest_generation != action.through_generation:
        return False
    if current_target_signature != action.target_signature:
        return False
    if current_state is None:
        return (
            action.expected_target_signature is None
            and action.expected_validated_generation == 0
            and action.expected_claim_token is None
        )
    if current_state.lot_id != action.lot_id:
        return False
    live_claim = (
        current_state.status == "processing"
        and current_state.claim_expires_at is not None
        and current_state.claim_expires_at > now
    )
    return (
        not live_claim
        and current_state.target_signature == action.expected_target_signature
        and current_state.validated_generation == action.expected_validated_generation
        and current_state.claim_token == action.expected_claim_token
    )


def bind_action_claim(action: MarketDirtyAction, claim_token: str) -> MarketDirtyAction:
    """Return the action token produced by the store's successful optimistic claim."""
    if not isinstance(claim_token, str) or not 0 < len(claim_token) <= 64:
        raise MarketDirtyStateError("invalid_claim_token")
    return MarketDirtyAction(
        **{**asdict(action), "expected_claim_token": claim_token}
    )


def _target_cells(target: MarketTargetInput) -> tuple[str, ...]:
    try:
        return coverage_cells(float(target.latitude), float(target.longitude))
    except (MarketDirtyStateError, TypeError, ValueError):
        return ()


def _deltas_after(
    deltas: Sequence[InventoryGenerationDelta], watermark: int, latest: int
) -> tuple[InventoryGenerationDelta, ...]:
    selected = tuple(
        sorted(
            (delta for delta in deltas if watermark < delta.generation <= latest),
            key=lambda item: item.generation,
        )
    )
    if len(selected) > MAX_DELTAS:
        raise MarketDirtyStateError("too_many_unvalidated_generations")
    return selected


def select_market_dirty_actions(
    targets: Sequence[MarketTargetInput],
    states: Mapping[str, MarketTargetState],
    deltas: Sequence[InventoryGenerationDelta],
    *,
    latest_generation: int,
    now: datetime,
    after_lot_id: str | None = None,
    source_has_more: bool = False,
    limit: int = 100,
) -> MarketDirtyBatch:
    """Return bounded recompute or cheap watermark-advance actions in lot key order."""
    if len(targets) > MAX_TARGETS:
        raise MarketDirtyStateError("too_many_targets")
    if len(deltas) > MAX_DELTAS or not _aware(now):
        raise MarketDirtyStateError("invalid_reconciliation_input")
    if isinstance(latest_generation, bool) or latest_generation < 0:
        raise MarketDirtyStateError("invalid_latest_generation")
    generations = [delta.generation for delta in deltas]
    if len(generations) != len(set(generations)):
        raise MarketDirtyStateError("duplicate_generation_delta")
    for delta in deltas:
        _validate_delta(delta, latest_generation=latest_generation)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise MarketDirtyStateError("invalid_batch_limit")
    bounded = min(limit, MAX_BATCH)
    actions: list[MarketDirtyAction] = []
    ordered = sorted(targets, key=lambda item: item.lot_id)
    scanned_count = 0
    scan_cursor = after_lot_id
    stopped_for_action_limit = False
    for target in ordered:
        if after_lot_id is not None and target.lot_id <= after_lot_id:
            continue
        if len(actions) >= bounded:
            stopped_for_action_limit = True
            break
        scanned_count += 1
        scan_cursor = target.lot_id
        signature = target_signature(target)
        cells = _target_cells(target)
        state = states.get(target.lot_id)
        if state is not None:
            _validate_state(target.lot_id, state, latest_generation=latest_generation)
        action = reason = None
        busy_claim = False
        watermark = state.validated_generation if state else 0
        if state is None:
            action, reason = "recompute", "initial_target"
        elif state.policy_version != POLICY_VERSION:
            action, reason = "recompute", "policy_changed"
        elif state.target_signature != signature or state.coverage_cells != cells:
            action, reason = "recompute", "target_changed"
        elif state.status == "processing" and state.claim_expires_at > now:
            busy_claim = True
        elif state.status == "processing":
            action, reason = "recompute", "claim_expired"
        elif state.status == "pending":
            action, reason = "recompute", "pending"
        elif state.status == "error" and (
            state.next_attempt_at is None or state.next_attempt_at <= now
        ):
            action, reason = "recompute", "retry_due"
        if action is None and not busy_claim and watermark < latest_generation:
            pending_deltas = _deltas_after(deltas, watermark, latest_generation)
            expected = set(range(watermark + 1, latest_generation + 1))
            actual = {delta.generation for delta in pending_deltas}
            if expected != actual:
                action, reason = "recompute", "generation_gap"
            elif any(delta.global_reconciliation for delta in pending_deltas):
                action, reason = "recompute", "global_reconciliation"
            elif cells and any(
                set(cells).intersection(delta.changed_cells) for delta in pending_deltas
            ):
                action, reason = "recompute", "nearby_inventory_changed"
            else:
                action, reason = "advance_watermark", "distant_inventory_only"
        if action is not None:
            actions.append(
                MarketDirtyAction(
                    lot_id=target.lot_id,
                    action=action,  # type: ignore[arg-type]
                    reason=str(reason),
                    target_signature=signature,
                    coverage_cells=cells,
                    through_generation=latest_generation,
                    expected_target_signature=(state.target_signature if state else None),
                    expected_validated_generation=watermark,
                    expected_claim_token=(state.claim_token if state else None),
                )
            )
    page = actions
    has_more = bool(source_has_more or stopped_for_action_limit)
    return MarketDirtyBatch(
        actions=tuple(page),
        next_scan_cursor=scan_cursor if has_more else None,
        has_more=has_more,
        scanned_count=scanned_count,
        latest_generation=latest_generation,
    )
