from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median
from typing import Literal, Protocol

from app.auction_taxonomy import classify_scenario

NORMALIZATION_VERSION = "auction-history-normalized.v1"
MAX_BATCH_SIZE = 500
MAX_AGGREGATE_ROWS = 5_000
MAX_DIMENSION_CLAIMS = 16

DimensionStatus = Literal["found", "unknown", "conflict", "invalid", "not_applicable"]
GenerationStatus = Literal["building", "active", "failed", "superseded"]

_FAILURE_MARKERS = (
    "failureprotocolsigned",
    "не состоялся",
    "не состоялись",
    "өтпеді",
    "отменен",
    "отменён",
)
_SUCCESS_MARKERS = ("successprotocolsigned", "состоялся", "состоялись", "өтті")
_LEASE_MARKERS = ("аренд", "землепольз", "временн")
_OWNERSHIP_MARKERS = ("собствен", "продажа земельного участка")
@dataclass(frozen=True, slots=True)
class RawAuctionHistoryRecord:
    lot_id: str
    source_updated_at: datetime | None
    status: str | None = None
    source_search_status: str | None = None
    land_rights: str | None = None
    right_claims: tuple[str, ...] = ()
    purpose: str | None = None
    title: str | None = None
    use_goal: str | None = None
    functional_purpose: str | None = None
    purpose_claims: tuple[str, ...] = ()
    lease_term_years: float | None = None
    lease_term_claims: tuple[float, ...] = ()
    auction_starts_at: datetime | None = None
    published_at: date | None = None
    area_ha: float | None = None
    start_price_kzt: float | None = None
    sale_price_kzt: float | None = None
    region: str | None = None
    district: str | None = None
    locality: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedAuctionHistoryRow:
    lot_id: str
    generation: int
    normalization_key: str
    right_kind: str
    right_status: DimensionStatus
    purpose_group: str
    purpose_status: DimensionStatus
    lease_band: str
    lease_status: DimensionStatus
    event_date: date | None
    event_date_status: DimensionStatus
    outcome: str
    outcome_status: DimensionStatus
    area_ha: float | None
    area_status: DimensionStatus
    start_price_kzt: float | None
    start_price_status: DimensionStatus
    sale_price_kzt: float | None
    sale_price_status: DimensionStatus
    sale_to_start_ratio: float | None
    start_price_per_ha_kzt: float | None
    sale_price_per_ha_kzt: float | None
    region_key: str | None
    district_key: str | None
    locality_key: str | None
    source_updated_at: datetime | None
    issues: tuple[str, ...]
    normalization_version: str = NORMALIZATION_VERSION


@dataclass(frozen=True, slots=True)
class BackfillCheckpoint:
    after_lot_id: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryGenerationRun:
    generation: int
    status: GenerationStatus
    source_cutoff: datetime
    source_high_water_lot_id: str | None
    expected_count: int
    processed_count: int = 0
    error_count: int = 0
    checkpoint: BackfillCheckpoint = BackfillCheckpoint()
    scan_complete: bool = False
    detail: str | None = None
    normalization_version: str = NORMALIZATION_VERSION


@dataclass(frozen=True, slots=True)
class BackfillBatchResult:
    status: str
    run: HistoryGenerationRun
    rows: tuple[NormalizedAuctionHistoryRow, ...]
    has_more: bool
    fetched_count: int
    upserted_count: int
    detail: str | None = None


class HistoryNormalizationStore(Protocol):
    def create_building_generation(
        self,
        source_cutoff: datetime,
        normalization_version: str,
    ) -> HistoryGenerationRun | None:
        """Atomically acquire the single-building-run lock and snapshot bounds."""
        ...

    def fetch_snapshot_after(
        self,
        run: HistoryGenerationRun,
        limit: int,
    ) -> Sequence[RawAuctionHistoryRecord]: ...

    def commit_batch(
        self,
        run: HistoryGenerationRun,
        rows: Sequence[NormalizedAuctionHistoryRow],
        next_checkpoint: BackfillCheckpoint,
        scan_complete: bool,
    ) -> tuple[HistoryGenerationRun, int]:
        """Atomically upsert rows and advance the optimistic checkpoint once."""
        ...

    def fail_generation(
        self,
        run: HistoryGenerationRun,
        detail: str,
    ) -> HistoryGenerationRun: ...

    def reconcile_and_promote(
        self,
        run: HistoryGenerationRun,
    ) -> HistoryGenerationRun:
        """In one transaction verify counts, switch active pointer, supersede old run."""
        ...

    def get_active_generation(self) -> HistoryGenerationRun | None: ...


