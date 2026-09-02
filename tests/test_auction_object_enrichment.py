from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auction_object_enrichment import (
    ERROR_EVIDENCE_TYPE,
    EVIDENCE_TYPE,
    JerlerEnrichmentDeferred,
    apply_source_object_data,
    sync_auction_source_objects,
    sync_auction_source_objects_detached,
    sync_missing_source_object_links,
)
from app.db import Base
from app.models import (
    AuctionEvidence,
    AuctionLandObject,
    AuctionLot,
    AuctionLotChange,
    AuctionLotGeoCheck,
    AuctionSource,
)
from app.provider_backpressure import (
    InMemoryProviderBackend,
    ProviderBackpressure,
    ProviderPolicy,
)
from app.providers.jerler import (
    JerlerObjectData,
    JerlerProvider,
    JerlerUnsafeUrlError,
    JerlerUpstreamError,
    parse_jerler_object,
)

SOURCE_URL = "https://traderesources.e-qazyna.kz/ru/source-object-view?id=1"
OBJECT_HTML = """
<html><body>
  <dl>
    <dt>Идентификатор земельного участка в ЕГКН:</dt><dd>23340720260504000001</dd>
    <dt>Кадастровый номер:</dt><dd>23-340</dd>
    <dt>Вид землепользования:</dt>
    <dd>временное возмездное краткосрочное землепользование</dd>
    <dt>Срок аренды:</dt><dd>3 года</dd>
    <dt>Делимость:</dt><dd>Делимый</dd>
    <dt>Аресты:</dt><dd>не имеются</dd>
    <dt>Ограничения:</dt><dd>охранная зона ЛЭП</dd>
    <dt>Дополнительный платеж:</dt><dd>16 200 ₸</dd>
    <dt>Ежегодная арендная плата:</dt><dd>17 970 ₸</dd>
  </dl>
  <a href="https://map.gov4c.kz/egkn/?id=23340720260504000001">Публичная кадастровая карта</a>
  <script>
    window.card = {"geometry":{"type":"Polygon",
      "coordinates":[[[76.1,50.1],[76.2,50.1],[76.2,50.2],[76.1,50.1]]]}};
  </script>
</body></html>
"""


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _lot() -> AuctionLot:
    return AuctionLot(
        source="e-qazyna",
        source_lot_id="452662",
        title="Кемпинг",
        source_url="https://sauda.e-qazyna.kz/ru/list/452662",
        source_object_url=SOURCE_URL,
        last_seen_at=datetime.now(UTC),
    )


def _backpressure(
    *,
    qps: float = 100,
    max_concurrency: int = 2,
    failure_threshold: int = 3,
) -> ProviderBackpressure:
    policy = ProviderPolicy(
        "jerler",
        qps=qps,
        burst=10,
        max_concurrency=max_concurrency,
        lease_ttl_seconds=45,
        failure_threshold=failure_threshold,
    )
    return ProviderBackpressure(
        {"jerler": policy},
        InMemoryProviderBackend(),
        app_env="test",
    )


def test_parse_jerler_public_card_extracts_identity_legal_costs_and_geometry() -> None:
    data = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    assert data.land_object_id == "23340720260504000001"
    assert data.cadastre_number == "23-340"
    assert data.land_rights == "временное возмездное краткосрочное землепользование"
    assert data.lease_term_years == 3
    assert data.divisible is True
    assert data.arrests_text == "не имеются"
    assert data.restrictions_text == "охранная зона ЛЭП"
    assert data.additional_payment_kzt == 16_200
    assert data.annual_rent_kzt == 17_970
    assert data.geometry_geojson and data.geometry_geojson["type"] == "Polygon"
    assert data.cadastral_map_url == (
        "https://map.gov4c.kz/egkn/?id=23340720260504000001"
    )


