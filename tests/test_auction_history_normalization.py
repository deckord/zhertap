from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.auction_history_normalization import (
    BackfillCheckpoint,
    HistoryGenerationRun,
    NormalizedAuctionHistoryRow,
    RawAuctionHistoryRecord,
    SimilarHistoryTarget,
    active_history_generation,
    aggregate_similar_normalized_history,
    build_similar_history_query_spec,
    history_cache_key,
    normalize_history_record,
    promote_history_generation,
    run_history_normalization_batch,
    start_history_generation,
)

UPDATED_AT = datetime(2026, 8, 17, tzinfo=UTC)


def _run(
    generation: int,
    *,
    status: str = "active",
    expected_count: int = 0,
    processed_count: int = 0,
    checkpoint: str | None = None,
    scan_complete: bool = True,
) -> HistoryGenerationRun:
    return HistoryGenerationRun(
        generation=generation,
        status=status,
        source_cutoff=UPDATED_AT,
        source_high_water_lot_id=None,
        expected_count=expected_count,
        processed_count=processed_count,
        checkpoint=BackfillCheckpoint(checkpoint),
        scan_complete=scan_complete,
    )


def _raw(lot_id: str, **overrides: object) -> RawAuctionHistoryRecord:
    values: dict[str, object] = {
        "lot_id": lot_id,
        "source_updated_at": UPDATED_AT,
        "status": "Аукцион состоялся",
        "land_rights": "Продажа права аренды земельного участка",
        "purpose": "Строительство магазина",
        "lease_term_years": 5,
        "auction_starts_at": datetime(2026, 6, 1, tzinfo=UTC),
        "area_ha": 1.0,
        "start_price_kzt": 1_000_000,
        "sale_price_kzt": 2_000_000,
        "region": "Область Абай",
        "district": "Жаңасемей",
        "locality": "Новобаженово",
    }
    values.update(overrides)
    return RawAuctionHistoryRecord(**values)


def test_failed_cyrillic_status_is_not_misclassified_as_success() -> None:
    failed = normalize_history_record(
        _raw("failed", status="Аукцион не состоялся", sale_price_kzt=None),
        generation=1,
    )
    successful = normalize_history_record(
        _raw("successful", status="Аукцион состоялся", sale_price_kzt=None),
        generation=1,
    )

    assert failed.outcome == "failure"
    assert failed.outcome_status == "found"
    assert successful.outcome == "success"
    assert successful.outcome_status == "found"


def test_ownership_lease_bands_and_conflicts_are_explicit() -> None:
    ownership = normalize_history_record(
        _raw("own", land_rights="Частная собственность", lease_term_years=49),
        generation=1,
    )
    short = normalize_history_record(
        _raw("short", lease_term_years=3),
        generation=1,
    )
    medium = normalize_history_record(
        _raw("medium", lease_term_years=5),
        generation=1,
    )
    long = normalize_history_record(
        _raw("long", lease_term_years=49),
        generation=1,
    )
    conflict = normalize_history_record(
        _raw("conflict", right_claims=("Частная собственность",)),
        generation=1,
    )

    assert (ownership.right_kind, ownership.lease_band, ownership.lease_status) == (
        "ownership",
        "not_applicable",
        "not_applicable",
    )
    assert short.lease_band == "short_0_3"
    assert medium.lease_band == "medium_3_10"
    assert long.lease_band == "long_10_plus"
    assert conflict.right_kind == "conflict"
    assert conflict.right_status == "conflict"
    assert conflict.lease_status == "conflict"


def test_camping_is_not_collapsed_into_hospitality_taxonomy() -> None:
    camping = normalize_history_record(
        _raw("camping", purpose="Строительство кемпинга"),
        generation=1,
    )
    hotel = normalize_history_record(
        _raw("hotel", purpose="Строительство гостиницы"),
        generation=1,
    )

    assert camping.purpose_group == "camping"
    assert camping.purpose_status == "found"
    assert hotel.purpose_group == "hospitality"
    assert hotel.purpose_status == "found"


def test_invalid_price_area_date_and_status_price_conflict_are_not_fabricated() -> None:
    row = normalize_history_record(
        _raw(
            "invalid",
            status="Аукцион не состоялся",
            auction_starts_at="bad-date",
            area_ha=float("nan"),
            start_price_kzt=-1,
            sale_price_kzt=2_000_000,
        ),
        generation=1,
    )

    assert row.event_date is None
    assert row.event_date_status == "invalid"
    assert row.area_ha is None
    assert row.area_status == "invalid"
    assert row.start_price_kzt is None
    assert row.start_price_status == "invalid"
    assert row.outcome == "conflict"
    assert row.outcome_status == "conflict"
    assert row.sale_to_start_ratio is None
    assert row.start_price_per_ha_kzt is None
    assert row.sale_price_per_ha_kzt is None


