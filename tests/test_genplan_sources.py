import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.genplan_sources import (
    probe_smart_geohub_urban_plan_sources,
    sync_ggk_urban_plan_sources,
    sync_smart_geohub_urban_plan_sources,
)
from app.models import UrbanPlanSource


def build_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGeoHubClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params: dict) -> FakeResponse:
        self.calls.append((url, params))
        if url.endswith("/api/list"):
            return FakeResponse(
                {
                    "type": "FeatureCollection",
                    "total": 42,
                    "features": [
                        {
                            "type": "Feature",
                            "id": "10209072.",
                            "collection": "gpzone",
                            "properties": {},
                            "geometry": {"bbox": [70.1, 52.9, 70.2, 53.0]},
                        }
                    ],
                }
            )
        if url.endswith("/api/geometry"):
            return FakeResponse(
                {
                    "type": "MultiPolygon",
                    "bbox": [70.1, 52.9, 70.2, 53.0],
                    "coordinates": [],
                }
            )
        raise AssertionError(url)


class FakeEmptyGeoHubClient(FakeGeoHubClient):
    def get(self, url: str, *, params: dict) -> FakeResponse:
        self.calls.append((url, params))
        if url.endswith("/api/list"):
            return FakeResponse(
                {
                    "type": "FeatureCollection",
                    "total": 0,
                    "features": [],
                }
            )
        raise AssertionError(url)


def test_sync_ggk_sources_creates_official_source_registry() -> None:
    session = build_session()

    stats = sync_ggk_urban_plan_sources(
        session,
        rows=[
            {
                "id": 3617,
                "locality": "г.Акколь",
                "title": "Генеральный план г. Акколь",
                "number": "Решение №1",
                "date": "2024-01-15",
                "status_id": 1,
                "deactivation_date": "",
            }
        ],
    )

    source = session.scalar(select(UrbanPlanSource))
    assert stats == {"seen": 1, "created": 1, "updated": 0, "skipped": 0}
    assert source is not None
    assert source.platform == "ggk_wfs"
    assert source.external_id == "3617"
    assert source.locality == "г.Акколь"
    assert source.coverage_status == "digital_found"
    assert source.import_status == "not_imported"
    assert "lph-household" in json.loads(source.profiles_json or "[]")


def test_sync_ggk_sources_updates_existing_without_duplicates() -> None:
    session = build_session()

    sync_ggk_urban_plan_sources(
        session,
        rows=[
            {
                "id": 3617,
                "locality": "г.Акколь",
                "title": "Старое название",
                "number": "",
                "date": "",
                "status_id": 1,
                "deactivation_date": "",
            }
        ],
    )
    stats = sync_ggk_urban_plan_sources(
        session,
        rows=[
            {
                "id": 3617,
                "locality": "г.Акколь",
                "title": "Генеральный план г. Акколь",
                "number": "Решение №2",
                "date": "2025-01-15",
                "status_id": 1,
                "deactivation_date": "",
            }
        ],
    )

    sources = session.scalars(select(UrbanPlanSource)).all()
    assert stats["created"] == 0
    assert stats["updated"] == 1
    assert len(sources) == 1
    assert sources[0].title == "Генеральный план г. Акколь"
    assert sources[0].approval_document == "Решение №2"


def test_sync_ggk_sources_marks_deactivated_documents_archived() -> None:
    session = build_session()

    sync_ggk_urban_plan_sources(
        session,
        rows=[
            {
                "id": 1,
                "locality": "г. Тест",
                "title": "Архивный генплан",
                "number": "",
                "date": "",
                "status_id": 9,
                "deactivation_date": "2026-01-01",
            }
        ],
    )

    source = session.scalar(select(UrbanPlanSource))
    assert source is not None
    assert source.coverage_status == "archived"