def test_sync_missing_source_object_links_extracts_real_jerler_url(session: Session) -> None:
    lot = AuctionLot(
        source="e-qazyna",
        source_lot_id="334931391085000000",
        auction_number="457684",
        title="Коянды",
        source_url="https://sauda.e-qazyna.kz/ru/list/334931391085000000",
        last_seen_at=datetime.now(UTC),
    )
    session.add(lot)
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == lot.source_url
        return httpx.Response(
            200,
            text="""
            <html><body>
              <a href='https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/332521343611000000/view'>Объект</a>
            </body></html>
            """,
        )

    result = sync_missing_source_object_links(
        session,
        transport=httpx.MockTransport(handler),
        limit=10,
    )

    assert result.selected == 1
    assert result.fetched == 1
    assert result.updated == 1
    assert lot.land_object_id is None
    assert lot.source_object_url == (
        "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/332521343611000000/view"
    )


def test_sync_missing_source_object_links_prioritizes_unconfirmed_land_coordinates(
    session: Session,
) -> None:
    recent_other = AuctionLot(
        id="lot-other",
        source="e-qazyna",
        source_lot_id="other",
        object_type="building",
        active=True,
        title="Other",
        source_url="https://sauda.e-qazyna.kz/ru/list/other",
        last_seen_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )
    older_land = AuctionLot(
        id="lot-land",
        source="e-qazyna",
        source_lot_id="land",
        object_type="land",
        active=True,
        title="Land",
        source_url="https://sauda.e-qazyna.kz/ru/list/land",
        last_seen_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
    )
    session.add_all(
        [
            recent_other,
            older_land,
            AuctionLotGeoCheck(
                lot_id="lot-land",
                coordinate_status="unconfirmed",
            ),
        ]
    )
    session.commit()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text="""<html><body>
            <a href='/ru/source-object-view?id=332521343611000000'>Объект</a>
            </body></html>""",
        )

    result = sync_missing_source_object_links(
        session,
        transport=httpx.MockTransport(handler),
        limit=1,
    )

    assert result.selected == 1
    assert requested == [older_land.source_url]


def test_parse_jerler_public_card_extracts_data_wkt_geometry() -> None:
    html = """
    <html><body>
      <div><span>Кадастровый номер: </span><span>23-248-030</span></div>
      <div data-name="flGeom" data-type="Polygon"
        data-wkt="POLYGON ((81.74536309633498 47.02306238248348,
        81.74536309431814 47.02281733916849,
        81.7461261377956 47.02272544469857,
        81.74536309633498 47.02306238248348))"></div>
    </body></html>
    """

    data = parse_jerler_object(html, source_url=SOURCE_URL)

    assert data.geometry_geojson == {
        "type": "Polygon",
        "coordinates": [
            [
                [81.74536309633498, 47.02306238248348],
                [81.74536309431814, 47.02281733916849],
                [81.7461261377956, 47.02272544469857],
                [81.74536309633498, 47.02306238248348],
            ]
        ],
    }


def test_parse_jerler_public_card_extracts_legacy_wkts_attribute() -> None:
    html = """
    <html><body>
      <div render-field-name="flKadNumber"><span>10-149-003</span></div>
      <div id="object-geometry-viewer"
        wkts="[&quot;POLYGON((67.22505641377782 43.92601167988127,
        67.2252625102661 43.92589188756145,
        67.22509257105648 43.925758205992196,
        67.22488526932554 43.925877130519865,
        67.22505641377782 43.92601167988127))&quot;]"
        wktsNeighbours="[]"></div>
    </body></html>
    """

    data = parse_jerler_object(html, source_url=SOURCE_URL)

    assert data.geometry_geojson == {
        "type": "Polygon",
        "coordinates": [
            [
                [67.22505641377782, 43.92601167988127],
                [67.2252625102661, 43.92589188756145],
                [67.22509257105648, 43.925758205992196],
                [67.22488526932554, 43.925877130519865],
                [67.22505641377782, 43.92601167988127],
            ]
        ],
    }