def test_generation_changes_normalization_and_cache_keys_deterministically() -> None:
    record = _raw("same")
    first = normalize_history_record(record, generation=1)
    repeated = normalize_history_record(record, generation=1)
    next_generation = normalize_history_record(record, generation=2)

    assert first == repeated
    assert first.normalization_key == repeated.normalization_key
    assert first.normalization_key != next_generation.normalization_key
    moved = normalize_history_record(replace(record, locality="Другое село"), generation=1)
    assert first.normalization_key != moved.normalization_key
    first_cache = history_cache_key(
        _active_store(_run(1)), "similar", {"district": "жаңасемей"}
    )
    repeated_cache = history_cache_key(
        _active_store(_run(1)), "similar", {"district": "жаңасемей"}
    )
    next_cache = history_cache_key(
        _active_store(_run(2)), "similar", {"district": "жаңасемей"}
    )
    assert first_cache == repeated_cache
    assert first_cache != next_cache


class _FakeStore:
    def __init__(self, records: list[RawAuctionHistoryRecord]) -> None:
        self.records = sorted(records, key=lambda row: row.lot_id)
        self.fetch_calls: list[tuple[str | None, str | None, datetime, int]] = []
        self.rows: dict[tuple[int, str], NormalizedAuctionHistoryRow] = {}
        self.runs: dict[int, HistoryGenerationRun] = {}
        self.active_generation_id: int | None = None

    def create_building_generation(
        self,
        source_cutoff: datetime,
        _normalization_version: str,
    ) -> HistoryGenerationRun | None:
        if any(run.status == "building" for run in self.runs.values()):
            return None
        snapshot = [
            row
            for row in self.records
            if row.source_updated_at is not None and row.source_updated_at <= source_cutoff
        ]
        generation = max(self.runs, default=0) + 1
        run = HistoryGenerationRun(
            generation=generation,
            status="building",
            source_cutoff=source_cutoff,
            source_high_water_lot_id=max((row.lot_id for row in snapshot), default=None),
            expected_count=len(snapshot),
            scan_complete=False,
        )
        self.runs[generation] = run
        return run

    def fetch_snapshot_after(
        self,
        run: HistoryGenerationRun,
        limit: int,
    ) -> list[RawAuctionHistoryRecord]:
        self.fetch_calls.append(
            (
                run.checkpoint.after_lot_id,
                run.source_high_water_lot_id,
                run.source_cutoff,
                limit,
            )
        )
        return [
            row
            for row in self.records
            if (run.checkpoint.after_lot_id is None or row.lot_id > run.checkpoint.after_lot_id)
            and (
                run.source_high_water_lot_id is None
                or row.lot_id <= run.source_high_water_lot_id
            )
            and row.source_updated_at is not None
            and row.source_updated_at <= run.source_cutoff
        ][:limit]

    def commit_batch(
        self,
        run: HistoryGenerationRun,
        rows: list[NormalizedAuctionHistoryRow],
        next_checkpoint: BackfillCheckpoint,
        scan_complete: bool,
    ) -> tuple[HistoryGenerationRun, int]:
        current = self.runs[run.generation]
        if current.status != "building" or current.checkpoint != run.checkpoint:
            return current, 0
        for row in rows:
            self.rows[(row.generation, row.lot_id)] = row
        updated = replace(
            current,
            processed_count=current.processed_count + len(rows),
            checkpoint=next_checkpoint,
            scan_complete=scan_complete,
        )
        self.runs[run.generation] = updated
        return updated, len(rows)

    def fail_generation(
        self,
        run: HistoryGenerationRun,
        detail: str,
    ) -> HistoryGenerationRun:
        failed = replace(run, status="failed", error_count=run.error_count + 1, detail=detail)
        self.runs[run.generation] = failed
        return failed

    def reconcile_and_promote(self, run: HistoryGenerationRun) -> HistoryGenerationRun:
        current = self.runs[run.generation]
        row_count = sum(1 for generation, _lot_id in self.rows if generation == run.generation)
        if (
            current != run
            or not run.scan_complete
            or run.error_count
            or row_count != run.expected_count
            or run.processed_count != run.expected_count
        ):
            return replace(run, status="failed", detail="reconciliation_failed")
        if self.active_generation_id is not None:
            old = self.runs[self.active_generation_id]
            self.runs[old.generation] = replace(old, status="superseded")
        active = replace(run, status="active")
        self.runs[run.generation] = active
        self.active_generation_id = run.generation
        return active

    def get_active_generation(self) -> HistoryGenerationRun | None:
        return (
            self.runs.get(self.active_generation_id)
            if self.active_generation_id is not None
            else None
        )