@dataclass(frozen=True, slots=True)
class SimilarHistoryTarget:
    lot_id: str
    right_kind: str
    purpose_group: str
    lease_band: str
    area_ha: float
    region_key: str | None
    district_key: str | None
    locality_key: str | None
    event_date_from: date | None = None
    event_date_to: date | None = None


@dataclass(frozen=True, slots=True)
class SimilarHistoryQuerySpec:
    generation: int
    exclude_lot_id: str
    right_kind: str
    purpose_group: str
    lease_band: str | None
    geography_column: str
    geography_value: str
    area_min_ha: float
    area_max_ha: float
    event_date_from: date | None
    event_date_to: date | None
    eligibility_statuses: tuple[tuple[str, str], ...]
    sale_metric_predicates: tuple[tuple[str, str], ...] = (
        ("outcome", "success"),
        ("outcome_status", "found"),
        ("sale_price_status", "found"),
    )
    aggregate_columns: tuple[str, ...] = (
        "outcome_counts",
        "median_start_price_kzt",
        "median_sale_price_kzt",
        "median_sale_to_start_ratio",
        "median_start_price_per_ha_kzt",
        "median_sale_price_per_ha_kzt",
    )


@dataclass(frozen=True, slots=True)
class SimilarHistoryAggregate:
    status: str
    matched_count: int
    successful_count: int
    failed_count: int
    unresolved_count: int
    conflict_count: int
    median_start_price_kzt: float | None
    median_sale_price_kzt: float | None
    median_sale_to_start_ratio: float | None
    median_start_price_per_ha_kzt: float | None
    median_sale_price_per_ha_kzt: float | None
    detail: str | None = None


def _clean_text(value: object, limit: int = 320) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip().casefold()
    return cleaned or None


def _clean_identifier(value: object, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    cleaned = value.strip()
    return cleaned or None


def _number(value: object, *, low: float, high: float) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) and low <= numeric <= high else None


def _right_claim(value: str | None) -> str | None:
    text = _clean_text(value, 320)
    if not text:
        return None
    lease = any(marker in text for marker in _LEASE_MARKERS)
    ownership = any(marker in text for marker in _OWNERSHIP_MARKERS)
    if lease == ownership:
        return "conflict" if lease else None
    return "lease" if lease else "ownership"


def _normalize_right(record: RawAuctionHistoryRecord) -> tuple[str, DimensionStatus]:
    if len(record.right_claims) > MAX_DIMENSION_CLAIMS:
        return "unknown", "invalid"
    claims = {
        claim
        for claim in (_right_claim(value) for value in (record.land_rights, *record.right_claims))
        if claim
    }
    if "conflict" in claims or len(claims) > 1:
        return "conflict", "conflict"
    if not claims:
        return "unknown", "unknown"
    return next(iter(claims)), "found"


def _purpose_claims(value: str | None) -> set[str]:
    text = _clean_text(value, 500)
    if not text:
        return set()
    scenario = classify_scenario(text)
    return set() if scenario == "unknown" else {scenario}


def _normalize_purpose(record: RawAuctionHistoryRecord) -> tuple[str, DimensionStatus]:
    if len(record.purpose_claims) > MAX_DIMENSION_CLAIMS:
        return "unknown", "invalid"
    values = (
        record.purpose,
        record.title,
        record.use_goal,
        record.functional_purpose,
        *record.purpose_claims,
    )
    groups: set[str] = set()
    for value in values:
        groups.update(_purpose_claims(value))
    if len(groups) > 1:
        return "conflict", "conflict"
    if not groups:
        return "unknown", "unknown"
    return next(iter(groups)), "found"


def _lease_band(value: float) -> str:
    if value <= 3:
        return "short_0_3"
    if value <= 10:
        return "medium_3_10"
    return "long_10_plus"


