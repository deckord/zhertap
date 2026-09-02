"""Bounded background checks for polygon-first neighbouring parcel evidence."""
from __future__ import annotations

from sqlalchemy import exists, select

from app.auction_neighbor_evidence import EVIDENCE_TYPE, record_neighbor_parcel_evidence
from app.models import AuctionEvidence, AuctionLandObject, AuctionLot
from app.providers.egkn import EgknProvider


def check_neighbor_parcel_batch(session_factory, *, provider: EgknProvider | None = None, limit: int = 5) -> dict[str, int]:
    bounded = max(1, min(int(limit), 20))
    has_evidence = exists(select(AuctionEvidence.id).where(AuctionEvidence.lot_id == AuctionLot.id, AuctionEvidence.evidence_type == EVIDENCE_TYPE))
    with session_factory() as session:
        lot_ids = list(session.scalars(
            select(AuctionLot.id)
            .join(AuctionLandObject, AuctionLandObject.id == AuctionLot.land_object_ref_id)
            .where(AuctionLot.active.is_(True), AuctionLot.object_type == "land", AuctionLandObject.boundary_source == "jerler:source_object", AuctionLandObject.boundary_geojson.is_not(None), ~has_evidence)
            .order_by(AuctionLot.last_seen_at.desc(), AuctionLot.id.asc()).limit(bounded)
        ))
    result = {"selected": len(lot_ids), "checked": 0, "found": 0, "manual": 0, "errors": 0}
    for lot_id in lot_ids:
        with session_factory() as session:
            lot = session.get(AuctionLot, lot_id)
            if lot is None:
                continue
            observation = record_neighbor_parcel_evidence(session, lot, provider=provider)
            session.commit()
        result["checked"] += 1
        if observation["result_status"] == "found":
            result["found"] += 1
        elif observation["result_status"] == "provider_failure":
            result["errors"] += 1
        else:
            result["manual"] += 1
    return result
