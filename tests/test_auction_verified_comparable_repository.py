from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, insert, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker

from app.auction_verified_comparable_inventory import (
    build_geo_selection_plan,
    normalize_inventory_fact,
)
from app.auction_verified_comparable_repository import (
    POSTGRES_EXPLAIN_PROPOSAL,
    VerifiedComparableRepositoryError,
    build_current_selection_statement,
    ingest_verified_comparable,
    query_verified_comparables,
)
from app.db import Base
from app.models import (
    AuctionMarketInventoryGeneration,
    AuctionVerifiedComparableCurrent,
    AuctionVerifiedComparableObservation,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
GENERATION = "a" * 64


def _factory(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'inventory.sqlite3'}")
    Base.metadata.create_all(
        engine,
        tables=[
            AuctionVerifiedComparableObservation.__table__,
            AuctionVerifiedComparableCurrent.__table__,
            AuctionMarketInventoryGeneration.__table__,
        ],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _fact(index: int, **changes: object):
    payload: dict[str, object] = {
        "sequence_id": index,
        "source_name": "Реестр продаж",
        "source_record_id": f"record-{index}",
        "source_sale_id": f"sale-{index}",
        "source_listing_id": None,
        "source_url": f"https://registry.example/{index}",
        "object_id": f"parcel-{index}",
        "fact_status": "found",
        "price_kind": "verified_sale",
        "verification_status": "verified",
        "verification_ref": f"contract:{index}",
        "right_type": "lease",
        "purpose_group": "camping",
        "lease_term_years": 3,
        "area_ha": 1,
        "price_kzt": 10_000_000 + index,
        "latitude": 50.4111,
        "longitude": 80.2275,
        "access_readiness": "ready",
        "infrastructure_readiness": "partial",
        "event_at": NOW - timedelta(days=10),
        "observed_at": NOW - timedelta(minutes=index),
        "title": f"Продажа {index}",
        "locality": "Семей",
        "provenance_refs": [f"registry-contract:{index}"],
        "conflict_fields": [],
    }
    payload.update(changes)
    return normalize_inventory_fact(payload)


def _query(factory, **changes):
    arguments = {
        "latitude": 50.4111,
        "longitude": 80.2275,
        "right_type": "lease",
        "purpose_group": "camping",
        "area_ha": 1,
        "valuation_at": NOW,
        "lease_term_years": 3,
    }
    arguments.update(changes)
    return query_verified_comparables(factory, **arguments)


def test_ingest_is_idempotent_and_keeps_one_authoritative_current(tmp_path) -> None:
    _, factory = _factory(tmp_path)
    fact = _fact(1)
    first = ingest_verified_comparable(
        factory, fact, generation_signature=GENERATION, raw_payload={"provider": "ok"}
    )
    second = ingest_verified_comparable(
        factory, fact, generation_signature=GENERATION, raw_payload={"provider": "ok"}
    )
    assert first.inserted is True
    assert first.current_changed is True
    assert second.inserted is False
    assert second.current_changed is False
    with factory() as session:
        assert session.scalar(select(func.count(AuctionVerifiedComparableObservation.id))) == 1
        current_count = session.scalar(
            select(func.count(AuctionVerifiedComparableCurrent.observation_id))
        )
        assert current_count == 1


def test_newer_conflict_tombstone_blocks_older_and_newer_restore_advances_current(tmp_path) -> None:
    _, factory = _factory(tmp_path)
    older = _fact(1, source_sale_id="same", observed_at=NOW - timedelta(days=3))
    tombstone = normalize_inventory_fact(
        {
            "sequence_id": 2,
            "source_name": "Реестр продаж",
            "source_record_id": "conflict-2",
            "source_sale_id": "same",
            "source_listing_id": None,
            "fact_status": "conflict",
            "price_kind": "verified_sale",
            "observed_at": NOW - timedelta(days=2),
            "provenance_refs": ["provider-conflict:2"],
            "conflict_fields": ["price_kzt"],
        }
    )
    restore = _fact(3, source_sale_id="same", observed_at=NOW - timedelta(days=1))
    ingest_verified_comparable(factory, older, generation_signature=GENERATION)
    conflict_result = ingest_verified_comparable(factory, tombstone, generation_signature="b" * 64)
    late_old = ingest_verified_comparable(factory, older, generation_signature=GENERATION)
    assert conflict_result.current_changed is True
    assert late_old.current_changed is False
    assert _query(factory).selected == ()
    restored = ingest_verified_comparable(factory, restore, generation_signature="c" * 64)
    assert restored.current_changed is True
    assert [item.fact.sequence_id for item in _query(factory).selected] == [3]


def test_new_crawl_generation_and_raw_noise_do_not_duplicate_same_normalized_fact(
    tmp_path,
) -> None:
    _, factory = _factory(tmp_path)
    fact = _fact(1)
    first = ingest_verified_comparable(
        factory,
        fact,
        generation_signature="a" * 64,
        raw_payload={"crawl_noise": 1},
    )
    second = ingest_verified_comparable(
        factory,
        fact,
        generation_signature="b" * 64,
        raw_payload={"crawl_noise": 2},
    )
    assert first.content_hash == second.content_hash
    assert second.inserted is False
    assert second.current_changed is False
    with factory() as session:
        observations = list(session.scalars(select(AuctionVerifiedComparableObservation)))
    assert len(observations) == 1
    assert observations[0].generation_signature == "a" * 64


def test_query_uses_one_current_sql_read_and_exact_haversine_after_bbox(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    inside = _fact(1, longitude=80.27)
    bbox_corner = _fact(2, latitude=50.451, longitude=80.29)
    ingest_verified_comparable(factory, inside, generation_signature=GENERATION)
    ingest_verified_comparable(factory, bbox_corner, generation_signature=GENERATION)
    selects = 0

    def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
        nonlocal selects
        if statement.lstrip().upper().startswith("SELECT"):
            selects += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        result = _query(factory)
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)
    assert selects == 1
    assert [item.fact.sequence_id for item in result.selected] == [1]
    assert result.selected[0].distance_km <= 5


def test_query_excludes_previous_calendar_year_in_indexed_sql(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    valuation_at = datetime(2026, 1, 15, 12, tzinfo=UTC)
    previous_year = _fact(
        1,
        event_at=datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
        observed_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    current_year = _fact(
        2,
        event_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
        observed_at=datetime(2026, 1, 3, 12, tzinfo=UTC),
    )
    ingest_verified_comparable(factory, previous_year, generation_signature=GENERATION)
    ingest_verified_comparable(factory, current_year, generation_signature=GENERATION)
    selected_parameters: list[tuple[object, ...]] = []

    def capture_select(_conn, _cursor, statement, parameters, _context, _many):
        if "auction_verified_comparable_current" in statement and statement.lstrip().upper().startswith("SELECT"):
            selected_parameters.append(tuple(parameters))

    event.listen(engine, "before_cursor_execute", capture_select)
    try:
        result = _query(factory, valuation_at=valuation_at)
    finally:
        event.remove(engine, "before_cursor_execute", capture_select)

    assert [item.fact.sequence_id for item in result.selected] == [2]
    assert selected_parameters
    assert previous_year.event_at not in selected_parameters[0]


def test_sql_prefilters_ten_thousand_irrelevant_current_rows_before_limit(tmp_path) -> None:
    _, factory = _factory(tmp_path)
    ingest_verified_comparable(
        factory, _fact(1, purpose_group="warehouse"), generation_signature=GENERATION
    )
    with factory() as session, session.begin():
        base = session.scalar(select(AuctionVerifiedComparableCurrent))
        assert base is not None
        template = {
            column.name: getattr(base, column.name)
            for column in AuctionVerifiedComparableCurrent.__table__.columns
        }
        mappings = []
        for index in range(2, 10_001):
            mapping = dict(template)
            mapping.update(
                source_identity_key=f"sha256:{index:064x}",
                observation_id=100_000 + index,
                source_sequence_id=index,
                source_record_id=f"dense-{index}",
                source_sale_id=f"dense-sale-{index}",
                content_hash=f"{index:064x}",
                observed_at=NOW - timedelta(seconds=index),
            )
            mappings.append(mapping)
        session.execute(insert(AuctionVerifiedComparableCurrent), mappings)
    for index in range(700, 703):
        ingest_verified_comparable(
            factory,
            _fact(index, observed_at=NOW + timedelta(minutes=index)),
            generation_signature=GENERATION,
        )
    result = _query(factory)
    assert {item.fact.sequence_id for item in result.selected} == {700, 701, 702}
    assert result.scanned_count == 3


def test_same_rank_divergence_is_order_independent_conflict_and_conflict_has_precedence(
    tmp_path,
) -> None:
    winners = []
    for suffix, reverse in (("a", False), ("b", True)):
        _, factory = _factory(tmp_path / suffix)
        low = _fact(1, source_sale_id="same", price_kzt=9_000_000, observed_at=NOW)
        high = _fact(1, source_sale_id="same", price_kzt=11_000_000, observed_at=NOW)
        ordered = (high, low) if reverse else (low, high)
        for fact in ordered:
            ingest_verified_comparable(factory, fact, generation_signature=GENERATION)
        with factory() as session:
            current = session.scalar(select(AuctionVerifiedComparableCurrent))
            assert current is not None
            assert current.fact_status == "conflict"
            assert current.conflicts_json == '["same_rank_divergence"]'
            assert session.scalar(select(func.count(AuctionVerifiedComparableObservation.id))) == 3
            winners.append(current.content_hash)
    assert winners[0] == winners[1]

    _, factory = _factory(tmp_path / "conflict")
    found = _fact(1, source_sale_id="same", observed_at=NOW)
    conflict = normalize_inventory_fact(
        {
            "sequence_id": 1,
            "source_name": "Реестр продаж",
            "source_record_id": "conflict",
            "source_sale_id": "same",
            "source_listing_id": None,
            "fact_status": "conflict",
            "price_kind": "verified_sale",
            "observed_at": NOW,
            "provenance_refs": ["conflict:same-rank"],
            "conflict_fields": ["price_kzt"],
        }
    )
    ingest_verified_comparable(factory, found, generation_signature=GENERATION)
    ingest_verified_comparable(factory, conflict, generation_signature=GENERATION)
    with factory() as session:
        assert session.scalar(select(AuctionVerifiedComparableCurrent.fact_status)) == "conflict"


def test_repository_revalidates_manual_dataclass_and_cursor_contract(tmp_path) -> None:
    _, factory = _factory(tmp_path)
    invalid = replace(_fact(1), latitude=99.0)
    with pytest.raises(VerifiedComparableRepositoryError, match="invalid_inventory_fact"):
        ingest_verified_comparable(factory, invalid, generation_signature=GENERATION)
    with pytest.raises(VerifiedComparableRepositoryError, match="invalid_cursor"):
        _query(factory, cursor=(NOW.replace(tzinfo=None), 1))


def test_postgresql_statement_has_current_target_filters_keyset_and_no_history_distinct() -> None:
    plan = build_geo_selection_plan(
        50.4111,
        80.2275,
        right_type="lease",
        purpose_group="camping",
        area_ha=1,
        valuation_at=NOW,
        lease_term_years=3,
    )
    statement = build_current_selection_statement(plan, cursor=(NOW - timedelta(days=1), 100))
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert "auction_verified_comparable_current" in sql
    assert "distinct" not in sql
    assert "right_type" in sql and "purpose_group" in sql and "lease_band" in sql
    assert "latitude" in sql and "longitude" in sql and "area_ha" in sql
    assert "observed_at" in sql and "observation_id" in sql and "limit" in sql
    assert "provenance_json" not in sql and "raw_payload_json" not in sql
    assert POSTGRES_EXPLAIN_PROPOSAL.startswith("EXPLAIN (ANALYZE, BUFFERS")


def test_schema_has_partial_target_index_and_d8_migration_parent() -> None:
    indexes = {index.name: index for index in AuctionVerifiedComparableCurrent.__table__.indexes}
    for name in (
        "ix_auction_verified_comparable_current_target_geo",
        "ix_auction_verified_comparable_current_target_event",
    ):
        assert indexes[name].dialect_options["postgresql"]["where"] is not None
    source = open(
        "migrations/versions/c9e4b7a2d5f8_verified_comparable_inventory.py",
        encoding="utf-8",
    ).read()
    assert 'down_revision: str | Sequence[str] | None = "d8f3a1c5e7b9"' in source
    assert "f3a2b6c9d8e1" not in source
