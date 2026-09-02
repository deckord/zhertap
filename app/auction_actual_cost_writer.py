"""Authoritative actual-cost source adapter and immutable evidence writer.

Integration requirement: before W11, the downstream decision-input adapter must
pair ``decision_cost_ranges`` with its latest ``decision_cost_ranges_sources``
manifest and reject a lot/scenario/policy/horizon mismatch. The strict W11 raw
payload intentionally remains limited to eight cost keys plus provenance refs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auction_decision_input_producers import (
    CostRangesResult,
    DocumentedMonetaryFact,
    produce_decision_cost_ranges,
)
from app.auction_price_ceiling import MAX_KZT, REQUIRED_COST_KEYS
from app.models import AuctionEvidence, AuctionLot

WRITER_VERSION = "authoritative-actual-cost-writer/2026.1"
POLICY_VERSION = "actual-cost-source-policy/2026.1"
STANDARD_INVESTMENT_POLICY_VERSION = "zhertap-standard-investment-policy/2026.1"
EVIDENCE_TYPE = "decision_cost_ranges"
QUARANTINE_EVIDENCE_TYPE = "decision_cost_ranges_quarantine"
SOURCE_EVIDENCE_TYPE = "decision_cost_ranges_sources"
MAX_FACTS = 64
MAX_PAYLOAD_BYTES = 64_000
MAX_QUARANTINE = 32
MAX_SOURCE_AGE = timedelta(days=365)
MIN_AUTHORITATIVE_CONFIDENCE = 0.5
ALLOWED_SOURCE_KINDS = frozenset(
    {
        "contractor_quote",
        "utility_quote",
        "invoice",
        "official_fee",
        "official_tax",
        "official_tariff",
        "connection_estimate",
        "financing_quote",
        "cost_plan",
        "risk_assessment",
    }
)
SOURCE_KINDS_BY_COST = {
    "connection": frozenset({"utility_quote", "invoice", "connection_estimate"}),
    "development": frozenset({"contractor_quote", "invoice"}),
    "registration": frozenset({"official_fee", "official_tariff", "invoice"}),
    "tax_annual": frozenset({"official_tax", "official_tariff"}),
    "due_diligence": frozenset({"contractor_quote", "invoice"}),
    "financing": frozenset({"financing_quote"}),
    "contingency": frozenset({"cost_plan"}),
    "risk_reserve": frozenset({"risk_assessment"}),
}
BASIS_BY_COST = {
    "connection": "one_time",
    "development": "one_time",
    "registration": "one_time",
    "tax_annual": "annual",
    "due_diligence": "one_time",
    "financing": "financing_horizon",
    "contingency": "one_time_reserve",
    "risk_reserve": "one_time_reserve",
}
EXCLUDED_CASH_ONLY_KEYS = frozenset(
    {"guarantee", "guarantee_payment", "additional_payment", "annual_rent"}
)
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_:./\-]{0,239}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/\-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCENARIO_HORIZON_MONTHS = {
    "resale": 12,
    "operating_business": 36,
    "land_rent": 60,
    "sublease": 36,
    "development": 60,
    "camping": 60,
    "hospitality": 84,
}


class ActualCostWriterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActualCostFact:
    target_lot_id: str
    scenario_key: str
    investment_policy_version: str
    holding_horizon_months: int
    cost_key: str
    low_kzt: int | float
    base_kzt: int | float
    high_kzt: int | float
    status: Literal["found", "unknown", "conflict"]
    source_kind: str
    source_identity: str
    source_ref: str
    source_url: str | None
    observed_at: datetime | None
    issued_at: datetime | None
    expires_at: datetime | None
    confidence: int | float
    source_version: str
    currency: str
    basis: str
    horizon_months: int | None = None
    generation_id: int | str | None = None


@dataclass(frozen=True, slots=True)
class QuarantinedCostFact:
    position: int
    source_ref: str | None
    reason: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ActualCostProduction:
    target_lot_id: str
    scenario_key: str
    investment_policy_version: str
    holding_horizon_months: int
    result: CostRangesResult
    quarantined: tuple[QuarantinedCostFact, ...]
    stale_keys: tuple[str, ...]
    excluded_keys: tuple[str, ...]
    source_manifest: tuple[dict[str, object], ...]
    quarantine_truncated: bool = False
    policy_version: str = POLICY_VERSION
    writer_version: str = WRITER_VERSION


@dataclass(frozen=True, slots=True)
class ActualCostPersistenceResult:
    status: Literal["written", "already_current"]
    evidence_id: int
    quarantine_evidence_id: int | None
    source_evidence_id: int | None
    generation_id: str


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _money(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric != int(numeric):
        return None
    normalized = int(numeric)
    return normalized if 0 <= normalized <= MAX_KZT else None


def _url(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 1_000:
        return None
    return value if value.startswith(("https://", "http://")) else None


def _strict_json(value: object, *, max_bytes: int = MAX_PAYLOAD_BYTES) -> str:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ActualCostWriterError("payload is not strict JSON") from exc
    if len(rendered.encode("utf-8")) > max_bytes:
        raise ActualCostWriterError("payload exceeds byte budget")
    return rendered


def _fingerprint(fact: object) -> str:
    material = repr(fact)[:4_000].encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()


def canonical_source_identity(
    source_kind: str,
    provider_name: str,
    record_id: str,
) -> str:
    values = (source_kind, provider_name, record_id)
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ActualCostWriterError("source identity components are required")
    normalized = [
        unicodedata.normalize("NFKC", value).strip().casefold() for value in values
    ]
    if any(len(value) > 300 for value in normalized):
        raise ActualCostWriterError("source identity component exceeds bound")
    return hashlib.sha256("\x1f".join(normalized).encode("utf-8")).hexdigest()


def _validate_fact(
    fact: object,
    *,
    as_of: datetime,
    target_lot_id: str,
    scenario_key: str,
    holding_horizon_months: int,
) -> tuple[ActualCostFact | None, str | None, bool]:
    if not isinstance(fact, ActualCostFact):
        return None, "invalid_fact_type", False
    if not isinstance(fact.cost_key, str) or not 1 <= len(fact.cost_key) <= 80:
        return None, "invalid_cost_key", False
    if fact.target_lot_id != target_lot_id:
        return None, "target_lot_mismatch", False
    if fact.scenario_key != scenario_key:
        return None, "scenario_mismatch", False
    if fact.investment_policy_version != STANDARD_INVESTMENT_POLICY_VERSION:
        return None, "investment_policy_mismatch", False
    if fact.holding_horizon_months != holding_horizon_months:
        return None, "holding_horizon_mismatch", False
    if fact.cost_key in EXCLUDED_CASH_ONLY_KEYS:
        return fact, None, True
    if fact.cost_key not in REQUIRED_COST_KEYS:
        return None, "unsupported_cost_key", False
    if fact.status not in {"found", "unknown", "conflict"}:
        return None, "invalid_status", False
    if fact.source_kind not in ALLOWED_SOURCE_KINDS:
        return None, "untrusted_source_kind", False
    if fact.source_kind not in SOURCE_KINDS_BY_COST[fact.cost_key]:
        return None, "source_kind_not_valid_for_cost", False
    if not isinstance(fact.source_identity, str) or not _SHA256.fullmatch(fact.source_identity):
        return None, "invalid_source_identity", False
    if fact.currency != "KZT":
        return None, "currency_must_be_kzt", False
    if fact.basis != BASIS_BY_COST[fact.cost_key]:
        return None, "invalid_cost_basis", False
    if fact.cost_key == "financing":
        if (
            isinstance(fact.horizon_months, bool)
            or not isinstance(fact.horizon_months, int)
            or fact.horizon_months != holding_horizon_months
        ):
            return None, "financing_horizon_required", False
    elif fact.horizon_months is not None:
        return None, "unexpected_horizon", False
    if not isinstance(fact.source_ref, str) or not _REF.fullmatch(fact.source_ref):
        return None, "invalid_source_ref", False
    if _url(fact.source_url) is None:
        return None, "missing_source_url", False
    if not isinstance(fact.source_version, str) or not _VERSION.fullmatch(
        fact.source_version
    ):
        return None, "invalid_source_version", False
    observed = _aware(fact.observed_at)
    issued = _aware(fact.issued_at)
    expires = _aware(fact.expires_at)
    if observed is None or issued is None:
        return None, "timestamp_unknown", False
    if observed > as_of + timedelta(minutes=5) or issued > observed + timedelta(minutes=5):
        return None, "timestamp_in_future", False
    if expires is not None and expires < issued:
        return None, "invalid_expiry", False
    if (
        isinstance(fact.confidence, bool)
        or not isinstance(fact.confidence, (int, float))
        or not math.isfinite(float(fact.confidence))
        or not MIN_AUTHORITATIVE_CONFIDENCE <= float(fact.confidence) <= 1
    ):
        return None, "invalid_confidence", False
    low, base, high = (_money(fact.low_kzt), _money(fact.base_kzt), _money(fact.high_kzt))
    if low is None or base is None or high is None or not low <= base <= high:
        return None, "invalid_money_range", False
    return fact, None, False


def produce_authoritative_actual_costs(
    facts: tuple[object, ...] | list[object],
    *,
    target_lot_id: str,
    scenario_key: str,
    as_of: datetime,
    max_source_age: timedelta = MAX_SOURCE_AGE,
) -> ActualCostProduction:
    checked = _aware(as_of)
    if checked is None:
        raise ActualCostWriterError("as_of must be timezone-aware")
    if not isinstance(target_lot_id, str) or not 1 <= len(target_lot_id) <= 64:
        raise ActualCostWriterError("invalid target lot id")
    holding_horizon_months = SCENARIO_HORIZON_MONTHS.get(scenario_key)
    if holding_horizon_months is None:
        raise ActualCostWriterError("invalid scenario key")
    if not isinstance(facts, (tuple, list)) or len(facts) > MAX_FACTS:
        raise ActualCostWriterError("fact input exceeds bound")
    if not timedelta(days=1) <= max_source_age <= timedelta(days=730):
        raise ActualCostWriterError("invalid source age policy")

    valid: list[tuple[int, ActualCostFact]] = []
    quarantined: list[QuarantinedCostFact] = []
    quarantine_total = 0
    invalid_governing: dict[str, list[tuple[datetime, str, int, str]]] = {}
    excluded: set[str] = set()
    for position, raw in enumerate(facts):
        fact, reason, cash_only = _validate_fact(
            raw,
            as_of=checked,
            target_lot_id=target_lot_id,
            scenario_key=scenario_key,
            holding_horizon_months=holding_horizon_months,
        )
        if cash_only and fact is not None:
            excluded.add(fact.cost_key)
            continue
        if fact is None or reason is not None:
            quarantine_total += 1
            raw_key = getattr(raw, "cost_key", None)
            raw_stamp = _aware(getattr(raw, "observed_at", None))
            raw_status = getattr(raw, "status", None)
            raw_identity = getattr(raw, "source_identity", None)
            if (
                raw_key in REQUIRED_COST_KEYS
                and raw_stamp is not None
                and raw_status in {"found", "unknown", "conflict"}
                and isinstance(raw_identity, str)
                and _SHA256.fullmatch(raw_identity)
            ):
                invalid_governing.setdefault(str(raw_key), []).append(
                    (raw_stamp, str(raw_status), position, raw_identity)
                )
            if len(quarantined) < MAX_QUARANTINE:
                ref = getattr(raw, "source_ref", None)
                quarantined.append(
                    QuarantinedCostFact(
                        position,
                        ref if isinstance(ref, str) and _REF.fullmatch(ref) else None,
                        reason or "invalid_fact",
                        _fingerprint(raw),
                    )
                )
            continue
        valid.append((position, fact))

    governing: dict[str, list[ActualCostFact]] = {}
    documented: list[DocumentedMonetaryFact] = []
    stale: set[str] = set()
    source_manifest: list[dict[str, object]] = []
    for key in REQUIRED_COST_KEYS:
        by_source: dict[str, list[ActualCostFact]] = {}
        for _, fact in valid:
            if fact.cost_key == key:
                by_source.setdefault(fact.source_identity, []).append(fact)
        invalid_by_source: dict[str, list[tuple[datetime, str, int, str]]] = {}
        for invalid in invalid_governing.get(key, []):
            invalid_by_source.setdefault(invalid[3], []).append(invalid)
        usable: list[ActualCostFact] = []
        blocked_conflicts: list[tuple[datetime, int]] = []
        saw_stale = False
        identities = sorted(set(by_source) | set(invalid_by_source))
        for identity in identities:
            candidates = by_source.get(identity, [])
            invalid = invalid_by_source.get(identity, [])
            newest_valid = max(
                (_aware(fact.observed_at) for fact in candidates),
                default=None,
            )
            newest_invalid = max((item[0] for item in invalid), default=None)
            if newest_invalid is not None and (
                newest_valid is None or newest_invalid >= newest_valid
            ):
                stamp, status, position, _ = max(invalid, key=lambda item: item[0])
                if status == "conflict":
                    blocked_conflicts.append((stamp, position))
                continue
            current = sorted(
                [
                fact for fact in candidates if _aware(fact.observed_at) == newest_valid
                ],
                key=lambda fact: (
                    fact.source_ref,
                    fact.source_url or "",
                    fact.source_version,
                    fact.low_kzt,
                    fact.base_kzt,
                    fact.high_kzt,
                ),
            )
            if not current:
                continue
            current_fresh: list[ActualCostFact] = []
            for fact in current:
                fact_issued = _aware(fact.issued_at)
                fact_expires = _aware(fact.expires_at)
                fact_is_stale = bool(
                    fact_issued is None
                    or checked - fact_issued > max_source_age
                    or (fact_expires is not None and fact_expires < checked)
                )
                source_manifest.append(
                    {
                        "target_lot_id": fact.target_lot_id,
                        "scenario_key": fact.scenario_key,
                        "investment_policy_version": fact.investment_policy_version,
                        "holding_horizon_months": fact.holding_horizon_months,
                        "cost_key": key,
                        "range": {
                            "low_kzt": _money(fact.low_kzt),
                            "base_kzt": _money(fact.base_kzt),
                            "high_kzt": _money(fact.high_kzt),
                        },
                        "currency": fact.currency,
                        "basis": fact.basis,
                        "horizon_months": fact.horizon_months,
                        "status": fact.status,
                        "freshness_status": "stale" if fact_is_stale else "fresh",
                        "source_kind": fact.source_kind,
                        "source_identity": fact.source_identity,
                        "source_ref": fact.source_ref,
                        "source_url": _url(fact.source_url),
                        "observed_at": _aware(fact.observed_at).isoformat(),  # type: ignore[union-attr]
                        "issued_at": fact_issued.isoformat(),  # type: ignore[union-attr]
                        "expires_at": (
                            fact_expires.isoformat() if fact_expires else None
                        ),
                        "confidence": float(fact.confidence),
                        "source_version": fact.source_version,
                    }
                )
                if fact_is_stale:
                    saw_stale = True
                else:
                    current_fresh.append(fact)
            ranges = {(fact.low_kzt, fact.base_kzt, fact.high_kzt) for fact in current}
            if any(fact.status == "conflict" for fact in current) or len(ranges) != 1:
                blocked_conflicts.append((newest_valid, 0))  # type: ignore[arg-type]
                continue
            if any(fact.status != "found" for fact in current):
                continue
            usable.extend(current_fresh)
        if usable:
            governing[key] = usable
        elif blocked_conflicts:
            stamp, position = max(blocked_conflicts, key=lambda item: item[0])
            documented.append(
                DocumentedMonetaryFact(
                    key,
                    0,
                    0,
                    0,
                    "conflict",
                    f"quarantine:cost:{position}",
                    None,
                    stamp,
                    POLICY_VERSION,
                )
            )
        elif saw_stale:
            stale.add(key)

    for key, candidates in governing.items():
        if any(fact.status == "conflict" for fact in candidates):
            leader = candidates[0]
            documented.append(
                DocumentedMonetaryFact(
                    key,
                    leader.low_kzt,
                    leader.base_kzt,
                    leader.high_kzt,
                    "conflict",
                    leader.source_ref,
                    leader.source_url,
                    leader.observed_at,
                    leader.generation_id,
                )
            )
            continue
        ranges = {(fact.low_kzt, fact.base_kzt, fact.high_kzt) for fact in candidates}
        if len(ranges) != 1 or any(fact.status != "found" for fact in candidates):
            leader = candidates[0]
            documented.append(
                DocumentedMonetaryFact(
                    key,
                    leader.low_kzt,
                    leader.base_kzt,
                    leader.high_kzt,
                    "conflict" if len(ranges) != 1 else "unknown",
                    leader.source_ref,
                    leader.source_url,
                    leader.observed_at,
                    leader.generation_id,
                )
            )
            continue
        leader = candidates[0]
        issued = _aware(leader.issued_at)
        expires = _aware(leader.expires_at)
        if issued is None or checked - issued > max_source_age or (
            expires is not None and expires < checked
        ):
            stale.add(key)
            continue
        for fact in candidates:
            documented.append(
                DocumentedMonetaryFact(
                    key,
                    fact.low_kzt,
                    fact.base_kzt,
                    fact.high_kzt,
                    "found",
                    fact.source_ref,
                    fact.source_url,
                    fact.observed_at,
                    fact.generation_id or fact.source_version,
                )
            )
    documented.sort(
        key=lambda fact: (
            fact.cost_key,
            fact.source_ref,
            fact.low_kzt,
            fact.base_kzt,
            fact.high_kzt,
        )
    )
    source_manifest.sort(
        key=lambda item: (
            str(item["cost_key"]),
            str(item["source_identity"]),
            str(item["source_ref"]),
        )
    )
    result = produce_decision_cost_ranges(documented)
    return ActualCostProduction(
        target_lot_id,
        scenario_key,
        STANDARD_INVESTMENT_POLICY_VERSION,
        holding_horizon_months,
        result,
        tuple(quarantined),
        tuple(key for key in REQUIRED_COST_KEYS if key in stale),
        tuple(sorted(excluded)),
        tuple(source_manifest),
        quarantine_total > len(quarantined),
    )


def _persistence_payload(production: ActualCostProduction) -> str:
    return _strict_json(production.result.evidence_payload())


def _marker(production: ActualCostProduction) -> str:
    audit_material = {
        "target_lot_id": production.target_lot_id,
        "scenario_key": production.scenario_key,
        "investment_policy_version": production.investment_policy_version,
        "holding_horizon_months": production.holding_horizon_months,
        "sources": production.source_manifest,
        "quarantine": [
            (item.position, item.source_ref, item.reason, item.fingerprint)
            for item in production.quarantined
        ],
        "quarantine_truncated": production.quarantine_truncated,
        "stale": production.stale_keys,
        "excluded": production.excluded_keys,
    }
    audit_hash = hashlib.sha256(_strict_json(audit_material).encode()).hexdigest()
    return (
        f"idempotency:{WRITER_VERSION}:{POLICY_VERSION}:"
        f"{production.result.generation_id}:{production.result.status}:{audit_hash}"
    )


def persist_actual_cost_evidence(
    session: Session,
    *,
    lot_id: str,
    production: ActualCostProduction,
    written_at: datetime,
) -> ActualCostPersistenceResult:
    """Append immutable evidence in a caller-owned short transaction; never commits."""
    checked = _aware(written_at)
    if checked is None or not isinstance(lot_id, str) or not 1 <= len(lot_id) <= 64:
        raise ActualCostWriterError("invalid persistence boundary")
    if production.target_lot_id != lot_id:
        raise ActualCostWriterError("production target lot mismatch")
    if (
        production.investment_policy_version != STANDARD_INVESTMENT_POLICY_VERSION
        or SCENARIO_HORIZON_MONTHS.get(production.scenario_key)
        != production.holding_horizon_months
    ):
        raise ActualCostWriterError("production scenario policy mismatch")
    lot = session.scalar(
        select(AuctionLot).where(AuctionLot.id == lot_id).with_for_update()
    )
    if lot is None:
        raise ActualCostWriterError("unknown auction lot")
    marker = _marker(production)
    latest = session.scalar(
        select(AuctionEvidence)
        .where(
            AuctionEvidence.lot_id == lot_id,
            AuctionEvidence.evidence_type == EVIDENCE_TYPE,
        )
        .order_by(AuctionEvidence.id.desc())
        .limit(1)
    )
    if latest is not None and latest.value_text == marker:
        return ActualCostPersistenceResult(
            "already_current",
            int(latest.id),
            None,
            None,
            production.result.generation_id,
        )
    metadata = production.result.persistence_metadata()
    title = (
        f"Actual costs {production.result.status}; "
        f"missing={len(production.result.missing_keys)}; "
        f"conflicts={len(production.result.conflict_keys)}; "
        f"stale={len(production.stale_keys)}; policy={POLICY_VERSION}"
    )[:320]
    evidence = AuctionEvidence(
        lot_id=lot_id,
        evidence_type=EVIDENCE_TYPE,
        status="found",
        title=title,
        value_text=marker,
        source_url=None,
        confidence=1.0 if production.result.status == "complete" else 0.0,
        raw_payload_json=_persistence_payload(production),
        observed_at=production.result.observed_at or checked,
    )
    session.add(evidence)
    session.flush()
    source_evidence = AuctionEvidence(
        lot_id=lot_id,
        evidence_type=SOURCE_EVIDENCE_TYPE,
        status="found" if production.source_manifest else "unknown",
        title=f"Actual cost source manifest: {len(production.source_manifest)}"[:320],
        value_text=f"generation:{production.result.generation_id}",
        confidence=(
            min(
                (float(item["confidence"]) for item in production.source_manifest),
                default=0.0,
            )
        ),
        raw_payload_json=_strict_json(
            {
                "writer_version": WRITER_VERSION,
                "policy_version": POLICY_VERSION,
                "investment_policy_version": production.investment_policy_version,
                "scenario_key": production.scenario_key,
                "holding_horizon_months": production.holding_horizon_months,
                "generation_id": production.result.generation_id,
                "sources": list(production.source_manifest),
            }
        ),
        observed_at=production.result.observed_at or checked,
    )
    session.add(source_evidence)
    session.flush()
    quarantine_id: int | None = None
    if production.quarantined:
        quarantine_payload = {
            "writer_version": WRITER_VERSION,
            "policy_version": POLICY_VERSION,
            "investment_policy_version": production.investment_policy_version,
            "scenario_key": production.scenario_key,
            "holding_horizon_months": production.holding_horizon_months,
            "generation_id": production.result.generation_id,
            "items": [
                {
                    "position": item.position,
                    "source_ref": item.source_ref,
                    "reason": item.reason,
                    "fingerprint": item.fingerprint,
                }
                for item in production.quarantined
            ],
            "truncated": production.quarantine_truncated,
        }
        quarantine = AuctionEvidence(
            lot_id=lot_id,
            evidence_type=QUARANTINE_EVIDENCE_TYPE,
            status="conflict",
            title=f"Actual cost facts quarantined: {len(production.quarantined)}"[:320],
            value_text=f"generation:{production.result.generation_id}",
            confidence=0.0,
            raw_payload_json=_strict_json(quarantine_payload),
            observed_at=checked,
        )
        session.add(quarantine)
        session.flush()
        quarantine_id = int(quarantine.id)
    # Keep metadata constructed and validated at the trusted boundary; it is
    # intentionally summarized in title/value_text to preserve W11 raw shape.
    if metadata["generation_id"] != production.result.generation_id:
        raise ActualCostWriterError("producer metadata mismatch")
    return ActualCostPersistenceResult(
        "written",
        int(evidence.id),
        quarantine_id,
        int(source_evidence.id),
        production.result.generation_id,
    )