def test_sync_smart_geohub_sources_creates_catalog_entries() -> None:
    session = build_session()
    base_url = "https://map.example.kz/"
    catalog = [
        {
            "name": "Генплан",
            "children": [
                {"name": "Жилые зоны", "collection": "gpzone-jil"},
                {"name": "Красные линии", "collection": "gpreg-redline"},
                {"name": "Обычный слой", "collection": "ordinary-layer"},
            ],
        }
    ]

    stats = sync_smart_geohub_urban_plan_sources(
        session,
        portals=[{"region": "Тестовая область", "base_url": base_url}],
        catalogs={base_url: catalog},
    )

    sources = session.scalars(
        select(UrbanPlanSource).order_by(UrbanPlanSource.external_id)
    ).all()
    assert stats == {"portals": 1, "seen": 2, "created": 2, "updated": 0, "failed": 0}
    assert [source.external_id for source in sources] == [
        "map.example.kz:gpreg-redline",
        "map.example.kz:gpzone-jil",
    ]
    assert {source.coverage_status for source in sources} == {"catalog_found"}
    assert all(source.region == "Тестовая область" for source in sources)


def test_sync_smart_geohub_sources_updates_existing_without_duplicates() -> None:
    session = build_session()
    base_url = "https://map.example.kz/"
    catalog = [{"name": "Жилые зоны", "collection": "gpzone-jil"}]

    sync_smart_geohub_urban_plan_sources(
        session,
        portals=[{"region": "Тестовая область", "base_url": base_url}],
        catalogs={base_url: catalog},
    )
    stats = sync_smart_geohub_urban_plan_sources(
        session,
        portals=[{"region": "Тестовая область", "base_url": base_url}],
        catalogs={base_url: [{"name": "Жилые территории", "collection": "gpzone-jil"}]},
    )

    sources = session.scalars(select(UrbanPlanSource)).all()
    assert stats["created"] == 0
    assert stats["updated"] == 1
    assert len(sources) == 1
    assert sources[0].title == "Жилые территории"


def test_sync_smart_geohub_sources_skips_duplicate_collection_in_same_catalog() -> None:
    session = build_session()
    base_url = "https://map.example.kz/"
    catalog = [
        {"name": "Жилые зоны", "collection": "gpzone-jil"},
        {"name": "Жилые зоны повтор", "collection": "gpzone-jil"},
    ]

    stats = sync_smart_geohub_urban_plan_sources(
        session,
        portals=[{"region": "Тестовая область", "base_url": base_url}],
        catalogs={base_url: catalog},
    )

    sources = session.scalars(select(UrbanPlanSource)).all()
    assert stats["seen"] == 1
    assert stats["created"] == 1
    assert len(sources) == 1


def test_probe_smart_geohub_sources_marks_geometry_found() -> None:
    session = build_session()
    source = UrbanPlanSource(
        platform="smart_geohub",
        source_type="digital_vector",
        external_id="map.example.kz:gpzone-jil",
        region="РўРµСЃС‚РѕРІР°СЏ РѕР±Р»Р°СЃС‚СЊ",
        title="Р–РёР»С‹Рµ Р·РѕРЅС‹",
        source_url="https://map.example.kz/",
        api_base_url="https://map.example.kz/api/",
        collections_json=json.dumps(["gpzone-jil"]),
        coverage_status="catalog_found",
    )
    session.add(source)
    session.commit()

    stats = probe_smart_geohub_urban_plan_sources(
        session,
        client=FakeGeoHubClient(),
    )

    session.refresh(source)
    assert stats == {
        "checked": 1,
        "geometry_found": 1,
        "no_features": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert source.coverage_status == "geometry_found"
    assert source.layer_count == 42
    raw = json.loads(source.raw_payload_json or "{}")
    assert raw["probe"]["sample_feature_id"] == "10209072."
    assert raw["probe"]["geometry_type"] == "MultiPolygon"


def test_probe_smart_geohub_sources_marks_empty_collection() -> None:
    session = build_session()
    source = UrbanPlanSource(
        platform="smart_geohub",
        source_type="digital_vector",
        external_id="map.example.kz:gpzone-empty",
        region="РўРµСЃС‚РѕРІР°СЏ РѕР±Р»Р°СЃС‚СЊ",
        title="РџСѓСЃС‚РѕР№ СЃР»РѕР№",
        source_url="https://map.example.kz/",
        api_base_url="https://map.example.kz/api/",
        collections_json=json.dumps(["gpzone-empty"]),
        coverage_status="catalog_found",
    )
    session.add(source)
    session.commit()

    stats = probe_smart_geohub_urban_plan_sources(
        session,
        client=FakeEmptyGeoHubClient(),
    )

    session.refresh(source)
    assert stats["checked"] == 1
    assert stats["no_features"] == 1
    assert source.coverage_status == "no_features"
    assert source.layer_count == 0