def _normalize_lease(
    record: RawAuctionHistoryRecord,
    right_kind: str,
) -> tuple[str, DimensionStatus]:
    if right_kind == "ownership":
        return "not_applicable", "not_applicable"
    if right_kind == "conflict":
        return "conflict", "conflict"
    if right_kind != "lease":
        return "unknown", "unknown"
    if len(record.lease_term_claims) > MAX_DIMENSION_CLAIMS:
        return "unknown", "invalid"
    raw_values = (record.lease_term_years, *record.lease_term_claims)
    if any(
        value is not None and _number(value, low=0.01, high=99) is None
        for value in raw_values
    ):
        return "unknown", "invalid"
    values = {
        numeric
        for numeric in (
            _number(value, low=0.01, high=99)
            for value in raw_values
        )
        if numeric is not None
    }
    bands = {_lease_band(value) for value in values}
    if len(bands) > 1:
        return "conflict", "conflict"
    if not bands:
        return "unknown", "unknown"
    return next(iter(bands)), "found"


def _normalize_event_date(record: RawAuctionHistoryRecord) -> tuple[date | None, DimensionStatus]:
    if record.auction_starts_at is not None:
        if not isinstance(record.auction_starts_at, datetime):
            return None, "invalid"
        return record.auction_starts_at.date(), "found"
    if record.published_at is not None:
        if isinstance(record.published_at, datetime):
            return record.published_at.date(), "found"
        if not isinstance(record.published_at, date):
            return None, "invalid"
        return record.published_at, "found"
    return None, "unknown"


def _status_markers(record: RawAuctionHistoryRecord) -> tuple[bool, bool]:
    values = []
    for value in (record.status, record.source_search_status):
        cleaned = _clean_text(value, 320)
        if cleaned:
            values.append(cleaned)
    text = " ".join(values)
    failure = any(marker in text for marker in _FAILURE_MARKERS)
    success_text = text
    for marker in _FAILURE_MARKERS:
        success_text = success_text.replace(marker, " ")
    success = any(marker in success_text for marker in _SUCCESS_MARKERS)
    return failure, success


def _price(value: object) -> tuple[float | None, DimensionStatus]:
    if value is None:
        return None, "unknown"
    numeric = _number(value, low=1, high=1_000_000_000_000_000)
    return (numeric, "found") if numeric is not None else (None, "invalid")


