import json
from types import SimpleNamespace

import pytest
from pyproj import Transformer
from shapely.geometry import shape
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.providers.urban_plan as urban_plan_provider
from app.config import settings
from app.db import Base
from app.models import SearchRequest, UrbanPlanLayer, UrbanPlanSource
from app.providers.urban_plan import UrbanPlanError, evaluate_urban_plan, normalize_geojson
from app.purposes import FIELD, LPH_FIELD_LAYER, LPH_HOUSEHOLD_LAYER


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def polygon(min_x: float, min_y: float, max_x: float, max_y: float) -> dict:
    return {
        "type": "Polygon",
        "coordinates": [[
            [min_x, min_y],
            [max_x, min_y],
            [max_x, max_y],
            [min_x, max_y],
            [min_x, min_y],
        ]],
    }


def add_layer(
    session: Session,
    *,
    kind: str,
    geometry: dict,
    title: str,
    purpose: str = "all",
) -> None:
    session.add(
        UrbanPlanLayer(
            region="Акмолинская область",
            district="Бурабайский район",
            locality="Бурабай",
            purpose=purpose,
            layer_kind=kind,
            zone_name="Жилая зона",
            title=title,
            approval_document="Решение маслихата №1",
            source_authority="Акимат",
            source_url="https://www.gov.kz/example",
            source_epsg=4326,
            source_sha256="a" * 64,
            provenance_status="verified_official",
            identity_status="matched",
            qa_status="STRICT",
            independent_review=True,
            approved_for_search=True,
            geometry_geojson=json.dumps(geometry),
            active=True,
        )
    )
    session.commit()


def request_and_candidate() -> tuple[SearchRequest, SimpleNamespace]:
    request = SearchRequest(
        region="Акмолинская область",
        district="Бурабайский район",
        locality="Бурабай",
        area_ha=0.10,
    )
    candidate = SimpleNamespace(latitude=52.9, longitude=70.2)
    return request, candidate


def test_strict_mode_blocks_delivery_without_official_layer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        request, candidate = request_and_candidate()
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is False
    assert result.decisions[0].status == "unavailable"
    assert "не выдаются" in result.message


def test_unavailable_layer_mentions_found_ggk_source(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        session.add(
            UrbanPlanSource(
                platform="ggk_wfs",
                source_type="digital_vector",
                external_id="123",
                locality="г. Бурабай",
                title="Генеральный план Бурабая",
                source_url="https://gov.ggk.kz/",
                coverage_status="digital_found",
                import_status="not_imported",
            )
        )
        session.commit()
        request, candidate = request_and_candidate()
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is False
    assert result.decisions[0].status == "unavailable"
    assert "Официальный цифровой генплан" in result.message
    assert result.decisions[0].source_url == "https://gov.ggk.kz/"


def test_candidate_passes_allowed_layer(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Генеральный план Бурабая",
        )
        request, candidate = request_and_candidate()
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is True
    assert result.decisions[0].status == "passed"
    assert result.decisions[0].source_url == "https://www.gov.kz/example"


def test_red_line_blocks_candidate(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Генеральный план Бурабая",
        )
        add_layer(
            session,
            kind="red_line",
            geometry={
                "type": "LineString",
                "coordinates": [[70.2, 52.89], [70.2, 52.91]],
            },
            title="Красные линии Бурабая",
        )
        request, candidate = request_and_candidate()
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.decisions[0].status == "blocked"
    assert "Красные линии" in result.decisions[0].message


def test_purpose_specific_layer_is_not_reused_for_gardening(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Зона ЛПХ",
            purpose="ЛПХ",
        )
        request, candidate = request_and_candidate()
        request.purpose = "Садоводство"
        request.area_ha = 0.12
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is False
    assert result.decisions[0].status == "unavailable"


def test_lph_layer_is_reused_for_new_lph_profile(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Зона ЛПХ",
            purpose="ЛПХ",
        )
        request, candidate = request_and_candidate()
        request.purpose = "ЛПХ (новый поиск)"
        request.allotment_type = "household"
        request.irrigation_type = "non_irrigated"
        request.area_ha = 0.25
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is True
    assert result.decisions[0].status == "passed"


def test_citywide_layer_matches_any_district_and_optional_locality(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Общегородской генеральный план",
            purpose="ЛПХ",
        )
        layer = session.scalar(select(UrbanPlanLayer))
        assert layer is not None
        layer.district = "*"
        layer.locality = "*"
        session.commit()
        request, candidate = request_and_candidate()
        request.district = "Другой внутригородской район"
        request.locality = None
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is True
    assert result.decisions[0].status == "passed"


def test_wildcard_layer_without_spatial_coverage_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Общегородской генеральный план",
            purpose="ЛПХ",
        )
        layer = session.scalar(select(UrbanPlanLayer))
        assert layer is not None
        layer.district = "*"
        layer.locality = "*"
        session.commit()
        request, candidate = request_and_candidate()
        candidate.longitude = 72.15
        candidate.latitude = 50.63
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is False
    assert result.coverage_status == "unavailable"
    assert result.decisions[0].status == "unavailable"


