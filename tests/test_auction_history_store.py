from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Numeric, create_engine, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from app.auction_history_normalization import (
    active_history_generation,
    promote_history_generation,
    run_history_normalization_batch,
    start_history_generation,
)
from app.auction_history_read import normalized_similar_history
from app.auction_history_store import SqlAlchemyHistoryNormalizationStore
from app.auction_history_worker import normalize_auction_history_step
from app.db import Base
from app.models import (
    AuctionHistoryGeneration,
    AuctionHistoryGenerationLot,
    AuctionHistoryNormalized,
    AuctionLot,
)

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _lot(index: int, **overrides: object) -> AuctionLot:
    values: dict[str, object] = {
        "source": "e-qazyna",
        "source_lot_id": f"history-{index}",
        "source_url": f"https://example.test/history-{index}",
        "title": "Участок для строительства магазина",
        "object_type": "land",
        "status": "Аукцион состоялся",
        "land_rights": "Продажа права аренды земельного участка",
        "lease_term_years": 5,
        "purpose": "Строительство магазина",
        "area_ha": 1.0,
        "start_price_kzt": 1_000_000,
        "sale_price_kzt": 2_000_000,
        "auction_starts_at": datetime(2026, 7, index + 1, tzinfo=UTC),
        "region": "Область Абай",
        "district": "Жаңасемей",
        "locality": "Новобаженово",
        "created_at": CUTOFF - timedelta(days=index),
        "updated_at": CUTOFF - timedelta(minutes=index),
    }
    values.update(overrides)
    return AuctionLot(**values)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_sqlalchemy_store_builds_batches_reconciles_and_atomically_activates() -> None:
    session = _session()
    try:
        session.add_all([_lot(1), _lot(2), _lot(3)])
        session.commit()
        store = SqlAlchemyHistoryNormalizationStore(session)

        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None
        assert run.expected_count == 3
        assert run.source_high_water_lot_id is not None
        assert start_history_generation(store, source_cutoff=CUTOFF) is None

        first = run_history_normalization_batch(store, run=run, batch_size=2)
        second = run_history_normalization_batch(store, run=first.run, batch_size=2)

        assert first.has_more is True
        assert second.has_more is False
        assert second.run.processed_count == 3
        assert second.run.scan_complete is True
        active = promote_history_generation(store, second.run)
        assert active.status == "active"
        assert active_history_generation(store) == active
        session.rollback()  # close the read transaction opened by active lookup

        rows = list(
            session.scalars(
                select(AuctionHistoryNormalized).where(
                    AuctionHistoryNormalized.generation == active.generation
                )
            )
        )
        assert len(rows) == 3
        assert {row.purpose_group for row in rows} == {"retail"}
        assert all(isinstance(row.sale_price_kzt, Decimal) for row in rows)
    finally:
        session.close()


def test_generation_membership_ignores_backdated_inserts_on_both_sides_of_cursor() -> None:
    session = _session()
    try:
        original_low = _lot(10, id="40000000-0000-0000-0000-000000000000")
        original_high = _lot(11, id="80000000-0000-0000-0000-000000000000")
        session.add_all((original_low, original_high))
        session.commit()
        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None

        first = run_history_normalization_batch(store, run=run, batch_size=1)
        assert first.run.checkpoint.after_lot_id == original_low.id

        inserted_behind = _lot(12, id="20000000-0000-0000-0000-000000000000")
        inserted_ahead = _lot(13, id="f0000000-0000-0000-0000-000000000000")
        session.add_all((inserted_behind, inserted_ahead))
        session.commit()

        final = run_history_normalization_batch(store, run=first.run, batch_size=10)
        active = promote_history_generation(store, final.run)
        member_ids = set(
            session.scalars(
                select(AuctionHistoryGenerationLot.lot_id).where(
                    AuctionHistoryGenerationLot.generation == active.generation
                )
            )
        )
        normalized_ids = set(
            session.scalars(
                select(AuctionHistoryNormalized.lot_id).where(
                    AuctionHistoryNormalized.generation == active.generation
                )
            )
        )

        assert active.expected_count == active.processed_count == 2
        assert member_ids == normalized_ids == {original_low.id, original_high.id}
    finally:
        session.close()


