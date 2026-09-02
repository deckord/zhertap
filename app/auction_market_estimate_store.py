"""Bounded worker-side W9 calculator and immutable evidence writer.

This module consumes an already lot-scoped comparable inventory.  It deliberately
does not ingest external providers or perform global/geospatial candidate selection;
those are separate upstream production responsibilities.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from shapely.geometry import shape
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auction_history_read import normalized_similar_history
from app.auction_market_comparables import (
    ComparableCandidate,
    ComparableTarget,
    MarketComparableResult,
    build_strict_market_comparables,
)
from app.auction_parcel_geometry import analyze_parcel_geometry
from app.auction_taxonomy import classify_scenario_claims
from app.models import (
    AuctionEvidence,
    AuctionHistoryGeneration,
    AuctionLandObject,
    AuctionLot,
    AuctionMarketComparable,
)

WRITER_VERSION = "strict-market-estimate-writer/2026.1"
EVIDENCE_TYPE = "strict_market_estimate"
MAX_ROWS = 100
MAX_RAW_BYTES = 64_000
MAX_TOTAL_BYTES = 512_000
MAX_OUTPUT_BYTES = 64_000
MAX_URL = 2_048
MAX_TEXT = 320
MAX_AUDIT_PREFIX = 256
_SQLITE_LOCK = threading.Lock()


class MarketEstimateStoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedComparableFact:
    row_id: int
    source_name: str
    source_url: str
    title: str
    listing_status: str
    observed_at: datetime
    area_ha: float | None
    price_kzt: float | None
    locality: str | None
    raw_payload: Mapping[str, object]
    raw_payload_fingerprint: str
    quarantine_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _PersistedComparableRow:
    row_id: int
    source_name: str
    source_url: str
    title: str
    listing_status: str
    observed_at: datetime
    area_ha: float | None
    price_kzt: float | None
    locality: str | None
    raw_payload_json: str
    raw_payload_fingerprint: str
    quarantine_reason: str | None = None


@dataclass(frozen=True, slots=True)
class HistoryAuditReference:
    status: str
    generation: int | None
    generation_signature: str | None
    payload: dict[str, object]
    observed_at: datetime | None
    audit_only: bool = True


@dataclass(frozen=True, slots=True)
class AuthoritativeTargetFacts:
    lot_id: str
    updated_at: datetime
    land_rights: str | None
    lease_term_years: float | None
    purpose: str
    area_ha: float | None
    locality: str | None
    site_evidence_id: int | None
    site_payload_json: str | None
    cadastre_evidence_id: int | None
    cadastre_status: str | None
    cadastre_payload_json: str | None
    legal_evidence_id: int | None
    legal_status: str | None
    legal_payload_json: str | None
    canonical_object_id: str | None = None
    canonical_boundary_geojson: str | None = None
    canonical_boundary_source: str | None = None


@dataclass(frozen=True, slots=True)
class AuthoritativeTargetBuild:
    status: str
    target: ComparableTarget
    generation_signature: str
    provenance_refs: tuple[str, ...]
    missing_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidateSet:
    candidates: tuple[ComparableCandidate, ...]
    source_generation_signature: str
    current_row_ids: tuple[int, ...]
    rejected: tuple[dict[str, object], ...]
    provenance_refs: tuple[str, ...]
    history_truncated: bool = False


@dataclass(frozen=True, slots=True)
class MarketEvidenceMaterial:
    payload: dict[str, object]
    payload_json: str
    input_hash: str
    status: str
    oldest_used_at: datetime | None


@dataclass(frozen=True, slots=True)
class MarketEvidenceWriteResult:
    lot_id: str
    changed: bool
    evidence_id: int
    input_hash: str
    status: str


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
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
        raise MarketEstimateStoreError(f"{label} is not strict JSON") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise MarketEstimateStoreError(f"{label} exceeds byte budget")
    return encoded


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if 0 < len(cleaned) <= limit else None


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _load_persisted_comparable_rows(
    session: Session, lot_id: str
) -> tuple[tuple[_PersistedComparableRow, ...], bool]:
    """Project a bounded newest window and quarantine corrupt payloads.

    The first query transfers only metadata and a tiny audit prefix.  Full payloads
    are fetched in a second bounded projection only when both per-row and aggregate
    budgets allow it.  Immutable bad rows therefore cannot poison every retry.
    """
    rows = list(
        session.execute(
            select(
                AuctionMarketComparable.id,
                AuctionMarketComparable.source_name,
                func.substr(AuctionMarketComparable.source_url, 1, MAX_URL + 1).label(
                    "bounded_url"
                ),
                AuctionMarketComparable.title,
                AuctionMarketComparable.listing_status,
                AuctionMarketComparable.observed_at,
                AuctionMarketComparable.area_ha,
                AuctionMarketComparable.price_kzt,
                AuctionMarketComparable.locality,
                func.length(AuctionMarketComparable.raw_payload_json).label("raw_length"),
                func.substr(AuctionMarketComparable.raw_payload_json, 1, MAX_AUDIT_PREFIX).label(
                    "raw_prefix"
                ),
            )
            .where(AuctionMarketComparable.lot_id == lot_id)
            .order_by(
                AuctionMarketComparable.observed_at.desc(),
                AuctionMarketComparable.id.desc(),
            )
            .limit(MAX_ROWS + 1)
        )
    )
    history_truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    selected_ids: list[int] = []
    quarantines: dict[int, str] = {}
    aggregate_chars = 0
    budget_exhausted = False
    for row in rows:
        row_id = int(row.id)
        raw_length = int(row.raw_length or 2)
        if raw_length > MAX_RAW_BYTES:
            quarantines[row_id] = "raw_payload_too_large"
            continue
        if budget_exhausted or aggregate_chars + raw_length > MAX_TOTAL_BYTES:
            quarantines[row_id] = "aggregate_budget_exceeded"
            budget_exhausted = True
            continue
        aggregate_chars += raw_length
        selected_ids.append(row_id)

    raw_by_id: dict[int, str] = {}
    if selected_ids:
        raw_by_id = {
            int(row.id): str(row.bounded_raw or "{}")
            for row in session.execute(
                select(
                    AuctionMarketComparable.id,
                    func.substr(
                        AuctionMarketComparable.raw_payload_json, 1, MAX_RAW_BYTES + 1
                    ).label("bounded_raw"),
                ).where(AuctionMarketComparable.id.in_(selected_ids))
            )
        }

    aggregate_bytes = 0
    bounded_rows: list[_PersistedComparableRow] = []
    for row in rows:
        row_id = int(row.id)
        if not row.bounded_url or len(row.bounded_url) > MAX_URL:
            continue
        quarantine_reason = quarantines.get(row_id)
        raw = raw_by_id.get(row_id, "{}")
        raw_size = len(raw.encode("utf-8"))
        if quarantine_reason is None and raw_size > MAX_RAW_BYTES:
            quarantine_reason = "raw_payload_too_large"
            raw = "{}"
        if quarantine_reason is None and aggregate_bytes + raw_size > MAX_TOTAL_BYTES:
            quarantine_reason = "aggregate_budget_exceeded"
            raw = "{}"
        if quarantine_reason is None:
            aggregate_bytes += raw_size
        observed = _aware(row.observed_at)
        if observed is None:
            continue
        prefix = str(row.raw_prefix or "")
        fingerprint_material = (
            f"{int(row.raw_length or 0)}:{quarantine_reason or 'accepted'}:{prefix}"
            if quarantine_reason
            else raw
        )
        bounded_rows.append(
            _PersistedComparableRow(
                row_id=row_id,
                source_name=str(row.source_name),
                source_url=str(row.bounded_url),
                title=str(row.title),
                listing_status=str(row.listing_status),
                observed_at=observed,
                area_ha=row.area_ha,
                price_kzt=row.price_kzt,
                locality=row.locality,
                raw_payload_json=raw,
                raw_payload_fingerprint=hashlib.sha256(
                    fingerprint_material.encode("utf-8")
                ).hexdigest(),
                quarantine_reason=quarantine_reason,
            )
        )
    return tuple(bounded_rows), history_truncated


def _parse_persisted_comparable_rows(
    rows: Sequence[_PersistedComparableRow],
) -> tuple[PersistedComparableFact, ...]:
    facts: list[PersistedComparableFact] = []
    for row in rows:
        quarantine_reason = row.quarantine_reason
        payload: object = {}
        if quarantine_reason is None:
            try:
                payload = json.loads(row.raw_payload_json)
            except json.JSONDecodeError:
                quarantine_reason = "malformed_json"
                payload = {}
            if not isinstance(payload, dict):
                quarantine_reason = "payload_not_object"
                payload = {}
        facts.append(
            PersistedComparableFact(
                row_id=row.row_id,
                source_name=row.source_name,
                source_url=row.source_url,
                title=row.title,
                listing_status=row.listing_status,
                observed_at=row.observed_at,
                area_ha=row.area_ha,
                price_kzt=row.price_kzt,
                locality=row.locality,
                raw_payload=payload,
                raw_payload_fingerprint=row.raw_payload_fingerprint,
                quarantine_reason=quarantine_reason,
            )
        )
    return tuple(facts)


def load_persisted_comparable_facts(
    session: Session, lot_id: str
) -> tuple[PersistedComparableFact, ...]:
    """Convenience read API; worker orchestration parses after closing its session."""
    rows, _ = _load_persisted_comparable_rows(session, lot_id)
    return _parse_persisted_comparable_rows(rows)


def _provider_identity(fact: PersistedComparableFact) -> str:
    record_id = _bounded_text(fact.raw_payload.get("source_record_id"), 128)
    object_id = _bounded_text(fact.raw_payload.get("object_id"), 128)
    return object_id or record_id or fact.source_url


def _latest_bounded_evidence(session: Session, lot_id: str, evidence_type: str) -> object | None:
    return session.execute(
        select(
            AuctionEvidence.id,
            AuctionEvidence.source_id,
            AuctionEvidence.status,
            AuctionEvidence.value_text,
            func.substr(AuctionEvidence.raw_payload_json, 1, MAX_RAW_BYTES + 1).label(
                "bounded_payload"
            ),
        )
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type == evidence_type,
            AuctionEvidence.status.in_(("found", "conflict")),
        )
        .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
        .limit(1)
    ).one_or_none()


def _load_lot_projection(session: Session, lot_id: str) -> AuctionLot:
    row = session.execute(
        select(
            AuctionLot.id,
            AuctionLot.updated_at,
            AuctionLot.status,
            AuctionLot.source_search_status,
            AuctionLot.land_rights,
            AuctionLot.purpose,
            AuctionLot.title,
            AuctionLot.use_goal,
            AuctionLot.functional_purpose_level2,
            AuctionLot.functional_purpose_level3,
            AuctionLot.functional_purpose_level4,
            AuctionLot.lease_term_years,
            AuctionLot.auction_starts_at,
            AuctionLot.published_at,
            AuctionLot.area_ha,
            AuctionLot.start_price_kzt,
            AuctionLot.sale_price_kzt,
            AuctionLot.region,
            AuctionLot.district,
            AuctionLot.locality,
            AuctionLot.land_object_ref_id,
        ).where(AuctionLot.id == lot_id)
    ).one_or_none()
    if row is None:
        raise MarketEstimateStoreError("auction lot not found")
    return AuctionLot(**dict(row._mapping))


def load_authoritative_target_facts(
    session: Session, lot_id: str, *, lot: AuctionLot | None = None
) -> AuthoritativeTargetFacts:
    lot = lot or _load_lot_projection(session, lot_id)
    site = _latest_bounded_evidence(session, lot_id, "decision_input:site_context")
    cadastre = _latest_bounded_evidence(session, lot_id, "cadastre_boundary")
    legal = _latest_bounded_evidence(session, lot_id, "source_object_card")
    canonical = (
        session.execute(
            select(
                AuctionLandObject.id,
                AuctionLandObject.boundary_geojson,
                AuctionLandObject.boundary_source,
            )
            .where(AuctionLandObject.id == lot.land_object_ref_id)
            .limit(1)
        ).one_or_none()
        if lot.land_object_ref_id
        else None
    )

    def payload(row: object | None, *, worker_owned: bool = False) -> str | None:
        if row is None or row.bounded_payload is None:
            return None
        raw = str(row.bounded_payload)
        if len(raw.encode("utf-8")) > MAX_RAW_BYTES:
            return None
        if worker_owned and not (
            row.status == "found"
            and row.source_id is None
            and isinstance(row.value_text, str)
            and len(row.value_text) == 64
            and all(character in "0123456789abcdef" for character in row.value_text.casefold())
        ):
            return None
        return raw

    # Keep the single-target path identical to the bounded batch loader: reconcile
    # every populated purpose claim rather than stopping at a generic first value.
    purpose = " | ".join(
        str(value).strip()
        for value in (
            lot.purpose,
            lot.use_goal,
            lot.functional_purpose_level4,
            lot.title,
        )
        if value and str(value).strip()
    )
    return AuthoritativeTargetFacts(
        lot_id=lot.id,
        updated_at=_aware(lot.updated_at) or datetime.now(UTC),
        land_rights=lot.land_rights,
        lease_term_years=lot.lease_term_years,
        purpose=purpose[:4_000],
        area_ha=lot.area_ha,
        locality=lot.locality,
        site_evidence_id=int(site.id) if site and payload(site, worker_owned=True) else None,
        site_payload_json=payload(site, worker_owned=True),
        cadastre_evidence_id=int(cadastre.id) if cadastre else None,
        cadastre_status=str(cadastre.status) if cadastre else None,
        cadastre_payload_json=payload(cadastre),
        legal_evidence_id=int(legal.id) if legal else None,
        legal_status=str(legal.status) if legal else None,
        legal_payload_json=payload(legal),
        canonical_object_id=str(canonical.id) if canonical else None,
        canonical_boundary_geojson=(
            str(canonical.boundary_geojson)
            if canonical and canonical.boundary_geojson
            else None
        ),
        canonical_boundary_source=(
            str(canonical.boundary_source)
            if canonical and canonical.boundary_source
            else None
        ),
    )


def load_authoritative_target_facts_batch(
    session: Session, lot_ids: Sequence[str]
) -> tuple[AuthoritativeTargetFacts, ...]:
    """Load <=100 targets and their newest three evidence types in two bounded queries."""
    if not 0 < len(lot_ids) <= 100 or len(set(lot_ids)) != len(lot_ids):
        raise MarketEstimateStoreError("invalid target batch")
    lots = session.execute(
        select(
            AuctionLot.id,
            AuctionLot.updated_at,
            AuctionLot.land_rights,
            AuctionLot.lease_term_years,
            AuctionLot.purpose,
            AuctionLot.use_goal,
            AuctionLot.functional_purpose_level4,
            AuctionLot.title,
            AuctionLot.area_ha,
            AuctionLot.locality,
            AuctionLandObject.id.label("canonical_object_id"),
            AuctionLandObject.boundary_geojson.label("canonical_boundary_geojson"),
            AuctionLandObject.boundary_source.label("canonical_boundary_source"),
        )
        .outerjoin(AuctionLandObject, AuctionLandObject.id == AuctionLot.land_object_ref_id)
        .where(AuctionLot.id.in_(lot_ids))
    ).all()
    evidence_types = (
        "decision_input:site_context",
        "cadastre_boundary",
        "source_object_card",
    )
    ranked = (
        select(
            AuctionEvidence.id.label("id"),
            AuctionEvidence.lot_id.label("lot_id"),
            AuctionEvidence.evidence_type.label("evidence_type"),
            AuctionEvidence.status.label("status"),
            AuctionEvidence.source_id.label("source_id"),
            AuctionEvidence.value_text.label("value_text"),
            func.substr(AuctionEvidence.raw_payload_json, 1, MAX_RAW_BYTES + 1).label(
                "bounded_payload"
            ),
            func.row_number()
            .over(
                partition_by=(AuctionEvidence.lot_id, AuctionEvidence.evidence_type),
                order_by=(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc()),
            )
            .label("rn"),
        )
        .where(
            AuctionEvidence.lot_id.in_(lot_ids),
            AuctionEvidence.evidence_type.in_(evidence_types),
            AuctionEvidence.status.in_(("found", "conflict")),
        )
        .subquery()
    )
    evidence_rows = session.execute(select(ranked).where(ranked.c.rn == 1)).all()
    aggregate = 0
    evidence: dict[tuple[str, str], object] = {}
    for row in evidence_rows:
        raw = str(row.bounded_payload) if row.bounded_payload is not None else None
        size = len(raw.encode("utf-8")) if raw is not None else 0
        if size > MAX_RAW_BYTES or aggregate + size > MAX_TOTAL_BYTES:
            raw = None
        else:
            aggregate += size
        evidence[(str(row.lot_id), str(row.evidence_type))] = (row, raw)

    def read(lot_id: str, kind: str, *, worker_owned: bool = False):
        item = evidence.get((lot_id, kind))
        if item is None:
            return None, None
        row, raw = item
        if worker_owned and not (
            row.status == "found"
            and row.source_id is None
            and isinstance(row.value_text, str)
            and len(row.value_text) == 64
            and all(char in "0123456789abcdef" for char in row.value_text.casefold())
        ):
            return row, None
        return row, raw

    results: list[AuthoritativeTargetFacts] = []
    for lot in lots:
        site, site_raw = read(lot.id, "decision_input:site_context", worker_owned=True)
        cadastre, cadastre_raw = read(lot.id, "cadastre_boundary")
        legal, legal_raw = read(lot.id, "source_object_card")
        # A generic primary value must not hide a specific official use goal or
        # card title. Conflicting supported claims are rejected below.
        purpose = " | ".join(
            str(value).strip()
            for value in (
                lot.purpose,
                lot.use_goal,
                lot.functional_purpose_level4,
                lot.title,
            )
            if value and str(value).strip()
        )
        results.append(
            AuthoritativeTargetFacts(
                lot_id=lot.id,
                updated_at=_aware(lot.updated_at) or datetime.now(UTC),
                land_rights=lot.land_rights,
                lease_term_years=lot.lease_term_years,
                purpose=purpose[:4_000],
                area_ha=lot.area_ha,
                locality=lot.locality,
                site_evidence_id=int(site.id) if site and site_raw else None,
                site_payload_json=site_raw,
                cadastre_evidence_id=int(cadastre.id) if cadastre else None,
                cadastre_status=str(cadastre.status) if cadastre else None,
                cadastre_payload_json=cadastre_raw,
                legal_evidence_id=int(legal.id) if legal else None,
                legal_status=str(legal.status) if legal else None,
                legal_payload_json=legal_raw,
                canonical_object_id=(
                    str(lot.canonical_object_id) if lot.canonical_object_id else None
                ),
                canonical_boundary_geojson=(
                    str(lot.canonical_boundary_geojson)
                    if lot.canonical_boundary_geojson
                    else None
                ),
                canonical_boundary_source=(
                    str(lot.canonical_boundary_source)
                    if lot.canonical_boundary_source
                    else None
                ),
            )
        )
    return tuple(results)


def _parsed_object(raw: str | None) -> dict[str, object]:
    if raw is None:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _right_type(value: str | None) -> str | None:
    normalized = " ".join((value or "").casefold().split())
    if any(token in normalized for token in ("аренд", "землепольз")):
        return "lease"
    if any(token in normalized for token in ("собствен", "меншік")):
        return "ownership"
    return None


def _readiness(site: dict[str, object]) -> tuple[str, str]:
    def nested(name: str) -> str:
        value = site.get(name)
        return str(value.get("readiness")) if isinstance(value, dict) else "unknown"

    physical = nested("physical_access")
    legal = nested("legal_access")
    infrastructure = nested("infrastructure")
    if physical == legal == "ready":
        access = "ready"
    elif physical in {"ready", "partial"} and legal in {"ready", "partial"}:
        access = "partial"
    elif physical == "not_ready" or legal == "not_ready":
        access = "none"
    else:
        access = "unknown"
    infrastructure_map = {
        "ready": "ready",
        "partial": "partial",
        "not_ready": "none",
    }
    return access, infrastructure_map.get(infrastructure, "unknown")


def build_authoritative_market_target(
    facts: AuthoritativeTargetFacts, *, valuation_at: datetime
) -> AuthoritativeTargetBuild:
    checked = _aware(valuation_at)
    if checked is None:
        raise MarketEstimateStoreError("valuation_at must be timezone-aware")
    site = _parsed_object(facts.site_payload_json)
    cadastre = _parsed_object(facts.cadastre_payload_json)
    legal = _parsed_object(facts.legal_payload_json)
    missing: list[str] = []
    right_type = _right_type(facts.land_rights)
    purpose_claims = classify_scenario_claims(facts.purpose)
    purpose_group = purpose_claims[0] if len(purpose_claims) == 1 else "other"
    area = _finite(facts.area_ha)
    access, infrastructure = _readiness(site)
    if right_type is None:
        missing.append("right_type_unknown")
    if len(purpose_claims) > 1:
        missing.append("purpose_group_conflict")
    elif not purpose_claims:
        missing.append("purpose_group_unknown")
    if area is None or not 0.0001 <= area <= 1_000_000:
        missing.append("area_unknown")
        area = 0.0
    if access == "unknown":
        missing.append("access_readiness_unknown")
    if infrastructure == "unknown":
        missing.append("infrastructure_readiness_unknown")
    raw_conflicts = legal.get("conflicts")
    if facts.legal_status == "conflict" and isinstance(raw_conflicts, list):
        conflict_fields = {
            str(item.get("field")) for item in raw_conflicts if isinstance(item, dict)
        }
        if conflict_fields.intersection(
            {"land_rights", "right_type", "lease_term_years", "purpose", "use_goal"}
        ):
            missing.append("legal_target_conflict")
    elif facts.legal_status == "conflict":
        missing.append("legal_target_conflict")
    latitude = longitude = None
    geometry = cadastre.get("geometry_geojson")
    geometry_ref: str | None = None
    if facts.cadastre_status == "found" and isinstance(geometry, dict):
        analysis = analyze_parcel_geometry(geometry)
        if analysis.status == "ok":
            centroid = shape(geometry).centroid
            longitude, latitude = float(centroid.x), float(centroid.y)
            geometry_ref = f"auction_evidence:{facts.cadastre_evidence_id}"
    if latitude is None and facts.canonical_boundary_geojson:
        try:
            canonical_geometry = json.loads(facts.canonical_boundary_geojson)
        except (TypeError, json.JSONDecodeError):
            canonical_geometry = None
        if isinstance(canonical_geometry, dict):
            analysis = analyze_parcel_geometry(canonical_geometry)
            if analysis.status == "ok":
                centroid = shape(canonical_geometry).centroid
                longitude, latitude = float(centroid.x), float(centroid.y)
                geometry_ref = f"auction_land_object:{facts.canonical_object_id}"
    if latitude is None and not facts.locality:
        missing.append("location_unknown")
    if right_type == "lease" and _finite(facts.lease_term_years) is None:
        missing.append("lease_term_unknown")
    target = ComparableTarget(
        target_id=facts.lot_id,
        right_type=right_type or "unknown",  # type: ignore[arg-type]
        purpose_group=purpose_group,
        area_ha=area,
        valuation_at=checked,
        locality=facts.locality,
        latitude=latitude,
        longitude=longitude,
        lease_term_years=facts.lease_term_years,
        access_readiness=access,
        infrastructure_readiness=infrastructure,
    )
    generation_payload = {
        "lot_id": facts.lot_id,
        "lot_updated_at": facts.updated_at.isoformat(),
        "site_evidence_id": facts.site_evidence_id,
        "cadastre_evidence_id": facts.cadastre_evidence_id,
        "legal_evidence_id": facts.legal_evidence_id,
        "target": {
            **asdict(target),
            "valuation_at": target.valuation_at.isoformat(),
        },
        "missing": sorted(set(missing)),
    }
    generation_json = _strict_json(
        generation_payload, limit=MAX_OUTPUT_BYTES, label="market target generation"
    )
    refs = [f"auction_lot:{facts.lot_id}"]
    refs.extend(
        f"auction_evidence:{value}"
        for value in (
            facts.site_evidence_id,
            facts.cadastre_evidence_id,
            facts.legal_evidence_id,
        )
        if value is not None
    )
    if geometry_ref and geometry_ref not in refs:
        refs.append(geometry_ref)
    return AuthoritativeTargetBuild(
        status="ready" if not missing else "insufficient",
        target=target,
        generation_signature=hashlib.sha256(generation_json.encode("utf-8")).hexdigest(),
        provenance_refs=tuple(refs),
        missing_reasons=tuple(sorted(set(missing))),
    )


def build_candidate_set(
    facts: Sequence[PersistedComparableFact], *, history_truncated: bool = False
) -> CandidateSet:
    if len(facts) > MAX_ROWS:
        raise MarketEstimateStoreError("too many comparable facts")
    ordered = sorted(facts, key=lambda item: (item.observed_at, item.row_id), reverse=True)
    newest: dict[tuple[str, str], PersistedComparableFact] = {}
    for fact in ordered:
        key = (fact.source_name.casefold(), _provider_identity(fact))
        newest.setdefault(key, fact)
    candidates: list[ComparableCandidate] = []
    rejected: list[dict[str, object]] = []
    provenance: list[str] = []
    generation_rows: list[tuple[object, ...]] = []
    for key in sorted(newest):
        fact = newest[key]
        payload = dict(fact.raw_payload)
        evidence_ref = f"market_comparable:{fact.row_id}"
        provenance.append(evidence_ref)
        generation_rows.append(
            (
                fact.row_id,
                fact.source_name,
                hashlib.sha256(key[1].encode("utf-8")).hexdigest(),
                fact.listing_status,
                fact.observed_at.isoformat(),
                payload.get("verification_status"),
                payload.get("price_kind"),
                fact.raw_payload_fingerprint,
                fact.quarantine_reason,
            )
        )
        if fact.quarantine_reason is not None:
            rejected.append({"row_id": fact.row_id, "reason": fact.quarantine_reason})
            continue
        if fact.listing_status.casefold() in {"conflict", "error", "withdrawn"} or payload.get(
            "status"
        ) in {"conflict", "error"}:
            rejected.append({"row_id": fact.row_id, "reason": "newest_record_conflict"})
            continue
        price_kind = payload.get("price_kind", "listing")
        verified_sale = price_kind == "verified_sale"
        verification_ref = _bounded_text(payload.get("verification_source_ref"), 240)
        if verified_sale and not (
            payload.get("verification_status") == "verified" and verification_ref
        ):
            rejected.append({"row_id": fact.row_id, "reason": "sale_not_verified"})
            continue
        if price_kind not in {"verified_sale", "listing"}:
            rejected.append({"row_id": fact.row_id, "reason": "invalid_price_kind"})
            continue
        source_record_id = _bounded_text(payload.get("source_record_id"), 128) or str(fact.row_id)
        right_type = payload.get("right_type")
        purpose_group = _bounded_text(payload.get("purpose_group"), 160)
        area = _finite(payload.get("area_ha")) or _finite(fact.area_ha)
        price = _finite(payload.get("price_kzt")) or _finite(fact.price_kzt)
        if right_type not in {"ownership", "lease"} or purpose_group is None:
            rejected.append({"row_id": fact.row_id, "reason": "missing_strict_dimensions"})
            continue
        candidates.append(
            ComparableCandidate(
                source_id=_bounded_text(fact.source_name, 128) or "invalid",
                source_record_id=source_record_id,
                source_url=fact.source_url,
                title=_bounded_text(fact.title, MAX_TEXT) or "Comparable",
                right_type=right_type,
                purpose_group=purpose_group,
                area_ha=area if area is not None else float("nan"),
                price_kzt=price if price is not None else float("nan"),
                price_kind=price_kind,
                observed_at=fact.observed_at,
                locality=_bounded_text(payload.get("locality") or fact.locality, 160),
                latitude=_finite(payload.get("latitude")),
                longitude=_finite(payload.get("longitude")),
                lease_term_years=_finite(payload.get("lease_term_years")),
                access_readiness=payload.get("access_readiness", "unknown"),
                infrastructure_readiness=payload.get("infrastructure_readiness", "unknown"),
                object_id=_bounded_text(payload.get("object_id"), 128),
            )
        )
        if verification_ref:
            provenance.append(verification_ref)
    generation_rows.append(("history_truncated", bool(history_truncated)))
    generation_json = _strict_json(
        generation_rows, limit=MAX_OUTPUT_BYTES, label="market source generation"
    )
    return CandidateSet(
        candidates=tuple(candidates),
        source_generation_signature=hashlib.sha256(generation_json.encode("utf-8")).hexdigest(),
        current_row_ids=tuple(sorted(fact.row_id for fact in newest.values())),
        rejected=tuple(rejected),
        provenance_refs=tuple(dict.fromkeys(provenance)),
        history_truncated=bool(history_truncated),
    )


def load_history_audit_reference(session: Session, lot: AuctionLot) -> HistoryAuditReference:
    generation, aggregate = normalized_similar_history(session, lot)
    generation_model = (
        session.get(AuctionHistoryGeneration, generation) if generation is not None else None
    )
    observed = _aware(generation_model.activated_at) if generation_model else None
    signature = None
    if generation_model is not None:
        signature_raw = ":".join(
            (
                str(generation_model.generation),
                generation_model.normalization_version,
                (
                    _aware(generation_model.source_cutoff).isoformat()
                    if _aware(generation_model.source_cutoff)
                    else "unknown"
                ),
            )
        )
        signature = hashlib.sha256(signature_raw.encode("utf-8")).hexdigest()
    return HistoryAuditReference(
        status=aggregate.status,
        generation=generation,
        generation_signature=signature,
        payload=asdict(aggregate),
        observed_at=observed,
    )


def _compact_result(result: MarketComparableResult) -> dict[str, object]:
    evaluations = [
        {
            "source_id": item.source_id,
            "source_record_id": item.source_record_id,
            "price_kind": item.price_kind,
            "observed_at": item.observed_at.isoformat() if item.observed_at else None,
            "eligible": item.eligible,
            "exclusion_reason": item.exclusion_reason,
            "quality_grade": item.quality_grade,
        }
        for item in result.evaluations
    ]
    return {
        "status": result.status,
        "estimate": asdict(result.estimate) if result.estimate else None,
        "confidence": result.confidence,
        "high_quality_verified_count": result.high_quality_verified_count,
        "verified_eligible_count": result.verified_eligible_count,
        "listing_eligible_count": result.listing_eligible_count,
        "evaluations": evaluations,
        "detail": result.detail,
        "engine_version": result.engine_version,
    }


def calculate_market_evidence(
    target: ComparableTarget,
    candidate_set: CandidateSet,
    *,
    history: HistoryAuditReference | None = None,
    target_build: AuthoritativeTargetBuild | None = None,
    market_target_signature: str | None = None,
    global_geo_selection_performed: bool = False,
) -> MarketEvidenceMaterial:
    result = build_strict_market_comparables(target, candidate_set.candidates)
    accepted = (
        result.status == "ok"
        and result.estimate is not None
        and result.high_quality_verified_count >= 3
        and (target_build is None or target_build.status == "ready")
    )
    compact = _compact_result(result)
    if not accepted:
        compact["status"] = "insufficient_data"
        compact["estimate"] = None
        compact["confidence"] = "none"
    used_observations = [
        item.observed_at
        for item in result.evaluations
        if item.eligible
        and item.quality_grade == "A"
        and item.price_kind == "verified_sale"
        and item.observed_at is not None
    ]
    oldest_used = min(used_observations, default=None)
    history_payload = (
        {
            "status": history.status,
            "generation": history.generation,
            "generation_signature": history.generation_signature,
            "observed_at": history.observed_at.isoformat() if history.observed_at else None,
            "aggregate": history.payload,
            "audit_only": True,
        }
        if history
        else {
            "status": "insufficient_data",
            "generation": None,
            "generation_signature": None,
            "observed_at": None,
            "aggregate": {},
            "audit_only": True,
        }
    )
    target_payload = asdict(target)
    target_payload["valuation_at"] = target.valuation_at.isoformat()
    payload = {
        **compact,
        "writer_version": WRITER_VERSION,
        "source_generation_signature": candidate_set.source_generation_signature,
        "current_source_row_ids": list(candidate_set.current_row_ids),
        "rejected_source_rows": list(candidate_set.rejected),
        "oldest_used_at": oldest_used.isoformat() if oldest_used else None,
        "provenance_refs": list(candidate_set.provenance_refs),
        "history_reference": history_payload,
        "source_history_truncated": candidate_set.history_truncated,
        "inventory_scope": {
            "kind": (
                "global_verified_comparable_inventory"
                if global_geo_selection_performed
                else "lot_scoped_candidate_inventory"
            ),
            "provider_ingest_performed": False,
            "global_geo_selection_performed": global_geo_selection_performed,
        },
        "target": target_payload,
        "target_generation_signature": (
            target_build.generation_signature if target_build else None
        ),
        "target_status": target_build.status if target_build else "provided_pure_input",
        "target_missing_reasons": (list(target_build.missing_reasons) if target_build else []),
        "market_target_signature": market_target_signature,
        "target_provenance_refs": (
            list(target_build.provenance_refs)
            if target_build
            else [f"auction_lot:{target.target_id}"]
        ),
    }
    payload_json = _strict_json(payload, limit=MAX_OUTPUT_BYTES, label="market evidence")
    return MarketEvidenceMaterial(
        payload=json.loads(payload_json),
        payload_json=payload_json,
        input_hash=hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        status=str(payload["status"]),
        oldest_used_at=oldest_used,
    )


def persist_market_evidence(
    session_factory: Callable[[], Session],
    lot_id: str,
    material: MarketEvidenceMaterial,
    *,
    observed_at: datetime | None = None,
    expected_target_signature: str | None = None,
) -> MarketEvidenceWriteResult:
    checked = _aware(observed_at) or datetime.now(UTC)
    with session_factory() as dialect_session:
        sqlite = dialect_session.get_bind().dialect.name == "sqlite"
    lock = _SQLITE_LOCK if sqlite else None
    if lock:
        lock.acquire()
    try:
        with session_factory() as session, session.begin():
            if session.get_bind().dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"strict-market-estimate:{lot_id}"},
                )
            lot_exists = session.scalar(
                select(AuctionLot.id).where(AuctionLot.id == lot_id).with_for_update()
            )
            if lot_exists is None:
                raise MarketEstimateStoreError("auction lot not found")
            if expected_target_signature is not None:
                from app.auction_market_dirty_state import MarketTargetInput, target_signature

                lot = session.get(AuctionLot, lot_id)
                guarded_facts = load_authoritative_target_facts(session, lot_id, lot=lot)
                guarded = build_authoritative_market_target(
                    guarded_facts, valuation_at=checked
                ).target
                guarded_input = MarketTargetInput(
                    lot_id=lot_id,
                    right_type=(
                        None
                        if guarded.right_type == "lease" and guarded.lease_term_years is None
                        else (
                            guarded.right_type
                            if guarded.right_type in {"ownership", "lease"}
                            else None
                        )
                    ),
                    purpose_group=guarded.purpose_group,
                    lease_term_years=guarded.lease_term_years,
                    area_ha=guarded.area_ha if guarded.area_ha > 0 else None,
                    latitude=guarded.latitude,
                    longitude=guarded.longitude,
                    access_readiness=guarded.access_readiness,
                    infrastructure_readiness=guarded.infrastructure_readiness,
                    canonical_object_id=(
                        lot.land_object_ref_id or lot.land_object_id or lot.cadastre_number
                    ),
                    source_sale_id=(lot.source_lot_id if lot.source == "e-qazyna" else None),
                )
                if target_signature(guarded_input) != expected_target_signature:
                    raise MarketEstimateStoreError("market target changed before persist")
            existing = session.execute(
                select(AuctionEvidence.id, AuctionEvidence.value_text)
                .where(
                    AuctionEvidence.lot_id == lot_id,
                    AuctionEvidence.evidence_type == EVIDENCE_TYPE,
                )
                .order_by(AuctionEvidence.id.desc())
                .limit(1)
            ).one_or_none()
            if existing is not None and existing.value_text == material.input_hash:
                evidence_id = existing.id
                changed = False
            else:
                evidence = AuctionEvidence(
                    lot_id=lot_id,
                    source_id=None,
                    evidence_type=EVIDENCE_TYPE,
                    status="found",
                    title="Строгая рыночная оценка W9",
                    value_text=material.input_hash,
                    source_url=None,
                    confidence=(
                        0.9
                        if material.payload.get("confidence") == "high"
                        else 0.75
                        if material.payload.get("confidence") == "medium"
                        else 0.0
                    ),
                    raw_payload_json=material.payload_json,
                    observed_at=checked,
                )
                session.add(evidence)
                session.flush()
                evidence_id = evidence.id
                changed = True
    finally:
        if lock:
            lock.release()
    return MarketEvidenceWriteResult(
        lot_id=lot_id,
        changed=changed,
        evidence_id=evidence_id,
        input_hash=material.input_hash,
        status=material.status,
    )


def recompute_market_evidence(
    session_factory: Callable[[], Session],
    lot_id: str,
    *,
    observed_at: datetime | None = None,
) -> MarketEvidenceWriteResult:
    """Read bounded facts, close the session, calculate, then open a short write tx."""
    with session_factory() as read_session:
        lot = _load_lot_projection(read_session, lot_id)
        persisted_rows, history_truncated = _load_persisted_comparable_rows(read_session, lot_id)
        history = load_history_audit_reference(read_session, lot)
        target_facts = load_authoritative_target_facts(read_session, lot_id, lot=lot)
    facts = _parse_persisted_comparable_rows(persisted_rows)
    candidate_set = build_candidate_set(facts, history_truncated=history_truncated)
    target_build = build_authoritative_market_target(
        target_facts,
        valuation_at=_aware(observed_at) or datetime.now(UTC),
    )
    material = calculate_market_evidence(
        target_build.target,
        candidate_set,
        history=history,
        target_build=target_build,
    )
    return persist_market_evidence(session_factory, lot_id, material, observed_at=observed_at)