def _fingerprint_payload(row: dict[str, object]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_history_record(
    record: RawAuctionHistoryRecord,
    *,
    generation: int,
) -> NormalizedAuctionHistoryRow:
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("generation must be a positive integer")
    lot_id = _clean_identifier(record.lot_id, 128)
    if lot_id is None:
        raise ValueError("lot_id is missing or unbounded")
    issues: list[str] = []
    right_kind, right_status = _normalize_right(record)
    purpose_group, purpose_status = _normalize_purpose(record)
    lease_band, lease_status = _normalize_lease(record, right_kind)
    event_date, event_status = _normalize_event_date(record)
    area = _number(record.area_ha, low=0.0001, high=1_000_000)
    area_status: DimensionStatus = (
        "unknown" if record.area_ha is None else ("found" if area is not None else "invalid")
    )
    start_price, start_status = _price(record.start_price_kzt)
    sale_price, sale_status = _price(record.sale_price_kzt)
    failure_marker, success_marker = _status_markers(record)
    if sale_price is not None and failure_marker:
        outcome, outcome_status = "conflict", "conflict"
    elif failure_marker and success_marker:
        outcome, outcome_status = "conflict", "conflict"
    elif sale_price is not None or success_marker:
        outcome, outcome_status = "success", "found"
    elif failure_marker:
        outcome, outcome_status = "failure", "found"
    else:
        outcome, outcome_status = "unresolved", "unknown"
    ratio = None
    if outcome == "success" and start_price is not None and sale_price is not None:
        candidate_ratio = sale_price / start_price
        if math.isfinite(candidate_ratio) and 0 < candidate_ratio <= 10_000:
            ratio = candidate_ratio
        else:
            issues.append("invalid_sale_to_start_ratio")
    start_per_area = start_price / area if start_price is not None and area is not None else None
    sale_per_area = sale_price / area if sale_price is not None and area is not None else None
    source_updated_at = (
        record.source_updated_at
        if isinstance(record.source_updated_at, datetime)
        and record.source_updated_at.utcoffset() is not None
        else None
    )
    if record.source_updated_at is not None and source_updated_at is None:
        issues.append("invalid_source_updated_at")
    for name, status in (
        ("right", right_status),
        ("purpose", purpose_status),
        ("lease", lease_status),
        ("event_date", event_status),
        ("area", area_status),
        ("start_price", start_status),
        ("sale_price", sale_status),
        ("outcome", outcome_status),
    ):
        if status in {"conflict", "invalid"}:
            issues.append(f"{name}_{status}")
    region_key = _clean_text(record.region, 160)
    district_key = _clean_text(record.district, 160)
    locality_key = _clean_text(record.locality, 160)
    keys = {
        "lot_id": lot_id,
        "generation": generation,
        "version": NORMALIZATION_VERSION,
        "source_updated_at": source_updated_at.isoformat() if source_updated_at else None,
        "right_kind": right_kind,
        "right_status": right_status,
        "purpose_group": purpose_group,
        "purpose_status": purpose_status,
        "lease_band": lease_band,
        "lease_status": lease_status,
        "event_date": event_date.isoformat() if event_date else None,
        "event_date_status": event_status,
        "outcome": outcome,
        "outcome_status": outcome_status,
        "area": area,
        "area_status": area_status,
        "start": start_price,
        "start_status": start_status,
        "sale": sale_price,
        "sale_status": sale_status,
        "region_key": region_key,
        "district_key": district_key,
        "locality_key": locality_key,
        "issues": tuple(sorted(set(issues))),
    }
    return NormalizedAuctionHistoryRow(
        lot_id=lot_id,
        generation=generation,
        normalization_key=_fingerprint_payload(keys),
        right_kind=right_kind,
        right_status=right_status,
        purpose_group=purpose_group,
        purpose_status=purpose_status,
        lease_band=lease_band,
        lease_status=lease_status,
        event_date=event_date,
        event_date_status=event_status,
        outcome=outcome,
        outcome_status=outcome_status,
        area_ha=area,
        area_status=area_status,
        start_price_kzt=start_price,
        start_price_status=start_status,
        sale_price_kzt=sale_price,
        sale_price_status=sale_status,
        sale_to_start_ratio=ratio,
        start_price_per_ha_kzt=start_per_area,
        sale_price_per_ha_kzt=sale_per_area,
        region_key=region_key,
        district_key=district_key,
        locality_key=locality_key,
        source_updated_at=source_updated_at,
        issues=tuple(sorted(set(issues))),
    )


def start_history_generation(
    store: HistoryNormalizationStore,
    *,
    source_cutoff: datetime,
) -> HistoryGenerationRun | None:
    """Start one snapshot run; the store rejects a concurrent building run."""
    if not isinstance(source_cutoff, datetime) or source_cutoff.utcoffset() is None:
        raise ValueError("source_cutoff must be timezone-aware")
    run = store.create_building_generation(source_cutoff, NORMALIZATION_VERSION)
    if run is not None:
        _validate_generation_run(run, expected_status="building")
        if run.source_cutoff != source_cutoff:
            raise ValueError("store changed the source cutoff")
    return run


def _validate_generation_run(
    run: HistoryGenerationRun,
    *,
    expected_status: GenerationStatus | None = None,
) -> None:
    if (
        not isinstance(run.generation, int)
        or isinstance(run.generation, bool)
        or run.generation < 1
    ):
        raise ValueError("invalid generation id")
    if not isinstance(run.source_cutoff, datetime) or run.source_cutoff.utcoffset() is None:
        raise ValueError("invalid generation cutoff")
    if run.normalization_version != NORMALIZATION_VERSION:
        raise ValueError("normalization version mismatch")
    if expected_status is not None and run.status != expected_status:
        raise ValueError(f"generation status must be {expected_status}")
    counts = (run.expected_count, run.processed_count, run.error_count)
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts):
        raise ValueError("invalid generation counts")
    if run.processed_count > run.expected_count:
        raise ValueError("processed count exceeds snapshot")
    if run.expected_count and _clean_identifier(run.source_high_water_lot_id, 128) is None:
        raise ValueError("non-empty snapshot requires a high-water id")
    if (
        run.checkpoint.after_lot_id is not None
        and run.source_high_water_lot_id is not None
        and run.checkpoint.after_lot_id > run.source_high_water_lot_id
    ):
        raise ValueError("checkpoint exceeds snapshot high-water")


