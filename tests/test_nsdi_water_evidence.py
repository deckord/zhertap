import json

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auction_land_identity import reconcile_lot_land_object, set_land_object_boundary
from app.auction_nsdi_evidence import record_water_protection_evidence
from app.db import Base
from app.models import AuctionEvidence, AuctionLot
from app.providers.nsdi import NsdiFeature

PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[71.61, 51.25], [71.62, 51.25], [71.62, 51.26], [71.61, 51.25]]
    ],
}


class _Provider:
    def features_for_bbox(self, _bbox):
        return (
            NsdiFeature(
                "zone.1",
                "geonode:waterprotectionzone",
                {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [71.615, 51.245],
                            [71.625, 51.245],
                            [71.625, 51.265],
                            [71.615, 51.245],
                        ]
                    ],
                },
                {"name": "Водоохранная зона"},
            ),
        )


def test_nsdi_water_check_persists_warning_evidence_with_source_and_percent() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="nsdi",
            title="Лот",
            source_url="https://x",
            cadastre_number="01:234:567:890",
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, PARCEL, source="jerler:source_object")

        result = record_water_protection_evidence(session, lot, provider=_Provider())
        session.commit()

        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type == "nsdi_water_protection",
            )
        )
        assert result.status == "intersection_found"
        assert evidence is not None and evidence.status == "warning"
        assert evidence.source_url == "https://map.gov.kz/geoserver/ows"
        payload = json.loads(evidence.raw_payload_json or "{}")
        assert payload["intersection_percent"] > 0
        assert payload["requires_manual_review"] is True


def test_nsdi_empty_result_persists_manual_required_not_clear() -> None:
    class EmptyProvider:
        def features_for_bbox(self, _bbox):
            return ()

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="nsdi-empty",
            title="Лот",
            source_url="https://x",
            cadastre_number="01:234:567:891",
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, PARCEL, source="jerler:source_object")

        result = record_water_protection_evidence(
            session, lot, provider=EmptyProvider()
        )
        session.commit()
        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type == "nsdi_water_protection",
            )
        )
        assert result.status == "no_intersection_in_published_layer"
        assert evidence is not None and evidence.status == "manual_required"
        assert evidence.confidence == 0.0
