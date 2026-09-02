from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.auction_map_projection import (
    AuctionMapProjectionFilter,
    MapBoundingBox,
    load_auction_map_projection,
)
from app.db import Base
from app.models import (
    AuctionDocument,
    AuctionEvidence,
    AuctionLot,
    AuctionLotGeoCheck,
    AuctionLotV2Analysis,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        yield database


def _lot(index: int, **overrides: object) -> AuctionLot:
    values: dict[str, object] = {
        "source": "e-qazyna",
        "source_lot_id": f"map-{index}",
        "source_url": f"https://example.test/map-{index}",
        "title": f"Земельный участок {index}",
        "object_type": "land",
        "source_search_status": "ApplicationsAccept",
        "region": "Область Абай",
        "district": "Жаңасемей",
        "locality": "Новобаженово",
        "functional_purpose_level2": "Коммерческая земля",
        "area_ha": 1.0,
        "start_price_kzt": 1_000_000,
        "auction_starts_at": NOW + timedelta(days=3),
        "active": True,
    }
    values.update(overrides)
    return AuctionLot(**values)


def _persist_marker_records(
    session: Session,
    lot: AuctionLot,
    *,
    index: int,
    latitude: float | None = 50.4,
    longitude: float | None = 80.2,
) -> None:
    session.flush()
    session.add(
        AuctionLotV2Analysis(
            lot_id=lot.id,
            score=index % 101,
            risk_level="low",
            confidence_level="high",
            recommended_action="participate",
            price_per_sotka=10_000,
        )
    )
    session.add(
        AuctionLotGeoCheck(
            lot_id=lot.id,
            latitude=latitude,
            longitude=longitude,
            coordinate_status="found" if latitude is not None else "missing",
            cadastre_status="found",
        )
    )


def test_300_markers_use_two_fixed_projection_queries(session: Session) -> None:
    lots = [_lot(index) for index in range(300)]
    session.add_all(lots)
    session.flush()
    for index, lot in enumerate(lots):
        _persist_marker_records(session, lot, index=index)
    # A document on every lot would expose accidental relationship loading.
    session.add_all(
        AuctionDocument(
            lot_id=lot.id,
            title="Документ",
            source_url=f"https://example.test/document/{lot.id}",
        )
        for lot in lots
    )
    session.commit()

    statements: list[str] = []

    def record(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        statements.append(statement)

    event.listen(session.bind, "before_cursor_execute", record)
    try:
        payload = load_auction_map_projection(
            session,
            AuctionMapProjectionFilter(lot_scope="all", limit=300),
            now=NOW,
        )
        serialized = payload.as_dict()
    finally:
        event.remove(session.bind, "before_cursor_execute", record)

    assert payload.total == payload.loaded == payload.mapped == 300
    assert len(serialized["markers"]) == 300
    assert len(statements) == 2
    assert all("auction_documents" not in statement.lower() for statement in statements)
    assert all("raw_payload_json" not in statement.lower() for statement in statements)


def test_missing_persisted_records_are_explicit_unknown_not_negative(
    session: Session,
) -> None:
    lot = _lot(1, auction_starts_at=None)
    session.add(lot)
    session.commit()

    payload = load_auction_map_projection(
        session,
        AuctionMapProjectionFilter(lot_ids=(lot.id,), limit=1),
        now=NOW,
    )

    assert payload.loaded == 1
    assert payload.mapped == 0
    item = payload.items[0]
    assert item.analysis_status == "unknown"
    assert item.geo_status == "unknown"
    assert item.coordinate_status == "unknown"
    assert item.evidence_status == "unknown"
    assert item.evidence_count == 0
    assert item.score is None
    assert item.as_marker() is None


def test_bbox_exact_filters_and_evidence_are_projected(session: Session) -> None:
    inside = _lot(1)
    outside = _lot(2)
    session.add_all([inside, outside])
    session.flush()
    _persist_marker_records(session, inside, index=90, latitude=50.1, longitude=80.1)
    _persist_marker_records(session, outside, index=20, latitude=53.0, longitude=75.0)
    session.add_all(
        [
            AuctionEvidence(
                lot_id=inside.id,
                evidence_type="egkn",
                status="found",
                title="ЕГКН",
                confidence=0.9,
                observed_at=NOW,
            ),
            AuctionEvidence(
                lot_id=inside.id,
                evidence_type="restriction",
                status="conflict",
                title="Конфликт",
                confidence=0.5,
                observed_at=NOW,
            ),
        ]
    )
    session.commit()

    payload = load_auction_map_projection(
        session,
        AuctionMapProjectionFilter(
            region="Область Абай",
            min_score=80,
            bbox=MapBoundingBox(south=49, west=79, north=51, east=81),
            lot_scope="all",
            limit=10,
        ),
        now=NOW,
    )

    assert [item.id for item in payload.items] == [inside.id]
    assert payload.items[0].evidence_status == "conflict"
    assert payload.items[0].evidence_count == 2
    assert payload.items[0].evidence_conflict_count == 1


def test_invalid_persisted_coordinates_are_not_mapped_or_serialized(
    session: Session,
) -> None:
    lot = _lot(1)
    session.add(lot)
    session.flush()
    _persist_marker_records(
        session,
        lot,
        index=80,
        latitude=float("nan"),
        longitude=float("inf"),
    )
    session.commit()

    payload = load_auction_map_projection(
        session,
        AuctionMapProjectionFilter(lot_ids=(lot.id,), limit=1),
        now=NOW,
    )

    item = payload.items[0]
    assert item.latitude is None
    assert item.longitude is None
    assert item.coordinate_status == "invalid"
    assert item.geo_status == "invalid"
    assert payload.mapped == 0
    assert payload.as_dict()["markers"] == []


def test_active_scope_excludes_past_and_archived_status_but_includes_future(
    session: Session,
) -> None:
    future = _lot(1)
    past = _lot(2, auction_starts_at=NOW - timedelta(minutes=1))
    archived_status = _lot(3, source_search_status="SuccessProtocolSigned")
    session.add_all([future, past, archived_status])
    session.flush()
    for index, lot in enumerate((future, past, archived_status), start=1):
        _persist_marker_records(session, lot, index=index)
    session.commit()

    active = load_auction_map_projection(
        session,
        AuctionMapProjectionFilter(lot_scope="active", limit=10),
        now=NOW,
    )
    archive = load_auction_map_projection(
        session,
        AuctionMapProjectionFilter(lot_scope="archive", limit=10),
        now=NOW,
    )

    assert [item.id for item in active.items] == [future.id]
    assert active.items[0].scope == "future"
    assert {item.id for item in archive.items} == {past.id, archived_status.id}


@pytest.mark.parametrize(
    "filters",
    [
        AuctionMapProjectionFilter(limit=501),
        AuctionMapProjectionFilter(min_price_kzt=float("nan")),
        AuctionMapProjectionFilter(min_score=True),
        AuctionMapProjectionFilter(
            bbox=MapBoundingBox(south=60, west=79, north=61, east=81)
        ),
        AuctionMapProjectionFilter(lot_ids=tuple(str(index) for index in range(501))),
    ],
)
def test_projection_rejects_unbounded_or_invalid_filters(
    session: Session,
    filters: AuctionMapProjectionFilter,
) -> None:
    with pytest.raises(ValueError):
        load_auction_map_projection(session, filters, now=NOW)
