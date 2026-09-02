from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuctionDocument, AuctionEvidence, AuctionLot
from app.shared_cache import shared_json_cache

PASSPORT_VERSION = "legal-passport.v3"
MAX_EVIDENCE = 50
MAX_DOCUMENTS = 20
MAX_JSON_CHARS = 256_000
MAX_AGGREGATE_JSON_CHARS = 1_000_000
LEGAL_EVIDENCE_TYPES = (
    "source_object_card",
    "official_lot",
    "official_document_summary",
    "akimat_announcement",
    "document_extraction",
)
LEGAL_EVIDENCE_STATUSES = ("found", "conflict")

FactStatus = Literal["unknown", "found", "conflict"]

# Different clauses in these fields are normally cumulative legal conditions,
# not mutually exclusive values. Preserve every citation, but do not manufacture
# a contradiction solely because two official clauses have different wording.
ADDITIVE_LEGAL_FIELDS = {
    "development_conditions",
    "development_obligation",
    "termination_ground",
    "renewal_condition",
    "responsibility_penalty",
}


@dataclass(frozen=True, slots=True)
class LegalProvenance:
    source_url: str | None
    observed_at: datetime | None
    confidence: float
    evidence_type: str
    evidence_id: int | None = None
    document_id: int | str | None = None
    document_title: str | None = None
    page: int | None = None
    section: str | None = None
    evidence_excerpt: str | None = None
    quote_hash: str | None = None
    content_hash: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["observed_at"] = self.observed_at.isoformat() if self.observed_at else None
        return payload


@dataclass(frozen=True, slots=True)
class LegalFact:
    key: str
    value: object | None
    status: FactStatus
    source_url: str | None
    observed_at: datetime | None
    confidence: float
    provenance: tuple[LegalProvenance, ...] = ()
    version: str = PASSPORT_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "value": self.value,
            "status": self.status,
            "source_url": self.source_url,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "confidence": self.confidence,
            "provenance": [item.as_dict() for item in self.provenance],
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class LegalDocumentRef:
    title: str
    source_url: str
    file_type: str | None
    storage_status: str


@dataclass(frozen=True, slots=True)
class AuctionLegalPassport:
    lot_id: str
    source_lot_id: str
    generated_at: datetime
    facts: dict[str, LegalFact]
    payments: dict[str, LegalFact]
    documents: tuple[LegalDocumentRef, ...] = ()
    version: str = PASSPORT_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "lot_id": self.lot_id,
            "source_lot_id": self.source_lot_id,
            "generated_at": self.generated_at.isoformat(),
            "version": self.version,
            "facts": {key: fact.as_dict() for key, fact in self.facts.items()},
            "payments": {key: fact.as_dict() for key, fact in self.payments.items()},
            "documents": [asdict(document) for document in self.documents],
        }


@dataclass(frozen=True, slots=True)
class _Candidate:
    value: object
    provenance: LegalProvenance


@dataclass(slots=True)
class _Collector:
    values: dict[str, list[_Candidate]] = field(default_factory=dict)
    forced_conflicts: set[str] = field(default_factory=set)
    preferred_values: dict[str, object] = field(default_factory=dict)

    def add(self, key: str, value: object, provenance: LegalProvenance) -> None:
        if value is None or value == "":
            return
        self.values.setdefault(key, []).append(_Candidate(value=value, provenance=provenance))


_RAW_ALIASES: dict[str, tuple[str, ...]] = {
    "land_category": ("land_category", "category", "категория земель", "категория"),
    "purpose": ("purpose", "target_purpose", "целевое назначение"),
    "development_conditions": (
        "development_conditions",
        "mastering_conditions",
        "conditions_of_development",
        "условия освоения",
        "условия застройки",
    ),
    "arrests": ("arrests_text", "arrests", "аресты", "наличие арестов"),
    "restrictions": (
        "restrictions_text",
        "restrictions",
        "encumbrances",
        "ограничения",
        "обременения",
    ),
    "encumbrances": ("encumbrances", "обременения"),
}


def _normalized(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}".rstrip("0").rstrip(".")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _normalized_right_type(value: object) -> str | None:
    normalized = _normalized(value)
    if normalized in {"lease", "ownership"}:
        return normalized
    if any(
        token in normalized
        for token in (
            "аренд",
            "временн",
            "землепользован",
            "жалдау",
            "уақытша",
            "жер пайдалану",
        )
    ):
        return "lease"
    if any(
        token in normalized
        for token in ("собствен", "частн", "меншік", "жеке меншік")
    ):
        return "ownership"
    return None