def _active_store(run: HistoryGenerationRun) -> _FakeStore:
    store = _FakeStore([])
    store.runs[run.generation] = run
    store.active_generation_id = run.generation
    return store


def test_keyset_backfill_is_bounded_checkpointed_and_idempotent() -> None:
    store = _FakeStore([_raw(f"lot-{index:02d}") for index in range(5)])
    run = start_history_generation(store, source_cutoff=UPDATED_AT)
    assert run is not None
    assert start_history_generation(store, source_cutoff=UPDATED_AT) is None

    first = run_history_normalization_batch(store, run=run, batch_size=2)
    second = run_history_normalization_batch(
        store,
        run=first.run,
        batch_size=2,
    )
    third = run_history_normalization_batch(
        store,
        run=second.run,
        batch_size=2,
    )
    repeated = run_history_normalization_batch(store, run=run, batch_size=2)
    active = promote_history_generation(store, third.run)

    assert first.has_more is True
    assert first.run.checkpoint == BackfillCheckpoint("lot-01")
    assert second.run.checkpoint == BackfillCheckpoint("lot-03")
    assert third.has_more is False
    assert third.run.checkpoint == BackfillCheckpoint("lot-04")
    assert third.run.processed_count == third.run.expected_count == 5
    assert third.run.scan_complete is True
    assert [call[0] for call in store.fetch_calls[:3]] == [None, "lot-01", "lot-03"]
    assert all(call[3] == 3 for call in store.fetch_calls[:3])
    assert len(store.rows) == 5
    assert repeated.status == "stale_checkpoint"
    assert repeated.rows == ()
    assert repeated.upserted_count == 0
    assert active.status == "active"
    assert active_history_generation(store) == active