def test_apply_source_object_geometry_updates_geo_check(session: Session) -> None:
    source = AuctionSource(
        code="jerler_source_object",
        source_type="official",
        name="Jerler",
        base_url="https://jerler.e-qazyna.kz/",
    )
    lot = _lot()
    data = JerlerObjectData(
        source_url=SOURCE_URL,
        geometry_geojson={
            "type": "Polygon",
            "coordinates": [
                [
                    [81.74536309633498, 47.02306238248348],
                    [81.74536309431814, 47.02281733916849],
                    [81.7461261377956, 47.02272544469857],
                    [81.74536309633498, 47.02306238248348],
                ]
            ],
        },
    )
    session.add_all((source, lot))
    session.flush()
    session.add(
        AuctionLotGeoCheck(
            lot_id=lot.id,
            coordinate_status="found",
            osm_status="checked",
            osm_checked_at=datetime(2026, 1, 1, 12, tzinfo=UTC),
            latitude=46.0,
            longitude=80.0,
            road_distance_m=120.0,
            power_distance_m=300.0,
            water_distance_m=900.0,
            open_water_distance_m=1000.0,
            cemetery_distance_m=2000.0,
            object_distance_m=180.0,
            object_kind="building",
            engineering_status="checked",
        )
    )
    session.flush()

    assert apply_source_object_data(session, lot, data, source=source) is True

    geo_check = session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    )
    assert geo_check is not None
    assert geo_check.coordinate_status == "found"
    assert geo_check.boundary_source == "jerler:source_object"
    assert geo_check.latitude == pytest.approx(47.022916887)
    assert geo_check.longitude == pytest.approx(81.745553856)
    assert geo_check.osm_status == "stale"
    assert geo_check.osm_checked_at is None
    assert geo_check.road_distance_m is None
    assert geo_check.power_distance_m is None
    assert geo_check.object_distance_m is None
    assert geo_check.object_kind is None
    assert geo_check.engineering_status == "manual_required"


def test_invalid_source_geometry_does_not_claim_verified_boundary(session: Session) -> None:
    source = AuctionSource(
        code="jerler_source_object",
        source_type="official",
        name="Jerler",
        base_url="https://jerler.e-qazyna.kz/",
    )
    lot = _lot()
    session.add_all((source, lot))
    session.flush()
    data = JerlerObjectData(
        source_url=SOURCE_URL,
        geometry_geojson={
            "type": "Polygon",
            # Open rings can still yield a plausible centroid, but are not a parcel boundary.
            "coordinates": [[[81.7, 47.0], [81.8, 47.0], [81.8, 47.1], [81.7, 47.1]]],
        },
    )

    apply_source_object_data(session, lot, data, source=source)

    assert session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    ) is None
    assert lot.land_object_ref_id is None
    evidence = session.scalar(
        select(AuctionEvidence).where(
            AuctionEvidence.lot_id == lot.id,
            AuctionEvidence.evidence_type == EVIDENCE_TYPE,
        )
    )
    assert evidence is not None
    assert evidence.status == "conflict"
    payload = json.loads(evidence.raw_payload_json or "{}")
    assert payload["conflicts"] == [
        {
            "field": "geometry_geojson",
            "lot_value": None,
            "source_object_value": "invalid_boundary",
            "resolution": "rejected_invalid_boundary",
        }
    ]


def test_jerler_geometry_preserves_verified_egkn_geo_check_and_records_conflict(
    session: Session,
) -> None:
    source = AuctionSource(
        code="jerler_source_object",
        source_type="official",
        name="Jerler",
        base_url="https://jerler.e-qazyna.kz/",
    )
    lot = _lot()
    session.add_all((source, lot))
    session.flush()
    session.add(
        AuctionLotGeoCheck(
            lot_id=lot.id,
            cadastre_status="verified",
            boundary_status="verified",
            boundary_source="egkn:u_view",
            coordinate_status="found",
            latitude=50.0,
            longitude=76.0,
            osm_status="checked",
            road_distance_m=125.0,
        )
    )
    session.flush()
    data = JerlerObjectData(
        source_url=SOURCE_URL,
        geometry_geojson={
            "type": "Polygon",
            "coordinates": [
                [[81.7, 47.0], [81.8, 47.0], [81.8, 47.1], [81.7, 47.0]]
            ],
        },
    )

    apply_source_object_data(session, lot, data, source=source)

    geo_check = session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    )
    assert geo_check is not None
    assert geo_check.boundary_source == "egkn:u_view"
    assert geo_check.latitude == 50.0
    assert geo_check.longitude == 76.0
    assert geo_check.osm_status == "checked"
    assert geo_check.road_distance_m == 125.0
    evidence = session.scalar(
        select(AuctionEvidence).where(
            AuctionEvidence.lot_id == lot.id,
            AuctionEvidence.evidence_type == EVIDENCE_TYPE,
        )
    )
    assert evidence is not None
    assert evidence.status == "conflict"
    payload = json.loads(evidence.raw_payload_json or "{}")
    assert payload["conflicts"] == [
        {
            "field": "geometry_geojson",
            "lot_value": "egkn:u_view",
            "source_object_value": "published_boundary",
            "resolution": "preserved_higher_priority_egkn_boundary",
        }
    ]