def _fact(
    key: str,
    candidates: list[_Candidate],
    *,
    forced_conflict: bool = False,
    preferred_value: object | None = None,
) -> LegalFact:
    if not candidates:
        return LegalFact(
            key=key,
            value=None,
            status="unknown",
            source_url=None,
            observed_at=None,
            confidence=0.0,
        )
    deduplicated: list[_Candidate] = []
    seen: set[tuple[str, str | None, str]] = set()
    for candidate in sorted(candidates, key=lambda item: item.provenance.confidence, reverse=True):
        marker = (
            _normalized(candidate.value),
            candidate.provenance.source_url,
            candidate.provenance.evidence_type,
        )
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(candidate)
    distinct_values = {_normalized(candidate.value) for candidate in deduplicated}
    values_conflict = len(distinct_values) > 1 and key not in ADDITIVE_LEGAL_FIELDS
    status: FactStatus = "conflict" if forced_conflict or values_conflict else "found"
    winner = deduplicated[0]
    if status == "conflict" and preferred_value is not None:
        preferred_normalized = _normalized(preferred_value)
        winner = next(
            (
                candidate
                for candidate in deduplicated
                if _normalized(candidate.value) == preferred_normalized
            ),
            winner,
        )
    confidence = winner.provenance.confidence
    if status == "conflict":
        confidence = min(confidence, 0.49)
    return LegalFact(
        key=key,
        value=winner.value,
        status=status,
        source_url=winner.provenance.source_url,
        observed_at=winner.provenance.observed_at,
        confidence=confidence,
        provenance=tuple(candidate.provenance for candidate in deduplicated),
    )


def _safe_payload(raw: str | None) -> dict[str, Any]:
    if not raw or len(raw) > MAX_JSON_CHARS:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _payload_value(payload: dict[str, Any], aliases: tuple[str, ...]) -> object | None:
    normalized_aliases = {alias.casefold() for alias in aliases}
    pending: list[object] = [payload]
    visited = 0
    while pending and visited < 500:
        current = pending.pop()
        visited += 1
        if isinstance(current, dict):
            for key, value in list(current.items())[:100]:
                if str(key).strip().casefold() in normalized_aliases and value not in (None, ""):
                    return value
                if isinstance(value, (dict, list)):
                    pending.append(value)
        elif isinstance(current, list):
            pending.extend(current[:100])
    return None


def _lot_provenance(lot: AuctionLot, *, confidence: float = 0.88) -> LegalProvenance:
    return LegalProvenance(
        source_url=lot.source_url,
        observed_at=lot.last_seen_at,
        confidence=confidence,
        evidence_type="official_lot",
    )


def _evidence_provenance(evidence: AuctionEvidence) -> LegalProvenance:
    return LegalProvenance(
        source_url=evidence.source_url,
        observed_at=evidence.observed_at,
        confidence=max(0.0, min(float(evidence.confidence or 0), 1.0)),
        evidence_type=evidence.evidence_type,
        evidence_id=evidence.id,
    )


def _add_lot_values(collector: _Collector, lot: AuctionLot) -> None:
    provenance = _lot_provenance(lot)
    for key, value in (
        ("land_rights", lot.land_rights),
        ("lease_term_years", lot.lease_term_years),
        ("divisible", lot.divisible),
        ("additional_payment_kzt", lot.additional_payment_kzt),
        ("annual_rent_kzt", lot.annual_rent_kzt),
        ("guarantee_payment_kzt", lot.guarantee_kzt),
    ):
        collector.add(key, value, provenance)
        if value not in (None, ""):
            collector.preferred_values[key] = value

    purpose = (
        lot.purpose
        or lot.functional_purpose_level4
        or lot.functional_purpose_level3
        or lot.functional_purpose_level2
        or lot.use_goal
    )
    collector.add("purpose", purpose, provenance)
    if purpose not in (None, ""):
        collector.preferred_values["purpose"] = purpose
    raw_payload = _safe_payload(lot.raw_payload_json)
    for key, aliases in _RAW_ALIASES.items():
        collector.add(key, _payload_value(raw_payload, aliases), provenance)


