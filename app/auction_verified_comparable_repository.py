"""Short-transaction repository for the global verified comparable inventory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import Select, and_, or_, select, text
from sqlalchemy.orm import Session

from app.auction_verified_comparable_inventory import (
    CONTRACT_VERSION,
    MAX_SCAN_ROWS,
    GeoSelectionPlan,
    GeoSelectionResult,
    InventoryFact,
    build_geo_selection_plan,
    normalize_inventory_fact,
    select_nearby_verified_sales,
    source_idempotency_key,
)
from app.models import AuctionVerifiedComparableCurrent

MAX_RAW_BYTES = 64_000
MAX_GENERATION = 64
ELIGIBLE_CURRENT_SQL = (
    "fact_status = 'found' AND price_kind = 'verified_sale' "
    "AND verification_status = 'verified' AND verification_ref IS NOT NULL "
    "AND conflicts_json = '[]'"
)
POSTGRES_EXPLAIN_PROPOSAL = (
    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
    "SELECT ... FROM auction_verified_comparable_current "
    "WHERE fact_status='found' AND price_kind='verified_sale' "
    "AND verification_status='verified' AND verification_ref IS NOT NULL "
    "AND right_type=:right AND purpose_group=:purpose AND lease_band=:lease_band "
    "AND event_at BETWEEN :event_from AND :event_to AND area_ha BETWEEN :area_min AND :area_max "
    "AND latitude BETWEEN :lat_min AND :lat_max AND longitude BETWEEN :lon_min AND :lon_max "
    "ORDER BY observed_at DESC, observation_id DESC LIMIT 501"
    " -- validate Index/Bitmap Index Scan at >=10000 current rows; no history scan"
)
POSTGRES_INDEX_RATIONALE = (
    "Both partial indexes share equality prefixes (right_type, purpose_group, lease_band). "
    "The event index supports selective freshness/keyset scans; the geo index supports bbox/area "
    "scans. PostgreSQL may choose either or a bitmap plan. Validate at >=10000 current rows."
)


class VerifiedComparableRepositoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ComparableIngestResult:
    source_identity_key: str
    observation_id: int
    inserted: bool
    current_changed: bool
    current_observation_id: int
    content_hash: str


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _strict_json(value: object, *, limit: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise VerifiedComparableRepositoryError(f"{label}_invalid") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise VerifiedComparableRepositoryError(f"{label}_too_large")
    return encoded


def _lease_band(years: float | None) -> str | None:
    if years is None:
        return None
    if years <= 3:
        return "short_3"
    if years <= 10:
        return "medium_10"
    return "long_99"


def _prepare_material(
    fact: InventoryFact,
    *,
    generation_signature: str,
    raw_payload: Mapping[str, object] | None,
) -> tuple[dict[str, object], str]:
    try:
        normalized = normalize_inventory_fact(asdict(fact))
    except ValueError as exc:
        raise VerifiedComparableRepositoryError("invalid_inventory_fact") from exc
    if normalized != fact:
        raise VerifiedComparableRepositoryError("noncanonical_inventory_fact")
    if len(generation_signature) != MAX_GENERATION or any(
        character not in "0123456789abcdef" for character in generation_signature.casefold()
    ):
        raise VerifiedComparableRepositoryError("invalid_generation_signature")
    provenance_json = _strict_json(list(fact.provenance_refs), limit=16_384, label="provenance")
    conflicts_json = _strict_json(list(fact.conflict_fields), limit=8_192, label="conflicts")
    raw_json = (
        _strict_json(raw_payload, limit=MAX_RAW_BYTES, label="raw_payload")
        if raw_payload is not None
        else None
    )
    material: dict[str, object] = {
        "source_sequence_id": fact.sequence_id,
        "source_identity_key": source_idempotency_key(fact),
        "source_name": fact.source_name,
        "source_record_id": fact.source_record_id,
        "source_sale_id": fact.source_sale_id,
        "source_listing_id": fact.source_listing_id,
        "source_url": fact.source_url,
        "object_id": fact.object_id,
        "fact_status": fact.fact_status,
        "price_kind": fact.price_kind,
        "verification_status": fact.verification_status,
        "verification_ref": fact.verification_ref,
        "right_type": fact.right_type,
        "purpose_group": fact.purpose_group,
        "lease_term_years": fact.lease_term_years,
        "lease_band": _lease_band(fact.lease_term_years),
        "area_ha": fact.area_ha,
        "price_kzt": fact.price_kzt,
        "latitude": fact.latitude,
        "longitude": fact.longitude,
        "access_readiness": fact.access_readiness,
        "infrastructure_readiness": fact.infrastructure_readiness,
        "event_at": fact.event_at,
        "observed_at": fact.observed_at,
        "title": fact.title,
        "locality": fact.locality,
        "provenance_json": provenance_json,
        "conflicts_json": conflicts_json,
        "raw_payload_json": raw_json,
        "generation_signature": generation_signature.casefold(),
        "contract_version": CONTRACT_VERSION,
    }
    # Idempotency is the normalized decision fact, not crawl/run audit metadata.
    # observed_at must be a stable upstream fact-version timestamp, never poll time.
    hash_payload = asdict(fact)
    hash_payload.pop("provenance_refs", None)
    hash_payload["observed_at"] = fact.observed_at.isoformat()
    hash_payload["event_at"] = fact.event_at.isoformat() if fact.event_at else None
    canonical = _strict_json(hash_payload, limit=MAX_RAW_BYTES, label="observation_material")
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    material["content_hash"] = content_hash
    return material, content_hash


def _current_values(material: Mapping[str, object], observation_id: int) -> dict[str, object]:
    excluded = {"raw_payload_json", "created_at"}
    return {
        **{key: value for key, value in material.items() if key not in excluded},
        "observation_id": observation_id,
        "updated_at": datetime.now(UTC),
    }


def ingest_verified_comparable(
    session_factory: Callable[[], Session],
    fact: InventoryFact,
    *,
    generation_signature: str,
    raw_payload: Mapping[str, object] | None = None,
) -> ComparableIngestResult:
    """Delegate all current-pointer writes through the generation-aware batch API.

    ``fact.observed_at`` is required to be the provider's stable version/update time.
    Using worker poll time would manufacture a new fact on every crawl.
    """
    # Local import avoids a module cycle while leaving no callable pointer-write bypass.
    from app.auction_market_dirty_store import (
        ComparableBatchItem,
        ingest_verified_comparable_batch,
    )

    batch = ingest_verified_comparable_batch(
        session_factory,
        [ComparableBatchItem(fact, generation_signature, raw_payload)],
        completed_at=datetime.now(UTC),
    )
    return batch.results[0]


def _lease_band_name(plan: GeoSelectionPlan) -> str | None:
    if plan.lease_band is None:
        return None
    if plan.lease_band[1] <= 3:
        return "short_3"
    if plan.lease_band[1] <= 10:
        return "medium_10"
    return "long_99"


def build_current_selection_statement(
    plan: GeoSelectionPlan,
    *,
    cursor: tuple[datetime, int] | None = None,
    exclude_object_id: str | None = None,
    exclude_source_sale_ids: tuple[str, ...] = (),
) -> Select:
    """Build the one-current-row indexed SQL query suitable for PostgreSQL EXPLAIN."""
    model = AuctionVerifiedComparableCurrent
    predicates = [
        text(ELIGIBLE_CURRENT_SQL),
        model.right_type == plan.right_type,
        model.purpose_group == plan.purpose_group,
        model.event_at >= plan.event_from,
        model.event_at <= plan.event_to,
        model.area_ha >= Decimal(str(plan.area_min_ha)),
        model.area_ha <= Decimal(str(plan.area_max_ha)),
        model.latitude >= Decimal(str(plan.bbox.latitude_min)),
        model.latitude <= Decimal(str(plan.bbox.latitude_max)),
        model.longitude >= Decimal(str(plan.bbox.longitude_min)),
        model.longitude <= Decimal(str(plan.bbox.longitude_max)),
    ]
    lease_band = _lease_band_name(plan)
    if lease_band is not None:
        predicates.append(model.lease_band == lease_band)
    if exclude_object_id is not None:
        predicates.append(or_(model.object_id.is_(None), model.object_id != exclude_object_id))
    if exclude_source_sale_ids:
        predicates.append(model.source_sale_id.not_in(exclude_source_sale_ids))
    if cursor is not None:
        cursor_at, cursor_id = cursor
        predicates.append(
            or_(
                model.observed_at < cursor_at,
                and_(model.observed_at == cursor_at, model.observation_id < cursor_id),
            )
        )
    projection = (
        model.observation_id,
        model.source_sequence_id,
        model.source_name,
        model.source_record_id,
        model.source_sale_id,
        model.source_listing_id,
        model.source_url,
        model.object_id,
        model.fact_status,
        model.price_kind,
        model.verification_status,
        model.verification_ref,
        model.right_type,
        model.purpose_group,
        model.lease_term_years,
        model.area_ha,
        model.price_kzt,
        model.latitude,
        model.longitude,
        model.access_readiness,
        model.infrastructure_readiness,
        model.event_at,
        model.observed_at,
        model.title,
        model.locality,
    )
    return (
        select(*projection)
        .where(*predicates)
        .order_by(model.observed_at.desc(), model.observation_id.desc())
        .limit(MAX_SCAN_ROWS + 1)
    )


def _fact_from_current(row: object) -> InventoryFact:
    return InventoryFact(
        sequence_id=int(row.source_sequence_id),
        source_name=row.source_name,
        source_record_id=row.source_record_id,
        source_sale_id=row.source_sale_id,
        source_listing_id=row.source_listing_id,
        source_url=row.source_url,
        object_id=row.object_id,
        fact_status=row.fact_status,  # type: ignore[arg-type]
        price_kind=row.price_kind,  # type: ignore[arg-type]
        verification_status=row.verification_status,
        verification_ref=row.verification_ref,
        right_type=row.right_type,  # type: ignore[arg-type]
        purpose_group=row.purpose_group,
        lease_term_years=float(row.lease_term_years) if row.lease_term_years else None,
        area_ha=float(row.area_ha) if row.area_ha else None,
        price_kzt=int(row.price_kzt) if row.price_kzt else None,
        latitude=float(row.latitude) if row.latitude else None,
        longitude=float(row.longitude) if row.longitude else None,
        access_readiness=row.access_readiness,  # type: ignore[arg-type]
        infrastructure_readiness=row.infrastructure_readiness,  # type: ignore[arg-type]
        event_at=_aware(row.event_at) if row.event_at else None,
        observed_at=_aware(row.observed_at),
        title=row.title,
        locality=row.locality,
        provenance_refs=tuple(
            value
            for value in (
                f"verified_comparable_observation:{row.observation_id}",
                row.verification_ref,
            )
            if value
        ),
        conflict_fields=(),
    )


def query_verified_comparables(
    session_factory: Callable[[], Session],
    *,
    latitude: float,
    longitude: float,
    right_type: str,
    purpose_group: str,
    area_ha: float,
    valuation_at: datetime,
    lease_term_years: float | None = None,
    radius_km: float = 5.0,
    max_age_days: int = 365,
    result_limit: int = 100,
    cursor: tuple[datetime, int] | None = None,
    exclude_object_id: str | None = None,
    exclude_source_sale_ids: tuple[str, ...] = (),
) -> GeoSelectionResult:
    """Read one bounded SQL page, close DB, then run exact Haversine in memory."""
    plan = build_geo_selection_plan(
        latitude,
        longitude,
        right_type=right_type,  # type: ignore[arg-type]
        purpose_group=purpose_group,
        area_ha=area_ha,
        valuation_at=valuation_at,
        lease_term_years=lease_term_years,
        radius_km=radius_km,
        max_age_days=max_age_days,
    )
    if cursor is not None and (
        not isinstance(cursor, tuple)
        or len(cursor) != 2
        or not isinstance(cursor[0], datetime)
        or cursor[0].utcoffset() is None
        or isinstance(cursor[1], bool)
        or not isinstance(cursor[1], int)
        or cursor[1] <= 0
    ):
        raise VerifiedComparableRepositoryError("invalid_cursor")
    if exclude_object_id is not None and (
        not isinstance(exclude_object_id, str) or not 0 < len(exclude_object_id) <= 128
    ):
        raise VerifiedComparableRepositoryError("invalid_object_exclusion")
    if (
        not isinstance(exclude_source_sale_ids, tuple)
        or len(exclude_source_sale_ids) > 32
        or any(
            not isinstance(value, str) or not 0 < len(value) <= 128
            for value in exclude_source_sale_ids
        )
    ):
        raise VerifiedComparableRepositoryError("invalid_sale_exclusions")
    statement = build_current_selection_statement(
        plan,
        cursor=cursor,
        exclude_object_id=exclude_object_id,
        exclude_source_sale_ids=exclude_source_sale_ids,
    )
    with session_factory() as session:
        rows = list(session.execute(statement))
        detached = [_fact_from_current(row) for row in rows[:MAX_SCAN_ROWS]]
        next_cursor = (
            (_aware(rows[MAX_SCAN_ROWS - 1].observed_at), rows[MAX_SCAN_ROWS - 1].observation_id)
            if len(rows) > MAX_SCAN_ROWS
            else None
        )
    result = select_nearby_verified_sales(
        latitude,
        longitude,
        detached,
        right_type=plan.right_type,
        purpose_group=plan.purpose_group,
        area_ha=area_ha,
        valuation_at=valuation_at,
        lease_term_years=lease_term_years,
        radius_km=radius_km,
        max_age_days=max_age_days,
        result_limit=result_limit,
    )
    return replace(
        result,
        next_cursor=(next_cursor[0].isoformat(), next_cursor[1]) if next_cursor else None,
    )
