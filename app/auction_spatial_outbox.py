"""Bounded transactional-outbox dispatcher state for spatial decision invalidations.

Broker delivery is at-least-once. The downstream W14 recompute is idempotent, while a
signal is marked dispatched only after the broker accepted the enqueue operation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import AuctionSpatialDecisionSignal

MAX_OUTBOX_BATCH = 100
OUTBOX_LEASE = timedelta(minutes=6)
MAX_OUTBOX_RETRY = timedelta(hours=1)


class SpatialOutboxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SpatialOutboxClaim:
    signal_id: int
    lot_id: str
    manifest_hash: str
    manifest_watermark: int
    attempt: int
    lease_until: datetime


@dataclass(frozen=True, slots=True)
class SpatialOutboxBatch:
    claims: tuple[SpatialOutboxClaim, ...]
    has_more: bool


@dataclass(frozen=True, slots=True)
class SpatialDispatchReport:
    claimed: int
    dispatched: int
    failed: int
    has_more: bool
    retry_after_seconds: float | None = None


def claim_spatial_decision_signals(
    session: Session,
    *,
    checked_at: datetime,
    limit: int,
) -> SpatialOutboxBatch:
    """Claim due rows using existing `failed` state as a durable timed lease.

    The schema intentionally has no transient processing state. A failed row whose
    next_attempt_at is in the future is leased; an expired lease is safely reclaimable.
    """
    checked = _aware(checked_at)
    bounded = max(1, min(_integer(limit), MAX_OUTBOX_BATCH))
    lease_until = checked + OUTBOX_LEASE
    claims: list[SpatialOutboxClaim] = []
    with session.begin():
        candidates = list(
            session.scalars(
                select(AuctionSpatialDecisionSignal)
                .where(
                    or_(
                        AuctionSpatialDecisionSignal.status == "pending",
                        (
                            (AuctionSpatialDecisionSignal.status == "failed")
                            & or_(
                                AuctionSpatialDecisionSignal.next_attempt_at.is_(None),
                                AuctionSpatialDecisionSignal.next_attempt_at <= checked,
                            )
                        ),
                    )
                )
                .order_by(
                    AuctionSpatialDecisionSignal.next_attempt_at,
                    AuctionSpatialDecisionSignal.id,
                )
                .limit(bounded + 1)
                .with_for_update(skip_locked=True)
            )
        )
        has_more = len(candidates) > bounded
        for row in candidates[:bounded]:
            attempt = min(int(row.attempts) + 1, 10_000)
            row.status = "failed"
            row.attempts = attempt
            row.next_attempt_at = lease_until
            claims.append(
                SpatialOutboxClaim(
                    int(row.id),
                    row.lot_id,
                    row.manifest_hash,
                    int(row.manifest_watermark),
                    attempt,
                    lease_until,
                )
            )
    return SpatialOutboxBatch(tuple(claims), has_more)


def mark_spatial_signal_dispatched(
    session: Session,
    claim: SpatialOutboxClaim,
    *,
    dispatched_at: datetime,
) -> bool:
    checked = _aware(dispatched_at)
    with session.begin():
        row = session.scalar(
            select(AuctionSpatialDecisionSignal)
            .where(AuctionSpatialDecisionSignal.id == claim.signal_id)
            .with_for_update()
        )
        if not _governing(row, claim):
            return False
        row.status = "dispatched"
        row.next_attempt_at = None
        row.dispatched_at = checked
    return True


def mark_spatial_signal_failed(
    session: Session,
    claim: SpatialOutboxClaim,
    *,
    failed_at: datetime,
) -> float | None:
    checked = _aware(failed_at)
    with session.begin():
        row = session.scalar(
            select(AuctionSpatialDecisionSignal)
            .where(AuctionSpatialDecisionSignal.id == claim.signal_id)
            .with_for_update()
        )
        if not _governing(row, claim):
            return None
        base = 2 ** min(claim.attempt, 10)
        digest = hashlib.sha256(
            f"{claim.signal_id}:{claim.manifest_watermark}".encode()
        ).digest()
        delay = min(
            base + int.from_bytes(digest[:2], "big") % (max(1, base // 4) + 1),
            int(MAX_OUTBOX_RETRY.total_seconds()),
        )
        row.status = "failed"
        row.next_attempt_at = checked + timedelta(seconds=delay)
    return float(delay)


def dispatch_spatial_decision_outbox(
    session_factory: Callable[[], Session],
    enqueue_w14: Callable[[tuple[SpatialOutboxClaim, ...]], None],
    *,
    checked_at: datetime,
    limit: int,
) -> SpatialDispatchReport:
    """Claim, broker-enqueue outside DB, then acknowledge in short transactions."""
    with session_factory() as session:
        batch = claim_spatial_decision_signals(
            session, checked_at=checked_at, limit=limit
        )
    if not batch.claims:
        return SpatialDispatchReport(0, 0, 0, batch.has_more)
    try:
        enqueue_w14(batch.claims)
    except Exception:
        delays = []
        for claim in batch.claims:
            with session_factory() as session:
                delay = mark_spatial_signal_failed(
                    session, claim, failed_at=checked_at
                )
            if delay is not None:
                delays.append(delay)
        return SpatialDispatchReport(
            len(batch.claims),
            0,
            len(delays),
            batch.has_more,
            min(delays) if delays else None,
        )
    dispatched = 0
    for claim in batch.claims:
        with session_factory() as session:
            dispatched += int(
                mark_spatial_signal_dispatched(
                    session, claim, dispatched_at=checked_at
                )
            )
    return SpatialDispatchReport(
        len(batch.claims), dispatched, 0, batch.has_more
    )


def _governing(
    row: AuctionSpatialDecisionSignal | None,
    claim: SpatialOutboxClaim,
) -> bool:
    return bool(
        row is not None
        and row.status == "failed"
        and row.attempts == claim.attempt
        and row.manifest_hash == claim.manifest_hash
        and row.manifest_watermark == claim.manifest_watermark
        and _aware(row.next_attempt_at) == claim.lease_until
    )


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise SpatialOutboxError("timestamp must be a datetime")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpatialOutboxError("limit must be an integer")
    return value