def run_history_normalization_batch(
    store: HistoryNormalizationStore,
    *,
    run: HistoryGenerationRun,
    batch_size: int = 200,
) -> BackfillBatchResult:
    try:
        _validate_generation_run(run, expected_status="building")
    except ValueError as exc:
        return BackfillBatchResult("invalid_run", run, (), False, 0, 0, str(exc))
    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= MAX_BATCH_SIZE
    ):
        return BackfillBatchResult(
            "invalid_batch_size", run, (), False, 0, 0, "batch_size_out_of_bounds"
        )
    if run.status != "building":
        return BackfillBatchResult(
            "invalid_run", run, (), False, 0, 0, "generation_not_building"
        )
    if run.scan_complete:
        return BackfillBatchResult("complete", run, (), False, 0, 0)
    try:
        fetched = list(store.fetch_snapshot_after(run, batch_size + 1))
    except Exception as exc:
        detail = f"snapshot_fetch_failed:{exc.__class__.__name__}"
        failed = store.fail_generation(run, detail)
        return BackfillBatchResult("failed", failed, (), False, 0, 0, detail)
    if len(fetched) > batch_size + 1:
        failed = store.fail_generation(run, "store_ignored_limit")
        return BackfillBatchResult(
            "invalid_store", failed, (), False, len(fetched), 0, "store_ignored_limit"
        )
    ids = [record.lot_id for record in fetched]
    if any(_clean_identifier(lot_id, 128) != lot_id for lot_id in ids):
        failed = store.fail_generation(run, "invalid_lot_id")
        return BackfillBatchResult(
            "invalid_store", failed, (), False, len(fetched), 0, "invalid_lot_id"
        )
    if ids != sorted(ids) or any(
        run.checkpoint.after_lot_id is not None and lot_id <= run.checkpoint.after_lot_id
        for lot_id in ids
    ) or any(
        run.source_high_water_lot_id is not None and lot_id > run.source_high_water_lot_id
        for lot_id in ids
    ):
        failed = store.fail_generation(run, "snapshot_bounds_violated")
        return BackfillBatchResult(
            "invalid_store", failed, (), False, len(fetched), 0, "snapshot_bounds_violated"
        )
    selected = fetched[:batch_size]
    try:
        rows = tuple(
            normalize_history_record(record, generation=run.generation) for record in selected
        )
    except ValueError as exc:
        failed = store.fail_generation(run, str(exc))
        return BackfillBatchResult(
            "invalid_record", failed, (), False, len(fetched), 0, str(exc)
        )
    next_checkpoint = BackfillCheckpoint(
        rows[-1].lot_id if rows else run.checkpoint.after_lot_id
    )
    has_more = len(fetched) > batch_size
    try:
        updated_run, upserted = store.commit_batch(
            run,
            rows,
            next_checkpoint,
            not has_more,
        )
    except Exception as exc:
        detail = f"batch_commit_failed:{exc.__class__.__name__}"
        failed = store.fail_generation(run, detail)
        return BackfillBatchResult("failed", failed, (), False, len(fetched), 0, detail)
    try:
        _validate_generation_run(updated_run, expected_status="building")
    except ValueError as exc:
        failed = store.fail_generation(run, str(exc))
        return BackfillBatchResult(
            "invalid_store", failed, (), False, len(fetched), 0, str(exc)
        )
    if updated_run.checkpoint != next_checkpoint:
        return BackfillBatchResult(
            "stale_checkpoint",
            updated_run,
            (),
            not updated_run.scan_complete,
            len(fetched),
            0,
            "batch was already committed or superseded",
        )
    if (
        not isinstance(upserted, int)
        or isinstance(upserted, bool)
        or not 0 <= upserted <= len(rows)
    ):
        failed = store.fail_generation(updated_run, "invalid_upsert_count")
        return BackfillBatchResult(
            "invalid_store", failed, (), False, len(fetched), 0, "invalid_upsert_count"
        )
    return BackfillBatchResult(
        "ok",
        updated_run,
        rows,
        has_more,
        len(fetched),
        int(upserted),
    )