def test_jerler_geometry_never_replaces_existing_egkn_canonical_boundary(
    session: Session,
) -> None:
    source = AuctionSource(
        code="jerler_source_object",
        source_type="official",
        name="Jerler",
        base_url="https://jerler.e-qazyna.kz/",
    )
    lot = _lot()
    lot.land_object_id = "23340720260504000001"
    canonical = AuctionLandObject.from_identifiers(egkn_id=lot.land_object_id)
    canonical.boundary_geojson = json.dumps(
        {"type": "Polygon", "coordinates": [[[76.0, 50.0], [76.1, 50.0], [76.0, 50.0]]]}
    )
    canonical.boundary_source = "egkn:cadastre_boundary"
    session.add_all((source, canonical, lot))
    session.flush()
    lot.land_object_ref_id = canonical.id
    original_boundary = canonical.boundary_geojson

    data = JerlerObjectData(
        source_url=SOURCE_URL,
        land_object_id=lot.land_object_id,
        geometry_geojson={
            "type": "Polygon",
            "coordinates": [[[81.7, 47.0], [81.8, 47.0], [81.7, 47.0]]],
        },
    )
    apply_source_object_data(session, lot, data, source=source)

    assert canonical.boundary_source == "egkn:cadastre_boundary"
    assert canonical.boundary_geojson == original_boundary


def test_provider_enforces_host_redirect_and_response_size_bounds() -> None:
    provider = JerlerProvider(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
    with pytest.raises(JerlerUnsafeUrlError):
        provider.fetch_object("http://traderesources.e-qazyna.kz/object/1")
    with pytest.raises(JerlerUnsafeUrlError):
        provider.fetch_object("https://127.0.0.1/object/1")

    redirecting = JerlerProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"location": "https://example.com/private"})
        )
    )
    with pytest.raises(JerlerUnsafeUrlError):
        redirecting.fetch_object(SOURCE_URL)

    oversized = JerlerProvider(
        max_response_bytes=16_384,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"x" * 20_000,
            )
        ),
    )
    with pytest.raises(Exception, match="size limit"):
        oversized.fetch_object(SOURCE_URL)


