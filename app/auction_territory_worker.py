"""Bounded linkage of persisted official territory observations to active lots."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, selectinload

from app.auction_territory_store import assess_and_persist_lot_applicability
from app.models import (
    AuctionLandObject,
    AuctionLot,
    AuctionTerritoryApplicability,
    AuctionTerritoryObservation,
)


def link_territory_observation_batch(
    session_factory: Callable[[], Session],
    *,
    observation_id: int,
    limit: int = 25,
) -> dict[str, int]:
    """Link one immutable observation to a bounded set of not-yet-assessed active lots."""
    bounded = max(1, min(int(limit), 100))
    with session_factory() as session:
        observation = session.get(AuctionTerritoryObservation, observation_id)
        if observation is None:
            return {
                "selected": 0,
                "assessed": 0,
                "applicable": 0,
                "manual_required": 0,
                "not_applicable": 0,
            }
        already_assessed = exists(
            select(AuctionTerritoryApplicability.id).where(
                AuctionTerritoryApplicability.observation_id == observation_id,
                AuctionTerritoryApplicability.lot_id == AuctionLot.id,
            )
        )
        lots = list(
            session.scalars(
                select(AuctionLot)
                .options(selectinload(AuctionLot.land_object))
                .join(AuctionLandObject, AuctionLandObject.id == AuctionLot.land_object_ref_id)
                .where(AuctionLot.active.is_(True), ~already_assessed)
                .order_by(AuctionLot.id.asc())
                .limit(bounded)
            )
        )
        counts = {
            "selected": len(lots),
            "assessed": 0,
            "applicable": 0,
            "manual_required": 0,
            "not_applicable": 0,
        }
        for lot in lots:
            result = assess_and_persist_lot_applicability(session, observation, lot)
            counts["assessed"] += 1
            counts[result.status] += 1
        session.commit()
        return counts