def promote_history_generation(
    store: HistoryNormalizationStore,
    run: HistoryGenerationRun,
) -> HistoryGenerationRun:
    """Promote only a reconciled complete snapshot through one store transaction."""
    if run.status != "building":
        raise ValueError("only a building generation can be promoted")
    if not run.scan_complete:
        raise ValueError("generation scan is incomplete")
    if run.error_count or run.processed_count != run.expected_count:
        raise ValueError("generation counts do not reconcile")
    promoted = store.reconcile_and_promote(run)
    _validate_generation_run(promoted, expected_status="active")
    if promoted.status != "active":
        raise ValueError("store did not atomically activate generation")
    return promoted


def active_history_generation(store: HistoryNormalizationStore) -> HistoryGenerationRun | None:
    run = store.get_active_generation()
    if run is None or run.status != "active":
        return None
    _validate_generation_run(run, expected_status="active")
    return run


def history_cache_key(
    store: HistoryNormalizationStore,
    namespace: str,
    filters: dict[str, object],
) -> str:
    run = active_history_generation(store)
    if run is None:
        raise ValueError("cache keys require the active generation")
    safe_namespace = _clean_text(namespace, 80)
    if safe_namespace is None:
        raise ValueError("invalid namespace")
    payload = json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(payload) > 4_000:
        raise ValueError("filters exceed cache-key budget")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"auction-history:{run.generation}:{safe_namespace}:{digest}"


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def build_similar_history_query_spec(
    target: SimilarHistoryTarget,
    store: HistoryNormalizationStore,
    *,
    area_ratio_min: float = 0.67,
    area_ratio_max: float = 1.50,
) -> SimilarHistoryQuerySpec:
    run = active_history_generation(store)
    if run is None:
        raise ValueError("queries require the active generation")
    target_area = _number(target.area_ha, low=0.0001, high=1_000_000)
    low_ratio = _number(area_ratio_min, low=0.05, high=1.0)
    high_ratio = _number(area_ratio_max, low=1.0, high=3.0)
    if target_area is None or low_ratio is None or high_ratio is None or low_ratio > high_ratio:
        raise ValueError("invalid area dimensions")
    if target.right_kind not in {"ownership", "lease"}:
        raise ValueError("invalid right kind")
    if target.purpose_group in {"", "unknown", "conflict"}:
        raise ValueError("invalid purpose group")
    if target.right_kind == "lease" and target.lease_band in {"", "unknown", "conflict"}:
        raise ValueError("invalid lease band")
    geography_column = (
        "locality_key"
        if target.locality_key
        else ("district_key" if target.district_key else "region_key")
    )
    geography_value = getattr(target, geography_column)
    if not geography_value:
        raise ValueError("missing geography")
    if target.event_date_from and target.event_date_to:
        if target.event_date_from > target.event_date_to:
            raise ValueError("invalid event date window")
    return SimilarHistoryQuerySpec(
        generation=run.generation,
        exclude_lot_id=target.lot_id,
        right_kind=target.right_kind,
        purpose_group=target.purpose_group,
        lease_band=target.lease_band if target.right_kind == "lease" else None,
        geography_column=geography_column,
        geography_value=geography_value,
        area_min_ha=target_area * low_ratio,
        area_max_ha=target_area * high_ratio,
        event_date_from=target.event_date_from,
        event_date_to=target.event_date_to,
        eligibility_statuses=(
            ("right_status", "found"),
            ("purpose_status", "found"),
            ("area_status", "found"),
            *(
                (("lease_status", "found"),)
                if target.right_kind == "lease"
                else ()
            ),
            *(
                (("event_date_status", "found"),)
                if target.event_date_from or target.event_date_to
                else ()
            ),
        ),
    )