def test_same_utm_zone_transforms_city_layer_only_once(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    real_shape = urban_plan_provider.shape
    calls = 0

    def counted_shape(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_shape(*args, **kwargs)

    monkeypatch.setattr(urban_plan_provider, "shape", counted_shape)
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.22, 52.92),
            title="Общегородской генеральный план",
        )
        request, candidate = request_and_candidate()
        second = SimpleNamespace(latitude=52.905, longitude=70.205)
        result = evaluate_urban_plan(session, request, [candidate, second])

    assert [row.status for row in result.decisions] == ["passed", "passed"]
    assert calls == 1


def test_active_legacy_layer_without_independent_qa_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(settings, "urban_plan_check_mode", "strict")
    with build_session() as session:
        add_layer(
            session,
            kind="allowed",
            geometry=polygon(70.19, 52.89, 70.21, 52.91),
            title="Непроверенный слой",
        )
        layer = session.scalar(select(UrbanPlanLayer))
        assert layer is not None
        layer.approved_for_search = False
        layer.qa_status = "pending"
        layer.independent_review = False
        session.commit()
        request, candidate = request_and_candidate()
        result = evaluate_urban_plan(session, request, [candidate])

    assert result.coverage_available is False
    assert result.decisions[0].status == "unavailable"


def test_upload_rejects_point_as_allowed_area() -> None:
    with pytest.raises(UrbanPlanError):
        normalize_geojson(
            json.dumps({"type": "Point", "coordinates": [70.2, 52.9]}),
            "allowed",
            4326,
        )


def test_upload_stores_transformed_wgs84_geometry() -> None:
    transformer = Transformer.from_crs(4326, 3857, always_xy=True)
    min_x, min_y = transformer.transform(70.19, 52.89)
    max_x, max_y = transformer.transform(70.21, 52.91)

    normalized = normalize_geojson(
        json.dumps(polygon(min_x, min_y, max_x, max_y)),
        "allowed",
        3857,
    )

    geometry = shape(json.loads(normalized))
    assert geometry.bounds == pytest.approx((70.19, 52.89, 70.21, 52.91))


def test_lph_layer_profiles_do_not_cross_household_and_field_scopes() -> None:
    household_request, _ = request_and_candidate()
    household_request.purpose = "ЛПХ(новый поиск)"
    field_request, _ = request_and_candidate()
    field_request.purpose = "ЛПХ(новый поиск)"
    field_request.allotment_type = FIELD

    household = UrbanPlanLayer(
        region=household_request.region,
        district=household_request.district,
        locality=household_request.locality or "",
        purpose=LPH_HOUSEHOLD_LAYER,
        layer_kind="allowed",
        title="Household",
        approval_document="Act",
        source_authority="Authority",
        source_url="https://gov.ggk.kz/",
        geometry_geojson=json.dumps(polygon(70.19, 52.89, 70.21, 52.91)),
    )
    field = UrbanPlanLayer(
        region=field_request.region,
        district=field_request.district,
        locality=field_request.locality or "",
        purpose=LPH_FIELD_LAYER,
        layer_kind="allowed",
        title="Field",
        approval_document="Act",
        source_authority="Authority",
        source_url="https://gov.ggk.kz/",
        geometry_geojson=json.dumps(polygon(70.19, 52.89, 70.21, 52.91)),
    )

    assert urban_plan_provider._matches_scope(household, household_request)
    assert not urban_plan_provider._matches_scope(field, household_request)
    assert urban_plan_provider._matches_scope(field, field_request)
    assert not urban_plan_provider._matches_scope(household, field_request)


def test_city_layer_matches_direct_city_selection_without_locality() -> None:
    request = SearchRequest(
        region="Акмолинская область (01)",
        district="г. Кокшетау (01-020)",
        locality=None,
        purpose="ЛПХ",
    )
    layer = UrbanPlanLayer(
        region="Акмолинская область",
        district="г.Кокшетау",
        locality="г.Кокшетау",
        purpose=LPH_HOUSEHOLD_LAYER,
        layer_kind="allowed",
        title="Kokshetau",
        approval_document="Act",
        source_authority="Authority",
        source_url="https://gov.ggk.kz/",
        geometry_geojson=json.dumps(polygon(69.3, 53.2, 69.5, 53.4)),
    )

    assert urban_plan_provider._matches_scope(layer, request)
