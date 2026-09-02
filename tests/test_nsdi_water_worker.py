import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.auction_land_identity import reconcile_lot_land_object, set_land_object_boundary
from app.auction_nsdi_worker import check_nsdi_water_batch
from app.db import Base
from app.models import AuctionEvidence, AuctionLot
from app.providers.nsdi import NsdiProviderError

PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[71.61, 51.25], [71.62, 51.25], [71.62, 51.26], [71.61, 51.25]]
    ],
}


class _Provider:
    def features_for_bbox(self, _bbox):
        return ()


class _UnavailableProvider:
    def features_for_bbox(self, _bbox):
        raise NsdiProviderError("temporary outage")


def test_nsdi_worker_records_one_evidence_per_canonical_boundary_lot() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="worker",
            title="Лот",
            source_url="https://x",
            cadastre_number="01:234:567:890",
            active=True,
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, PARCEL, source="jerler:source_object")
        session.commit()

    result = check_nsdi_water_batch(factory, provider=_Provider(), limit=5)
    repeated = check_nsdi_water_batch(factory, provider=_Provider(), limit=5)
    with Session(engine) as session:
        evidence = session.scalars(
            select(AuctionEvidence).where(
                AuctionEvidence.evidence_type == "nsdi_water_protection"
            )
        ).all()

    assert result == {"selected": 1, "checked": 1, "warnings": 0, "unavailable": 0}
    assert repeated["selected"] == 0
    assert len(evidence) == 1
    assert evidence[0].status == "manual_required"


def test_nsdi_worker_retries_stale_source_unavailable_evidence() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="retry-worker",
            title="Лот",
            source_url="https://x",
            cadastre_number="01:234:567:891",
            active=True,
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, PARCEL, source="jerler:source_object")
        session.commit()

    unavailable = check_nsdi_water_batch(
        factory,
        provider=_UnavailableProvider(),
        limit=5,
    )
    immediate = check_nsdi_water_batch(factory, provider=_Provider(), limit=5)
    with Session(engine) as session:
        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.evidence_type == "nsdi_water_protection"
            )
        )
        assert evidence is not None
        evidence.observed_at = datetime.now(UTC) - timedelta(minutes=16)
        session.commit()

    retried = check_nsdi_water_batch(factory, provider=_Provider(), limit=5)
    with Session(engine) as session:
        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.evidence_type == "nsdi_water_protection"
            )
        )
        assert evidence is not None
        payload = json.loads(evidence.raw_payload_json or "{}")

    assert unavailable["unavailable"] == 1
    assert immediate["selected"] == 0
    assert retried == {"selected": 1, "checked": 1, "warnings": 0, "unavailable": 0}
    assert payload["status"] == "no_intersection_in_published_layer"
    assert evidence.observed_at > datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)


def test_nsdi_worker_refreshes_legacy_evidence_without_regional_coverage_marker() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="legacy-national-claim",
            title="Лот",
            source_url="https://x",
            cadastre_number="01:234:567:892",
            active=True,
        )
        session.add(lot)
        session.flush()
        land_object = reconcile_lot_land_object(session, lot)
        assert land_object is not None
        set_land_object_boundary(land_object, PARCEL, source="jerler:source_object")
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="nsdi_water_protection",
                title="legacy",
                status="manual_required",
                raw_payload_json='{"status":"no_intersection_in_published_layer"}',
                observed_at=datetime.now(UTC),
            )
        )
        session.commit()

    refreshed = check_nsdi_water_batch(factory, provider=_Provider(), limit=5)
    with Session(engine) as session:
        evidence = session.scalar(
            select(AuctionEvidence).where(
                AuctionEvidence.evidence_type == "nsdi_water_protection"
            )
        )
        payload = json.loads(evidence.raw_payload_json or "{}")

    assert refreshed["selected"] == 1
    assert payload["coverage_area"] is None
    assert payload["status"] == "no_intersection_in_published_layer"
