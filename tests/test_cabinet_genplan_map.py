import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

from fastapi.testclient import TestClient
from shapely.geometry import box, mapping
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.web as web
from app.db import Base, get_db
from app.main import app
from app.models import (
    Account,
    Candidate,
    ReviewStatus,
    SearchRequest,
    UrbanPlanLayer,
    UrbanPlanStatus,
    WebSession,
)
from app.purposes import LPH_HOUSEHOLD_LAYER


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


@contextmanager
def client_for(session: Session) -> Iterator[TestClient]:
    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def authorize_client(client: TestClient, session: Session, account: Account) -> None:
    token = "cabinet-genplan-session"
    session.add(
        WebSession(
            account_id=account.id,
            token_hash=web._hash(token),
            expires_at=web._now() + timedelta(days=1),
        )
    )
    session.commit()
    client.cookies.set("zhertap_session", token)


def add_layer(session: Session) -> None:
    session.add(
        UrbanPlanLayer(
            region="Акмолинская область",
            district="г.Акколь",
            locality="г.Акколь",
            purpose=LPH_HOUSEHOLD_LAYER,
            layer_kind="allowed",
            zone_name="Территория усадебной застройки",
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
            qa_status="VERIFIED_STRICT",
            independent_review=True,
            approved_for_search=True,
            geometry_geojson=json.dumps(mapping(box(70.93, 51.99, 70.95, 52.01))),
            active=True,
        )
    )
    session.commit()


