"""Worker-only bounded assembly and persistence of versioned auction decision inputs."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auction_actual_cost_writer import (
    POLICY_VERSION as ACTUAL_COST_POLICY_VERSION,
)
from app.auction_actual_cost_writer import (
    SCENARIO_HORIZON_MONTHS,
    STANDARD_INVESTMENT_POLICY_VERSION,
)
from app.auction_actual_cost_writer import (
    WRITER_VERSION as ACTUAL_COST_WRITER_VERSION,
)
from app.auction_decision_input import (
    ASSEMBLER_VERSION,
    POLICY_VERSION,
    ContractCoverage,
    DecisionInputAssembly,
    DecisionLotFacts,
    EvidenceArtifact,
    assemble_decision_input,
)
from app.auction_history_read import normalized_similar_history
from app.auction_market_dirty_state import MarketTargetInput, target_signature
from app.auction_market_estimate_store import (
    build_authoritative_market_target,
    load_authoritative_target_facts,
)
from app.auction_spatial_decision_input import (
    ASSEMBLER_VERSION as SPATIAL_ASSEMBLER_VERSION,
)
from app.auction_spatial_decision_input import (
    SpatialEvidenceInput,
    assemble_spatial_decision_inputs,
    load_spatial_evidence,
)
from app.auction_taxonomy import (
    UNCLASSIFIED_SCENARIO,
    classify_scenario,
    select_decision_scenario,
)
from app.models import (
    AuctionDecisionInputState,
    AuctionDocument,
    AuctionEvidence,
    AuctionHistoryGeneration,
    AuctionLot,
    AuctionMarketComparable,
)

STORE_VERSION = "decision-input-store/2026.2"
UPSTREAM_EVIDENCE_TYPES = (
    "source_object_card",
    "official_lot",
    "official_document_summary",
    "akimat_announcement",
    "cadastre_boundary",
    "egkn_context_layer",
    "document_extraction",
    "strict_market_estimate",
    "decision_cost_ranges",
    "decision_cost_ranges_sources",
)
UPSTREAM_STATUSES = ("found", "conflict")
EXTRACTION_TYPE = "document_extraction"
MAX_WORKLIST = 100
MAX_UPSTREAM_ROWS = 48
MAX_DOCUMENTS = 20
MAX_ITEM_BYTES = 64_000
MAX_AGGREGATE_BYTES = 1_000_000
MAX_ERROR_CHARS = 500
ACTUAL_COST_SOURCE_TYPE = "decision_cost_ranges_sources"
ACTUAL_COST_TYPE = "decision_cost_ranges"
CLAIM_TTL = timedelta(minutes=10)
MAX_RETRY_COUNT = 20
MAX_RETRY_SECONDS = 6 * 60 * 60
RECOVERY_REVALIDATE_AFTER = timedelta(hours=24)

_SQLITE_LOCK = threading.Lock()


class DecisionInputStoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LotScalar:
    id: str
    source_lot_id: str
    updated_at: datetime
    start_price_kzt: float | None
    purpose: str
    land_rights: str | None
    lease_term_years: float | None
    guarantee_kzt: float | None
    additional_payment_kzt: float | None
    annual_rent_kzt: float | None


@dataclass(frozen=True, slots=True)
class ReadBundle:
    lot: LotScalar
    spatial: dict[str, SpatialEvidenceInput]
    legal_passport: dict[str, object]
    contract_extractions: tuple[dict[str, object], ...]
    contract_coverage: ContractCoverage | None
    history_generation: int | None
    history_payload: dict[str, object]
    history_observed_at: datetime | None
    market_result: dict[str, object] | None
    actual_cost_ranges: dict[str, object] | None
    market_signature: str
    market_watermark_id: int
    market_watermark_at: datetime | None
    market_row_count: int
    document_signature: str
    document_watermark_id: int
    document_watermark_at: datetime | None
    document_row_count: int
    source_watermark_id: int
    source_watermark_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecomputeResult:
    lot_id: str
    status: str
    changed: bool
    input_hash: str | None
    evidence_ids: tuple[int, ...] = ()
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _BoundedEvidenceRow:
    id: int
    status: str
    observed_at: datetime | None
    value_text: str | None
    bounded_payload: str | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _strict_json(value: object, *, max_bytes: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionInputStoreError(f"{label} is not strict JSON") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise DecisionInputStoreError(f"{label} exceeds byte budget")
    return encoded


def _bounded_payload(raw: str | None, *, label: str) -> dict[str, object] | None:
    if raw is None:
        return None
    if len(raw.encode("utf-8")) > MAX_ITEM_BYTES:
        raise DecisionInputStoreError(f"{label} exceeds byte budget")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    _strict_json(value, max_bytes=MAX_ITEM_BYTES, label=label)
    return value


def _fit_upstream_rows(rows: list[object]) -> list[object]:
    """Bound history without dropping the newest row of any evidence type."""
    pinned_ids: set[int] = set()
    seen_types: set[str] = set()
    for row in rows:
        evidence_type = str(row.evidence_type)
        if evidence_type not in seen_types:
            seen_types.add(evidence_type)
            pinned_ids.add(int(row.id))

    selected_ids = set(pinned_ids)
    used_bytes = sum(
        len((row.bounded_payload or "").encode("utf-8"))
        for row in rows
        if int(row.id) in pinned_ids
    )
    for row in rows:
        row_id = int(row.id)
        if row_id in selected_ids:
            continue
        row_bytes = len((row.bounded_payload or "").encode("utf-8"))
        if used_bytes + row_bytes <= MAX_AGGREGATE_BYTES:
            selected_ids.add(row_id)
            used_bytes += row_bytes
    return [row for row in rows if int(row.id) in selected_ids]


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _decision_scenario_for_purpose(purpose: str) -> str:
    return select_decision_scenario(purpose).scenario_key or UNCLASSIFIED_SCENARIO


def _latest_cost_evidence(
    session: Session,
    *,
    lot_id: str,
    evidence_type: str,
) -> _BoundedEvidenceRow | None:
    row = session.execute(
        select(
            AuctionEvidence.id,
            AuctionEvidence.status,
            AuctionEvidence.observed_at,
            func.substr(AuctionEvidence.value_text, 1, 1_001).label("bounded_value"),
            func.substr(AuctionEvidence.raw_payload_json, 1, MAX_ITEM_BYTES + 1).label(
                "bounded_payload"
            ),
        )
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type == evidence_type,
        )
        .order_by(AuctionEvidence.id.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    if len((row.bounded_value or "").encode("utf-8")) > 1_000:
        return None
    if len((row.bounded_payload or "").encode("utf-8")) > MAX_ITEM_BYTES:
        return None
    return _BoundedEvidenceRow(
        int(row.id),
        str(row.status),
        _aware(row.observed_at),
        row.bounded_value,
        row.bounded_payload,
    )


def _paired_actual_cost_ranges(
    session: Session,
    *,
    lot_id: str,
    purpose: str,
) -> dict[str, object] | None:
    """Accept only the latest exact writer/manifest pair; legacy rows fail closed."""
    costs_row = _latest_cost_evidence(session, lot_id=lot_id, evidence_type=ACTUAL_COST_TYPE)
    sources_row = _latest_cost_evidence(
        session, lot_id=lot_id, evidence_type=ACTUAL_COST_SOURCE_TYPE
    )
    if (
        costs_row is None
        or sources_row is None
        or costs_row.status != "found"
        or sources_row.status != "found"
    ):
        return None
    costs = _bounded_payload(costs_row.bounded_payload, label="actual cost ranges")
    manifest = _bounded_payload(sources_row.bounded_payload, label="actual cost source manifest")
    if costs is None or manifest is None:
        return None
    marker_parts = (costs_row.value_text or "").split(":")
    if (
        len(marker_parts) != 6
        or marker_parts[0] != "idempotency"
        or marker_parts[1] != ACTUAL_COST_WRITER_VERSION
        or marker_parts[2] != ACTUAL_COST_POLICY_VERSION
        or not _SHA256.fullmatch(marker_parts[3])
        or marker_parts[4] not in {"complete", "incomplete", "insufficient_data"}
        or not _SHA256.fullmatch(marker_parts[5])
    ):
        return None
    generation_id = marker_parts[3]
    scenario_key = _decision_scenario_for_purpose(purpose)
    horizon = SCENARIO_HORIZON_MONTHS.get(scenario_key)
    if (
        horizon is None
        or sources_row.value_text != f"generation:{generation_id}"
        or manifest.get("writer_version") != ACTUAL_COST_WRITER_VERSION
        or manifest.get("policy_version") != ACTUAL_COST_POLICY_VERSION
        or manifest.get("investment_policy_version") != STANDARD_INVESTMENT_POLICY_VERSION
        or manifest.get("generation_id") != generation_id
        or manifest.get("scenario_key") != scenario_key
        or manifest.get("holding_horizon_months") != horizon
    ):
        return None
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 64:
        return None
    raw_refs = costs.get("provenance_refs")
    if not isinstance(raw_refs, list) or len(raw_refs) > 100:
        return None
    cost_refs = {item for item in raw_refs if isinstance(item, str)}
    for item in sources:
        if (
            not isinstance(item, dict)
            or item.get("target_lot_id") != lot_id
            or item.get("scenario_key") != scenario_key
            or item.get("investment_policy_version") != STANDARD_INVESTMENT_POLICY_VERSION
            or item.get("holding_horizon_months") != horizon
            or item.get("currency") != "KZT"
            or item.get("status") != "found"
            or item.get("freshness_status") != "fresh"
            or item.get("source_ref") not in cost_refs
            or item.get("source_url") not in cost_refs
        ):
            return None
    paired = dict(costs)
    refs = [item for item in (raw_refs or []) if isinstance(item, str)]
    refs.extend((f"auction_evidence:{costs_row.id}", f"auction_evidence:{sources_row.id}"))
    paired["provenance_refs"] = list(dict.fromkeys(refs))[:100]
    return paired


def _fact(
    key: str,
    value: object,
    *,
    observed_at: datetime,
    evidence_id: int | None,
    evidence_type: str,
    forced_status: str | None = None,
) -> dict[str, object]:
    found = value is not None and value != ""
    source_ref = f"auction_evidence:{evidence_id}" if evidence_id else "auction_lot:canonical"
    return {
        "key": key,
        "value": value if found else None,
        "status": forced_status or ("found" if found else "unknown"),
        "source_url": None,
        "observed_at": observed_at.isoformat(),
        "confidence": 0.98 if evidence_id else 0.8,
        "provenance": [
            {
                "source_url": source_ref,
                "observed_at": observed_at.isoformat(),
                "confidence": 0.98 if evidence_id else 0.8,
                "evidence_type": evidence_type,
                "evidence_id": evidence_id,
            }
        ],
        "version": "legal-passport.worker.v1",
    }


def _payment_fact(
    key: str,
    amount: float | None,
    treatment: str,
    frequency: str,
    *,
    observed_at: datetime,
) -> dict[str, object]:
    exact = (
        int(amount)
        if isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and float(amount).is_integer()
        and 0 <= amount <= 10**15
        else None
    )
    value = (
        {
            "amount_kzt": exact,
            "cost_treatment": treatment,
            "frequency": frequency,
        }
        if exact is not None
        else None
    )
    return _fact(
        key,
        value,
        observed_at=observed_at,
        evidence_id=None,
        evidence_type="auction_lot",
    )


def _right_type(land_rights: str | None) -> str | None:
    normalized = " ".join((land_rights or "").casefold().split())
    if any(token in normalized for token in ("аренд", "землепольз")):
        return "lease"
    if any(token in normalized for token in ("собствен", "меншік")):
        return "ownership"
    return None


def _legal_passport(
    lot: LotScalar,
    source_payload: dict[str, object] | None,
    source_evidence_id: int | None,
    source_observed_at: datetime | None,
    source_conflicts: tuple[str, ...] = (),
) -> dict[str, object]:
    payload = source_payload or {}
    observed = source_observed_at or lot.updated_at
    rights = payload.get("land_rights") or lot.land_rights
    lease = payload.get("lease_term_years") or lot.lease_term_years
    purpose = payload.get("purpose") or lot.purpose
    conflict_fields = set(source_conflicts)
    unknown_conflict = "*" in conflict_fields

    def status_for(*fields: str) -> str | None:
        return "conflict" if unknown_conflict or conflict_fields.intersection(fields) else None

    return {
        "lot_id": lot.id,
        "source_lot_id": lot.source_lot_id,
        "generated_at": observed.isoformat(),
        "version": "legal-passport.worker.v1",
        "facts": {
            "right_type": _fact(
                "right_type",
                _right_type(str(rights) if rights else None),
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card" if source_evidence_id else "auction_lot",
                forced_status=status_for("land_rights", "right_type"),
            ),
            "lease_term_years": _fact(
                "lease_term_years",
                lease,
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card",
                forced_status=status_for("lease_term_years"),
            ),
            "purpose": _fact(
                "purpose",
                purpose,
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card",
                forced_status=status_for("purpose", "use_goal"),
            ),
            "arrests": _fact(
                "arrests",
                payload.get("arrests_text"),
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card",
                forced_status=status_for("arrests_text", "arrests"),
            ),
            "restrictions": _fact(
                "restrictions",
                payload.get("restrictions_text"),
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card",
                forced_status=status_for("restrictions_text", "restrictions"),
            ),
            "encumbrances": _fact(
                "encumbrances",
                payload.get("encumbrances_text"),
                observed_at=observed,
                evidence_id=source_evidence_id,
                evidence_type="source_object_card",
                forced_status=status_for("encumbrances_text", "encumbrances"),
            ),
        },
        "payments": {
            "guarantee": _payment_fact(
                "guarantee",
                lot.guarantee_kzt,
                "blocked_capital",
                "once_before_auction",
                observed_at=observed,
            ),
            "additional_payment": _payment_fact(
                "additional_payment",
                lot.additional_payment_kzt,
                "expense",
                "once_after_win",
                observed_at=observed,
            ),
            "annual_rent": _payment_fact(
                "annual_rent",
                lot.annual_rent_kzt,
                "expense",
                "annual",
                observed_at=observed,
            ),
        },
        "documents": [],
    }


def _contract_inputs(
    document_rows: list[object],
    extraction_rows: list[object],
    *,
    observed_fallback: datetime,
) -> tuple[tuple[dict[str, object], ...], ContractCoverage | None]:
    eligible: dict[str, tuple[str | None, datetime]] = {}
    for row in document_rows:
        # Inventory has no authoritative legal/non-legal classification yet, so every
        # entry owns a coverage slot. Unsupported or pending files stay unprocessed.
        digest = (
            row.content_sha256
            if row.file_type in {"pdf", "docx"}
            and row.storage_status == "downloaded"
            and isinstance(row.content_sha256, str)
            and len(row.content_sha256) == 64
            else None
        )
        eligible[str(row.id)] = (
            digest,
            _aware(row.downloaded_at) or observed_fallback,
        )
    processed: dict[str, dict[str, object]] = {}
    extraction_observed: dict[str, datetime] = {}
    governing: dict[str, tuple[int, str]] = {}
    unidentified_conflict = False
    for row in extraction_rows:
        payload = _bounded_payload(row.bounded_payload, label="document extraction")
        if payload is None:
            unidentified_conflict |= row.status == "conflict"
            continue
        document_id = str(payload.get("document_id") or "")
        content_hash = payload.get("content_sha256") or payload.get("content_hash")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        expected = eligible.get(document_id)
        if not document_id or expected is None:
            unidentified_conflict |= row.status == "conflict"
            continue
        if expected[0] is None or content_hash != expected[0]:
            continue
        if document_id in governing:
            continue  # newest evidence for this document/current hash is authoritative
        governing[document_id] = (int(row.id), str(row.status))
        extraction_observed[document_id] = _aware(row.observed_at) or observed_fallback
        raw_conflicts = result.get("conflicts") if isinstance(result, dict) else None
        if (
            row.status != "found"
            or not isinstance(result, dict)
            or result.get("status") != "ok"
            or not isinstance(raw_conflicts, list)
        ):
            continue
        normalized = dict(result)
        for candidate in normalized.get("candidates", []):
            if isinstance(candidate, dict):
                candidate["document_id"] = document_id
        processed[document_id] = normalized
    if not eligible:
        return (), None
    eligible_ids = tuple(sorted(eligible))
    processed_ids = tuple(sorted(processed))
    generation_material = [
        (
            document_id,
            eligible[document_id][0],
            governing.get(document_id),
            document_id in processed,
        )
        for document_id in eligible_ids
    ]
    generation_material.append(("unidentified_conflict", unidentified_conflict))
    generation = hashlib.sha256(
        _strict_json(
            generation_material, max_bytes=MAX_ITEM_BYTES, label="contract generation"
        ).encode("utf-8")
    ).hexdigest()
    per_document_freshness = [
        min(
            eligible[document_id][1],
            extraction_observed.get(document_id, eligible[document_id][1]),
        )
        for document_id in eligible_ids
    ]
    observed = min(per_document_freshness, default=observed_fallback)
    coverage = ContractCoverage(
        eligible_document_ids=eligible_ids,
        processed_document_ids=processed_ids,
        observed_at=observed,
        generation_id=generation,
        coverage_complete=(not unidentified_conflict and set(eligible_ids) == set(processed_ids)),
    )
    return tuple(processed[key] for key in processed_ids), coverage


def _market_signature(count: int, maximum_id: int | None, observed_at: datetime | None) -> str:
    raw = f"{count}:{maximum_id or 0}:{_aware(observed_at).isoformat() if observed_at else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _current_market_target_signature(session: Session, lot: AuctionLot) -> str:
    facts = load_authoritative_target_facts(session, lot.id, lot=lot)
    built = build_authoritative_market_target(facts, valuation_at=_aware(lot.updated_at))
    target = built.target
    return target_signature(
        MarketTargetInput(
            lot_id=lot.id,
            right_type=(
                None
                if target.right_type == "lease" and target.lease_term_years is None
                else target.right_type if target.right_type in {"ownership", "lease"} else None
            ),
            purpose_group=target.purpose_group,
            lease_term_years=target.lease_term_years,
            area_ha=target.area_ha if target.area_ha > 0 else None,
            latitude=target.latitude,
            longitude=target.longitude,
            access_readiness=target.access_readiness,
            infrastructure_readiness=target.infrastructure_readiness,
            canonical_object_id=lot.land_object_id or lot.cadastre_number,
            source_sale_id=lot.source_lot_id if lot.source == "e-qazyna" else None,
        )
    )


def _read_bundle(session: Session, lot_id: str) -> ReadBundle:
    lot = session.get(AuctionLot, lot_id)
    if lot is None:
        raise DecisionInputStoreError("auction lot not found")
    updated = _aware(lot.updated_at) or datetime.now(UTC)
    # A marketing title is not authoritative evidence of permitted purpose.
    purpose = lot.purpose or lot.use_goal or lot.functional_purpose_level4 or ""
    scalar = LotScalar(
        id=lot.id,
        source_lot_id=lot.source_lot_id,
        updated_at=updated,
        start_price_kzt=lot.start_price_kzt,
        purpose=purpose[:4_000],
        land_rights=lot.land_rights,
        lease_term_years=lot.lease_term_years,
        guarantee_kzt=lot.guarantee_kzt,
        additional_payment_kzt=lot.additional_payment_kzt,
        annual_rent_kzt=lot.annual_rent_kzt,
    )
    rows = list(
        session.execute(
            select(
                AuctionEvidence.id,
                AuctionEvidence.evidence_type,
                AuctionEvidence.status,
                AuctionEvidence.observed_at,
                func.substr(AuctionEvidence.raw_payload_json, 1, MAX_ITEM_BYTES + 1).label(
                    "bounded_payload"
                ),
            )
            .where(
                AuctionEvidence.lot_id == lot_id,
                AuctionEvidence.evidence_type.in_(UPSTREAM_EVIDENCE_TYPES),
                AuctionEvidence.status.in_(UPSTREAM_STATUSES),
            )
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
            .limit(MAX_UPSTREAM_ROWS)
        )
    )
    rows = _fit_upstream_rows(rows)
    source_watermark_id, source_watermark_at = session.execute(
        select(func.max(AuctionEvidence.id), func.max(AuctionEvidence.observed_at)).where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type.in_(UPSTREAM_EVIDENCE_TYPES),
            AuctionEvidence.status.in_(UPSTREAM_STATUSES),
        )
    ).one()
    source_watermark_id = int(source_watermark_id or 0)
    source_watermark_at = _aware(source_watermark_at)
    source_card = next((row for row in rows if row.evidence_type == "source_object_card"), None)
    source_payload = (
        _bounded_payload(source_card.bounded_payload, label="source object")
        if source_card
        else None
    )
    conflict_fields: list[str] = []
    if source_card and source_card.status == "conflict":
        raw_conflicts = source_payload.get("conflicts") if source_payload else None
        if isinstance(raw_conflicts, list):
            conflict_fields.extend(
                str(item.get("field"))
                for item in raw_conflicts
                if isinstance(item, dict) and item.get("field")
            )
        if not conflict_fields:
            conflict_fields.append("*")
    passport = _legal_passport(
        scalar,
        source_payload,
        int(source_card.id) if source_card else None,
        _aware(source_card.observed_at) if source_card else None,
        tuple(dict.fromkeys(conflict_fields)),
    )
    documents = list(
        session.execute(
            select(
                AuctionDocument.id,
                AuctionDocument.file_type,
                AuctionDocument.storage_status,
                AuctionDocument.content_sha256,
                AuctionDocument.downloaded_at,
                AuctionDocument.created_at,
            )
            .where(AuctionDocument.lot_id == lot_id)
            .order_by(AuctionDocument.id.asc())
            .limit(MAX_DOCUMENTS + 1)
        )
    )
    if len(documents) > MAX_DOCUMENTS:
        raise DecisionInputStoreError("document inventory exceeds supported bound")
    document_material = [
        (
            row.id,
            row.file_type,
            row.storage_status,
            row.content_sha256,
            _aware(row.downloaded_at).isoformat() if row.downloaded_at else None,
            _aware(row.created_at).isoformat() if row.created_at else None,
        )
        for row in documents
    ]
    document_signature = hashlib.sha256(
        _strict_json(
            document_material, max_bytes=MAX_ITEM_BYTES, label="document inventory"
        ).encode("utf-8")
    ).hexdigest()
    document_watermark_id = max((int(row.id) for row in documents), default=0)
    document_watermark_at = max(
        (
            stamp
            for row in documents
            for stamp in (_aware(row.downloaded_at), _aware(row.created_at))
            if stamp is not None
        ),
        default=None,
    )
    extraction_rows = [row for row in rows if row.evidence_type == EXTRACTION_TYPE]
    extractions, coverage = _contract_inputs(documents, extraction_rows, observed_fallback=updated)
    history_generation, history = normalized_similar_history(session, lot)
    history_model = (
        session.get(AuctionHistoryGeneration, history_generation)
        if history_generation is not None
        else None
    )
    history_observed = _aware(history_model.activated_at) if history_model else None
    market_count, market_max_id, market_observed = session.execute(
        select(
            func.count(AuctionMarketComparable.id),
            func.max(AuctionMarketComparable.id),
            func.max(AuctionMarketComparable.observed_at),
        ).where(AuctionMarketComparable.lot_id == lot_id)
    ).one()
    market_signature = _market_signature(int(market_count or 0), market_max_id, market_observed)
    market_row = next((row for row in rows if row.evidence_type == "strict_market_estimate"), None)
    market_result = (
        _bounded_payload(market_row.bounded_payload, label="strict market estimate")
        if market_row and market_row.status == "found"
        else None
    )
    if market_result is not None and market_row is not None:
        market_result = dict(market_result)
        market_result["input_evidence_refs"] = [f"auction_evidence:{market_row.id}"]
        persisted_target_signature = market_result.get("market_target_signature")
        if not isinstance(persisted_target_signature, str) or (
            persisted_target_signature != _current_market_target_signature(session, lot)
        ):
            market_result = None
    costs = _paired_actual_cost_ranges(
        session,
        lot_id=lot_id,
        purpose=scalar.purpose,
    )
    return ReadBundle(
        lot=scalar,
        spatial=load_spatial_evidence(session, lot_id),
        legal_passport=passport,
        contract_extractions=extractions,
        contract_coverage=coverage,
        history_generation=history_generation,
        history_payload=asdict(history),
        history_observed_at=history_observed,
        market_result=market_result,
        actual_cost_ranges=costs,
        market_signature=market_signature,
        market_watermark_id=int(market_max_id or 0),
        market_watermark_at=_aware(market_observed),
        market_row_count=int(market_count or 0),
        document_signature=document_signature,
        document_watermark_id=document_watermark_id,
        document_watermark_at=document_watermark_at,
        document_row_count=len(documents),
        source_watermark_id=source_watermark_id,
        source_watermark_at=source_watermark_at,
    )


def _artifact_from_spatial(
    payload: Mapping[str, object] | None,
    freshness: Mapping[str, object] | None,
    generation: object,
) -> EvidenceArtifact | None:
    if payload is None:
        return None
    observed = None
    if freshness and isinstance(freshness.get("observed_at"), str):
        try:
            observed = datetime.fromisoformat(str(freshness["observed_at"]))
        except ValueError:
            observed = None
    refs = payload.get("provenance_refs") if isinstance(payload, Mapping) else None
    return EvidenceArtifact(
        payload=payload,
        status="found" if freshness and freshness.get("status") == "fresh" else "unknown",
        observed_at=_aware(observed),
        generation_id=generation if isinstance(generation, (str, int)) else None,
        coverage_complete=bool(freshness and freshness.get("status") == "fresh"),
        provenance_refs=tuple(item for item in (refs or []) if isinstance(item, str))[:100],
    )


def build_decision_inputs(bundle: ReadBundle, *, assembled_at: datetime) -> DecisionInputAssembly:
    profile = classify_scenario(bundle.lot.purpose)
    spatial = assemble_spatial_decision_inputs(
        bundle.spatial,
        profile=profile,
        legal_passport=bundle.legal_passport,
        now=assembled_at,
    )
    persistable = spatial.as_persistable_dict()
    outputs = spatial.decision_inputs
    artifacts: dict[str, EvidenceArtifact | None] = {}
    for key in ("geometry_context", "restriction_context", "site_context", "planning_context"):
        payload = outputs.get(key)
        artifacts[key] = _artifact_from_spatial(
            payload if isinstance(payload, Mapping) else None,
            spatial.source_freshness.get(key),
            spatial.evidence_generation_ids.get(key),
        )
    history_artifact = EvidenceArtifact(
        payload=bundle.history_payload,
        status="found" if bundle.history_payload.get("status") == "ok" else "unknown",
        observed_at=bundle.history_observed_at,
        generation_id=bundle.history_generation,
        coverage_complete=bundle.history_payload.get("status") == "ok",
        provenance_refs=(
            (f"auction_history_generation:{bundle.history_generation}",)
            if bundle.history_generation is not None
            else ()
        ),
    )
    assembly = assemble_decision_input(
        DecisionLotFacts(
            lot_id=bundle.lot.id,
            source_lot_id=bundle.lot.source_lot_id,
            updated_at=bundle.lot.updated_at,
            start_price_kzt=bundle.lot.start_price_kzt,
            purpose_text=bundle.lot.purpose,
        ),
        scenario_key=spatial.scenario_key,
        legal_passport=bundle.legal_passport,
        contract_extractions=bundle.contract_extractions,
        contract_coverage=bundle.contract_coverage,
        geometry_context=artifacts["geometry_context"],
        restriction_context=artifacts["restriction_context"],
        site_context=artifacts["site_context"],
        planning_context=artifacts["planning_context"],
        history_reference=history_artifact,
        market_result=bundle.market_result,
        actual_cost_ranges=bundle.actual_cost_ranges,
        assembled_at=assembled_at,
    )
    # Ensure the spatial adapter itself participated in the fingerprint without trusting callers.
    if persistable.get("spatial_assembler_version") != SPATIAL_ASSEMBLER_VERSION:
        raise DecisionInputStoreError("spatial assembler version mismatch")
    return assembly


def _lock_state(session: Session, lot_id: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"auction-decision-input:{lot_id}"},
        )


def _claim(session: Session, lot_id: str, now: datetime) -> str | None:
    _lock_state(session, lot_id)
    state = session.get(AuctionDecisionInputState, lot_id)
    if state is None:
        state = AuctionDecisionInputState(
            lot_id=lot_id,
            status="pending",
            assembler_version=ASSEMBLER_VERSION,
            spatial_assembler_version=SPATIAL_ASSEMBLER_VERSION,
            policy_version=POLICY_VERSION,
            created_at=now,
            updated_at=now,
        )
        session.add(state)
        session.flush()
    expiry = _aware(state.claim_expires_at)
    if state.status == "processing" and expiry is not None and expiry > now:
        return None
    token = str(uuid.uuid4())
    state.status = "processing"
    state.claim_token = token
    state.claim_expires_at = now + CLAIM_TTL
    state.next_attempt_at = None
    state.updated_at = now
    return token


def _record_error(
    session_factory: Callable[[], Session], lot_id: str, token: str, exc: Exception, now: datetime
) -> None:
    with session_factory() as session, session.begin():
        _lock_state(session, lot_id)
        state = session.get(AuctionDecisionInputState, lot_id)
        if state is None or state.claim_token != token:
            return
        state.retry_count = min(MAX_RETRY_COUNT, state.retry_count + 1)
        delay = min(MAX_RETRY_SECONDS, 60 * (2 ** min(state.retry_count - 1, 8)))
        state.status = "error"
        state.next_attempt_at = now + timedelta(seconds=delay)
        state.last_error_code = type(exc).__name__[:64]
        state.last_error_message = str(exc)[:MAX_ERROR_CHARS]
        state.claim_token = None
        state.claim_expires_at = None
        state.updated_at = now


def recompute_decision_inputs(
    session_factory: Callable[[], Session], lot_id: str, *, now: datetime | None = None
) -> RecomputeResult:
    """Claim, read, assemble outside DB transactions, then persist in one short transaction."""
    checked = _aware(now) or datetime.now(UTC)
    sqlite = False
    with session_factory() as dialect_session:
        sqlite = dialect_session.get_bind().dialect.name == "sqlite"
    lock = _SQLITE_LOCK if sqlite else None
    if lock:
        lock.acquire()
    try:
        try:
            with session_factory() as claim_session, claim_session.begin():
                token = _claim(claim_session, lot_id, checked)
        except IntegrityError:
            return RecomputeResult(lot_id, "busy", False, None)
    finally:
        if lock:
            lock.release()
    if token is None:
        return RecomputeResult(lot_id, "busy", False, None)
    try:
        with session_factory() as read_session:
            bundle = _read_bundle(read_session, lot_id)
        assembly = build_decision_inputs(bundle, assembled_at=checked)
        status = "ready" if not assembly.module_outputs.get("stale_reasons") else "insufficient"
        evidence_ids: list[int] = []
        with session_factory() as write_session, write_session.begin():
            _lock_state(write_session, lot_id)
            state = write_session.get(AuctionDecisionInputState, lot_id)
            if state is None or state.claim_token != token:
                return RecomputeResult(lot_id, "superseded", False, assembly.input_hash)
            changed = state.input_hash != assembly.input_hash
            if changed:
                observed = checked
                for evidence_type, payload_json in sorted(assembly.persistence_payloads.items()):
                    evidence = AuctionEvidence(
                        lot_id=lot_id,
                        source_id=None,
                        evidence_type=evidence_type,
                        status="found",
                        title=f"Zhertap decision input · {evidence_type[15:]}",
                        value_text=assembly.input_hash,
                        source_url=None,
                        confidence=1.0,
                        raw_payload_json=payload_json,
                        observed_at=observed,
                    )
                    write_session.add(evidence)
                    write_session.flush()
                    evidence_ids.append(evidence.id)
            state.status = status
            state.source_watermark_id = bundle.source_watermark_id
            state.source_watermark_at = bundle.source_watermark_at
            state.lot_updated_at = bundle.lot.updated_at
            state.history_generation = bundle.history_generation
            state.market_signature = bundle.market_signature
            state.market_watermark_id = bundle.market_watermark_id
            state.market_watermark_at = bundle.market_watermark_at
            state.market_row_count = bundle.market_row_count
            state.document_signature = bundle.document_signature
            state.document_watermark_id = bundle.document_watermark_id
            state.document_watermark_at = bundle.document_watermark_at
            state.document_row_count = bundle.document_row_count
            state.input_hash = assembly.input_hash
            state.assembler_version = ASSEMBLER_VERSION
            state.spatial_assembler_version = SPATIAL_ASSEMBLER_VERSION
            state.policy_version = POLICY_VERSION
            state.claim_token = None
            state.claim_expires_at = None
            state.retry_count = 0
            state.next_attempt_at = None
            state.last_error_code = None
            state.last_error_message = None
            state.validated_at = checked
            state.updated_at = checked
        return RecomputeResult(lot_id, status, changed, assembly.input_hash, tuple(evidence_ids))
    except Exception as exc:
        _record_error(session_factory, lot_id, token, exc, checked)
        return RecomputeResult(lot_id, "error", False, None, error_code=type(exc).__name__[:64])


def decision_input_worklist(
    session: Session, *, limit: int = 25, now: datetime | None = None
) -> list[str]:
    """Return bounded dirty lots; no stale-only polling and no decision_input self-loop."""
    bounded = max(1, min(int(limit), MAX_WORKLIST))
    checked = _aware(now) or datetime.now(UTC)
    upstream_id = (
        select(func.max(AuctionEvidence.id))
        .where(
            AuctionEvidence.lot_id == AuctionLot.id,
            AuctionEvidence.evidence_type.in_(UPSTREAM_EVIDENCE_TYPES),
            AuctionEvidence.status.in_(UPSTREAM_STATUSES),
        )
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    upstream_at = (
        select(func.max(AuctionEvidence.observed_at))
        .where(
            AuctionEvidence.lot_id == AuctionLot.id,
            AuctionEvidence.evidence_type.in_(UPSTREAM_EVIDENCE_TYPES),
            AuctionEvidence.status.in_(UPSTREAM_STATUSES),
        )
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    active_generation = (
        select(AuctionHistoryGeneration.generation)
        .where(AuctionHistoryGeneration.status == "active")
        .limit(1)
        .scalar_subquery()
    )
    market_id = (
        select(func.max(AuctionMarketComparable.id))
        .where(AuctionMarketComparable.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    market_count = (
        select(func.count(AuctionMarketComparable.id))
        .where(AuctionMarketComparable.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    market_at = (
        select(func.max(AuctionMarketComparable.observed_at))
        .where(AuctionMarketComparable.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    document_id = (
        select(func.max(AuctionDocument.id))
        .where(AuctionDocument.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    document_count = (
        select(func.count(AuctionDocument.id))
        .where(AuctionDocument.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    document_created_at = (
        select(func.max(AuctionDocument.created_at))
        .where(AuctionDocument.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    document_downloaded_at = (
        select(func.max(AuctionDocument.downloaded_at))
        .where(AuctionDocument.lot_id == AuctionLot.id)
        .correlate(AuctionLot)
        .scalar_subquery()
    )
    retry_due = or_(
        AuctionDecisionInputState.status == "pending",
        and_(
            AuctionDecisionInputState.status == "error",
            or_(
                AuctionDecisionInputState.next_attempt_at.is_(None),
                AuctionDecisionInputState.next_attempt_at <= checked,
            ),
        ),
        and_(
            AuctionDecisionInputState.status == "processing",
            or_(
                AuctionDecisionInputState.claim_expires_at.is_(None),
                AuctionDecisionInputState.claim_expires_at <= checked,
            ),
        ),
    )
    changed = or_(
        AuctionDecisionInputState.lot_updated_at.is_(None),
        AuctionLot.updated_at.is_distinct_from(AuctionDecisionInputState.lot_updated_at),
        func.coalesce(upstream_id, 0) != AuctionDecisionInputState.source_watermark_id,
        upstream_at.is_distinct_from(AuctionDecisionInputState.source_watermark_at),
        func.coalesce(market_id, 0) != AuctionDecisionInputState.market_watermark_id,
        func.coalesce(market_count, 0) != AuctionDecisionInputState.market_row_count,
        market_at.is_distinct_from(AuctionDecisionInputState.market_watermark_at),
        func.coalesce(document_id, 0) != AuctionDecisionInputState.document_watermark_id,
        func.coalesce(document_count, 0) != AuctionDecisionInputState.document_row_count,
        document_created_at > AuctionDecisionInputState.document_watermark_at,
        document_downloaded_at > AuctionDecisionInputState.document_watermark_at,
        active_generation.is_distinct_from(AuctionDecisionInputState.history_generation),
        AuctionDecisionInputState.assembler_version != ASSEMBLER_VERSION,
        AuctionDecisionInputState.spatial_assembler_version != SPATIAL_ASSEMBLER_VERSION,
        AuctionDecisionInputState.policy_version != POLICY_VERSION,
        AuctionDecisionInputState.validated_at < checked - RECOVERY_REVALIDATE_AFTER,
    )
    return list(
        session.scalars(
            select(AuctionLot.id)
            .outerjoin(
                AuctionDecisionInputState,
                AuctionDecisionInputState.lot_id == AuctionLot.id,
            )
            .where(
                AuctionLot.object_type == "land",
                AuctionLot.active.is_(True),
                or_(AuctionDecisionInputState.lot_id.is_(None), retry_due, changed),
                or_(
                    AuctionDecisionInputState.status.is_(None),
                    AuctionDecisionInputState.status != "processing",
                    AuctionDecisionInputState.claim_expires_at.is_(None),
                    AuctionDecisionInputState.claim_expires_at <= checked,
                ),
                or_(
                    AuctionDecisionInputState.status.is_(None),
                    AuctionDecisionInputState.status != "error",
                    AuctionDecisionInputState.next_attempt_at.is_(None),
                    AuctionDecisionInputState.next_attempt_at <= checked,
                ),
            )
            .order_by(AuctionLot.updated_at.desc(), AuctionLot.id.asc())
            .limit(bounded)
        )
    )


def recompute_decision_input_batch(
    session_factory: Callable[[], Session], *, limit: int = 25, now: datetime | None = None
) -> list[RecomputeResult]:
    checked = _aware(now) or datetime.now(UTC)
    with session_factory() as session:
        lot_ids = decision_input_worklist(session, limit=limit, now=checked)
    return [recompute_decision_inputs(session_factory, lot_id, now=checked) for lot_id in lot_ids]
