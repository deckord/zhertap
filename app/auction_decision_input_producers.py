from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auction_decision_input import ContractCoverage
from app.auction_market_comparables import ENGINE_VERSION, MarketComparableResult
from app.auction_price_ceiling import MAX_KZT, REQUIRED_COST_KEYS
from app.models import AuctionDocument, AuctionEvidence
from app.auction_document_extractor import TEXT_FILE_TYPES

PRODUCER_VERSION = "decision-input-producers/2026.1"
MAX_DOCUMENTS = 20
MAX_EXTRACTIONS = 48
MAX_FACTS = 64
MAX_REFS = 100
MAX_ITEM_BYTES = 64_000
MAX_AGGREGATE_BYTES = 1_000_000
MAX_TEXT = 500
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:.\-/]{0,239}$")


class DecisionInputProducerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DocumentInventoryRecord:
    document_id: str
    file_type: str | None
    storage_status: str
    content_sha256: str | None
    observed_at: datetime | None
    source_url: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionEvidenceRecord:
    evidence_id: int
    status: str
    observed_at: datetime | None
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ContractCoverageInputs:
    documents: tuple[DocumentInventoryRecord, ...]
    extractions: tuple[ExtractionEvidenceRecord, ...]
    extraction_history_truncated: bool = False


@dataclass(frozen=True, slots=True)
class ContractCoverageResult:
    status: Literal["complete", "incomplete", "insufficient_data"]
    coverage: ContractCoverage | None
    accepted_extractions: tuple[dict[str, object], ...]
    reasons: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    producer_version: str = PRODUCER_VERSION


@dataclass(frozen=True, slots=True)
class DocumentedMonetaryFact:
    cost_key: str
    low_kzt: int | float
    base_kzt: int | float
    high_kzt: int | float
    status: Literal["found", "unknown", "conflict"]
    source_ref: str
    source_url: str | None
    observed_at: datetime | None
    generation_id: int | str | None = None


@dataclass(frozen=True, slots=True)
class CostRangesResult:
    status: Literal["complete", "incomplete", "insufficient_data"]
    payload: dict[str, object]
    missing_keys: tuple[str, ...]
    conflict_keys: tuple[str, ...]
    excluded_keys: tuple[str, ...]
    observed_at: datetime | None
    generation_id: str
    provenance_refs: tuple[str, ...]
    producer_version: str = PRODUCER_VERSION

    def evidence_payload(self) -> dict[str, object]:
        """Return the exact raw JSON shape consumed by auction_decision_input_store."""
        return {
            **self.payload,
            "provenance_refs": list(self.provenance_refs),
        }

    def persistence_metadata(self) -> dict[str, object]:
        """Writer must persist this audit in bounded title/value_text, not raw cost keys."""
        return {
            "evidence_type": "decision_cost_ranges",
            "evidence_status": "found",
            "producer_status": self.status,
            "producer_version": self.producer_version,
            "observed_at": self.observed_at,
            "generation_id": self.generation_id,
            "missing_keys": self.missing_keys,
            "conflict_keys": self.conflict_keys,
            "excluded_keys": self.excluded_keys,
        }


