from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.map_links import google_maps_place_url
from app.models import (
    PlanningCandidateReview,
    PlanningCandidateStatus,
    utcnow,
)
from app.planning_service import PlanningScope

PLANNING_CANDIDATE_STATUS_LABELS = {
    PlanningCandidateStatus.queued.value: "К проверке",
    PlanningCandidateStatus.empty.value: "Пусто",
    PlanningCandidateStatus.built.value: "Дом / застроено",
    PlanningCandidateStatus.road.value: "Дорога",
    PlanningCandidateStatus.garden.value: "Огород / двор",
    PlanningCandidateStatus.unclear.value: "Непонятно",
}


def planning_candidate_key(latitude: float, longitude: float) -> str:
    return f"{round(latitude, 6):.6f},{round(longitude, 6):.6f}"


def google_maps_url(latitude: float, longitude: float) -> str:
    return google_maps_place_url(latitude, longitude)


def list_planning_candidate_reviews(
    session: Session,
    *,
    scope: PlanningScope,
    limit: int = 200,
) -> list[PlanningCandidateReview]:
    statement = select(PlanningCandidateReview)
    if scope.region:
        statement = statement.where(PlanningCandidateReview.region == scope.region)
    if scope.district:
        statement = statement.where(PlanningCandidateReview.district == scope.district)
    if scope.locality is not None:
        statement = statement.where(PlanningCandidateReview.locality == scope.locality)
    if scope.requested_use:
        statement = statement.where(
            PlanningCandidateReview.requested_use == scope.requested_use
        )
    return list(
        session.scalars(
            statement.order_by(
                (
                    PlanningCandidateReview.status
                    == PlanningCandidateStatus.queued.value
                ).desc(),
                PlanningCandidateReview.updated_at.desc(),
                PlanningCandidateReview.id.desc(),
            ).limit(limit)
        )
    )


def get_next_queued_planning_candidate(
    session: Session,
) -> PlanningCandidateReview | None:
    return session.scalar(
        select(PlanningCandidateReview)
        .where(PlanningCandidateReview.status == PlanningCandidateStatus.queued.value)
        .order_by(
            PlanningCandidateReview.selection_reason.is_(None).asc(),
            PlanningCandidateReview.nearby_cadastre.is_(None).asc(),
            PlanningCandidateReview.region.asc(),
            PlanningCandidateReview.district.asc(),
            PlanningCandidateReview.locality.asc(),
            PlanningCandidateReview.requested_use.asc(),
            PlanningCandidateReview.id.asc(),
        )
    )


def planning_candidate_review_lookup(
    reviews: list[PlanningCandidateReview],
) -> dict[str, PlanningCandidateReview]:
    return {
        planning_candidate_key(item.latitude, item.longitude): item
        for item in reviews
    }


def upsert_planning_candidate_review(
    session: Session,
    *,
    scope: PlanningScope,
    latitude: float,
    longitude: float,
    status: str,
    note: str | None = None,
    trust_level: str | None = None,
    allowed_area_ha: float | None = None,
    nearby_cadastre: str | None = None,
    nearby_distance_m: float | None = None,
    nearby_land_use: str | None = None,
    candidate_area_ha: float | None = None,
    selection_reason: str | None = None,
    reviewed_by: str | None = None,
) -> PlanningCandidateReview:
    if status not in PLANNING_CANDIDATE_STATUS_LABELS:
        raise ValueError("unknown planning candidate status")
    latitude = round(latitude, 6)
    longitude = round(longitude, 6)
    locality = scope.locality or ""
    review = session.scalar(
        select(PlanningCandidateReview).where(
            PlanningCandidateReview.region == (scope.region or ""),
            PlanningCandidateReview.district == (scope.district or ""),
            PlanningCandidateReview.locality == locality,
            PlanningCandidateReview.requested_use == (scope.requested_use or ""),
            PlanningCandidateReview.latitude == latitude,
            PlanningCandidateReview.longitude == longitude,
        )
    )
    now = utcnow()
    payload: dict[str, Any] = {
        "region": scope.region or "",
        "district": scope.district or "",
        "locality": locality,
        "requested_use": scope.requested_use or "",
        "latitude": latitude,
        "longitude": longitude,
        "google_maps_url": google_maps_url(latitude, longitude),
        "status": status,
        "note": (note or "").strip() or None,
        "trust_level": trust_level,
        "allowed_area_ha": allowed_area_ha,
        "nearby_cadastre": (nearby_cadastre or "").strip() or None,
        "nearby_distance_m": nearby_distance_m,
        "nearby_land_use": (nearby_land_use or "").strip() or None,
        "candidate_area_ha": candidate_area_ha,
        "selection_reason": (selection_reason or "").strip() or None,
        "reviewed_by": reviewed_by,
        "reviewed_at": now,
    }
    if review is None:
        review = PlanningCandidateReview(**payload)
        session.add(review)
    else:
        for key, value in payload.items():
            setattr(review, key, value)
        review.updated_at = now
    session.commit()
    return review
