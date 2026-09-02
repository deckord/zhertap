import json

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auction_land_identity import reconcile_lot_land_object, set_land_object_boundary
from app.auction_nsdi_evidence import record_water_protection_evidence
from app.db import Base
from app.models import AuctionEvidence, AuctionLot
from app.providers.nsdi import NationalWaterProtectionProvider, NsdiProviderError

KOSTANAY_PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[63.60, 53.20], [63.61, 53.20], [63.61, 53.21], [63.60, 53.20]]
    ],
}
ASTANA_PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[71.40, 51.10], [71.41, 51.10], [71.41, 51.11], [71.40, 51.10]]
    ],
}


def test_water_zone_provider_declares_kostanay_coverage_and_rejects_outside_bbox() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    provider = NationalWaterProtectionProvider(transport=httpx.MockTransport(handler))

    assert provider.coverage_area == "Костанайская область"
    assert provider.covers_bbox((63.60, 53.20, 63.61, 53.21)) is True
    assert provider.covers_bbox((71.40, 51.10, 71.41, 51.11)) is False
    try:
        provider.features_for_bbox((71.40, 51.10, 71.41, 51.11))
    except NsdiProviderError as exc:
        assert str(exc) == "bbox outside published layer extent"
    else:
        raise AssertionError("outside-extent request must fail closed")
    assert calls == 0


def test_outside_published_extent_is_persisted_distinct_from_empty_layer() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="outside-zone-coverage",
            title="Лот",
            source_url="https://x",
            region="Костанайская область",
            cadastre_number="01:234:567:899",
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, ASTANA_PARCEL, source="jerler:source_object")

        result = record_water_protection_evidence(
            session,
            lot,
            provider=NationalWaterProtectionProvider(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, json={"type": "FeatureCollection", "features": []}
                    )
                )
            ),
        )
        session.commit()
        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type == "nsdi_water_protection",
            )
        )

    assert result.status == "outside_published_extent"
    assert evidence is not None and evidence.status == "manual_required"
    payload = json.loads(evidence.raw_payload_json or "{}")
    assert payload["coverage_area"] == "Костанайская область"
    assert payload["source_layer"] == "geonode:waterprotectionzone"
    assert payload["status"] == "outside_published_extent"


def test_bbox_overlap_in_another_region_is_not_treated_as_layer_coverage() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="aktobe-inside-extent-box",
            title="Лот",
            source_url="https://x",
            region="Актюбинская область",
            cadastre_number="02:234:567:899",
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, KOSTANAY_PARCEL, source="jerler:source_object")

        result = record_water_protection_evidence(
            session,
            lot,
            provider=NationalWaterProtectionProvider(
                transport=httpx.MockTransport(handler)
            ),
        )

    assert result.status == "outside_published_coverage"
    assert calls == 0
