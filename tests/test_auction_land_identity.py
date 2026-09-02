from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.auction_land_identity import (
    backfill_canonical_land_objects,
    backfill_canonical_land_objects_page,
    reconcile_lot_land_object,
)
from app.db import Base
from app.models import AuctionLandObject, AuctionLot


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session


def _canonical_object() -> AuctionLandObject:
    return AuctionLandObject.from_identifiers(
        egkn_id="23340720260504000001",
        cadastre_number="23-340-001-001",
        jerler_object_id="17830",
    )


def _contradictory_lot(*, object_id: str | None = None) -> AuctionLot:
    return AuctionLot(
        source="e-qazyna",
        source_lot_id="identity-conflict-1",
        object_type="land",
        title="Conflicting official identity fixture",
        source_url="https://sauda.e-qazyna.kz/ru/list/identity-conflict-1",
        source_object_url=(
            "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/17830/view"
        ),
        land_object_id="23340720260504000001",
        cadastre_number="23-340-001-999",
        land_object_ref_id=object_id,
        last_seen_at=datetime.now(UTC),
    )


def test_reconcile_rejects_same_egkn_when_existing_cadastre_disagrees(
    session: Session,
) -> None:
    canonical = _canonical_object()
    session.add(canonical)
    session.flush()
    lot = _contradictory_lot(object_id=canonical.id)
    session.add(lot)
    session.flush()

    result = reconcile_lot_land_object(session, lot)

    assert result is None
    assert lot.land_object_ref_id is None
    assert canonical.egkn_id == "23340720260504000001"
    assert canonical.cadastre_number == "23-340-001-001"
    assert canonical.jerler_object_id == "17830"
    assert session.scalar(select(func.count()).select_from(AuctionLandObject)) == 1


def test_reconcile_allows_new_jerler_publication_for_same_official_parcel(
    session: Session,
) -> None:
    canonical = _canonical_object()
    session.add(canonical)
    session.flush()
    lot = _contradictory_lot()
    lot.cadastre_number = canonical.cadastre_number
    lot.source_object_url = (
        "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/17831/view"
    )
    session.add(lot)
    session.flush()

    result = reconcile_lot_land_object(session, lot)

    assert result is canonical
    assert lot.land_object_ref_id == canonical.id
    # The first observed Jerler registry row remains provenance; it is not
    # overwritten by a later auction publication for the same parcel.
    assert canonical.jerler_object_id == "17830"


def test_backfill_counts_identifier_contradiction_as_unlinked(session: Session) -> None:
    canonical = _canonical_object()
    session.add(canonical)
    session.flush()
    lot = _contradictory_lot()
    session.add(lot)
    session.flush()

    result = backfill_canonical_land_objects(session, limit=10)

    assert result == {"selected": 1, "linked": 0, "unlinked": 1}
    assert lot.land_object_ref_id is None
    assert canonical.cadastre_number == "23-340-001-001"
    assert session.scalar(select(func.count()).select_from(AuctionLandObject)) == 1


def test_paged_backfill_advances_past_unlinkable_rows_without_starving_older_lots(
    session: Session,
) -> None:
    canonical = _canonical_object()
    session.add(canonical)
    session.flush()

    conflict = _contradictory_lot()
    conflict.id = "00000000-0000-0000-0000-000000000001"
    eligible = AuctionLot(
        id="00000000-0000-0000-0000-000000000002",
        source="e-qazyna",
        source_lot_id="identity-page-eligible",
        object_type="land",
        title="Eligible exact identity",
        source_url="https://sauda.e-qazyna.kz/ru/list/identity-page-eligible",
        land_object_id="23340720260504000002",
        last_seen_at=datetime.now(UTC),
    )
    session.add_all([conflict, eligible])
    session.flush()

    first = backfill_canonical_land_objects_page(
        session,
        limit=1,
        after_lot_id=None,
        high_water_lot_id=eligible.id,
    )
    second = backfill_canonical_land_objects_page(
        session,
        limit=1,
        after_lot_id=first.last_scanned_lot_id,
        high_water_lot_id=eligible.id,
    )

    assert first.selected == 1
    assert first.unlinked == 1
    assert first.last_scanned_lot_id == conflict.id
    assert first.has_more is True
    assert second.selected == 1
    assert second.linked == 1
    assert second.last_scanned_lot_id == eligible.id
    assert second.has_more is False
    assert eligible.land_object_ref_id is not None


def test_paged_backfill_advances_cursor_across_identifierless_rows(session: Session) -> None:
    placeholder = AuctionLot(
        id="00000000-0000-0000-0000-000000000010",
        source="e-qazyna",
        source_lot_id="identity-page-placeholder",
        object_type="land",
        title="No stable identity",
        source_url="https://sauda.e-qazyna.kz/ru/list/identity-page-placeholder",
        last_seen_at=datetime.now(UTC),
    )
    session.add(placeholder)
    session.flush()

    page = backfill_canonical_land_objects_page(
        session,
        limit=1,
        after_lot_id=None,
        high_water_lot_id=placeholder.id,
    )

    assert page.scanned == 1
    assert page.selected == 0
    assert page.last_scanned_lot_id == placeholder.id
    assert page.has_more is False