@dataclass(frozen=True, slots=True)
class ActiveHistoryAudit:
    generation_id: int
    status: str
    activated_at: datetime | None
    provenance_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StrictMarketEvidenceResult:
    status: Literal["ok", "insufficient_data"]
    payload: dict[str, object]
    observed_at: datetime | None
    generation_id: str
    provenance_refs: tuple[str, ...]
    reasons: tuple[str, ...]
    producer_version: str = PRODUCER_VERSION

    def evidence_payload(self) -> dict[str, object]:
        return {
            **self.payload,
            "producer_version": self.producer_version,
            "input_generation_id": self.generation_id,
            "input_evidence_refs": list(self.provenance_refs),
            "producer_reasons": list(self.reasons),
            "producer_observed_at": (
                self.observed_at.isoformat() if self.observed_at else None
            ),
        }


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _db_aware(value: datetime | None) -> datetime | None:
    """Database timestamps are UTC; SQLite drops the timezone marker on round-trip."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _strict_json(value: object, *, label: str, max_bytes: int) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionInputProducerError(f"{label} is not strict JSON") from exc
    if len(rendered.encode("utf-8")) > max_bytes:
        raise DecisionInputProducerError(f"{label} exceeds byte budget")
    return rendered


def _bounded_ref(value: object) -> str | None:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        return None
    return value


def _bounded_url(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 1_000:
        return None
    return value if value.startswith(("https://", "http://")) else None


def _document_id(value: object) -> str:
    normalized = str(value)
    if not normalized or len(normalized) > 64 or not normalized.isdigit():
        raise DecisionInputProducerError("invalid document id")
    return normalized


def load_contract_coverage_inputs(session: Session, lot_id: str) -> ContractCoverageInputs:
    """Read a bounded authoritative inventory; parsing happens after this short query."""
    if not isinstance(lot_id, str) or not 1 <= len(lot_id) <= 64:
        raise DecisionInputProducerError("invalid lot id")
    document_rows = list(
        session.execute(
            select(
                AuctionDocument.id,
                AuctionDocument.file_type,
                AuctionDocument.storage_status,
                AuctionDocument.content_sha256,
                AuctionDocument.downloaded_at,
                AuctionDocument.created_at,
                AuctionDocument.source_url,
            )
            .where(AuctionDocument.lot_id == lot_id)
            .order_by(AuctionDocument.id.asc())
            .limit(MAX_DOCUMENTS + 1)
        )
    )
    if len(document_rows) > MAX_DOCUMENTS:
        raise DecisionInputProducerError("document inventory exceeds bound")
    extraction_rows = list(
        session.execute(
            select(
                AuctionEvidence.id,
                AuctionEvidence.status,
                AuctionEvidence.observed_at,
                func.substr(AuctionEvidence.raw_payload_json, 1, MAX_ITEM_BYTES + 1),
            )
            .where(
                AuctionEvidence.lot_id == lot_id,
                AuctionEvidence.evidence_type == "document_extraction",
            )
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
            .limit(MAX_EXTRACTIONS + 1)
        )
    )
    extraction_history_truncated = len(extraction_rows) > MAX_EXTRACTIONS
    extraction_rows = extraction_rows[:MAX_EXTRACTIONS]
    aggregate = sum(len((row[3] or "").encode("utf-8")) for row in extraction_rows)
    if aggregate > MAX_AGGREGATE_BYTES:
        raise DecisionInputProducerError("extraction evidence exceeds aggregate byte budget")
    documents = tuple(
        DocumentInventoryRecord(
            document_id=str(row.id),
            file_type=row.file_type,
            storage_status=row.storage_status,
            content_sha256=row.content_sha256,
            observed_at=_db_aware(row.downloaded_at) or _db_aware(row.created_at),
            source_url=_bounded_url(row.source_url),
        )
        for row in document_rows
    )
    extractions: list[ExtractionEvidenceRecord] = []
    for row in extraction_rows:
        raw = row[3]
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_ITEM_BYTES:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            extractions.append(
                ExtractionEvidenceRecord(
                    evidence_id=int(row.id),
                    status=str(row.status),
                    observed_at=_db_aware(row.observed_at),
                    payload=payload,
                )
            )
    return ContractCoverageInputs(
        documents,
        tuple(extractions),
        extraction_history_truncated=extraction_history_truncated,
    )


def build_authoritative_contract_coverage(
    inputs: ContractCoverageInputs,
    *,
    assembled_at: datetime,
) -> ContractCoverageResult:
    assembled = _aware(assembled_at)
    if assembled is None:
        raise DecisionInputProducerError("assembled_at must be timezone-aware")
    if len(inputs.documents) > MAX_DOCUMENTS or len(inputs.extractions) > MAX_EXTRACTIONS:
        raise DecisionInputProducerError("contract input count exceeds bound")
    extraction_bytes = sum(
        len(
            _strict_json(
                evidence.payload,
                label="contract extraction evidence",
                max_bytes=MAX_ITEM_BYTES,
            ).encode("utf-8")
        )
        for evidence in inputs.extractions
    )
    if extraction_bytes > MAX_AGGREGATE_BYTES:
        raise DecisionInputProducerError("contract extraction aggregate exceeds byte budget")
    if not inputs.documents:
        return ContractCoverageResult(
            "insufficient_data", None, (), ("document_inventory_empty",), ()
        )
    inventory: dict[str, DocumentInventoryRecord] = {}
    reasons: list[str] = []
    if inputs.extraction_history_truncated:
        reasons.append("extraction_history_truncated")
    refs: list[str] = []
    for document in inputs.documents:
        document_id = _document_id(document.document_id)
        if document_id in inventory:
            raise DecisionInputProducerError("duplicate document id")
        inventory[document_id] = document
        refs.append(f"auction_document:{document_id}")
        if _aware(document.observed_at) is None:
            reasons.append(f"document_timestamp_unknown:{document_id}")
        if not (
            document.file_type in TEXT_FILE_TYPES
            and document.storage_status == "downloaded"
            and isinstance(document.content_sha256, str)
            and _SHA256.fullmatch(document.content_sha256)
        ):
            reasons.append(f"document_not_extractable:{document_id}")

    accepted: dict[str, dict[str, object]] = {}
    extraction_stamps: dict[str, datetime] = {}
    seen_current_hash: set[str] = set()
    ambiguous_conflict = False
    ordered_evidence = sorted(
        inputs.extractions,
        key=lambda item: (
            _aware(item.observed_at) or datetime.min.replace(tzinfo=UTC),
            item.evidence_id,
        ),
        reverse=True,
    )
    for evidence in ordered_evidence:
        if evidence.evidence_id < 1:
            continue
        payload = dict(evidence.payload)
        document_id = str(payload.get("document_id") or "")
        document = inventory.get(document_id)
        result = payload.get("result")
        digest = payload.get("content_sha256") or payload.get("content_hash")
        stamp = _aware(evidence.observed_at)
        if evidence.status == "conflict" and (
            document is None or digest != document.content_sha256
        ):
            ambiguous_conflict = True
            reasons.append("ambiguous_document_extraction_conflict")
        if document is None or digest != document.content_sha256:
            continue
        if document_id in seen_current_hash:
            continue
        seen_current_hash.add(document_id)
        refs.append(f"auction_evidence:{evidence.evidence_id}")
        if (
            evidence.status != "found"
            or
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or stamp is None
        ):
            reasons.append(f"latest_document_extraction_not_usable:{document_id}")
            continue
        normalized = json.loads(
            _strict_json(
                result,
                label="contract extraction result",
                max_bytes=MAX_ITEM_BYTES,
            )
        )
        candidates = normalized.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 100:
            continue
        conflicts = normalized.get("conflicts")
        if not isinstance(conflicts, list) or len(conflicts) > 100:
            continue
        if conflicts:
            reasons.append(f"document_extraction_conflict:{document_id}")
            continue
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate["document_id"] = document_id
        accepted[document_id] = normalized
        extraction_stamps[document_id] = stamp

    eligible_ids = tuple(sorted(inventory, key=lambda item: int(item)))
    processed_ids = tuple(sorted(accepted, key=lambda item: int(item)))
    complete = (
        set(eligible_ids) == set(processed_ids)
        and not ambiguous_conflict
        and not any(reason.startswith("document_not_extractable:") for reason in reasons)
        and not any(reason.startswith("document_timestamp_unknown:") for reason in reasons)
    )
    if not complete:
        missing = sorted(set(eligible_ids) - set(processed_ids), key=int)
        reasons.extend(f"document_unprocessed:{item}" for item in missing)
    material = [
        (item, inventory[item].content_sha256, item in accepted)
        for item in eligible_ids
    ]
    generation = hashlib.sha256(
        _strict_json(material, label="contract generation", max_bytes=MAX_ITEM_BYTES).encode()
    ).hexdigest()
    stamps = [
        stamp
        for document in inventory.values()
        for stamp in (_aware(document.observed_at),)
        if stamp is not None
    ] + list(extraction_stamps.values())
    observed = min(stamps, default=assembled)
    coverage = ContractCoverage(
        eligible_document_ids=eligible_ids,
        processed_document_ids=processed_ids,
        observed_at=observed,
        generation_id=generation,
        coverage_complete=complete,
    )
    return ContractCoverageResult(
        "complete" if complete else "incomplete",
        coverage,
        tuple(accepted[item] for item in processed_ids),
        tuple(dict.fromkeys(reasons)),
        tuple(dict.fromkeys(refs))[:MAX_REFS],
    )


def _kzt(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or float(value) != int(value):
        return None
    normalized = int(value)
    return normalized if 0 <= normalized <= MAX_KZT else None


def produce_decision_cost_ranges(
    facts: tuple[DocumentedMonetaryFact, ...] | list[DocumentedMonetaryFact],
) -> CostRangesResult:
    if len(facts) > MAX_FACTS:
        raise DecisionInputProducerError("monetary fact count exceeds bound")
    accepted: dict[str, tuple[int, int, int, list[str], list[datetime], list[str]]] = {}
    conflicts = {
        fact.cost_key
        for fact in facts
        if fact.status == "conflict" and fact.cost_key in REQUIRED_COST_KEYS
    }
    excluded: set[str] = set()
    for fact in facts:
        if len(fact.cost_key) > 80:
            raise DecisionInputProducerError("cost key exceeds bound")
        if fact.cost_key not in REQUIRED_COST_KEYS:
            # Guarantee, auction additional payment and annual rent belong to legal_payments.
            excluded.add(fact.cost_key)
            continue
        if fact.cost_key in conflicts:
            continue
        ref = _bounded_ref(fact.source_ref)
        stamp = _aware(fact.observed_at)
        low, base, high = (_kzt(fact.low_kzt), _kzt(fact.base_kzt), _kzt(fact.high_kzt))
        if (
            fact.status != "found"
            or ref is None
            or stamp is None
            or low is None
            or base is None
            or high is None
            or not low <= base <= high
        ):
            continue
        candidate = (low, base, high)
        current = accepted.get(fact.cost_key)
        url_ref = _bounded_url(fact.source_url)
        refs = [ref, *( [url_ref] if url_ref else [])]
        generation = str(fact.generation_id)[:128] if fact.generation_id is not None else ""
        if current is None:
            accepted[fact.cost_key] = (low, base, high, refs, [stamp], [generation])
        elif current[:3] != candidate:
            conflicts.add(fact.cost_key)
        else:
            current[3].extend(refs)
            current[4].append(stamp)
            current[5].append(generation)
    for key in conflicts:
        accepted.pop(key, None)
    payload: dict[str, object] = {}
    all_refs: list[str] = []
    all_stamps: list[datetime] = []
    material: list[object] = []
    for key in REQUIRED_COST_KEYS:
        item = accepted.get(key)
        if item is None:
            continue
        low, base, high, refs, stamps, generations = item
        unique_refs = list(dict.fromkeys(refs))[:MAX_REFS]
        payload[key] = {
            "low_kzt": low,
            "base_kzt": base,
            "high_kzt": high,
            "provenance_refs": unique_refs,
        }
        all_refs.extend(unique_refs)
        all_stamps.extend(stamps)
        material.append((key, low, base, high, sorted(set(generations))))
    missing = tuple(key for key in REQUIRED_COST_KEYS if key not in payload)
    status: Literal["complete", "incomplete", "insufficient_data"] = (
        "complete" if not missing else "incomplete" if payload else "insufficient_data"
    )
    generation = hashlib.sha256(
        _strict_json(material, label="cost generation", max_bytes=MAX_ITEM_BYTES).encode()
    ).hexdigest()
    return CostRangesResult(
        status,
        payload,
        missing,
        tuple(sorted(conflicts)),
        tuple(sorted(excluded)),
        min(all_stamps, default=None),
        generation,
        tuple(dict.fromkeys(all_refs))[:MAX_REFS],
    )


def adapt_strict_market_estimate(
    result: MarketComparableResult | None,
    *,
    input_generation_id: int | str,
    history_audit: ActiveHistoryAudit | None = None,
) -> StrictMarketEvidenceResult:
    generation = str(input_generation_id)
    if not generation or len(generation) > 128:
        raise DecisionInputProducerError("invalid market input generation")
    reasons: list[str] = []
    refs: list[str] = []
    observed: list[datetime] = []
    payload = result.as_dict() if result is not None else {
        "status": "insufficient_data",
        "estimate": None,
        "confidence": "none",
        "high_quality_verified_count": 0,
        "verified_eligible_count": 0,
        "listing_eligible_count": 0,
        "evaluations": [],
        "detail": "Strict W9 market evidence is unavailable.",
        "engine_version": ENGINE_VERSION,
    }
    _strict_json(payload, label="strict market result", max_bytes=MAX_AGGREGATE_BYTES)
    evaluations = payload.get("evaluations")
    if not isinstance(evaluations, list) or len(evaluations) > 500:
        raise DecisionInputProducerError("invalid market evaluations")
    for item in evaluations:
        if not isinstance(item, dict):
            continue
        if not (
            item.get("eligible") is True
            and item.get("quality_grade") == "A"
            and item.get("price_kind") == "verified_sale"
        ):
            continue
        source_id = _bounded_ref(item.get("source_id"))
        record_id = _bounded_ref(item.get("source_record_id"))
        source_url = _bounded_url(item.get("source_url"))
        raw_stamp = item.get("observed_at")
        try:
            stamp = (
                _aware(datetime.fromisoformat(raw_stamp))
                if isinstance(raw_stamp, str)
                else None
            )
        except ValueError:
            stamp = None
        if source_id and record_id and source_url and stamp:
            refs.append(f"market:{source_id}:{record_id}")
            refs.append(source_url)
            observed.append(stamp)
    estimate = payload.get("estimate")
    used = estimate.get("verified_comparables_used") if isinstance(estimate, dict) else None
    accepted = (
        payload.get("engine_version") == ENGINE_VERSION
        and payload.get("status") == "ok"
        and payload.get("confidence") in {"medium", "high"}
        and isinstance(payload.get("high_quality_verified_count"), int)
        and not isinstance(payload.get("high_quality_verified_count"), bool)
        and int(payload["high_quality_verified_count"]) >= 3
        and isinstance(used, int)
        and not isinstance(used, bool)
        and used >= 3
        and len({ref for ref in refs if ref.startswith("market:")}) >= 3
    )
    if not accepted:
        reasons.append("strict_market_required_evidence_incomplete")
        payload["status"] = "insufficient_data"
        payload["estimate"] = None
        payload["confidence"] = "none"
    history_payload: dict[str, object] = {"status": "unavailable", "audit_only": True}
    if history_audit is not None:
        activated = _aware(history_audit.activated_at)
        history_refs = tuple(
            filter(None, (_bounded_ref(item) for item in history_audit.provenance_refs))
        )
        if history_audit.status == "active" and activated is not None:
            history_payload = {
                "status": "active",
                "generation_id": history_audit.generation_id,
                "activated_at": activated.isoformat(),
                "audit_only": True,
                "provenance_refs": list(history_refs),
            }
        else:
            reasons.append("history_generation_not_active")
    payload["history_audit"] = history_payload
    return StrictMarketEvidenceResult(
        "ok" if accepted else "insufficient_data",
        payload,
        min(observed, default=None),
        generation,
        tuple(dict.fromkeys(refs))[:MAX_REFS],
        tuple(dict.fromkeys(reasons)),
    )
