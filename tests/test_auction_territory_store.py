from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auction_territory_store import (
    TerritoryObservationConflict,
    assess_and_persist_lot_applicability,
    persist_territory_observation,
)
from app.db import Base
from app.models import (
    AuctionLandObject,
    AuctionLot,
    AuctionTerritoryApplicability,
    AuctionTerritoryObservation,
)

NOW = datetime(2026, 9, 2, 3, tzinfo=UTC)


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_id": "official-project-registry",
        "source_record_id": "project-42",
        "source_revision": 1,
        "record_kind": "event",
        "authority_name": "Акимат области",
        "source_url": "https://gov.kz/project/42",
        "source_published_at": datetime(2026, 9, 1, tzinfo=UTC),
        "observed_at": NOW,
        "territory_code": "KZ-10",
        "geometry_geojson": {
            "type": "Polygon",
            "coordinates": [[[71.4, 51.1], [71.5, 51.1], [71.5, 51.2], [71.4, 51.2], [71.4, 51.1]]],
        },
        "event": {
            "event_key": "road-42",
            "event_code": "road_opened",
            "direction": "positive",
            "direction_basis": "official_field",
            "lifecycle_state": "completed",
            "event_date": date(2026, 8, 30),
        },
    }
    payload.update(changes)
    return payload


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def test_persist_is_idempotent_and_rejects_changed_same_revision(session: Session) -> None:
    first = persist_territory_observation(session, _payload())
    retry = persist_territory_observation(session, _payload())
    assert retry.id == first.id
    assert session.scalar(select(func.count(AuctionTerritoryObservation.id))) == 1

    changed = _payload(authority_name="Другой акимат")
    with pytest.raises(TerritoryObservationConflict, match="revision_content_conflict"):
        persist_territory_observation(session, changed)


def test_new_revision_is_immutable_and_preserves_provenance(session: Session) -> None:
    first = persist_territory_observation(session, _payload())
    second_payload = _payload(source_revision=2)
    second_payload["event"] = {**second_payload["event"], "lifecycle_state": "completed"}
    second = persist_territory_observation(session, second_payload)
    assert second.id != first.id
    assert first.source_revision == 1
    assert second.source_revision == 2
    assert second.authority_name == "Акимат области"
    assert second.source_published_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert second.geometry_sha256 and len(second.geometry_sha256) == 64
    assert len(second.content_hash) == 64
    assert second.contract_version.startswith("territory-intelligence/")


def test_official_polygon_applicability_is_durable_and_boundary_sensitive(session: Session) -> None:
    observation = persist_territory_observation(session, _payload())
    land = AuctionLandObject.from_identifiers(jerler_object_id="jerler-42")
    land.boundary_source = "jerler.e-qazyna.kz"
    land.boundary_geojson = (
        '{"type":"Polygon","coordinates":[[[71.42,51.12],[71.44,51.12],'
        '[71.44,51.14],[71.42,51.14],[71.42,51.12]]]}'
    )
    lot = AuctionLot(
        id="lot-42",
        source="e-qazyna",
        source_lot_id="42",
        title="Lot 42",
        source_url="https://e-qazyna.kz/42",
        land_object=land,
    )
    session.add(lot)
    session.flush()

    first = assess_and_persist_lot_applicability(session, observation, lot)
    retry = assess_and_persist_lot_applicability(session, observation, lot)
    assert first.id == retry.id
    assert (first.status, first.scope, first.basis, first.overlap_ratio) == (
        "applicable", "parcel", "scope_polygon_covers_parcel", 1.0
    )
    old_hash = first.parcel_boundary_sha256

    land.boundary_geojson = (
        '{"type":"Polygon","coordinates":[[[71.6,51.12],[71.61,51.12],'
        '[71.61,51.14],[71.6,51.14],[71.6,51.12]]]}'
    )
    changed = assess_and_persist_lot_applicability(session, observation, lot)
    assert changed.id == first.id
    assert changed.parcel_boundary_sha256 != old_hash
    assert (changed.status, changed.basis) == (
        "not_applicable", "scope_polygon_excludes_parcel"
    )
    assert session.scalar(select(func.count(AuctionTerritoryApplicability.id))) == 1


def test_missing_official_polygon_never_becomes_applicable(session: Session) -> None:
    observation = persist_territory_observation(
        session, _payload(geometry_geojson=None)
    )
    land = AuctionLandObject.from_identifiers(jerler_object_id="jerler-43")
    lot = AuctionLot(
        id="lot-43", source="e-qazyna", source_lot_id="43", title="Lot 43",
        source_url="https://e-qazyna.kz/43", land_object=land,
    )
    session.add(lot)
    session.flush()
    result = assess_and_persist_lot_applicability(session, observation, lot)
    assert (result.status, result.scope, result.basis) == (
        "manual_required", "unknown", "insufficient_official_scope"
    )