def test_cabinet_search_opens_internal_genplan_map() -> None:
    with build_session() as session:
        account = Account(
            phone="+77010000001",
            phone_verified_at=web._now(),
            paid_access=True,
            access_granted_at=web._now(),
            access_expires_at=web._now() + timedelta(days=30),
        )
        session.add(account)
        session.flush()
        search = SearchRequest(
            web_account_id=account.id,
            language="ru",
            region="Акмолинская область",
            district="г.Акколь",
            locality="г.Акколь",
            purpose="ЛПХ",
            raw_query=f"web-cabinet:{account.id}",
            status="ready",
            progress=100,
        )
        session.add(search)
        add_layer(session)
        session.add(
            Candidate(
                request_id=search.id,
                rank=1,
                region_chain="Акмолинская область → г.Акколь",
                locality="г.Акколь",
                latitude=52.000000,
                longitude=70.940000,
                nearby_cadastre="01001000001",
                nearby_distance_m=12.0,
                requested_area_ha=0.25,
                road_distance_m=40.0,
                power_evidence="нет данных",
                water_evidence="нет данных",
                sewer_evidence="септик проверяется на месте",
                cemetery_distance_m=None,
                score=91.0,
                risk_notes="тестовая точка",
                google_maps_url="https://www.google.com/maps/@52.000000,70.940000,19z/data=!3m1!1e3",
                review_status=ReviewStatus.approved.value,
                urban_plan_status=UrbanPlanStatus.passed.value,
            )
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail = client.get(f"/cabinet/searches/{search.id}")
            map_page = client.get(f"/cabinet/searches/{search.id}/genplan-map")
            status = client.get(f"/cabinet/searches/{search.id}/status")
            layers = client.get(f"/cabinet/searches/{search.id}/genplan-map.geojson")
            coverage = client.get(
                f"/cabinet/searches/{search.id}/genplan-coverage.json"
                "?lat=52.000000&lon=70.940000"
            )

    assert detail.status_code == 200
    assert f'href="/cabinet/searches/{search.id}/genplan-map"' in detail.text
    assert "Открыть карту генплана" in detail.text
    assert (
        f'href="/cabinet/searches/{search.id}/genplan-map?lat=52.000000&lon=70.940000&rank=1"'
        in detail.text
    )
    assert "https://www.google.com/maps/place/" in detail.text

    assert map_page.status_code == 200
    assert f'data-layers-url="/cabinet/searches/{search.id}/genplan-map.geojson"' in map_page.text
    assert 'data-candidate-point' in map_page.text
    assert 'data-lat="52.000000"' in map_page.text
    assert 'data-lon="70.940000"' in map_page.text
    assert "site-genplan-map.js" in map_page.text
    assert "Открыть официальный файл" in map_page.text

    assert status.status_code == 200
    assert status.json()["genplan_map_url"] == f"/cabinet/searches/{search.id}/genplan-map"
    assert status.json()["candidates"][0]["google_maps_url"].startswith(
        "https://www.google.com/maps/place/"
    )

    assert layers.status_code == 200
    assert layers.json()["features"][0]["properties"]["layer_kind"] == "allowed"

    assert coverage.status_code == 200
    assert coverage.json()["result"] == "POSSIBLE"


def test_blocked_genplan_candidates_can_be_previewed_without_approving_them() -> None:
    with build_session() as session:
        account = Account(
            phone="+77029990002",
            phone_verified_at=web._now(),
            paid_access=True,
            access_granted_at=web._now(),
            access_expires_at=web._now() + timedelta(days=30),
        )
        session.add(account)
        session.flush()
        search = SearchRequest(
            web_account_id=account.id,
            language="ru",
            region="Акмолинская область",
            district="г.Акколь",
            locality="г.Акколь",
            purpose="ЛПХ",
            area_ha=0.25,
            raw_query=f"web-cabinet:{account.id}",
            status="ready",
            progress=100,
            search_outcome="filtered_out",
            urban_plan_status=UrbanPlanStatus.blocked.value,
            urban_plan_message="прошло 0 из 1 кандидатов",
        )
        session.add(search)
        session.flush()
        session.add(
            Candidate(
                request_id=search.id,
                rank=1,
                region_chain="Акмолинская область → г.Акколь",
                locality="г.Акколь",
                latitude=52.000000,
                longitude=70.940000,
                nearby_cadastre="01001000001",
                nearby_distance_m=12.0,
                requested_area_ha=0.25,
                road_distance_m=40.0,
                power_evidence="нет данных",
                water_evidence="нет данных",
                sewer_evidence="септик проверяется на месте",
                cemetery_distance_m=None,
                score=77.0,
                risk_notes="не попало в разрешенную зону",
                google_maps_url="https://www.google.com/maps/@52.000000,70.940000,19z/data=!3m1!1e3",
                review_status=ReviewStatus.approved.value,
                urban_plan_status=UrbanPlanStatus.blocked.value,
            )
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail = client.get(f"/cabinet/searches/{search.id}")
            status = client.get(f"/cabinet/searches/{search.id}/status")

    assert detail.status_code == 200
    assert "Показать без проверки генплана" in detail.text
    assert "Подходящих мест по генплану не осталось" in detail.text

    payload = status.json()
    assert status.status_code == 200
    assert payload["candidate_count"] == 0
    assert payload["can_show_without_genplan"] is True
    assert payload["genplan_preview_candidate_count"] == 1
    assert (
        payload["genplan_preview_candidates"][0]["urban_plan_status"]
        == UrbanPlanStatus.blocked.value
    )


def test_trial_search_hides_cadastre_and_rejects_map_endpoints() -> None:
    with build_session() as session:
        account = Account(
            phone="+77010000003",
            phone_verified_at=web._now(),
            trial_started_at=web._now(),
            trial_expires_at=web._now() + timedelta(days=1),
        )
        session.add(account)
        session.flush()
        search = SearchRequest(
            web_account_id=account.id,
            language="ru",
            region="Акмолинская область",
            district="г.Акколь",
            locality="г.Акколь",
            purpose="ЛПХ",
            raw_query=f"web-cabinet:{account.id}",
            status="ready",
            progress=100,
        )
        session.add(search)
        session.flush()
        session.add(
            Candidate(
                request_id=search.id,
                rank=1,
                region_chain="Акмолинская область → г.Акколь",
                locality="г.Акколь",
                latitude=52.000000,
                longitude=70.940000,
                nearby_cadastre="01001000001",
                nearby_distance_m=12.0,
                requested_area_ha=0.25,
                road_distance_m=40.0,
                power_evidence="нет данных",
                water_evidence="нет данных",
                sewer_evidence="септик проверяется на месте",
                cemetery_distance_m=None,
                score=91.0,
                risk_notes="тестовая точка",
                google_maps_url="https://www.google.com/maps/@52.000000,70.940000,19z/data=!3m1!1e3",
                egkn_url="https://map.gov4c.kz/egkn/?cadastre=01001000001",
                review_status=ReviewStatus.approved.value,
                urban_plan_status=UrbanPlanStatus.passed.value,
            )
        )
        session.commit()

        with client_for(session) as client:
            authorize_client(client, session, account)
            detail = client.get(f"/cabinet/searches/{search.id}")
            status = client.get(f"/cabinet/searches/{search.id}/status")
            map_page = client.get(f"/cabinet/searches/{search.id}/genplan-map")
            layers = client.get(f"/cabinet/searches/{search.id}/genplan-map.geojson")
            coverage = client.get(
                f"/cabinet/searches/{search.id}/genplan-coverage.json"
                "?lat=52.000000&lon=70.940000"
            )

    assert detail.status_code == 200
    assert "01001000001" not in detail.text
    assert "скрыт до оплаты" in detail.text
    assert "Карта после оплаты" in detail.text
    assert "Генплан после оплаты" in detail.text
    assert "ЕГКН после оплаты" in detail.text
    assert "https://www.google.com/maps/place/" not in detail.text
    assert "https://map.gov4c.kz/egkn/" not in detail.text
    assert f'href="/cabinet/searches/{search.id}/genplan-map"' not in detail.text

    payload = status.json()
    assert status.status_code == 200
    assert payload["report_unlocked"] is False
    assert payload["genplan_map_url"] is None
    assert payload["urban_plan_reference"] is None
    assert payload["candidates"][0]["nearby_cadastre"] is None
    assert payload["candidates"][0]["latitude"] is None
    assert payload["candidates"][0]["longitude"] is None
    assert payload["candidates"][0]["google_maps_url"] is None
    assert payload["candidates"][0]["egkn_url"] is None
    assert payload["candidates"][0]["locked"] is True
    assert map_page.status_code == 403
    assert layers.status_code == 403
    assert coverage.status_code == 403