def test_snapshot_cutoff_excludes_newer_rows_and_activation_supersedes_old() -> None:
    session = _session()
    try:
        session.add_all(
            [
                _lot(1),
                _lot(
                    2,
                    created_at=CUTOFF + timedelta(minutes=1),
                    updated_at=CUTOFF + timedelta(minutes=1),
                ),
            ]
        )
        session.commit()
        store = SqlAlchemyHistoryNormalizationStore(session)
        first_run = start_history_generation(store, source_cutoff=CUTOFF)
        assert first_run is not None
        assert first_run.expected_count == 1
        first_batch = run_history_normalization_batch(store, run=first_run, batch_size=10)
        first_active = promote_history_generation(store, first_batch.run)
        assert first_active.status == "active"

        second_run = start_history_generation(
            store,
            source_cutoff=CUTOFF + timedelta(minutes=2),
        )
        assert second_run is not None
        assert second_run.expected_count == 2
        second_batch = run_history_normalization_batch(store, run=second_run, batch_size=10)
        second_active = promote_history_generation(store, second_batch.run)

        first_model = session.get(AuctionHistoryGeneration, first_active.generation)
        assert first_model is not None
        assert first_model.status == "superseded"
        assert second_active.status == "active"
        assert second_active.generation != first_active.generation
    finally:
        session.close()


def test_history_schema_uses_numeric_money_and_partial_lifecycle_indexes() -> None:
    session = _session()
    try:
        inspector = inspect(session.get_bind())
        generation_indexes = {
            item["name"] for item in inspector.get_indexes("auction_history_generations")
        }
        normalized_indexes = {
            item["name"] for item in inspector.get_indexes("auction_history_normalized")
        }
        assert "uq_auction_history_generations_one_building" in generation_indexes
        assert "uq_auction_history_generations_one_active" in generation_indexes
        assert {
            "ix_auction_history_norm_locality_dims",
            "ix_auction_history_norm_district_dims",
            "ix_auction_history_norm_region_dims",
            "ix_auction_history_norm_area_date",
        } <= normalized_indexes
        for column_name in (
            "start_price_kzt",
            "sale_price_kzt",
            "start_price_per_ha_kzt",
            "sale_price_per_ha_kzt",
        ):
            assert isinstance(
                AuctionHistoryNormalized.__table__.c[column_name].type,
                Numeric,
            )
    finally:
        session.close()


def test_history_migration_stays_on_identity_branch() -> None:
    migration = ROOT / "migrations" / "versions" / "f8c1d2e3a4b5_auction_history_normalized.py"
    source = migration.read_text(encoding="utf-8")

    assert 'revision: str = "f8c1d2e3a4b5"' in source
    assert 'down_revision: str | Sequence[str] | None = "e7b9c2d4f6a1"' in source
    assert "sa.Numeric(20, 2)" in source
    assert "uq_auction_history_generations_one_building" in source
    assert "uq_auction_history_generations_one_active" in source
    assert "f3a2b6c9d8e1" not in source


def test_worker_processes_one_bounded_batch_and_resumes() -> None:
    session = _session()
    try:
        session.add_all([_lot(1), _lot(2)])
        session.commit()
        factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)

        first = normalize_auction_history_step(
            factory,
            batch_size=1,
            source_cutoff=CUTOFF,
        )
        assert first["status"] == "ok"
        assert first["has_more"] is True
        second = normalize_auction_history_step(
            factory,
            generation=int(first["generation"]),
            batch_size=1,
        )
        assert second["status"] == "active"
        assert second["has_more"] is False
        assert second["processed_count"] == 2
    finally:
        session.close()


def test_snapshot_keeps_rows_that_are_updated_after_generation_start() -> None:
    session = _session()
    try:
        first = _lot(1)
        changed_during_scan = _lot(2)
        session.add_all((first, changed_during_scan))
        session.commit()
        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None
        assert run.expected_count == 2

        changed_during_scan.updated_at = CUTOFF + timedelta(hours=1)
        session.commit()

        batch = run_history_normalization_batch(store, run=run, batch_size=10)
        active = promote_history_generation(store, batch.run)

        assert active.status == "active"
        assert active.processed_count == 2
    finally:
        session.close()


