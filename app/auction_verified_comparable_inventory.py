"""Pure contract for a future global verified-market comparable inventory.

The module deliberately contains no provider client, database model, or write path.
It defines the normalized source-fact boundary and the deterministic part of an
indexed geo query: a Kazakhstan-safe bounding box followed by an exact Haversine
filter.  A future repository adapter must apply the returned bbox and descending
keyset in SQL before passing at most ``MAX_SCAN_ROWS`` facts here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.auction_market_comparables import ComparableCandidate

CONTRACT_VERSION = "verified-comparable-inventory/2026.2-same-year"
KZ_LAT_MIN = 40.0
KZ_LAT_MAX = 56.0
KZ_LON_MIN = 46.0
KZ_LON_MAX = 88.0
MAX_RADIUS_KM = 5.0
MAX_SCAN_ROWS = 500
MAX_INPUT_ROWS = 5_000
MAX_RESULT_ROWS = 200
MAX_PROVENANCE_REFS = 16
MAX_TEXT = 320
MAX_URL = 2_048

FactStatus = Literal["found", "conflict", "error"]
PriceKind = Literal["verified_sale", "listing"]
RightType = Literal["ownership", "lease"]
Readiness = Literal["none", "partial", "ready", "unknown"]


class ComparableInventoryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryFact:
    sequence_id: int
    source_name: str
    source_record_id: str
    source_sale_id: str | None
    source_listing_id: str | None
    source_url: str | None
    object_id: str | None
    fact_status: FactStatus
    price_kind: PriceKind
    verification_status: str | None
    verification_ref: str | None
    right_type: RightType | None
    purpose_group: str | None
    lease_term_years: float | None
    area_ha: float | None
    price_kzt: int | None
    latitude: float | None
    longitude: float | None
    access_readiness: Readiness | None
    infrastructure_readiness: Readiness | None
    event_at: datetime | None
    observed_at: datetime
    title: str | None
    locality: str | None
    provenance_refs: tuple[str, ...]
    conflict_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RejectedInventoryFact:
    sequence_id: int
    source_key: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class GeoBoundingBox:
    latitude_min: float
    latitude_max: float
    longitude_min: float
    longitude_max: float


@dataclass(frozen=True, slots=True)
class GeoSelectionPlan:
    bbox: GeoBoundingBox
    radius_km: float
    right_type: RightType
    purpose_group: str
    area_min_ha: float
    area_max_ha: float
    lease_band: tuple[float, float] | None
    lease_lower_exclusive: bool
    event_from: datetime
    event_to: datetime
    order: str = "observed_at_desc_sequence_id_desc"
    keyset_columns: tuple[str, str] = ("observed_at", "sequence_id")
    sql_contract: str = (
        "latest-per-source-key first; then found+verified_sale+verification_status=verified+"
        "verification_ref-present+target predicates+bbox; then descending keyset and LIMIT"
    )
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class SelectedComparable:
    fact: InventoryFact
    source_key: str
    distance_km: float
    candidate: ComparableCandidate


@dataclass(frozen=True, slots=True)
class GeoSelectionResult:
    status: str
    selected: tuple[SelectedComparable, ...]
    rejected: tuple[RejectedInventoryFact, ...]
    scanned_count: int
    next_cursor: tuple[str, int] | None
    input_generation_hash: str
    plan: GeoSelectionPlan
    contract_version: str = CONTRACT_VERSION


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or len(value) > limit:
        return None
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized if normalized and len(normalized) <= limit else None


def _finite(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def _aware(value: object) -> datetime | None:
    return value if isinstance(value, datetime) and value.utcoffset() is not None else None


def _identity_component(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def source_idempotency_key(fact: InventoryFact) -> str:
    """Return the immutable provider identity; observations may share this key."""
    source = _identity_component(fact.source_name)
    if fact.price_kind == "verified_sale":
        identifier = fact.source_sale_id
        kind = "sale"
    else:
        identifier = fact.source_listing_id
        kind = "listing"
    if not source or not identifier:
        raise ComparableInventoryError("source identity is incomplete")
    identity = json.dumps(
        [source, kind, _identity_component(identifier)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(identity.encode('utf-8')).hexdigest()}"


def normalize_inventory_fact(payload: Mapping[str, object]) -> InventoryFact:
    """Validate a provider-neutral fact, including minimal conflict tombstones."""
    sequence = payload.get("sequence_id")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 < sequence <= 2**63 - 1:
        raise ComparableInventoryError("invalid_sequence_id")
    source_name = _text(payload.get("source_name"), 128)
    record_id = _text(payload.get("source_record_id"), 128)
    if not source_name or not record_id:
        raise ComparableInventoryError("invalid_or_unbounded_text")
    status = payload.get("fact_status")
    price_kind = payload.get("price_kind")
    if status not in {"found", "conflict", "error"}:
        raise ComparableInventoryError("invalid_fact_status")
    if price_kind not in {"verified_sale", "listing"}:
        raise ComparableInventoryError("invalid_price_kind")
    sale_id = _text(payload.get("source_sale_id"), 128)
    listing_id = _text(payload.get("source_listing_id"), 128)
    if price_kind == "verified_sale" and not sale_id:
        raise ComparableInventoryError("sale_identity_missing")
    if price_kind == "listing" and not listing_id:
        raise ComparableInventoryError("listing_identity_missing")
    observed = _aware(payload.get("observed_at"))
    if observed is None:
        raise ComparableInventoryError("timestamps_must_be_timezone_aware")
    provenance = payload.get("provenance_refs", ())
    conflicts = payload.get("conflict_fields", ())
    if not isinstance(provenance, (list, tuple)) or len(provenance) > MAX_PROVENANCE_REFS:
        raise ComparableInventoryError("invalid_provenance")
    if not isinstance(conflicts, (list, tuple)) or len(conflicts) > MAX_PROVENANCE_REFS:
        raise ComparableInventoryError("invalid_conflicts")
    provenance_refs = tuple(filter(None, (_text(item, 512) for item in provenance)))
    conflict_fields = tuple(filter(None, (_text(item, 128) for item in conflicts)))
    if not provenance_refs:
        raise ComparableInventoryError("provenance_required")

    source_url = _text(payload.get("source_url"), MAX_URL)
    title = _text(payload.get("title"), MAX_TEXT)
    purpose_value = _text(payload.get("purpose_group"), 160)
    purpose = purpose_value.casefold() if purpose_value else None
    verification_status = _text(payload.get("verification_status"), 64)
    verification_ref = _text(payload.get("verification_ref"), 512)
    right_type = payload.get("right_type")
    latitude = _finite(payload.get("latitude"), KZ_LAT_MIN, KZ_LAT_MAX)
    longitude = _finite(payload.get("longitude"), KZ_LON_MIN, KZ_LON_MAX)
    area = _finite(payload.get("area_ha"), 0.0001, 1_000_000)
    price = payload.get("price_kzt")
    event_at = payload.get("event_at")
    if event_at is not None and _aware(event_at) is None:
        raise ComparableInventoryError("timestamps_must_be_timezone_aware")
    readiness_values = {"none", "partial", "ready", "unknown"}
    access = payload.get("access_readiness", "unknown")
    infrastructure = payload.get("infrastructure_readiness", "unknown")
    lease = payload.get("lease_term_years")
    lease_years = _finite(lease, 0.01, 99) if lease is not None else None
    if status == "found":
        if not source_url or not title or not purpose:
            raise ComparableInventoryError("invalid_or_unbounded_text")
        if right_type not in {"ownership", "lease"}:
            raise ComparableInventoryError("invalid_right_type")
        if price_kind == "verified_sale" and (
            verification_status != "verified" or not verification_ref
        ):
            raise ComparableInventoryError("sale_not_verified")
        if price_kind == "verified_sale" and _aware(event_at) is None:
            raise ComparableInventoryError("sale_event_at_required")
        if latitude is None or longitude is None:
            raise ComparableInventoryError("coordinates_outside_kazakhstan")
        if area is None:
            raise ComparableInventoryError("invalid_area")
        if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= 10**15:
            raise ComparableInventoryError("invalid_price_kzt")
        if access not in readiness_values or infrastructure not in readiness_values:
            raise ComparableInventoryError("invalid_readiness")
        if right_type == "lease" and lease_years is None:
            raise ComparableInventoryError("lease_term_missing")
    else:
        source_url = source_url if source_url else None
        title = title if title else None
        purpose = purpose if purpose else None
        right_type = right_type if right_type in {"ownership", "lease"} else None
        access = access if access in readiness_values else None
        infrastructure = infrastructure if infrastructure in readiness_values else None
        if isinstance(price, bool) or not isinstance(price, int) or not 1 <= price <= 10**15:
            price = None
    fact = InventoryFact(
        sequence_id=sequence,
        source_name=source_name,
        source_record_id=record_id,
        source_sale_id=sale_id,
        source_listing_id=listing_id,
        source_url=source_url,
        object_id=_text(payload.get("object_id"), 128),
        fact_status=status,
        price_kind=price_kind,
        verification_status=verification_status,
        verification_ref=verification_ref,
        right_type=right_type,  # type: ignore[arg-type]
        purpose_group=purpose,
        lease_term_years=lease_years,
        area_ha=area,
        price_kzt=price,
        latitude=latitude,
        longitude=longitude,
        access_readiness=access,  # type: ignore[arg-type]
        infrastructure_readiness=infrastructure,  # type: ignore[arg-type]
        event_at=_aware(event_at),
        observed_at=observed,
        title=title,
        locality=_text(payload.get("locality"), 160),
        provenance_refs=provenance_refs,
        conflict_fields=conflict_fields,
    )
    source_idempotency_key(fact)
    return fact


def build_geo_selection_plan(
    latitude: float,
    longitude: float,
    *,
    right_type: RightType,
    purpose_group: str,
    area_ha: float,
    valuation_at: datetime,
    lease_term_years: float | None = None,
    radius_km: float = 5.0,
    max_age_days: int = 365,
) -> GeoSelectionPlan:
    lat = _finite(latitude, KZ_LAT_MIN, KZ_LAT_MAX)
    lon = _finite(longitude, KZ_LON_MIN, KZ_LON_MAX)
    radius = _finite(radius_km, 0.01, MAX_RADIUS_KM)
    purpose_value = _text(purpose_group, 160)
    purpose = purpose_value.casefold() if purpose_value else None
    area = _finite(area_ha, 0.0001, 1_000_000)
    valued = _aware(valuation_at)
    if (
        lat is None
        or lon is None
        or radius is None
        or right_type not in {"ownership", "lease"}
        or purpose is None
        or area is None
        or valued is None
        or isinstance(max_age_days, bool)
        or not isinstance(max_age_days, int)
        or not 1 <= max_age_days <= 3_650
    ):
        raise ComparableInventoryError("invalid_geo_query")
    lease_band = None
    lease_lower_exclusive = False
    if right_type == "lease":
        lease = _finite(lease_term_years, 0.01, 99)
        if lease is None:
            raise ComparableInventoryError("invalid_geo_query")
        if lease <= 3:
            lease_band = (0.01, 3.0)
        elif lease <= 10:
            lease_band = (3.0, 10.0)
            lease_lower_exclusive = True
        else:
            lease_band = (10.0, 99.0)
            lease_lower_exclusive = True
    latitude_delta = radius / 110.574
    longitude_delta = radius / (111.320 * max(math.cos(math.radians(lat)), 0.01))
    calendar_year_start = valued.replace(
        month=1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return GeoSelectionPlan(
        bbox=GeoBoundingBox(
            latitude_min=max(KZ_LAT_MIN, lat - latitude_delta),
            latitude_max=min(KZ_LAT_MAX, lat + latitude_delta),
            longitude_min=max(KZ_LON_MIN, lon - longitude_delta),
            longitude_max=min(KZ_LON_MAX, lon + longitude_delta),
        ),
        radius_km=radius,
        right_type=right_type,
        purpose_group=purpose,
        area_min_ha=area * 0.7,
        area_max_ha=area * 1.3,
        lease_band=lease_band,
        lease_lower_exclusive=lease_lower_exclusive,
        event_from=max(calendar_year_start, valued - timedelta(days=max_age_days)),
        event_to=valued,
    )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _lease_matches(plan: GeoSelectionPlan, years: float | None) -> bool:
    if plan.lease_band is None:
        return True
    if years is None:
        return False
    lower_matches = years > plan.lease_band[0] if plan.lease_lower_exclusive else years >= 0.01
    return lower_matches and years <= plan.lease_band[1]


def _generation_hash(facts: Sequence[InventoryFact]) -> str:
    rows = []
    for fact in facts:
        row = asdict(fact)
        row["source_key"] = source_idempotency_key(fact)
        row["observed_at"] = fact.observed_at.isoformat()
        row["event_at"] = fact.event_at.isoformat() if fact.event_at else None
        rows.append(row)
    encoded = json.dumps(rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def select_nearby_verified_sales(
    latitude: float,
    longitude: float,
    facts: Sequence[InventoryFact],
    *,
    right_type: RightType,
    purpose_group: str,
    area_ha: float,
    valuation_at: datetime,
    lease_term_years: float | None = None,
    radius_km: float = 5.0,
    max_age_days: int = 365,
    result_limit: int = 100,
) -> GeoSelectionResult:
    """Simulate latest-state SQL prefilters, bounded keyset, then exact radius.

    Production pagination must execute the plan's latest-per-source-key relation on
    every page before target predicates.  Applying a cursor to raw observations is
    forbidden because it could resurrect an older sale hidden by a newer tombstone.
    """
    plan = build_geo_selection_plan(
        latitude,
        longitude,
        right_type=right_type,
        purpose_group=purpose_group,
        area_ha=area_ha,
        valuation_at=valuation_at,
        lease_term_years=lease_term_years,
        radius_km=radius_km,
        max_age_days=max_age_days,
    )
    if len(facts) > MAX_INPUT_ROWS:
        raise ComparableInventoryError("input_inventory_exceeds_bound")
    if isinstance(result_limit, bool) or not isinstance(result_limit, int):
        raise ComparableInventoryError("invalid_result_limit")
    limit = min(max(result_limit, 1), MAX_RESULT_ROWS)
    ordered = sorted(facts, key=lambda item: (item.observed_at, item.sequence_id), reverse=True)
    newest: dict[str, InventoryFact] = {}
    for fact in ordered:
        newest.setdefault(source_idempotency_key(fact), fact)
    selected: list[SelectedComparable] = []
    rejected: list[RejectedInventoryFact] = []
    compatible: list[tuple[str, InventoryFact]] = []
    for source_key, fact in newest.items():
        if fact.fact_status != "found":
            rejected.append(
                RejectedInventoryFact(fact.sequence_id, source_key, f"newest_{fact.fact_status}")
            )
            continue
        if fact.conflict_fields:
            rejected.append(
                RejectedInventoryFact(fact.sequence_id, source_key, "found_has_conflicts")
            )
            continue
        if fact.price_kind != "verified_sale":
            rejected.append(RejectedInventoryFact(fact.sequence_id, source_key, "listing_not_sale"))
            continue
        if (
            fact.right_type != plan.right_type
            or fact.purpose_group.casefold() != plan.purpose_group.casefold()
            or fact.area_ha is None
            or not plan.area_min_ha <= fact.area_ha <= plan.area_max_ha
            or fact.event_at is None
            or not plan.event_from <= fact.event_at <= plan.event_to
            or not _lease_matches(plan, fact.lease_term_years)
        ):
            rejected.append(
                RejectedInventoryFact(fact.sequence_id, source_key, "target_prefilter_mismatch")
            )
            continue
        if not (
            fact.latitude is not None
            and fact.longitude is not None
            and
            plan.bbox.latitude_min <= fact.latitude <= plan.bbox.latitude_max
            and plan.bbox.longitude_min <= fact.longitude <= plan.bbox.longitude_max
        ):
            rejected.append(RejectedInventoryFact(fact.sequence_id, source_key, "outside_bbox"))
            continue
        compatible.append((source_key, fact))

    page = compatible[:MAX_SCAN_ROWS]
    for source_key, fact in page:
        assert fact.latitude is not None and fact.longitude is not None
        distance = _haversine_km(latitude, longitude, fact.latitude, fact.longitude)
        if distance > plan.radius_km:
            rejected.append(RejectedInventoryFact(fact.sequence_id, source_key, "outside_radius"))
            continue
        selected.append(
            SelectedComparable(
                fact=fact,
                source_key=source_key,
                distance_km=distance,
                candidate=ComparableCandidate(
                    source_id=fact.source_name,
                    source_record_id=fact.source_record_id,
                    source_url=fact.source_url or "",
                    title=fact.title or "",
                    right_type=fact.right_type,  # type: ignore[arg-type]
                    purpose_group=fact.purpose_group or "",
                    area_ha=fact.area_ha or 0.0,
                    price_kzt=float(fact.price_kzt or 0),
                    price_kind="verified_sale",
                    observed_at=fact.event_at or fact.observed_at,
                    locality=fact.locality,
                    latitude=fact.latitude,
                    longitude=fact.longitude,
                    lease_term_years=fact.lease_term_years,
                    access_readiness=fact.access_readiness or "unknown",
                    infrastructure_readiness=fact.infrastructure_readiness or "unknown",
                    object_id=fact.object_id,
                ),
            )
        )
    selected.sort(key=lambda item: (item.distance_km, -item.fact.sequence_id, item.source_key))
    selected = selected[:limit]
    next_cursor = None
    if len(compatible) > MAX_SCAN_ROWS and page:
        next_cursor = (page[-1][1].observed_at.isoformat(), page[-1][1].sequence_id)
    return GeoSelectionResult(
        status="ok" if selected else "insufficient",
        selected=tuple(selected),
        rejected=tuple(rejected),
        scanned_count=len(ordered),
        next_cursor=next_cursor,
        input_generation_hash=_generation_hash(ordered),
        plan=plan,
    )
