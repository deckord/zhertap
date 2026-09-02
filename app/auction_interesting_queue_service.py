"""Read-only queue projection from current E-Qazyna lots.

The service intentionally has no persistence and no background work. It scans a bounded
set of exact active source states, delegates all reason policy to the evidence contract,
and orders by official auction time rather than an opaque score.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
else:
    Session = Any

try:
    from app.auction_interesting_queue import InterestingQueueCandidate, build_interesting_queue_candidate
    from app.auction_interesting_queue_producer import OfficialAuctionEventInput, OfficialLotQueueInput, TERMINAL_REPEAT_STATUSES, produce_identity_evidence, produce_official_lot_evidence, produce_official_repeat_event, produce_rare_lease_term_reason, produce_strict_market_reason
    from app.models import AuctionEvidence, AuctionLandObject, AuctionLot
except ModuleNotFoundError:  # standalone delivery-workspace tests
    from auction_interesting_queue import InterestingQueueCandidate, build_interesting_queue_candidate
    from auction_interesting_queue_producer import OfficialAuctionEventInput, OfficialLotQueueInput, TERMINAL_REPEAT_STATUSES, produce_identity_evidence, produce_official_lot_evidence, produce_official_repeat_event, produce_rare_lease_term_reason, produce_strict_market_reason
    AuctionEvidence = Any  # type: ignore[misc,assignment]
    AuctionLandObject = Any  # type: ignore[misc,assignment]
    AuctionLot = Any  # type: ignore[misc,assignment]

MAX_QUEUE_RESULTS = 100
MAX_QUEUE_SCAN = 500


@dataclass(frozen=True, slots=True)
class InterestingLotQueueRow:
    lot: Any
    evidence: InterestingQueueCandidate


def queue_input_from_lot(lot: Any) -> OfficialLotQueueInput:
    """Copy only authoritative fields stored from the official lot observation."""
    return OfficialLotQueueInput(
        lot_id=str(lot.id),
        source_search_status=lot.source_search_status,
        source_url=lot.source_url,
        source_observed_at=lot.last_seen_at,
        purpose=lot.purpose,
        land_rights=lot.land_rights,
        lease_term_years=lot.lease_term_years,
        auction_starts_at=lot.auction_starts_at,
        start_price_kzt=lot.start_price_kzt,
        document_urls=tuple(document.source_url for document in lot.documents),
    )


def official_repeat_input_from_lot(lot: Any) -> OfficialAuctionEventInput | None:
    """Pick the latest strictly earlier terminal sibling for one exact Jerler object."""
    land_object = getattr(lot, "land_object", None)
    canonical_key = getattr(land_object, "canonical_key", None)
    identity_confidence = getattr(land_object, "identity_confidence", None)
    current_starts = getattr(lot, "auction_starts_at", None)
    current_source_id = str(getattr(lot, "source_lot_id", "") or "").strip()
    if not (
        land_object is not None
        and isinstance(canonical_key, str)
        and canonical_key.startswith("jerler:")
        and identity_confidence == "jerler"
        and isinstance(current_starts, datetime)
        and current_starts.tzinfo is not None
        and current_starts.utcoffset() is not None
        and current_source_id
    ):
        return None

    prior: Any | None = None
    for sibling in getattr(land_object, "lots", ()):
        sibling_source_id = str(getattr(sibling, "source_lot_id", "") or "").strip()
        sibling_starts = getattr(sibling, "auction_starts_at", None)
        if not (
            sibling_source_id
            and sibling_source_id != current_source_id
            and getattr(sibling, "source_search_status", None) in TERMINAL_REPEAT_STATUSES
            and isinstance(sibling_starts, datetime)
            and sibling_starts.tzinfo is not None
            and sibling_starts.utcoffset() is not None
            and sibling_starts < current_starts
        ):
            continue
        if prior is None or sibling_starts > prior.auction_starts_at:
            prior = sibling
    if prior is None:
        return None
    return OfficialAuctionEventInput(
        canonical_key=canonical_key,
        identity_confidence=identity_confidence,
        current_source_lot_id=current_source_id,
        current_source_url=getattr(lot, "source_url", None),
        current_auction_starts_at=current_starts,
        current_observed_at=getattr(lot, "last_seen_at", None),
        current_start_price_kzt=getattr(lot, "start_price_kzt", None),
        previous_source_lot_id=getattr(prior, "source_lot_id", None),
        previous_source_url=getattr(prior, "source_url", None),
        previous_auction_starts_at=getattr(prior, "auction_starts_at", None),
        previous_source_search_status=getattr(prior, "source_search_status", None),
        previous_start_price_kzt=getattr(prior, "start_price_kzt", None),
    )


def _same_filter_key(lot: Any) -> tuple[str, str, str, str] | None:
    """Exact current-catalog filter used for legal-term comparison; no fuzzy matching."""
    values = tuple(
        " ".join(str(getattr(lot, field, "") or "").split()).casefold()
        for field in ("region", "district", "functional_purpose_level2", "land_rights")
    )
    return values if all(values) else None  # type: ignore[return-value]


def rare_lease_term_evidence_by_lot(
    lots: list[Any], *, evaluated_at: datetime
) -> dict[str, dict[str, object]]:
    """Build complete in-scan exact-filter cohorts for a non-spatial official legal fact."""
    groups: dict[tuple[str, str, str, str], list[Any]] = {}
    for lot in lots:
        key = _same_filter_key(lot)
        if key is not None:
            groups.setdefault(key, []).append(lot)
    result: dict[str, dict[str, object]] = {}
    for lot in lots:
        lot_id = str(lot.id)
        key = _same_filter_key(lot)
        alternatives = [item for item in groups.get(key, ()) if str(item.id) != lot_id]
        label = " / ".join(
            " ".join(str(getattr(lot, field, "") or "").split())
            for field in ("region", "district", "functional_purpose_level2", "land_rights")
        )
        result[lot_id] = dict(
            produce_rare_lease_term_reason(
                lot, alternatives, filter_label=label, evaluated_at=evaluated_at
            )
        )
    return result


def evidence_from_lot(
    lot: Any,
    *,
    evaluated_at: datetime,
    market_evidence: Any | None = None,
    rare_lease_term_evidence: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Compose vertical producers by key so a checked fact replaces its manual gap."""
    source = queue_input_from_lot(lot)
    by_key = {
        str(item["key"]): dict(item)
        for item in produce_official_lot_evidence(source, evaluated_at=evaluated_at)
    }
    for item in produce_identity_evidence(lot, evaluated_at=evaluated_at):
        by_key[str(item["key"])] = dict(item)
    event = produce_official_repeat_event(
        official_repeat_input_from_lot(lot), evaluated_at=evaluated_at
    )
    by_key[str(event["key"])] = dict(event)
    by_key["strict_comparables"] = dict(
        produce_strict_market_reason(lot, market_evidence, evaluated_at=evaluated_at)
    )
    if rare_lease_term_evidence is not None:
        by_key["rare_lease_term"] = dict(rare_lease_term_evidence)
    return list(by_key.values())


