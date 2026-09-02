"""Worker-side e-Qazyna completed-sale ingest and global W9 bridge."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from shapely.geometry import shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auction_market_dirty_store import (
    ComparableBatchItem,
    ingest_verified_comparable_batch,
)
from app.auction_market_estimate_store import (
    CandidateSet,
    MarketEvidenceWriteResult,
    build_authoritative_market_target,
    calculate_market_evidence,
    load_authoritative_target_facts,
    load_authoritative_target_facts_batch,
    load_history_audit_reference,
    persist_market_evidence,
)
from app.auction_parcel_geometry import analyze_parcel_geometry
from app.auction_verified_comparable_inventory import (
    InventoryFact,
    normalize_inventory_fact,
)
from app.auction_verified_comparable_repository import query_verified_comparables
from app.models import (
    AuctionEvidence,
    AuctionHistoryGeneration,
    AuctionHistoryNormalized,
    AuctionLandObject,
    AuctionLot,
)

PROVIDER_VERSION = "eqazyna-completed-sales/2026.1"
MAX_BATCH = 100
MAX_EVIDENCE_BYTES = 8_000
MAX_EVIDENCE_AGGREGATE_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class EqazynaSaleSourceRow:
    lot_id: str
    source_lot_id: str
    source_url: str
    status: str | None
    source_search_status: str | None
    land_object_id: str | None
    cadastre_number: str | None
    lease_term_years: float | None
    auction_starts_at: datetime | None
    title: str
    locality: str | None
    generation: int | None
    normalization_key: str | None
    right_kind: str | None
    right_status: str | None
    purpose_group: str | None
    purpose_status: str | None
    lease_status: str | None
    event_date: object | None
    event_date_status: str | None
    outcome: str | None
    outcome_status: str | None
    area_ha: Decimal | None
    area_status: str | None
    sale_price_kzt: Decimal | None
    sale_price_status: str | None
    canonical_object_id: str | None = None
    canonical_boundary_geojson: str | None = None
    canonical_boundary_source: str | None = None


@dataclass(frozen=True, slots=True)
class EqazynaSaleBatchResult:
    status: str
    selected: int
    ingested: int
    unchanged: int
    rejected: int
    rejection_reasons: dict[str, int]
    last_lot_id: str | None
    high_water_lot_id: str | None
    has_more: bool
    duration_ms: int
    max_source_lag_seconds: int | None
    inventory_generation: int | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _sequence_id(source_lot_id: str) -> int:
    if source_lot_id.isdigit():
        value = int(source_lot_id)
        if 0 < value <= 2**63 - 1:
            return value
    digest = hashlib.sha256(source_lot_id.encode("utf-8")).digest()
    return max(1, int.from_bytes(digest[:8], "big") & (2**63 - 1))


def _readiness(payload: dict[str, object]) -> tuple[str, str] | None:
    def value(key: str) -> str:
        item = payload.get(key)
        return str(item.get("readiness")) if isinstance(item, dict) else "unknown"

    physical = value("physical_access")
    legal = value("legal_access")
    infrastructure = value("infrastructure")
    if physical == legal == "ready":
        access = "ready"
    elif physical in {"ready", "partial"} and legal in {"ready", "partial"}:
        access = "partial"
    elif physical == "not_ready" or legal == "not_ready":
        access = "none"
    else:
        return None
    infrastructure_map = {"ready": "ready", "partial": "partial", "not_ready": "none"}
    normalized_infrastructure = infrastructure_map.get(infrastructure)
    return (access, normalized_infrastructure) if normalized_infrastructure else None


def _parse_evidence(raw: str | None) -> dict[str, object]:
    if raw is None or len(raw.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _canonical_target_object_id(lot: AuctionLot) -> str | None:
    """Prefer the internal canonical object shared across repeated/source listings."""
    return lot.land_object_ref_id or lot.land_object_id or lot.cadastre_number


def build_eqazyna_verified_sale_fact(
    row: EqazynaSaleSourceRow,
    *,
    cadastre_status: str | None,
    cadastre_payload: dict[str, object],
    site_status: str | None,
    site_payload: dict[str, object],
    source_object_status: str | None = None,
    source_object_payload: dict[str, object] | None = None,
) -> tuple[InventoryFact | None, str]:
    """Fail closed unless every sale/right/purpose/geometry/readiness fact is exact."""
    status_text = " ".join(
        str(value).casefold() for value in (row.status, row.source_search_status) if value
    )
    failure = any(
        marker in status_text
        for marker in (
            "failureprotocolsigned",
            "не состоялся",
            "не состоялись",
            "өтпеді",
            "отменен",
            "отменён",
        )
    )
    success_text = status_text
    for marker in ("failureprotocolsigned", "не состоялся", "не состоялись", "өтпеді"):
        success_text = success_text.replace(marker, " ")
    official_success = any(
        marker in success_text
        for marker in ("successprotocolsigned", "состоялся", "состоялись", "өтті")
    )
    if failure or not official_success:
        return None, "official_success_status_missing_or_conflict"
    required_statuses = (
        row.right_status,
        row.purpose_status,
        row.event_date_status,
        row.outcome_status,
        row.area_status,
        row.sale_price_status,
    )
    if any(status != "found" for status in required_statuses) or row.outcome != "success":
        return None, "auction_result_incomplete_or_conflict"
    event_at = _aware(row.auction_starts_at)
    if event_at is None or row.event_date != event_at.date():
        return None, "event_date_unknown_or_conflict"
    if row.right_kind not in {"ownership", "lease"}:
        return None, "right_unknown_or_conflict"
    lease_term_years = row.lease_term_years
    lease_term_provenance = "lease_term:normalized_history"
    if row.right_kind == "lease" and (
        row.lease_status != "found"
        or lease_term_years is None
        or not math.isfinite(lease_term_years)
    ):
        official_lease = (source_object_payload or {}).get("lease_term_years")
        if (
            source_object_status == "found"
            and not isinstance(official_lease, bool)
            and isinstance(official_lease, (int, float))
            and math.isfinite(float(official_lease))
            and 0 < float(official_lease) <= 1_000
        ):
            lease_term_years = float(official_lease)
            lease_term_provenance = "lease_term:source_object_card"
    if row.right_kind == "lease" and (
        lease_term_years is None or not math.isfinite(lease_term_years)
    ):
        return None, "lease_term_unknown_or_conflict"
    if row.purpose_group in {None, "unknown", "other"}:
        return None, "purpose_unknown_or_conflict"
    if row.area_ha is None or row.sale_price_kzt is None:
        return None, "sale_or_area_missing"
    if row.sale_price_kzt != row.sale_price_kzt.to_integral_value():
        return None, "sale_price_not_whole_kzt"
    canonical_object_id = row.canonical_object_id or row.land_object_id or row.cadastre_number
    if canonical_object_id is None:
        return None, "canonical_object_identity_missing"
    geometry = cadastre_payload.get("geometry_geojson") if cadastre_status == "found" else None
    geometry_source = "egkn:cadastre_boundary"
    if geometry is None and row.canonical_boundary_source == "jerler:source_object":
        try:
            geometry = json.loads(row.canonical_boundary_geojson or "")
        except (TypeError, json.JSONDecodeError):
            geometry = None
        geometry_source = "jerler:source_object"
    if not isinstance(geometry, dict):
        return None, "coordinates_unknown_or_conflict"
    geometry_result = analyze_parcel_geometry(geometry)
    if geometry_result.status != "ok":
        return None, "coordinates_unknown_or_conflict"
    centroid = shape(geometry).centroid
    if site_status == "found":
        readiness = _readiness(site_payload)
        if readiness is None:
            return None, "readiness_unknown_or_conflict"
        access, infrastructure = readiness
        readiness_provenance = "site_readiness:verified"
    elif site_status in {None, "missing"}:
        # A completed official auction remains a verified price observation when
        # access/infrastructure were not assessed. Keep both dimensions unknown:
        # strict W9 valuation excludes unknown-readiness rows, while the global
        # inventory can still expose an auditable official sale as a reference.
        access = infrastructure = "unknown"
        readiness_provenance = "site_readiness:unknown"
    else:
        return None, "readiness_unknown_or_conflict"
    generation_material = (
        f"{row.generation}:{row.normalization_key}:{lease_term_years}:"
        f"{lease_term_provenance}:{PROVIDER_VERSION}"
    ).encode()
    fact = normalize_inventory_fact(
        {
            "sequence_id": _sequence_id(row.source_lot_id),
            "source_name": "e-qazyna-auction-results",
            "source_record_id": row.source_lot_id,
            "source_sale_id": row.source_lot_id,
            "source_listing_id": None,
            "source_url": row.source_url,
            "object_id": canonical_object_id,
            "fact_status": "found",
            "price_kind": "verified_sale",
            "verification_status": "verified",
            "verification_ref": f"eqazyna-auction-result:{row.source_lot_id}",
            "right_type": row.right_kind,
            "purpose_group": row.purpose_group,
            "lease_term_years": (lease_term_years if row.right_kind == "lease" else None),
            "area_ha": float(row.area_ha),
            "price_kzt": int(row.sale_price_kzt),
            "latitude": float(centroid.y),
            "longitude": float(centroid.x),
            "access_readiness": access,
            "infrastructure_readiness": infrastructure,
            "event_at": event_at,
            # Stable upstream auction event time, never worker poll time.
            "observed_at": event_at,
            "title": row.title[:320],
            "locality": row.locality,
            "provenance_refs": [
                f"auction_lot:{row.lot_id}",
                f"auction_history_generation:{row.generation}",
                f"normalization_key:{row.normalization_key}",
                "event_timestamp_proxy:auction_starts_at",
                f"parcel_geometry:{geometry_source}",
                readiness_provenance,
                lease_term_provenance,
            ],
            "conflict_fields": [],
        }
    )
    return fact, hashlib.sha256(generation_material).hexdigest()


def _load_source_batch(
    session: Session,
    *,
    after_lot_id: str | None,
    high_water_lot_id: str | None,
    limit: int,
) -> tuple[
    list[EqazynaSaleSourceRow],
    dict[tuple[str, str], tuple[str, str | None]],
    str | None,
    bool,
]:
    active_generation = session.scalar(
        select(AuctionHistoryGeneration.generation)
        .where(AuctionHistoryGeneration.status == "active")
        .limit(1)
    )
    conditions = [AuctionLot.source == "e-qazyna", AuctionLot.object_type == "land"]
    if high_water_lot_id is None:
        high_water_lot_id = session.scalar(select(func.max(AuctionLot.id)).where(*conditions))
    if high_water_lot_id is None:
        return [], {}, None, False
    conditions.append(AuctionLot.id <= high_water_lot_id)
    if after_lot_id is not None:
        conditions.append(AuctionLot.id > after_lot_id)
    normalized_join = (AuctionHistoryNormalized.lot_id == AuctionLot.id) & (
        AuctionHistoryNormalized.generation == active_generation
    )
    rows = session.execute(
        select(
            AuctionLot.id,
            AuctionLot.source_lot_id,
            AuctionLot.source_url,
            AuctionLot.status,
            AuctionLot.source_search_status,
            AuctionLot.land_object_id,
            AuctionLot.cadastre_number,
            AuctionLot.lease_term_years,
            AuctionLot.auction_starts_at,
            AuctionLot.title,
            AuctionLot.locality,
            AuctionHistoryNormalized.generation,
            AuctionHistoryNormalized.normalization_key,
            AuctionHistoryNormalized.right_kind,
            AuctionHistoryNormalized.right_status,
            AuctionHistoryNormalized.purpose_group,
            AuctionHistoryNormalized.purpose_status,
            AuctionHistoryNormalized.lease_status,
            AuctionHistoryNormalized.event_date,
            AuctionHistoryNormalized.event_date_status,
            AuctionHistoryNormalized.outcome,
            AuctionHistoryNormalized.outcome_status,
            AuctionHistoryNormalized.area_ha,
            AuctionHistoryNormalized.area_status,
            AuctionHistoryNormalized.sale_price_kzt,
            AuctionHistoryNormalized.sale_price_status,
            AuctionLandObject.id,
            AuctionLandObject.boundary_geojson,
            AuctionLandObject.boundary_source,
        )
        .outerjoin(AuctionHistoryNormalized, normalized_join)
        .outerjoin(AuctionLandObject, AuctionLandObject.id == AuctionLot.land_object_ref_id)
        .where(*conditions)
        .order_by(AuctionLot.id.asc())
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    source_rows = [EqazynaSaleSourceRow(*row) for row in rows[:limit]]
    lot_ids = [row.lot_id for row in source_rows]
    evidence: dict[tuple[str, str], tuple[str, str | None]] = {}
    if lot_ids:
        ranked = (
            select(
                AuctionEvidence.lot_id,
                AuctionEvidence.evidence_type,
                AuctionEvidence.status,
                AuctionEvidence.source_id,
                AuctionEvidence.value_text,
                func.substr(AuctionEvidence.raw_payload_json, 1, MAX_EVIDENCE_BYTES + 1).label(
                    "bounded_payload"
                ),
                func.row_number()
                .over(
                    partition_by=(AuctionEvidence.lot_id, AuctionEvidence.evidence_type),
                    order_by=(
                        AuctionEvidence.observed_at.desc(),
                        AuctionEvidence.id.desc(),
                    ),
                )
                .label("row_number"),
            ).where(
                AuctionEvidence.lot_id.in_(lot_ids),
                AuctionEvidence.evidence_type.in_(
                    (
                        "cadastre_boundary",
                        "decision_input:site_context",
                        "source_object_card",
                    )
                ),
                AuctionEvidence.status.in_(("found", "conflict")),
            )
        ).subquery()
        evidence_rows = session.execute(
            select(
                ranked.c.lot_id,
                ranked.c.evidence_type,
                ranked.c.status,
                ranked.c.source_id,
                ranked.c.value_text,
                ranked.c.bounded_payload,
            )
            .where(ranked.c.row_number == 1)
            .order_by(ranked.c.lot_id, ranked.c.evidence_type)
            .limit(len(lot_ids) * 3)
        )
        aggregate_bytes = 0
        for evidence_row in evidence_rows:
            key = (str(evidence_row.lot_id), str(evidence_row.evidence_type))
            if key not in evidence:
                status = str(evidence_row.status)
                if evidence_row.evidence_type == "decision_input:site_context":
                    value_text = evidence_row.value_text
                    worker_owned = (
                        evidence_row.source_id is None
                        and isinstance(value_text, str)
                        and len(value_text) == 64
                        and all(
                            character in "0123456789abcdef" for character in value_text.casefold()
                        )
                    )
                    if not worker_owned:
                        status = "conflict"
                raw = evidence_row.bounded_payload
                raw_size = len(str(raw).encode("utf-8")) if raw is not None else 0
                if raw_size > MAX_EVIDENCE_BYTES:
                    status = "conflict"
                    raw = None
                elif aggregate_bytes + raw_size > MAX_EVIDENCE_AGGREGATE_BYTES:
                    status = "conflict"
                    raw = None
                else:
                    aggregate_bytes += raw_size
                evidence[key] = (
                    status,
                    str(raw) if raw is not None else None,
                )
    return source_rows, evidence, high_water_lot_id, has_more


def ingest_eqazyna_verified_sales_batch(
    session_factory,
    *,
    after_lot_id: str | None = None,
    high_water_lot_id: str | None = None,
    limit: int = 100,
) -> EqazynaSaleBatchResult:
    started = time.monotonic()
    bounded = max(1, min(int(limit), MAX_BATCH))
    with session_factory() as session:
        rows, evidence, high_water, has_more = _load_source_batch(
            session,
            after_lot_id=after_lot_id,
            high_water_lot_id=high_water_lot_id,
            limit=bounded,
        )
    ingested = unchanged = rejected = 0
    reasons: dict[str, int] = {}
    batch_items: list[ComparableBatchItem] = []
    for row in rows:
        cadastre_status, cadastre_raw = evidence.get(
            (row.lot_id, "cadastre_boundary"), (None, None)
        )
        site_status, site_raw = evidence.get(
            (row.lot_id, "decision_input:site_context"), (None, None)
        )
        source_object_status, source_object_raw = evidence.get(
            (row.lot_id, "source_object_card"), (None, None)
        )
        fact, generation_or_reason = build_eqazyna_verified_sale_fact(
            row,
            cadastre_status=cadastre_status,
            cadastre_payload=_parse_evidence(cadastre_raw),
            site_status=site_status,
            site_payload=_parse_evidence(site_raw),
            source_object_status=source_object_status,
            source_object_payload=_parse_evidence(source_object_raw),
        )
        if fact is None:
            rejected += 1
            reasons[generation_or_reason] = reasons.get(generation_or_reason, 0) + 1
            continue
        batch_items.append(
            ComparableBatchItem(
                fact=fact,
                generation_signature=generation_or_reason,
                raw_payload={
                    "provider_version": PROVIDER_VERSION,
                    "auction_lot_id": row.lot_id,
                    "history_generation": row.generation,
                    "normalization_key": row.normalization_key,
                    "event_timestamp_quality": "auction_start_proxy",
                },
            )
        )
    if batch_items:
        batch_result = ingest_verified_comparable_batch(
            session_factory, batch_items, completed_at=datetime.now(UTC)
        )
        for result in batch_result.results:
            if result.inserted or result.current_changed:
                ingested += 1
            else:
                unchanged += 1
    return EqazynaSaleBatchResult(
        status="ok" if rows else "empty",
        selected=len(rows),
        ingested=ingested,
        unchanged=unchanged,
        rejected=rejected,
        rejection_reasons=reasons,
        last_lot_id=rows[-1].lot_id if rows else after_lot_id,
        high_water_lot_id=high_water,
        has_more=has_more,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        max_source_lag_seconds=(
            max(
                0,
                int(
                    (
                        datetime.now(UTC)
                        - min(
                            event
                            for event in (_aware(row.auction_starts_at) for row in rows)
                            if event is not None
                        )
                    ).total_seconds()
                ),
            )
            if any(_aware(row.auction_starts_at) is not None for row in rows)
            else None
        ),
        inventory_generation=(
            batch_result.delta.generation if batch_items and batch_result.delta else None
        ),
    )


def _market_input_right_type(
    right_type: str | None,
    lease_term_years: float | None,
) -> str | None:
    """Keep incomplete lease targets valid and fail closed for valuation."""
    if right_type == "lease" and lease_term_years is None:
        return None
    return right_type if right_type in {"ownership", "lease"} else None


def load_global_market_target_input(session_factory, lot_id: str):
    """Read bounded authoritative target facts, close DB, then normalize for dirty policy."""
    from app.auction_market_dirty_state import MarketTargetInput

    with session_factory() as session:
        lot = session.get(AuctionLot, lot_id)
        if lot is None:
            raise ValueError("auction lot not found")
        facts = load_authoritative_target_facts(session, lot_id, lot=lot)
        canonical = _canonical_target_object_id(lot)
        source_sale = lot.source_lot_id if lot.source == "e-qazyna" else None
    built = build_authoritative_market_target(facts, valuation_at=datetime.now(UTC))
    target = built.target
    return MarketTargetInput(
        lot_id=lot_id,
        right_type=_market_input_right_type(target.right_type, target.lease_term_years),
        purpose_group=target.purpose_group,
        lease_term_years=target.lease_term_years,
        area_ha=target.area_ha if target.area_ha > 0 else None,
        latitude=target.latitude,
        longitude=target.longitude,
        access_readiness=target.access_readiness,
        infrastructure_readiness=target.infrastructure_readiness,
        canonical_object_id=canonical,
        source_sale_id=source_sale,
    )


def load_global_market_target_inputs(session_factory, lot_ids: list[str]):
    """Batch target projection; DB reads finish before geometry/readiness CPU."""
    from app.auction_market_dirty_state import MarketTargetInput

    with session_factory() as session:
        facts = load_authoritative_target_facts_batch(session, lot_ids)
        identities = {
            row.id: row
            for row in session.execute(
                select(
                    AuctionLot.id,
                    AuctionLot.source,
                    AuctionLot.source_lot_id,
                    AuctionLot.land_object_ref_id,
                    AuctionLot.land_object_id,
                    AuctionLot.cadastre_number,
                ).where(AuctionLot.id.in_(lot_ids))
            )
        }
    targets: list[MarketTargetInput] = []
    for item in facts:
        built = build_authoritative_market_target(item, valuation_at=datetime.now(UTC))
        target = built.target
        identity = identities[item.lot_id]
        targets.append(
            MarketTargetInput(
                lot_id=item.lot_id,
                right_type=_market_input_right_type(target.right_type, target.lease_term_years),
                purpose_group=target.purpose_group,
                lease_term_years=target.lease_term_years,
                area_ha=target.area_ha if target.area_ha > 0 else None,
                latitude=target.latitude,
                longitude=target.longitude,
                access_readiness=target.access_readiness,
                infrastructure_readiness=target.infrastructure_readiness,
                canonical_object_id=(
                    identity.land_object_ref_id
                    or identity.land_object_id
                    or identity.cadastre_number
                ),
                source_sale_id=(identity.source_lot_id if identity.source == "e-qazyna" else None),
            )
        )
    return targets


def recompute_market_from_global_inventory(
    session_factory,
    lot_id: str,
    *,
    observed_at: datetime | None = None,
    expected_target_signature: str | None = None,
) -> MarketEvidenceWriteResult:
    """Compose target/history reads, close DB, query global inventory, then run W9 CPU."""
    valuation_at = _aware(observed_at) or datetime.now(UTC)
    with session_factory() as session:
        lot = session.get(AuctionLot, lot_id)
        if lot is None:
            raise ValueError("auction lot not found")
        target_facts = load_authoritative_target_facts(session, lot_id, lot=lot)
        history = load_history_audit_reference(session, lot)
        canonical_object_id = _canonical_target_object_id(lot)
        target_source_sale_id = lot.source_lot_id if lot.source == "e-qazyna" else None
    target_build = build_authoritative_market_target(target_facts, valuation_at=valuation_at)
    selected = ()
    selection_hash = hashlib.sha256(b"target-insufficient").hexdigest()
    cursor_present = False
    target_has_coordinates = (
        isinstance(target_build.target.latitude, (int, float))
        and isinstance(target_build.target.longitude, (int, float))
        and math.isfinite(float(target_build.target.latitude))
        and math.isfinite(float(target_build.target.longitude))
        and 40 <= float(target_build.target.latitude) <= 56
        and 46 <= float(target_build.target.longitude) <= 88
    )
    reference_only_missing = set(target_build.missing_reasons).issubset(
        {"access_readiness_unknown", "infrastructure_readiness_unknown"}
    )
    global_selection_performed = False
    if (
        (target_build.status == "ready" or reference_only_missing)
        and canonical_object_id is not None
        and target_has_coordinates
    ):
        result = query_verified_comparables(
            session_factory,
            latitude=float(target_build.target.latitude),
            longitude=float(target_build.target.longitude),
            right_type=target_build.target.right_type,
            purpose_group=target_build.target.purpose_group,
            area_ha=target_build.target.area_ha,
            valuation_at=valuation_at,
            lease_term_years=target_build.target.lease_term_years,
            radius_km=5.0,
            result_limit=200,
            exclude_object_id=canonical_object_id,
            exclude_source_sale_ids=(
                (target_source_sale_id,) if target_source_sale_id is not None else ()
            ),
        )
        selected = result.selected
        selection_hash = result.input_generation_hash
        cursor_present = result.next_cursor is not None
        global_selection_performed = True
    candidates = CandidateSet(
        candidates=tuple(item.candidate for item in selected),
        source_generation_signature=selection_hash,
        current_row_ids=tuple(item.fact.sequence_id for item in selected),
        rejected=(),
        provenance_refs=tuple(
            reference for item in selected for reference in item.fact.provenance_refs
        ),
        history_truncated=cursor_present,
    )
    material = calculate_market_evidence(
        target_build.target,
        candidates,
        history=history,
        target_build=target_build,
        market_target_signature=expected_target_signature,
        global_geo_selection_performed=global_selection_performed,
    )
    if expected_target_signature is not None:
        from app.auction_market_dirty_state import target_signature

        current_target = load_global_market_target_input(session_factory, lot_id)
        if target_signature(current_target) != expected_target_signature:
            raise ValueError("market_target_changed_before_persist")
    return persist_market_evidence(
        session_factory,
        lot_id,
        material,
        observed_at=valuation_at,
        expected_target_signature=expected_target_signature,
    )
