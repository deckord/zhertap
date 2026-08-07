import json
from collections.abc import Iterator
from datetime import date

from fastapi.testclient import TestClient
from shapely.geometry import box, mapping
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
import app.web as web
from app.db import Base
from app.models import PlanningCandidateReview, UrbanPlanLayer
from app.purposes import LPH_HOUSEHOLD_LAYER


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_allowed_layer(session: Session) -> UrbanPlanLayer:
    layer = UrbanPlanLayer(
        region="Akmola",
        district="Akkol",
        locality="Akkol",
        purpose=LPH_HOUSEHOLD_LAYER,
        layer_kind="allowed",
        zone_name="Homestead residential area",
        title="Akkol general plan",
        approval_document="Decision C 38-2",
        approval_date=date(2011, 5, 23),
        source_authority="test",
        source_url="https://example.test",
        source_epsg=4326,
        source_sha256="b" * 64,
        source_version="test",
        provenance_status="verified_official",
        identity_status="matched",
        qa_status="WARNING",
        independent_review=False,
        approved_for_search=False,
        geometry_geojson=json.dumps(mapping(box(70.93, 51.99, 70.95, 52.01))),
        active=False,
    )
    session.add(layer)
    session.commit()
    return layer


def csrf_headers() -> dict[str, str]:
    return {"x-csrf-token": web.csrf_token_value("", "testclient")}


def test_admin_candidate_finder_renders_review_controls() -> None:
    session = build_session()
    add_allowed_layer(session)

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        response = TestClient(main.app).get(
            "/admin/urban-plans"
            "?candidate_find=1"
            "&candidate_region=Akmola"
            "&candidate_district=Akkol"
            "&candidate_locality=Akkol"
            "&candidate_use=LPH_HOMESTEAD"
            "&candidate_limit=3"
            "&candidate_step=100"
            "&candidate_buffer=20"
            "&candidate_shadow=1"
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert "planning-review-form" in response.text
    assert 'name="candidate_status"' in response.text


def test_admin_planning_candidate_review_saves_marker() -> None:
    session = build_session()
    layer = add_allowed_layer(session)

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        response = TestClient(main.app).post(
            "/admin/planning-candidates/review",
            headers=csrf_headers(),
            data={
                "candidate_region": layer.region,
                "candidate_district": layer.district,
                "candidate_locality": layer.locality,
                "candidate_use": "LPH_HOMESTEAD",
                "candidate_latitude": "51.992950",
                "candidate_longitude": "70.930765",
                "candidate_status": "empty",
                "candidate_note": "manual satellite check",
                "candidate_trust_level": "SHADOW",
                "candidate_allowed_area_ha": "12.5",
                "candidate_limit": "3",
                "candidate_step": "100",
                "candidate_buffer": "20",
                "candidate_shadow": "1",
            },
            follow_redirects=False,
        )
        review = session.query(PlanningCandidateReview).one()
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 303
    assert review.status == "empty"
    assert review.note == "manual satellite check"
    assert review.reviewed_by == "admin"
    assert review.google_maps_url.startswith("https://www.google.com/maps/@")


def test_admin_next_candidate_review_flow() -> None:
    session = build_session()
    layer = add_allowed_layer(session)
    review = PlanningCandidateReview(
        region=layer.region,
        district=layer.district,
        locality=layer.locality,
        requested_use="LPH_HOMESTEAD",
        latitude=51.992950,
        longitude=70.930765,
        google_maps_url="https://www.google.com/maps/@51.992950,70.930765,281m/data=!3m1!1e3",
        status="queued",
        trust_level="SHADOW",
        allowed_area_ha=12.5,
        reviewed_by="queue",
    )
    session.add(review)
    session.commit()

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        page = TestClient(main.app).get("/admin/planning-candidates/review-next")
        response = TestClient(main.app).post(
            "/admin/planning-candidates/review",
            headers=csrf_headers(),
            data={
                "review_return": "next",
                "candidate_region": layer.region,
                "candidate_district": layer.district,
                "candidate_locality": layer.locality,
                "candidate_use": "LPH_HOMESTEAD",
                "candidate_latitude": "51.992950",
                "candidate_longitude": "70.930765",
                "candidate_status": "built",
                "candidate_note": "satellite has buildings",
                "candidate_trust_level": "SHADOW",
                "candidate_allowed_area_ha": "12.5",
            },
            follow_redirects=False,
        )
        session.refresh(review)
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert page.status_code == 200
    assert "Открыть Google спутник" in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/planning-candidates/review-next"
    assert review.status == "built"
