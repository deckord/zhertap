import json
from datetime import UTC, datetime

from sqlalchemy import select

from app.auction_land_identity import set_land_object_boundary
from app.db import SessionLocal
from app.models import AuctionEvidence, AuctionLandObject, AuctionLot, AuctionMarketTargetState

with SessionLocal() as session:
    rows = list(
        session.execute(
            select(AuctionLandObject, AuctionLot.id, AuctionEvidence.raw_payload_json)
            .join(AuctionLot, AuctionLot.land_object_ref_id == AuctionLandObject.id)
            .join(AuctionEvidence, AuctionEvidence.lot_id == AuctionLot.id)
            .where(
                AuctionLot.active.is_(True),
                AuctionLot.object_type == "land",
                AuctionEvidence.evidence_type == "source_object_card",
                AuctionEvidence.status.in_(("found", "conflict")),
                AuctionEvidence.raw_payload_json.is_not(None),
            )
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        )
    )
    seen: set[str] = set()
    updated_objects: set[str] = set()
    updated_lots: set[str] = set()
    skipped_authoritative = 0
    for land_object, lot_id, raw_payload in rows:
        if land_object.id in seen:
            if land_object.id in updated_objects:
                updated_lots.add(lot_id)
            continue
        seen.add(land_object.id)
        if land_object.boundary_source not in {None, "jerler:source_object"}:
            skipped_authoritative += 1
            continue
        try:
            payload = json.loads(raw_payload or "{}")
        except json.JSONDecodeError:
            continue
        if set_land_object_boundary(
            land_object,
            payload.get("geometry_geojson"),
            source="jerler:source_object",
        ):
            updated_objects.add(land_object.id)
            updated_lots.add(lot_id)

    now = datetime.now(UTC)
    states_dirtied = 0
    for state in session.scalars(
        select(AuctionMarketTargetState).where(AuctionMarketTargetState.lot_id.in_(updated_lots))
    ):
        state.status = "pending"
        state.claim_token = None
        state.claim_expires_at = None
        state.next_attempt_at = now
        state.updated_at = now
        states_dirtied += 1
    session.commit()

print(
    f"canonical_objects_seen={len(seen)} boundaries_updated={len(updated_objects)} "
    f"lots_dirtied={len(updated_lots)} market_states_dirtied={states_dirtied} "
    f"authoritative_boundaries_preserved={skipped_authoritative}"
)
