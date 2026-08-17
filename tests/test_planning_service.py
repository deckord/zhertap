import json
from collections.abc import Iterator
from datetime import date

from fastapi.testclient import TestClient
from shapely.geometry import LineString, box, mapping
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
from app.config import settings
from app.db import Base
from app.models import UrbanPlanLayer
from app.planning_service import PlanningScope, planning_check
from app.purposes import LPH_HOUSEHOLD_LAYER


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def add_layer(
    session: Session,
    *,
    kind: str,
    geometry: dict,
    active: bool = True,
    approved_for_search: bool = True,
    qa_status: str = "VERIFIED_STRICT",
    zone_name: str = "Территория усадебной застройки",
    district: str = "г.Акколь",
    locality: str = "г.Акколь",
    title: str = "Генеральный план г. Акколь",
) -> None:
    session.add(
        UrbanPlanLayer(
            region="Акмолинская область",
            district=district,
            locality=locality,
            purpose=LPH_HOUSEHOLD_LAYER,
            layer_kind=kind,
            zone_name=zone_name,
            title=title,
            approval_document="Решение маслихата №С 38-2",
            approval_date=date(2011, 5, 23),
            source_authority="АИС ГГК",
            source_url="https://map.gov.kz/services/",
            source_epsg=4326,
            source_sha256="a" * 64,
            source_version="test",
            provenance_status="verified_official",
            identity_status="matched",
            qa_status=qa_status,
            independent_review=True,
            approved_for_search=approved_for_search,
            geometry_geojson=json.dumps(geometry),
            active=active,
        )
    )
    session.commit()


def test_planning_check_returns_possible_inside_allowed_zone() -> None:
    with build_session() as session:
        add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

        result = planning_check(
            session,
            geometry=mapping(box(70.935, 51.995, 70.94, 52.0)),
            scope=PlanningScope(requested_use="LPH_HOMESTEAD"),
        )

    assert result["coverage_status"] == "AVAILABLE"
    assert result["result"] == "POSSIBLE"
    assert result["intersections"][0]["trust_level"] == "SEARCH"
    assert result["documents"][0]["approval_document"] == "Решение маслихата №С 38-2"


def test_planning_check_reports_restriction_intersection() -> None:
    with build_session() as session:
        add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))
        add_layer(
            session,
            kind="red_line",
            geometry=mapping(LineString([(70.936, 51.996), (70.945, 52.005)])),
            zone_name="Красные линии",
        )

        result = planning_check(
            session,
            geometry=mapping(box(70.935, 51.995, 70.94, 52.0)),
            scope=PlanningScope(requested_use="LPH_HOMESTEAD"),
        )

    assert result["result"] == "BLOCKED_BY_RESTRICTION"
    assert result["restrictions"][0]["layer_type"] == "red_line"


def test_locality_genplan_takes_priority_over_regional_fallback() -> None:
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.0, 51.0, 72.0, 53.0)),
            district="*",
            locality="*",
            title="Региональный fallback",
        )
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.93, 51.99, 70.95, 52.01)),
            title="Генеральный план города",
        )

        result = planning_check(
            session,
            geometry=mapping(box(70.935, 51.995, 70.94, 52.0)),
            scope=PlanningScope(
                region="Акмолинская область",
                district="г.Акколь",
                locality="г.Акколь",
                requested_use="LPH_HOMESTEAD",
            ),
        )

    assert {item["document_title"] for item in result["intersections"]} == {
        "Генеральный план города"
    }


def test_shadow_layers_are_visible_only_when_requested() -> None:
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.93, 51.99, 70.95, 52.01)),
            active=False,
            approved_for_search=False,
            qa_status="WARNING",
        )

        hidden = planning_check(
            session,
            geometry=mapping(box(70.935, 51.995, 70.94, 52.0)),
            scope=PlanningScope(requested_use="LPH_HOMESTEAD"),
        )
        visible = planning_check(
            session,
            geometry=mapping(box(70.935, 51.995, 70.94, 52.0)),
            scope=PlanningScope(requested_use="LPH_HOMESTEAD"),
            include_shadow=True,
        )

    assert hidden["coverage_status"] == "NO_DATA"
    assert visible["coverage_status"] == "SHADOW_ONLY"
    assert visible["result"] == "MANUAL_REVIEW"


def test_unspecified_scope_does_not_report_available_for_far_layer() -> None:
    with build_session() as session:
        add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

        result = planning_check(
            session,
            geometry=mapping(box(69.7, 42.3, 69.71, 42.31)),
            scope=PlanningScope(requested_use="LPH_HOMESTEAD"),
        )

    assert result["coverage_status"] == "NO_DATA"
    assert result["result"] == "MANUAL_REVIEW"


def test_internal_planning_api_check_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "internal_api_key", "")
    session = build_session()
    add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    try:
        response = TestClient(main.app).post(
            "/api/planning/check",
            json={
                "requested_use": "LPH_HOMESTEAD",
                "geometry": mapping(box(70.935, 51.995, 70.94, 52.0)),
            },
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert response.json()["result"] == "POSSIBLE"


def test_admin_urban_plan_map_geojson_endpoint() -> None:
    session = build_session()
    add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        response = TestClient(main.app).get(
            "/admin/urban-plans/map/geojson"
            "?region=Акмолинская+область"
            "&district=г.Акколь"
            "&locality=г.Акколь"
            "&requested_use=LPH_HOMESTEAD"
            "&include_shadow=true",
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    payload = response.json()
    assert response.status_code == 200
    assert payload["type"] == "FeatureCollection"
    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"]["layer_kind"] == "allowed"


def test_admin_urban_plan_coverage_json_endpoint() -> None:
    session = build_session()
    add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        response = TestClient(main.app).get(
            "/admin/urban-plans/coverage.json"
            "?lat=52.000000"
            "&lon=70.940000"
            "&region=Акмолинская+область"
            "&district=г.Акколь"
            "&locality=г.Акколь"
            "&requested_use=LPH_HOMESTEAD"
            "&include_shadow=true",
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert response.json()["result"] == "POSSIBLE"


def test_internal_planning_api_batch_check_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "internal_api_key", "")
    session = build_session()
    add_layer(session, kind="allowed", geometry=mapping(box(70.93, 51.99, 70.95, 52.01)))

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    try:
        response = TestClient(main.app).post(
            "/api/planning/batch-check",
            json={
                "requested_use": "LPH_HOMESTEAD",
                "items": [
                    {
                        "id": "inside",
                        "geometry": mapping(box(70.935, 51.995, 70.94, 52.0)),
                    },
                    {
                        "id": "outside",
                        "geometry": mapping(box(69.7, 42.3, 69.71, 42.31)),
                    },
                ],
            },
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    payload = response.json()
    assert response.status_code == 200
    assert payload["count"] == 2
    by_id = {row["id"]: row for row in payload["results"]}
    assert by_id["inside"]["result"] == "POSSIBLE"
    assert by_id["outside"]["coverage_status"] == "NO_DATA"


def test_internal_planning_api_batch_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "internal_api_key", "")

    response = TestClient(main.app).post(
        "/api/planning/batch-check",
        json={"items": [{"geometry": mapping(box(70.0, 52.0, 70.1, 52.1))}] * 101},
    )

    assert response.status_code == 413