def test_worker_marks_unreconciled_generation_failed_instead_of_leaving_it_stuck() -> None:
    session = _session()
    try:
        first = _lot(1)
        removed_during_scan = _lot(2)
        session.add_all((first, removed_during_scan))
        session.commit()
        factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None

        session.delete(removed_during_scan)
        session.commit()

        result = normalize_auction_history_step(
            factory,
            generation=run.generation,
            batch_size=10,
        )

        assert result["status"] == "failed"
        model = session.get(AuctionHistoryGeneration, run.generation)
        assert model is not None
        assert model.status == "failed"
        assert model.detail == "generation counts do not reconcile"
    finally:
        session.close()


def test_read_path_uses_only_active_generation_and_returns_medians() -> None:
    session = _session()
    try:
        target = _lot(1, sale_price_kzt=None)
        comparable_low = _lot(2, start_price_kzt=1_000_000, sale_price_kzt=2_000_000)
        comparable_high = _lot(3, start_price_kzt=3_000_000, sale_price_kzt=6_000_000)
        session.add_all([target, comparable_low, comparable_high])
        session.commit()

        generation, unavailable = normalized_similar_history(session, target)
        assert generation is None
        assert unavailable.status == "insufficient_data"
        session.rollback()

        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None
        batch = run_history_normalization_batch(store, run=run, batch_size=10)
        active = promote_history_generation(store, batch.run)

        generation, aggregate = normalized_similar_history(session, target)
        assert generation == active.generation
        assert aggregate.status == "ok"
        assert aggregate.matched_count == 2
        assert aggregate.median_start_price_kzt == 2_000_000
        assert aggregate.median_sale_price_kzt == 4_000_000
        assert aggregate.median_sale_to_start_ratio == 2
    finally:
        session.close()


def test_normalized_comparables_exclude_all_attempts_of_target_object() -> None:
    session = _session()
    try:
        target = _lot(1, land_object_id="OBJECT-1", sale_price_kzt=None)
        repeated_attempt = _lot(
            2,
            land_object_id="OBJECT-1",
            start_price_kzt=9_000_000,
            sale_price_kzt=18_000_000,
        )
        real_comparable = _lot(
            3,
            land_object_id="OBJECT-2",
            start_price_kzt=3_000_000,
            sale_price_kzt=6_000_000,
        )
        session.add_all([target, repeated_attempt, real_comparable])
        session.commit()

        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None
        batch = run_history_normalization_batch(store, run=run, batch_size=10)
        promote_history_generation(store, batch.run)

        _, aggregate = normalized_similar_history(session, target)

        assert aggregate.status == "ok"
        assert aggregate.matched_count == 1
        assert aggregate.median_start_price_kzt == 3_000_000
        assert aggregate.median_sale_price_kzt == 6_000_000
    finally:
        session.close()


def test_normalized_comparables_exclude_complete_cadastre_repeat() -> None:
    session = _session()
    try:
        target = _lot(
            1,
            land_object_id=None,
            cadastre_number="14:215:002:000",
            sale_price_kzt=None,
        )
        repeated_attempt = _lot(
            2,
            land_object_id=None,
            cadastre_number="14:215:002:000",
            start_price_kzt=9_000_000,
            sale_price_kzt=18_000_000,
        )
        real_comparable = _lot(
            3,
            land_object_id=None,
            cadastre_number="14:215:002:001",
            start_price_kzt=3_000_000,
            sale_price_kzt=6_000_000,
        )
        session.add_all([target, repeated_attempt, real_comparable])
        session.commit()

        store = SqlAlchemyHistoryNormalizationStore(session)
        run = start_history_generation(store, source_cutoff=CUTOFF)
        assert run is not None
        batch = run_history_normalization_batch(store, run=run, batch_size=10)
        promote_history_generation(store, batch.run)

        _, aggregate = normalized_similar_history(session, target)

        assert aggregate.matched_count == 1
        assert aggregate.median_start_price_kzt == 3_000_000
    finally:
        session.close()