def test_partial_or_changed_snapshot_cannot_be_promoted_or_cached() -> None:
    store = _FakeStore([_raw(f"lot-{index:02d}") for index in range(3)])
    run = start_history_generation(store, source_cutoff=UPDATED_AT)
    assert run is not None
    first = run_history_normalization_batch(store, run=run, batch_size=2)

    with pytest.raises(ValueError, match="incomplete"):
        promote_history_generation(store, first.run)
    with pytest.raises(ValueError, match="active"):
        history_cache_key(store, "similar", {})

    changed_store = _FakeStore([_raw(f"lot-{index:02d}") for index in range(3)])
    changed_run = start_history_generation(changed_store, source_cutoff=UPDATED_AT)
    assert changed_run is not None
    changed_store.records[0] = replace(
        changed_store.records[0],
        source_updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    final_batch = run_history_normalization_batch(
        changed_store,
        run=changed_run,
        batch_size=5,
    )
    assert final_batch.run.scan_complete is True
    assert final_batch.run.processed_count == 2
    assert final_batch.run.expected_count == 3
    with pytest.raises(ValueError, match="reconcile"):
        promote_history_generation(changed_store, final_batch.run)


def test_snapshot_contract_error_marks_generation_failed_with_error_count() -> None:
    class BrokenStore(_FakeStore):
        def fetch_snapshot_after(
            self,
            run: HistoryGenerationRun,
            limit: int,
        ) -> list[RawAuctionHistoryRecord]:
            return [_raw(f"overflow-{index:03d}") for index in range(limit + 1)]

    store = BrokenStore([_raw("one")])
    run = start_history_generation(store, source_cutoff=UPDATED_AT)
    assert run is not None

    result = run_history_normalization_batch(store, run=run, batch_size=2)

    assert result.status == "invalid_store"
    assert result.run.status == "failed"
    assert result.run.error_count == 1
    assert active_history_generation(store) is None


def test_similar_history_uses_strict_dimensions_area_and_medians() -> None:
    target = SimilarHistoryTarget(
        lot_id="target",
        right_kind="lease",
        purpose_group="retail",
        lease_band="medium_3_10",
        area_ha=1.0,
        region_key="область абай",
        district_key="жаңасемей",
        locality_key="новобаженово",
    )
    included = [
        normalize_history_record(
            _raw(
                "sale-1",
                area_ha=0.67,
                start_price_kzt=1_000_000,
                sale_price_kzt=2_000_000,
            ),
            generation=1,
        ),
        normalize_history_record(
            _raw(
                "sale-2",
                area_ha=1.5,
                start_price_kzt=3_000_000,
                sale_price_kzt=6_000_000,
            ),
            generation=1,
        ),
        normalize_history_record(
            _raw(
                "failed",
                status="Аукцион не состоялся",
                sale_price_kzt=None,
                start_price_kzt=5_000_000,
            ),
            generation=1,
        ),
    ]
    excluded = [
        normalize_history_record(_raw("small", area_ha=0.66), generation=1),
        normalize_history_record(_raw("large", area_ha=1.51), generation=1),
        normalize_history_record(
            _raw("wrong-right", land_rights="Частная собственность"), generation=1
        ),
        normalize_history_record(
            _raw("wrong-purpose", purpose="Строительство склада"), generation=1
        ),
        normalize_history_record(
            _raw("wrong-place", locality="Другое село"), generation=1
        ),
    ]

    result = aggregate_similar_normalized_history(
        target,
        included + excluded,
        _active_store(_run(1)),
    )

    assert result.status == "ok"
    assert result.matched_count == 3
    assert result.successful_count == 2
    assert result.failed_count == 1
    assert result.median_start_price_kzt == pytest.approx(3_000_000)
    assert result.median_sale_price_kzt == pytest.approx(4_000_000)
    assert result.median_sale_to_start_ratio == pytest.approx(2)
    assert result.median_start_price_per_ha_kzt == pytest.approx(2_000_000)
    assert result.median_sale_price_per_ha_kzt == pytest.approx(3_492_537.3134)


def test_query_spec_is_indexable_exact_and_aggregate_only() -> None:
    target = SimilarHistoryTarget(
        lot_id="target",
        right_kind="lease",
        purpose_group="retail",
        lease_band="medium_3_10",
        area_ha=1.0,
        region_key="область абай",
        district_key="жаңасемей",
        locality_key="новобаженово",
        event_date_from=date(2025, 8, 17),
        event_date_to=date(2026, 8, 17),
    )

    spec = build_similar_history_query_spec(target, _active_store(_run(9)))

    assert spec.generation == 9
    assert spec.geography_column == "locality_key"
    assert spec.geography_value == "новобаженово"
    assert spec.area_min_ha == pytest.approx(0.67)
    assert spec.area_max_ha == pytest.approx(1.5)
    assert spec.lease_band == "medium_3_10"
    assert ("right_status", "found") in spec.eligibility_statuses
    assert ("purpose_status", "found") in spec.eligibility_statuses
    assert ("lease_status", "found") in spec.eligibility_statuses
    assert ("event_date_status", "found") in spec.eligibility_statuses
    assert spec.sale_metric_predicates == (
        ("outcome", "success"),
        ("outcome_status", "found"),
        ("sale_price_status", "found"),
    )
    assert "median_sale_to_start_ratio" in spec.aggregate_columns


def test_purpose_and_lease_claim_conflicts_never_enter_similar_set() -> None:
    purpose_conflict = normalize_history_record(
        _raw("purpose", purpose_claims=("Строительство склада",)),
        generation=1,
    )
    lease_conflict = normalize_history_record(
        _raw("lease", lease_term_claims=(49,)),
        generation=1,
    )

    assert purpose_conflict.purpose_status == "conflict"
    assert purpose_conflict.purpose_group == "conflict"
    assert lease_conflict.lease_status == "conflict"
    assert lease_conflict.lease_band == "conflict"


def test_aggregate_does_not_mix_generations_or_conflicted_sale_prices() -> None:
    target = SimilarHistoryTarget(
        "target",
        "lease",
        "retail",
        "medium_3_10",
        1.0,
        "область абай",
        "жаңасемей",
        "новобаженово",
    )
    confirmed = normalize_history_record(_raw("confirmed", sale_price_kzt=2_000_000), generation=1)
    conflicted = normalize_history_record(
        _raw("conflicted", status="Аукцион не состоялся", sale_price_kzt=100_000_000),
        generation=1,
    )
    next_generation = normalize_history_record(
        _raw("next", sale_price_kzt=200_000_000),
        generation=2,
    )

    result = aggregate_similar_normalized_history(
        target,
        [confirmed, conflicted, next_generation],
        _active_store(_run(1)),
    )

    assert result.matched_count == 2
    assert result.successful_count == 1
    assert result.conflict_count == 1
    assert result.median_sale_price_kzt == 2_000_000
    assert result.median_sale_price_per_ha_kzt == 2_000_000


def test_batch_size_and_aggregate_row_caps_are_explicit() -> None:
    store = _FakeStore([_raw("one")])
    building = start_history_generation(store, source_cutoff=UPDATED_AT)
    assert building is not None
    invalid_batch = run_history_normalization_batch(store, run=building, batch_size=501)
    row = normalize_history_record(_raw("row"), generation=1)
    target = SimilarHistoryTarget(
        "target",
        "lease",
        "retail",
        "medium_3_10",
        1.0,
        "область абай",
        "жаңасемей",
        "новобаженово",
    )
    oversized = aggregate_similar_normalized_history(
        target,
        [row] * 5_001,
        _active_store(_run(1)),
    )

    assert invalid_batch.status == "invalid_batch_size"
    assert store.fetch_calls == []
    assert oversized.status == "invalid_input"
    assert oversized.detail == "row_limit_exceeded"