def test_provider_classifies_only_network_429_and_5xx_as_upstream_failures() -> None:
    limited = JerlerProvider(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"retry-after": "41"})
        )
    )
    with pytest.raises(JerlerUpstreamError) as limited_error:
        limited.fetch_object(SOURCE_URL)
    assert limited_error.value.status_code == 429
    assert limited_error.value.retry_after_seconds == 41

    unavailable = JerlerProvider(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    with pytest.raises(JerlerUpstreamError) as unavailable_error:
        unavailable.fetch_object(SOURCE_URL)
    assert unavailable_error.value.status_code == 503

    def fail_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    disconnected = JerlerProvider(transport=httpx.MockTransport(fail_connect))
    with pytest.raises(JerlerUpstreamError) as network_error:
        disconnected.fetch_object(SOURCE_URL)
    assert network_error.value.status_code is None

    not_found = JerlerProvider(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    )
    with pytest.raises(httpx.HTTPStatusError) as local_error:
        not_found.fetch_object(SOURCE_URL)
    assert not isinstance(local_error.value, JerlerUpstreamError)


def test_source_object_batch_upserts_lot_and_single_evidence_idempotently(
    session: Session,
) -> None:
    lot = _lot()
    session.add(lot)
    session.commit()
    parsed = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    class FakeProvider:
        calls = 0

        def fetch_object(self, _url: str) -> JerlerObjectData:
            self.calls += 1
            return parsed

    provider = FakeProvider()
    limiter = _backpressure()
    first = sync_auction_source_objects(
        session, provider=provider, backpressure=limiter, limit=10, ttl_minutes=60
    )
    session.commit()
    second = sync_auction_source_objects(
        session, provider=provider, backpressure=limiter, limit=10, ttl_minutes=60
    )
    session.commit()

    session.refresh(lot)
    assert first.as_dict() == {
        "selected": 1,
        "fetched": 1,
        "updated": 1,
        "skipped_fresh": 0,
        "errors": 0,
    }
    assert second.selected == 0
    assert provider.calls == 1
    assert lot.land_object_id == "23340720260504000001"
    assert lot.lease_term_years == 3
    assert lot.additional_payment_kzt == 16_200
    assert lot.land_object_ref_id is not None
    land_object = session.get(AuctionLandObject, lot.land_object_ref_id)
    assert land_object is not None
    assert land_object.egkn_id == "23340720260504000001"
    assert land_object.boundary_source == "jerler:source_object"
    assert json.loads(land_object.boundary_geojson or "null") == parsed.geometry_geojson
    evidence_rows = list(
        session.scalars(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
        )
    )
    assert len(evidence_rows) == 1
    payload = json.loads(evidence_rows[0].raw_payload_json or "{}")
    assert payload["arrests_text"] == "не имеются"
    assert payload["geometry_geojson"]["type"] == "Polygon"
    assert session.scalar(select(func.count(AuctionLotChange.id))) == 7


def test_source_object_missing_geometry_retries_before_full_ttl(session: Session) -> None:
    lot = _lot()
    source = AuctionSource(
        code="jerler_source_object",
        source_type="official",
        name="Jerler",
        base_url="https://jerler.e-qazyna.kz/",
    )
    session.add_all((lot, source))
    session.flush()
    session.add(
        AuctionEvidence(
            lot_id=lot.id,
            source_id=source.id,
            evidence_type=EVIDENCE_TYPE,
            status="found",
            title="Официальная карточка земельного объекта",
            value_text="Кадастр: 23-248-030",
            source_url=SOURCE_URL,
            confidence=0.98,
            raw_payload_json=json.dumps({"cadastre_number": "23-248-030"}),
            observed_at=datetime(2026, 8, 20, 10, tzinfo=UTC),
        )
    )
    session.commit()
    parsed = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    class FakeProvider:
        calls = 0

        def fetch_object(self, _url: str) -> JerlerObjectData:
            self.calls += 1
            return parsed

    provider = FakeProvider()
    result = sync_auction_source_objects(
        session,
        provider=provider,
        backpressure=_backpressure(),
        limit=1,
        ttl_minutes=1440,
    )
    session.commit()

    geo_check = session.scalar(
        select(AuctionLotGeoCheck).where(AuctionLotGeoCheck.lot_id == lot.id)
    )
    assert result.selected == 1
    assert provider.calls == 1
    assert geo_check is not None
    assert geo_check.boundary_source == "jerler:source_object"


def test_source_object_batch_records_error_without_raising(session: Session) -> None:
    lot = _lot()
    session.add(lot)
    session.commit()

    class FailingProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            raise RuntimeError("temporary failure")

    result = sync_auction_source_objects(
        session,
        provider=FailingProvider(),
        backpressure=_backpressure(),
        limit=1,
    )
    session.commit()

    assert result.errors == 1
    evidence = session.scalar(
        select(AuctionEvidence).where(AuctionEvidence.evidence_type == ERROR_EVIDENCE_TYPE)
    )
    assert evidence is not None
    assert evidence.status == "error"
    assert "temporary failure" in (evidence.value_text or "")


def test_source_object_conflict_preserves_lot_value_and_records_provenance(
    session: Session,
) -> None:
    lot = _lot()
    lot.cadastre_number = "23-340"
    lot.land_rights = "Продажа права аренды земельного участка"
    session.add(lot)
    session.commit()
    parsed = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)
    parsed.cadastre_number = "23-340-001-999"

    class FakeProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            return parsed

    result = sync_auction_source_objects(
        session,
        provider=FakeProvider(),
        backpressure=_backpressure(),
        limit=1,
    )
    session.commit()
    session.refresh(lot)

    assert result.updated == 1  # missing identity, term and costs were still filled
    assert lot.cadastre_number == "23-340"
    assert lot.land_rights == "Продажа права аренды земельного участка"
    evidence = session.scalar(
        select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
    )
    assert evidence is not None
    assert evidence.status == "conflict"
    payload = json.loads(evidence.raw_payload_json or "{}")
    conflicts = {item["field"]: item for item in payload["conflicts"]}
    assert conflicts["cadastre_number"]["lot_value"] == "23-340"
    assert conflicts["cadastre_number"]["source_object_value"] == "23-340-001-999"
    assert conflicts["land_rights"]["resolution"] == "preserved_lot_value"


