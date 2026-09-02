"""Bounded background checks of canonical parcel boundaries against NSDI water zones."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.auction_nsdi_evidence import (
    CANONICAL_BOUNDARY_SOURCE,
    EVIDENCE_TYPE,
    record_water_protection_evidence,
)
from app.models import AuctionEvidence, AuctionLandObject, AuctionLot
from app.providers.nsdi import NationalWaterProtectionProvider


def check_nsdi_water_batch(
    session_factory: Callable[[], Session],
    *,
    provider: NationalWaterProtectionProvider | None = None,
    limit: int = 5,
) -> dict[str, int]:
    """Check only bounded active lots without existing NSDI water evidence."""
    bounded = max(1, min(int(limit), 10))
    retry_before = datetime.now(UTC) - timedelta(minutes=15)
    with session_factory() as session:
        already_checked = exists(
            select(AuctionEvidence.id).where(
                AuctionEvidence.lot_id == AuctionLot.id,
                AuctionEvidence.evidence_type == EVIDENCE_TYPE,
                AuctionEvidence.raw_payload_json.like(
                    '%"coverage_contract":"nsdi-regional-coverage/2026.1"%'
                ),
                or_(
                    AuctionEvidence.raw_payload_json.not_like(
                        '%"status":"source_unavailable"%'
                    ),
                    AuctionEvidence.observed_at >= retry_before,
                ),
            )
        )
        lot_ids = list(
            session.scalars(
                select(AuctionLot.id)
                .join(
                    AuctionLandObject,
                    AuctionLandObject.id == AuctionLot.land_object_ref_id,
                )
                .where(
                    AuctionLot.active.is_(True),
                    AuctionLandObject.boundary_geojson.is_not(None),
                    AuctionLandObject.boundary_source == CANONICAL_BOUNDARY_SOURCE,
                    ~already_checked,
                )
                .order_by(AuctionLot.last_seen_at.desc(), AuctionLot.id.asc())
                .limit(bounded)
            )
        )
    checked = 0
    warnings = 0
    unavailable = 0
    provider = provider or NationalWaterProtectionProvider()
    for lot_id in lot_ids:
        with session_factory() as session:
            lot = session.get(AuctionLot, lot_id)
            if lot is None:
                continue
            result = record_water_protection_evidence(
                session, lot, provider=provider
            )
            session.commit()
            checked += 1
            warnings += int(result.status == "intersection_found")
            unavailable += int(
                result.status
                in {
                    "source_unavailable",
                    "boundary_unavailable",
                    "canonical_polygon_unavailable",
                }
            )
    return {
        "selected": len(lot_ids),
        "checked": checked,
        "warnings": warnings,
        "unavailable": unavailable,
    }
