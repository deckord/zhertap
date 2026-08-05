import json
from collections.abc import Iterator
from datetime import date

from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, box, mapping
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.main as main
from app.db import Base
from app.models import UrbanPlanLayer
from app.planning_free_space import find_planning_candidate_points
from app.planning_service import PlanningScope
from app.providers.egkn import DistrictInfo, ParcelRecord, SettlementInfo
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
    active: bool = False,
    approved_for_search: bool = False,
    qa_status: str = "WARNING",
    zone_name: str = "Территория усадебной застройки",
) -> None:
    session.add(
        UrbanPlanLayer(
            region="Акмолинская область",
            district="г.Акколь",
            locality="г.Акколь",
            purpose=LPH_HOUSEHOLD_LAYER,
            layer_kind=kind,
            zone_name=zone_name,
            title="Генеральный план г. Акколь",
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
            independent_review=False,
            approved_for_search=approved_for_search,
            geometry_geojson=json.dumps(geometry),
            active=active,
        )
    )
    session.commit()


def test_find_planning_candidate_points_returns_google_links() -> None:
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.93, 51.99, 70.95, 52.01)),
        )
        add_layer(
            session,
            kind="red_line",
            geometry=mapping(LineString([(70.94, 51.99), (70.94, 52.01)])),
            zone_name="Красные линии",
        )

        result = find_planning_candidate_points(
            session,
            scope=PlanningScope(
                region="Акмолинская область",
                district="г.Акколь",
                locality="г.Акколь",
                requested_use="LPH_HOMESTEAD",
            ),
            include_shadow=True,
            limit=5,
            grid_step_m=100,
            restriction_buffer_m=25,
        )

    assert result["trust_level"] == "SHADOW"
    assert len(result["points"]) == 5
    assert result["points"][0].google_maps_url.startswith("https://www.google.com/maps/@")
    assert all(point.distance_to_restriction_m is not None for point in result["points"])


def test_find_planning_candidate_points_uses_egkn_as_orientation(monkeypatch) -> None:
    class FakeEgknProvider:
        def find_district(self, region: str, district: str) -> DistrictInfo:
            return DistrictInfo(
                id=1,
                region_name=region,
                code="01-001",
                name=district,
                display_name=district,
                srs=4326,
                ate_code="",
            )

        def find_settlement(self, district_id: int, locality: str) -> SettlementInfo:
            return SettlementInfo(
                gid="settlement:1",
                name=locality,
                kato="",
                district_id=district_id,
                geometry=box(70.93, 51.99, 70.95, 52.01),
            )

        def district_search_area(self, district: DistrictInfo) -> SettlementInfo:
            return self.find_settlement(district.id, district.name)

        def parcels(
            self,
            district: DistrictInfo,
            settlement: SettlementInfo,
        ) -> list[ParcelRecord]:
            return [
                ParcelRecord(
                    geometry=box(70.935, 51.995, 70.939, 51.999),
                    cadastre="01-001-001-001",
                    address="test",
                    land_use="для ведения личного подсобного хозяйства",
                    area_m2=1200,
                )
            ]

    monkeypatch.setattr("app.planning_free_space.EgknProvider", FakeEgknProvider)
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.93, 51.99, 70.95, 52.01)),
        )

        result = find_planning_candidate_points(
            session,
            scope=PlanningScope(
                region="Акмолинская область",
                district="г.Акколь",
                locality="г.Акколь",
                requested_use="LPH_HOMESTEAD",
            ),
            include_shadow=True,
            limit=3,
            grid_step_m=90,
            use_egkn_context=True,
        )

    parcel = box(70.935, 51.995, 70.939, 51.999)
    assert result["egkn_parcel_count"] == 1
    assert result["egkn_anchor_count"] == 1
    assert result["points"][0].nearby_cadastre == "01-001-001-001"
    assert "ориентир" in (result["points"][0].selection_reason or "")
    assert not parcel.covers(
        Point(result["points"][0].longitude, result["points"][0].latitude)
    )


def test_far_egkn_parcel_is_not_saved_as_nearby_orientation(monkeypatch) -> None:
    class FakeEgknProvider:
        def find_district(self, region: str, district: str) -> DistrictInfo:
            return DistrictInfo(
                id=1,
                region_name=region,
                code="01-001",
                name=district,
                display_name=district,
                srs=4326,
                ate_code="",
            )

        def find_settlement(self, district_id: int, locality: str) -> SettlementInfo:
            return SettlementInfo(
                gid="settlement:1",
                name=locality,
                kato="",
                district_id=district_id,
                geometry=box(70.00, 52.00, 70.06, 52.06),
            )

        def district_search_area(self, district: DistrictInfo) -> SettlementInfo:
            return self.find_settlement(district.id, district.name)

        def parcels(
            self,
            district: DistrictInfo,
            settlement: SettlementInfo,
        ) -> list[ParcelRecord]:
            return [
                ParcelRecord(
                    geometry=box(70.00, 52.00, 70.001, 52.001),
                    cadastre="01-001-001-999",
                    address="test",
                    land_use="ЛПХ",
                    area_m2=1200,
                )
            ]

    monkeypatch.setattr("app.planning_free_space.EgknProvider", FakeEgknProvider)
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=mapping(box(70.045, 52.045, 70.06, 52.06)),
        )

        result = find_planning_candidate_points(
            session,
            scope=PlanningScope(
                region="Акмолинская область",
                district="г.Акколь",
                locality="г.Акколь",
                requested_use="LPH_HOMESTEAD",
            ),
            include_shadow=True,
            limit=1,
            grid_step_m=120,
            use_egkn_context=True,
        )

    assert result["points"][0].nearby_cadastre is None
    assert result["points"][0].nearby_distance_m is None
    assert "слишком далеко" in (result["points"][0].selection_reason or "")


def test_admin_urban_plans_candidate_finder_renders_points() -> None:
    session = build_session()
    add_layer(
        session,
        kind="allowed",
        geometry=mapping(box(70.93, 51.99, 70.95, 52.01)),
    )

    def override_db() -> Iterator[Session]:
        yield session

    main.app.dependency_overrides[main.get_db] = override_db
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    try:
        response = TestClient(main.app).get(
            "/admin/urban-plans"
            "?candidate_find=1"
            "&candidate_region=Акмолинская+область"
            "&candidate_district=г.Акколь"
            "&candidate_locality=г.Акколь"
            "&candidate_use=LPH_HOMESTEAD"
            "&candidate_limit=3"
            "&candidate_step=100"
            "&candidate_buffer=20"
            "&candidate_shadow=1"
            "&candidate_egkn=0"
        )
    finally:
        main.app.dependency_overrides.clear()
        session.close()

    assert response.status_code == 200
    assert "Найти места внутри зоны" in response.text
    assert "Открыть Google" in response.text
    assert "Используются черновые слои" in response.text