def _add_evidence_values(
    collector: _Collector,
    evidence_rows: list[AuctionEvidence],
    *,
    raw_json_budget_chars: int,
) -> None:
    direct_fields = {
        "land_rights": "land_rights",
        "lease_term_years": "lease_term_years",
        "divisible": "divisible",
        "additional_payment_kzt": "additional_payment_kzt",
        "annual_rent_kzt": "annual_rent_kzt",
    }
    remaining_json_chars = max(0, min(raw_json_budget_chars, MAX_AGGREGATE_JSON_CHARS))
    for evidence in evidence_rows:
        if evidence.status in {"error", "missing", "planned", "manual_required"}:
            continue
        raw_payload = evidence.raw_payload_json
        payload: dict[str, Any] = {}
        if raw_payload and len(raw_payload) <= min(MAX_JSON_CHARS, remaining_json_chars):
            payload = _safe_payload(raw_payload)
            remaining_json_chars -= len(raw_payload)
        provenance = _evidence_provenance(evidence)
        for key, payload_key in direct_fields.items():
            collector.add(key, payload.get(payload_key), provenance)
        for key, aliases in _RAW_ALIASES.items():
            collector.add(key, _payload_value(payload, aliases), provenance)

        if evidence.evidence_type == "document_extraction":
            _add_document_extraction_values(collector, evidence, payload)

        conflicts = payload.get("conflicts")
        if not isinstance(conflicts, list):
            continue
        for conflict in conflicts[:50]:
            if not isinstance(conflict, dict):
                continue
            key = str(conflict.get("field") or "")
            if key not in {
                "land_rights",
                "lease_term_years",
                "divisible",
                "additional_payment_kzt",
                "annual_rent_kzt",
            }:
                continue
            collector.forced_conflicts.add(key)
            if conflict.get("lot_value") not in (None, ""):
                collector.preferred_values[key] = conflict["lot_value"]
            collector.add(key, conflict.get("lot_value"), _lot_conflict_provenance(evidence))
            collector.add(key, conflict.get("source_object_value"), provenance)


def _add_document_extraction_values(
    collector: _Collector,
    evidence: AuctionEvidence,
    payload: dict[str, Any],
) -> None:
    result = payload.get("result")
    candidates = result.get("candidates") if isinstance(result, dict) else None
    if not isinstance(candidates, list):
        return
    field_map = {
        "right_type": "right_type",
        "lease_term_years": "lease_term_years",
        "target_purpose": "purpose",
        "divisibility": "divisible",
        "encumbrances": "encumbrances",
        "restrictions": "restrictions",
        "development_obligation": "development_obligation",
        "development_deadline": "development_deadline",
        "termination_ground": "termination_ground",
        "renewal_condition": "renewal_condition",
        "transfer_right": "transfer_right",
        "responsibility_penalty": "responsibility_penalty",
        "guarantee_payment_kzt": "guarantee_payment_kzt",
        "annual_payment_kzt": "annual_rent_kzt",
        "one_time_payment_kzt": "additional_payment_kzt",
    }
    llm_only_fields = {
        "development_obligation",
        "development_deadline",
        "termination_ground",
        "renewal_condition",
        "transfer_right",
        "responsibility_penalty",
    }
    for candidate in candidates[:100]:
        if not isinstance(candidate, dict):
            continue
        source_field = str(candidate.get("field") or "")
        target_field = field_map.get(source_field)
        value = candidate.get("value")
        if target_field is None or value in (None, ""):
            continue
        excerpt = str(candidate.get("evidence_excerpt") or "").strip()
        extractor_version = str(candidate.get("extractor_version") or "").casefold()
        if source_field in llm_only_fields and "llm" not in extractor_version:
            continue
        if "llm" in extractor_version:
            from app.auction_llm import candidate_is_grounded

            if not candidate_is_grounded(source_field, value, excerpt):
                continue
        if source_field == "right_type":
            value = _normalized_right_type(value)
            if value is None:
                continue
        if str(candidate.get("status") or "").casefold() == "conflict":
            # The extractor may identify an internal contradiction even when it
            # emits one bounded representative value. Preserve that explicit
            # review state instead of silently downgrading the clause to found.
            preferred_right = _normalized_right_type(
                collector.preferred_values.get("land_rights")
            )
            if source_field != "right_type" or value != preferred_right:
                collector.forced_conflicts.add(target_field)
        try:
            confidence = float(candidate.get("confidence"))
        except (TypeError, ValueError):
            confidence = float(evidence.confidence or 0)
        collector.add(
            target_field,
            value,
            LegalProvenance(
                source_url=evidence.source_url,
                observed_at=evidence.observed_at,
                confidence=max(0.0, min(confidence, 1.0)),
                evidence_type=evidence.evidence_type,
                evidence_id=evidence.id,
                document_id=_bounded_scalar(candidate.get("document_id"), max_chars=100),
                document_title=_bounded_text(candidate.get("document_title"), max_chars=300),
                page=_bounded_page(candidate.get("page")),
                section=_bounded_text(candidate.get("section"), max_chars=300),
                evidence_excerpt=_bounded_text(excerpt, max_chars=1_200),
                quote_hash=_bounded_text(candidate.get("quote_hash"), max_chars=128),
                content_hash=_bounded_text(candidate.get("content_hash"), max_chars=128),
            ),
        )


