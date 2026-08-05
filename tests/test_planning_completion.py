import json
from datetime import date

from shapely.geometry import box, mapping
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import PlanningCandidateReview, UrbanPlanLayer
from app.planning_candidate_reviews import upsert_planning_candidate_review
from app.planning_completion import (
    _first_pass_points,
    build_planning_completion_report,
    queue_planning_candidates_for_all_scopes,
)
from app.planning_free_space import PlanningCandidatePoint
from app.planning_service import PlanningScope
from app.purposes import LPH_HOUSEHOLD_LAYER


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_allowed_layer(
    session: Session,
    *,
    region: str,
    district: str,
    locality: str,
    west: float,
) -> None:
    session.add(
        UrbanPlanLayer(
            region=region,
            district=district,
            locality=locality,
            purpose=LPH_HOUSEHOLD_LAYER,
            layer_kind="allowed",
            zone_name="Homestead residential area",
            title=f"{locality} general plan",
            approval_document="Decision 1",
            approval_date=date(2026, 1, 1),
            source_authority="test",
            source_url="https://example.test",
            source_epsg=4326,
            source_sha256="c" * 64,
            source_version="test",
            provenance_status="verified_official",
            identity_status="matched",
            qa_status="WARNING",
            independent_review=False,
            approved_for_search=False,
            geometry_geojson=json.dumps(mapping(box(west, 51.99, west + 0.02, 52.01))),
            active=False,
        )
    )
    session.commit()


def test_completion_report_and_queue_all_candidate_points() -> None:
    with build_session() as session:
        add_allowed_layer(
            session,
            region="Akmola",
            district="Akkol",
            locality="Akkol",
            west=70.93,
        )
        add_allowed_layer(
            session,
            region="Akmola",
            district="Stepnogorsk",
            locality="Stepnogorsk",
            west=71.80,
        )

        before = build_planning_completion_report(session)
        stats = queue_planning_candidates_for_all_scopes(
            session,
            limit_per_scope=2,
            max_scopes=10,
            grid_step_m=100,
            reviewed_by="admin",
        )
        after = build_planning_completion_report(session)

        assert before["needs_queue_scopes"] == 2
        assert stats["scopes_checked"] == 2
        assert stats["points_created"] == 4
        assert after["queued_points"] == 4
        assert after["queued_scopes"] == 2


def test_queue_all_keeps_manual_review_status() -> None:
    with build_session() as session:
        add_allowed_layer(
            session,
            region="Akmola",
            district="Akkol",
            locality="Akkol",
            west=70.93,
        )
        first = queue_planning_candidates_for_all_scopes(
            session,
            limit_per_scope=1,
            max_scopes=1,
            grid_step_m=100,
            reviewed_by="admin",
        )
        review = session.query(PlanningCandidateReview).one()
        upsert_planning_candidate_review(
            session,
            scope=PlanningScope(
                region=review.region,
                district=review.district,
                locality=review.locality,
                requested_use=review.requested_use,
            ),
            latitude=review.latitude,
            longitude=review.longitude,
            status="empty",
            note="already checked",
            reviewed_by="operator",
        )
        stats = queue_planning_candidates_for_all_scopes(
            session,
            limit_per_scope=1,
            max_scopes=1,
            grid_step_m=100,
            reviewed_by="admin",
        )
        review = session.query(PlanningCandidateReview).one()

        assert first["points_created"] == 1
        assert stats["points_existing"] == 1
        assert stats["points_created"] == 0
        assert review.status == "empty"
        assert review.reviewed_by == "operator"


def test_first_pass_points_keeps_only_strong_nearby_orientation() -> None:
    points = [
        PlanningCandidatePoint(
            rank=1,
            latitude=52.0,
            longitude=70.0,
            google_maps_url="https://maps.test/1",
            distance_to_restriction_m=None,
            trust_level="SHADOW",
            nearby_cadastre="strong",
            nearby_distance_m=120,
        ),
        PlanningCandidatePoint(
            rank=2,
            latitude=52.1,
            longitude=70.1,
            google_maps_url="https://maps.test/2",
            distance_to_restriction_m=None,
            trust_level="SHADOW",
            nearby_cadastre="weak",
            nearby_distance_m=420,
        ),
        PlanningCandidatePoint(
            rank=3,
            latitude=52.2,
            longitude=70.2,
            google_maps_url="https://maps.test/3",
            distance_to_restriction_m=None,
            trust_level="SHADOW",
        ),
    ]

    selected = _first_pass_points(points, limit=5)

    assert [point.nearby_cadastre for point in selected] == ["strong"]
