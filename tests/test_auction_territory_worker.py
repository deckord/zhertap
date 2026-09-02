from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.auction_territory_store import persist_territory_observation
from app.auction_territory_worker import link_territory_observation_batch
from app.db import Base
from app.models import AuctionLandObject, AuctionLot, AuctionTerritoryApplicability
from app.tasks import celery_app

NOW = datetime(2026, 9, 2, 3, tzinfo=UTC)


def _payload() -> dict[str, object]:
    return {
        "provider_id": "official-project-registry",
        "source_record_id": "project-42",
        "source_revision": 1,
        "record_kind": "event",
        "authority_name": "Акимат области",
        "source_url": "https://gov.kz/project/42",
        "source_published_at": datetime(2026, 9, 1, tzinfo=UTC),
        "observed_at": NOW,
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


def test_bounded_worker_links_active_canonical_lots_idempotently(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'territory.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        observation = persist_territory_observation(session, _payload())
        for index, active in enumerate((True, True, False), start=1):
            land = AuctionLandObject.from_identifiers(jerler_object_id=f"jerler-{index}")
            land.boundary_source = "jerler.e-qazyna.kz"
            land.boundary_geojson = (
                '{"type":"Polygon","coordinates":[[[71.42,51.12],[71.44,51.12],'
                '[71.44,51.14],[71.42,51.14],[71.42,51.12]]]}'
            )
            session.add(AuctionLot(
                id=f"lot-{index}", source="e-qazyna", source_lot_id=str(index),
                title=f"Lot {index}", source_url=f"https://e-qazyna.kz/{index}",
                active=active, land_object=land,
            ))
        session.commit()
        observation_id = observation.id

    first = link_territory_observation_batch(factory, observation_id=observation_id, limit=1)
    second = link_territory_observation_batch(factory, observation_id=observation_id, limit=10)
    retry = link_territory_observation_batch(factory, observation_id=observation_id, limit=10)
    assert first == {
        "selected": 1,
        "assessed": 1,
        "applicable": 1,
        "manual_required": 0,
        "not_applicable": 0,
    }
    assert second["assessed"] == 1
    assert retry["assessed"] == 0
    with factory() as session:
        assert session.scalar(select(func.count(AuctionTerritoryApplicability.id))) == 2


def test_task_is_routed_to_auctions_queue() -> None:
    assert celery_app.conf.task_routes["land_scout.link_territory_observation"] == {
        "queue": "auctions"
    }