def _bounded_text(value: object, *, max_chars: int) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_chars] or None


def _bounded_scalar(value: object, *, max_chars: int) -> int | str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _bounded_text(value, max_chars=max_chars)


def _bounded_page(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if 1 <= page <= 10_000 else None


def _lot_conflict_provenance(evidence: AuctionEvidence) -> LegalProvenance:
    return LegalProvenance(
        source_url=None,
        observed_at=evidence.observed_at,
        confidence=0.88,
        evidence_type="official_lot_conflicting_value",
        evidence_id=evidence.id,
    )


def _derive_right_type(collector: _Collector) -> None:
    for candidate in collector.values.get("land_rights", []):
        right_type = _normalized_right_type(candidate.value)
        if right_type is not None:
            collector.add("right_type", right_type, candidate.provenance)
    if "land_rights" in collector.forced_conflicts:
        right_types = {
            _normalized(candidate.value) for candidate in collector.values.get("right_type", [])
        }
        if len(right_types) > 1:
            collector.forced_conflicts.add("right_type")
            preferred_right = _normalized_right_type(
                collector.preferred_values.get("land_rights")
            )
            if preferred_right is not None:
                collector.preferred_values["right_type"] = preferred_right
    if "right_type" not in collector.preferred_values:
        preferred_right = _normalized_right_type(
            collector.preferred_values.get("land_rights")
        )
        if preferred_right is not None:
            collector.preferred_values["right_type"] = preferred_right


def _payment_fact(
    key: str,
    amount: float | None,
    lot: AuctionLot,
    *,
    treatment: str,
    frequency: str,
) -> LegalFact:
    if amount is None:
        return _fact(key, [])
    value = {
        "amount_kzt": float(amount),
        "cost_treatment": treatment,
        "frequency": frequency,
    }
    return _fact(key, [_Candidate(value, _lot_provenance(lot))])


def _payment_from_candidates(
    key: str,
    candidates: list[_Candidate],
    *,
    treatment: str,
    frequency: str,
    forced_conflict: bool = False,
    preferred_amount: object | None = None,
) -> LegalFact:
    structured = [
        _Candidate(
            value={
                "amount_kzt": float(candidate.value),
                "cost_treatment": treatment,
                "frequency": frequency,
            },
            provenance=candidate.provenance,
        )
        for candidate in candidates
        if isinstance(candidate.value, (int, float))
    ]
    preferred_value = None
    if isinstance(preferred_amount, (int, float)):
        preferred_value = {
            "amount_kzt": float(preferred_amount),
            "cost_treatment": treatment,
            "frequency": frequency,
        }
    return _fact(
        key,
        structured,
        forced_conflict=forced_conflict,
        preferred_value=preferred_value,
    )


def get_auction_legal_passport(
    session: Session,
    lot_id: str,
    *,
    max_evidence: int = MAX_EVIDENCE,
    max_documents: int = MAX_DOCUMENTS,
    max_raw_json_chars: int = MAX_AGGREGATE_JSON_CHARS,
    generated_at: datetime | None = None,
) -> AuctionLegalPassport | None:
    """Build a bounded, read-only legal passport from already collected evidence."""
    lot = session.get(AuctionLot, lot_id)
    if lot is None:
        return None
    evidence_limit = max(1, min(int(max_evidence), MAX_EVIDENCE))
    document_limit = max(0, min(int(max_documents), MAX_DOCUMENTS))
    evidence_rows = list(
        session.scalars(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == lot.id,
                AuctionEvidence.evidence_type.in_(LEGAL_EVIDENCE_TYPES),
                AuctionEvidence.status.in_(LEGAL_EVIDENCE_STATUSES),
            )
            .order_by(AuctionEvidence.observed_at.desc(), AuctionEvidence.id.desc())
            .limit(evidence_limit)
        )
    )
    documents = list(
        session.scalars(
            select(AuctionDocument)
            .where(AuctionDocument.lot_id == lot.id)
            .order_by(AuctionDocument.id.asc())
            .limit(document_limit)
        )
    )
    collector = _Collector()
    _add_lot_values(collector, lot)
    _add_evidence_values(
        collector,
        evidence_rows,
        raw_json_budget_chars=max_raw_json_chars,
    )
    _derive_right_type(collector)
    facts = {
        key: _fact(
            key,
            collector.values.get(key, []),
            forced_conflict=key in collector.forced_conflicts,
            preferred_value=collector.preferred_values.get(key),
        )
        for key in (
            "right_type",
            "land_rights",
            "lease_term_years",
            "land_category",
            "purpose",
            "divisible",
            "arrests",
            "restrictions",
            "encumbrances",
            "development_conditions",
            "development_obligation",
            "development_deadline",
            "termination_ground",
            "renewal_condition",
            "transfer_right",
            "responsibility_penalty",
        )
    }
    payments = {
        "guarantee": _payment_from_candidates(
            "guarantee",
            collector.values.get("guarantee_payment_kzt", []),
            treatment="blocked_capital",
            frequency="once_before_auction",
            preferred_amount=collector.preferred_values.get("guarantee_payment_kzt"),
        ),
        "additional_payment": _payment_from_candidates(
            "additional_payment",
            collector.values.get("additional_payment_kzt", [])
            or (
                [_Candidate(lot.additional_payment_kzt, _lot_provenance(lot))]
                if lot.additional_payment_kzt is not None
                else []
            ),
            treatment="expense",
            frequency="once_after_win",
            forced_conflict="additional_payment_kzt" in collector.forced_conflicts,
            preferred_amount=collector.preferred_values.get("additional_payment_kzt"),
        ),
        "annual_rent": _payment_from_candidates(
            "annual_rent",
            collector.values.get("annual_rent_kzt", [])
            or (
                [_Candidate(lot.annual_rent_kzt, _lot_provenance(lot))]
                if lot.annual_rent_kzt is not None
                else []
            ),
            treatment="expense",
            frequency="annual_during_lease",
            forced_conflict="annual_rent_kzt" in collector.forced_conflicts,
            preferred_amount=collector.preferred_values.get("annual_rent_kzt"),
        ),
    }
    return AuctionLegalPassport(
        lot_id=lot.id,
        source_lot_id=lot.source_lot_id,
        generated_at=generated_at or datetime.now().astimezone(),
        facts=facts,
        payments=payments,
        documents=tuple(
            LegalDocumentRef(
                title=document.title,
                source_url=document.source_url,
                file_type=document.file_type,
                storage_status=document.storage_status,
            )
            for document in documents
        ),
    )