def interesting_queue_rows_from_lots(
    lots: list[Any],
    *,
    market_by_lot: dict[str, Any],
    evaluated_at: datetime,
    limit: int,
) -> list[InterestingLotQueueRow]:
    """Project scoped lots and explicitly separate thesis from readiness-only rows.

    A stable binary partition puts source-backed comparative/event theses first,
    then keeps the official auction-time order inside both groups.  This is not an
    opaque score: readiness-only lots remain visible with ``eligible=False`` so the
    UI can state that there is no confirmed reason to distinguish them.
    """
    projected: list[InterestingLotQueueRow] = []
    rare_lease_terms = rare_lease_term_evidence_by_lot(lots, evaluated_at=evaluated_at)
    for lot in lots:
        source = queue_input_from_lot(lot)
        candidate = build_interesting_queue_candidate(
            lot_id=source.lot_id,
            source_search_status=source.source_search_status,
            evidence=evidence_from_lot(
                lot,
                evaluated_at=evaluated_at,
                market_evidence=market_by_lot.get(str(lot.id)),
                rare_lease_term_evidence=rare_lease_terms.get(str(lot.id)),
            ),
            evaluated_at=evaluated_at,
        )
        projected.append(InterestingLotQueueRow(lot=lot, evidence=candidate))
    ordered = [row for row in projected if row.evidence.eligible]
    ordered.extend(row for row in projected if not row.evidence.eligible)
    return ordered[:limit]


def list_interesting_lot_queue(
    session: Session,
    *,
    evaluated_at: datetime | None = None,
    limit: int = 20,
) -> list[InterestingLotQueueRow]:
    """Return active land lots with separated thesis, readiness and unknown evidence.

    Missing/stale sources remain ``manual_required`` in each candidate. Lots without
    a comparative/event thesis are returned as non-eligible verification rows rather
    than disappearing. No extraction, provider call, score, recommendation, or
    database write is performed.
    """
    from sqlalchemy import func, select
    from sqlalchemy.orm import selectinload

    checked_at = evaluated_at or datetime.now(UTC)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    bounded_limit = max(1, min(int(limit), MAX_QUEUE_RESULTS))
    scan_limit = min(MAX_QUEUE_SCAN, max(bounded_limit * 5, bounded_limit))
    lots = session.scalars(
        select(AuctionLot)
        .options(
            selectinload(AuctionLot.documents),
            selectinload(AuctionLot.land_object).selectinload(AuctionLandObject.lots),
        )
        .where(
            AuctionLot.source == "e-qazyna",
            AuctionLot.object_type == "land",
            AuctionLot.active.is_(True),
            AuctionLot.source_search_status.in_(("ApplicationsAccept", "Pending", "Running")),
        )
        .order_by(
            AuctionLot.auction_starts_at.is_(None),
            AuctionLot.auction_starts_at,
            AuctionLot.last_seen_at.desc(),
            AuctionLot.id,
        )
        .limit(scan_limit)
    ).all()

    market_by_lot: dict[str, Any] = {}
    if lots:
        lot_ids = tuple(str(lot.id) for lot in lots)
        latest_market_ids = (
            select(
                AuctionEvidence.lot_id.label("lot_id"),
                func.max(AuctionEvidence.id).label("evidence_id"),
            )
            .where(
                AuctionEvidence.lot_id.in_(lot_ids),
                AuctionEvidence.evidence_type == "strict_market_estimate",
            )
            .group_by(AuctionEvidence.lot_id)
            .subquery()
        )
        market_rows = session.scalars(
            select(AuctionEvidence).join(
                latest_market_ids,
                AuctionEvidence.id == latest_market_ids.c.evidence_id,
            )
        ).all()
        market_by_lot = {str(row.lot_id): row for row in market_rows}

    return interesting_queue_rows_from_lots(
        lots,
        market_by_lot=market_by_lot,
        evaluated_at=checked_at,
        limit=bounded_limit,
    )