def test_detached_batch_has_no_open_db_transaction_during_http() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(_lot())
        session.commit()

    class InspectingProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            # A separate probe can acquire/use the database while HTTP is in progress.
            with factory() as probe:
                assert probe.scalar(select(func.count(AuctionLot.id))) == 1
            return parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    result = sync_auction_source_objects_detached(
        factory,
        provider=InspectingProvider(),
        backpressure=_backpressure(),
    )

    assert result.fetched == 1
    assert result.updated == 1


def test_detached_batch_can_target_a_bounded_lot_set() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        first = _lot()
        second = _lot()
        second.source_lot_id = "452663"
        second.source_url = "https://sauda.e-qazyna.kz/ru/list/452663"
        second.source_object_url = (
            "https://traderesources.e-qazyna.kz/ru/source-object-view?id=2"
        )
        session.add_all([first, second])
        session.commit()
        first_id = first.id
        second_id = second.id

    class GoodProvider:
        def fetch_object(self, url: str) -> JerlerObjectData:
            return parse_jerler_object(OBJECT_HTML, source_url=url)

    result = sync_auction_source_objects_detached(
        factory,
        provider=GoodProvider(),
        backpressure=_backpressure(),
        limit=10,
        lot_ids=[second_id],
    )

    assert result.selected == 1
    with factory() as session:
        evidence_lot_ids = set(
            session.scalars(
                select(AuctionEvidence.lot_id).where(
                    AuctionEvidence.evidence_type == EVIDENCE_TYPE
                )
            )
        )
    assert evidence_lot_ids == {second_id}
    assert first_id not in evidence_lot_ids


def test_detached_batch_deferral_exposes_committed_partial_progress() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    second_url = "https://traderesources.e-qazyna.kz/ru/source-object-view?id=2"
    with factory() as session:
        first = _lot()
        second = _lot()
        second.source_lot_id = "452663"
        second.source_url = "https://sauda.e-qazyna.kz/ru/list/452663"
        second.source_object_url = second_url
        session.add_all([first, second])
        session.commit()

    class DeferredAfterFirstProvider:
        calls = 0

        def fetch_object(self, url: str) -> JerlerObjectData:
            self.calls += 1
            if self.calls == 2:
                raise JerlerUpstreamError(
                    "rate limited",
                    status_code=429,
                    retry_after_seconds=37,
                )
            return parse_jerler_object(OBJECT_HTML, source_url=url)

    with pytest.raises(JerlerEnrichmentDeferred) as caught:
        sync_auction_source_objects_detached(
            factory,
            provider=DeferredAfterFirstProvider(),
            backpressure=_backpressure(),
            limit=2,
        )

    partial = caught.value.partial_result
    assert partial is not None
    assert partial.selected == 2
    assert partial.fetched == 1
    assert partial.updated == 1
    assert partial.errors == 0
    with factory() as session:
        assert session.scalar(
            select(func.count(AuctionEvidence.id)).where(
                AuctionEvidence.evidence_type == EVIDENCE_TYPE
            )
        ) == 1

    class GoodProvider:
        def fetch_object(self, url: str) -> JerlerObjectData:
            return parse_jerler_object(OBJECT_HTML, source_url=url)

    continuation = sync_auction_source_objects_detached(
        factory,
        provider=GoodProvider(),
        backpressure=_backpressure(),
        limit=2,
    )
    assert continuation.selected == 1
    assert continuation.fetched == 1
    with factory() as session:
        assert session.scalar(
            select(func.count(AuctionEvidence.id)).where(
                AuctionEvidence.evidence_type == EVIDENCE_TYPE
            )
        ) == 2