def cached_auction_legal_passport(
    session: Session,
    lot_id: str,
    *,
    ttl_seconds: int = 60,
) -> dict[str, object] | None:
    """Return an API-safe cache-aside passport payload.

    The key is versioned by lot/evidence timestamps and the bounded document set.
    Concurrent cold misses may duplicate the same bounded read.
    Production fallback is Redis-only by SharedJsonCache policy.
    """
    latest_evidence = (
        select(func.max(AuctionEvidence.observed_at))
        .where(
            AuctionEvidence.lot_id == AuctionLot.id,
            AuctionEvidence.evidence_type.in_(LEGAL_EVIDENCE_TYPES),
            AuctionEvidence.status.in_(LEGAL_EVIDENCE_STATUSES),
        )
        .scalar_subquery()
    )
    stamp = session.execute(
        select(AuctionLot.updated_at, latest_evidence)
        .where(AuctionLot.id == lot_id)
        .limit(1)
    ).one_or_none()
    if stamp is None:
        return None
    lot_updated_at, evidence_observed_at = stamp
    document_rows = session.execute(
        select(
            AuctionDocument.id,
            AuctionDocument.storage_status,
            AuctionDocument.downloaded_at,
            AuctionDocument.content_sha256,
        )
        .where(AuctionDocument.lot_id == lot_id)
        .order_by(AuctionDocument.id.asc())
        .limit(MAX_DOCUMENTS)
    ).all()
    document_stamp_payload = tuple(
        (
            document_id,
            storage_status,
            downloaded_at.isoformat() if downloaded_at else None,
            content_sha256,
        )
        for document_id, storage_status, downloaded_at, content_sha256 in document_rows
    )
    document_stamp = hashlib.sha256(
        repr(document_stamp_payload).encode("utf-8")
    ).hexdigest()
    cache_key = ":".join(
        (
            lot_id,
            PASSPORT_VERSION,
            lot_updated_at.isoformat() if lot_updated_at else "none",
            evidence_observed_at.isoformat() if evidence_observed_at else "none",
            document_stamp,
        )
    )
    cached = shared_json_cache.get("auction-legal-passport", cache_key)
    if isinstance(cached, dict):
        return cached
    passport = get_auction_legal_passport(session, lot_id)
    if passport is None:
        return None
    payload = passport.as_dict()
    shared_json_cache.set(
        "auction-legal-passport",
        cache_key,
        payload,
        ttl_seconds=max(5, min(int(ttl_seconds), 3600)),
    )
    return payload