def aggregate_similar_normalized_history(
    target: SimilarHistoryTarget,
    rows: Sequence[NormalizedAuctionHistoryRow],
    store: HistoryNormalizationStore,
    *,
    area_ratio_min: float = 0.67,
    area_ratio_max: float = 1.50,
) -> SimilarHistoryAggregate:
    """Bounded reference evaluator; production uses the aggregate-only query spec."""
    if len(rows) > MAX_AGGREGATE_ROWS:
        return SimilarHistoryAggregate(
            "invalid_input", 0, 0, 0, 0, 0, None, None, None, None, None, "row_limit_exceeded"
        )
    run = active_history_generation(store)
    if run is None:
        return SimilarHistoryAggregate(
            "invalid_input", 0, 0, 0, 0, 0, None, None, None, None, None, "inactive_generation"
        )
    target_area = _number(target.area_ha, low=0.0001, high=1_000_000)
    if (
        target_area is None
        or target.right_kind not in {"ownership", "lease"}
        or target.purpose_group in {"unknown", "conflict", ""}
        or (target.right_kind == "lease" and target.lease_band in {"unknown", "conflict"})
        or not _number(area_ratio_min, low=0.05, high=1.0)
        or not _number(area_ratio_max, low=1.0, high=3.0)
        or area_ratio_min > area_ratio_max
    ):
        return SimilarHistoryAggregate(
            "invalid_target", 0, 0, 0, 0, 0, None, None, None, None, None, "invalid_dimensions"
        )
    geography_field = (
        "locality_key"
        if target.locality_key
        else ("district_key" if target.district_key else "region_key")
    )
    geography_value = getattr(target, geography_field)
    if not geography_value:
        return SimilarHistoryAggregate(
            "invalid_target", 0, 0, 0, 0, 0, None, None, None, None, None, "missing_geography"
        )
    matched = []
    for row in rows:
        if row.generation != run.generation:
            continue
        if row.lot_id == target.lot_id:
            continue
        if row.right_status != "found" or row.right_kind != target.right_kind:
            continue
        if row.purpose_status != "found" or row.purpose_group != target.purpose_group:
            continue
        if target.right_kind == "lease" and (
            row.lease_status != "found" or row.lease_band != target.lease_band
        ):
            continue
        if getattr(row, geography_field) != geography_value:
            continue
        if row.area_status != "found" or row.area_ha is None:
            continue
        if target.event_date_from or target.event_date_to:
            if row.event_date_status != "found" or row.event_date is None:
                continue
            if target.event_date_from and row.event_date < target.event_date_from:
                continue
            if target.event_date_to and row.event_date > target.event_date_to:
                continue
        ratio = row.area_ha / target_area
        if not area_ratio_min <= ratio <= area_ratio_max:
            continue
        matched.append(row)
    outcomes = [row.outcome for row in matched]
    starts = [row.start_price_kzt for row in matched if row.start_price_status == "found"]
    sales = [
        row.sale_price_kzt
        for row in matched
        if row.outcome == "success"
        and row.outcome_status == "found"
        and row.sale_price_status == "found"
    ]
    ratios = [row.sale_to_start_ratio for row in matched if row.sale_to_start_ratio is not None]
    starts_per_area = [
        row.start_price_per_ha_kzt
        for row in matched
        if row.start_price_per_ha_kzt is not None
    ]
    sales_per_area = [
        row.sale_price_per_ha_kzt
        for row in matched
        if row.outcome == "success"
        and row.outcome_status == "found"
        and row.sale_price_per_ha_kzt is not None
    ]
    return SimilarHistoryAggregate(
        status="ok",
        matched_count=len(matched),
        successful_count=outcomes.count("success"),
        failed_count=outcomes.count("failure"),
        unresolved_count=outcomes.count("unresolved"),
        conflict_count=outcomes.count("conflict"),
        median_start_price_kzt=_median(starts),
        median_sale_price_kzt=_median(sales),
        median_sale_to_start_ratio=_median(ratios),
        median_start_price_per_ha_kzt=_median(starts_per_area),
        median_sale_price_per_ha_kzt=_median(sales_per_area),
    )