def test_fetch_error_preserves_last_good_evidence_and_uses_short_backoff() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        lot = _lot()
        session.add(lot)
        session.commit()
        lot_id = lot.id

    good_data = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    class GoodProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            return good_data

    sync_auction_source_objects_detached(
        factory,
        provider=GoodProvider(),
        backpressure=_backpressure(),
    )
    with factory() as session:
        good = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
        )
        assert good is not None
        original_payload = good.raw_payload_json
        good.observed_at = datetime(2020, 1, 1, tzinfo=UTC)
        session.commit()

    class FailingProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            raise RuntimeError("outage")

    failed = sync_auction_source_objects_detached(
        factory,
        provider=FailingProvider(),
        backpressure=_backpressure(),
    )
    immediate_retry = sync_auction_source_objects_detached(
        factory,
        provider=FailingProvider(),
        backpressure=_backpressure(),
    )

    assert failed.errors == 1
    assert immediate_retry.selected == 0
    with factory() as session:
        good = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
        )
        error = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.evidence_type == ERROR_EVIDENCE_TYPE,
                AuctionEvidence.lot_id == lot_id,
            )
        )
        assert good is not None and good.raw_payload_json == original_payload
        assert good.status == "found"
        assert error is not None and error.status == "error"


def test_backpressure_denial_stops_batch_and_exposes_retry_without_calling_provider(
    session: Session,
) -> None:
    session.add(_lot())
    session.commit()
    limiter = _backpressure(max_concurrency=1)
    held = limiter.acquire("jerler", owner_token="held-call")
    assert held.allowed

    class MustNotRunProvider:
        calls = 0

        def fetch_object(self, _url: str) -> JerlerObjectData:
            self.calls += 1
            raise AssertionError("outbound call must be blocked")

    provider = MustNotRunProvider()
    with pytest.raises(JerlerEnrichmentDeferred) as caught:
        sync_auction_source_objects(
            session,
            provider=provider,
            backpressure=limiter,
            limit=1,
        )

    assert caught.value.reason == "concurrency_limited"
    assert caught.value.retry_after_seconds > 0
    assert provider.calls == 0
    limiter.release(held)


def test_upstream_failure_opens_circuit_but_parser_failure_does_not(
    session: Session,
) -> None:
    session.add(_lot())
    session.commit()
    upstream_limiter = _backpressure(failure_threshold=1)

    class UpstreamFailureProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            raise JerlerUpstreamError(
                "rate limited",
                status_code=429,
                retry_after_seconds=37,
            )

    with pytest.raises(JerlerEnrichmentDeferred) as caught:
        sync_auction_source_objects(
            session,
            provider=UpstreamFailureProvider(),
            backpressure=upstream_limiter,
            limit=1,
        )
    assert caught.value.reason == "upstream_failure"
    assert caught.value.retry_after_seconds >= 37
    metrics = upstream_limiter.metrics_snapshot("jerler")
    assert metrics.failures == 1
    assert metrics.active_leases == 0
    assert upstream_limiter.acquire("jerler").status == "circuit_open"

    session.rollback()
    parser_limiter = _backpressure(failure_threshold=1)

    class ParserFailureProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            raise ValueError("malformed public card")

    result = sync_auction_source_objects(
        session,
        provider=ParserFailureProvider(),
        backpressure=parser_limiter,
        limit=1,
    )
    assert result.errors == 1
    metrics = parser_limiter.metrics_snapshot("jerler")
    assert metrics.failures == 0
    assert metrics.active_leases == 0
    assert parser_limiter.acquire("jerler").allowed


def test_successful_guarded_fetch_records_once_and_releases_lease(session: Session) -> None:
    session.add(_lot())
    session.commit()
    limiter = _backpressure()
    parsed = parse_jerler_object(OBJECT_HTML, source_url=SOURCE_URL)

    class SuccessfulProvider:
        def fetch_object(self, _url: str) -> JerlerObjectData:
            return parsed

    result = sync_auction_source_objects(
        session,
        provider=SuccessfulProvider(),
        backpressure=limiter,
        limit=1,
    )

    assert result.fetched == 1
    metrics = limiter.metrics_snapshot("jerler")
    assert metrics.successes == 1
    assert metrics.failures == 0
    assert metrics.active_leases == 0
