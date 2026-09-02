from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from app.auction_market_comparables import ENGINE_VERSION as STRICT_MARKET_ENGINE_VERSION
from app.auction_price_ceiling import REQUIRED_COST_KEYS
from app.auction_taxonomy import (
    UNCLASSIFIED_SCENARIO,
    classify_scenario,
    select_decision_scenario,
)

ASSEMBLER_VERSION = "decision-input-assembler/2026.4"
POLICY_VERSION = "zhertap-standard-investment-policy/2026.1"
MAX_ARTIFACT_BYTES = 64_000
MAX_OUTPUT_BYTES = 512_000
MAX_PERSISTED_TOTAL_BYTES = 1_000_000
MAX_EXTRACTIONS = 20
MAX_CANDIDATES = 100
MAX_REFS = 100
MAX_REF_LENGTH = 240
REQUIRED_MODULES = (
    "legal_passport",
    "geometry_context",
    "restriction_context",
    "site_context",
    "planning_context",
    "history_reference",
    "market_estimate",
)

ArtifactStatus = Literal["found", "partial", "conflict", "unknown", "error"]


@dataclass(frozen=True, slots=True)
class StandardInvestmentPolicy:
    holding_period_years: str
    target_roi_percent: str
    target_margin_percent: str
    label: str


STANDARD_POLICY: dict[str, StandardInvestmentPolicy] = {
    "resale": StandardInvestmentPolicy("1", "20", "15", "Перепродажа: горизонт 1 год"),
    "operating_business": StandardInvestmentPolicy(
        "3", "25", "20", "Операционный бизнес: горизонт 3 года"
    ),
    "land_rent": StandardInvestmentPolicy("5", "20", "15", "Арендный доход: 5 лет"),
    "sublease": StandardInvestmentPolicy("3", "25", "20", "Субаренда: 3 года"),
    "development": StandardInvestmentPolicy("5", "30", "25", "Девелопмент: 5 лет"),
    "camping": StandardInvestmentPolicy("5", "30", "25", "Кемпинг: 5 лет"),
    "hospitality": StandardInvestmentPolicy("7", "30", "25", "Гостиница: 7 лет"),
}


@dataclass(frozen=True, slots=True)
class DecisionLotFacts:
    lot_id: str
    source_lot_id: str
    updated_at: datetime
    start_price_kzt: int | float | None
    purpose_text: str


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    payload: Mapping[str, object]
    status: ArtifactStatus
    observed_at: datetime | None
    generation_id: int | str | None = None
    coverage_complete: bool = False
    provenance_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractCoverage:
    eligible_document_ids: tuple[str, ...]
    processed_document_ids: tuple[str, ...]
    observed_at: datetime
    generation_id: int | str
    coverage_complete: bool = False


@dataclass(frozen=True, slots=True)
class DecisionInputAssembly:
    module_outputs: dict[str, object]
    input_hash: str
    persistence_payloads: dict[str, str]
    assembler_version: str = ASSEMBLER_VERSION
    policy_version: str = POLICY_VERSION

    def as_dict(self) -> dict[str, object]:
        return {
            "assembler_version": self.assembler_version,
            "policy_version": self.policy_version,
            "input_hash": self.input_hash,
            "module_outputs": self.module_outputs,
            "persistence_payloads": self.persistence_payloads,
        }


class DecisionInputError(ValueError):
    pass


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise DecisionInputError("naive datetime is not allowed")
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def _canonical(value: object, *, maximum: int, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise DecisionInputError(f"{label} is not strict JSON: {exc}") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise DecisionInputError(f"{label} exceeds byte budget")
    return encoded


def _mapping(value: object | None, *, label: str) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = dict(value)
    else:
        as_dict_method = getattr(value, "as_dict", None)
        if not callable(as_dict_method):
            raise DecisionInputError(f"{label} must be an object or expose as_dict")
        raw = as_dict_method()
        if not isinstance(raw, dict):
            raise DecisionInputError(f"{label}.as_dict must return an object")
    return json.loads(_canonical(raw, maximum=MAX_ARTIFACT_BYTES, label=label))


def _aware(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DecisionInputError(f"{label} must be timezone-aware")
    return value


def _ref(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned if 0 < len(cleaned) <= MAX_REF_LENGTH else None


def _refs(values: Sequence[object]) -> list[str]:
    if len(values) > MAX_REFS:
        raise DecisionInputError("too many provenance references")
    result = []
    for value in values:
        cleaned = _ref(value)
        if cleaned is None:
            raise DecisionInputError("invalid provenance reference")
        result.append(cleaned)
    return list(dict.fromkeys(result))


def _exact_kzt(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer() or not 0 <= numeric <= 10**15:
        return None
    return int(numeric)


def _exact_range(amount: int, refs: Sequence[str] = ()) -> dict[str, object]:
    result: dict[str, object] = {
        "low_kzt": amount,
        "base_kzt": amount,
        "high_kzt": amount,
    }
    if refs:
        result["provenance_refs"] = list(refs)
    return result


def _fact(container: dict[str, object] | None, section: str, key: str) -> dict[str, object]:
    if not container:
        return {"status": "unknown", "value": None, "provenance": []}
    raw_section = container.get(section)
    if not isinstance(raw_section, dict):
        return {"status": "unknown", "value": None, "provenance": []}
    raw = raw_section.get(key)
    return raw if isinstance(raw, dict) else {"status": "unknown", "value": None, "provenance": []}


def _fact_refs(fact: dict[str, object], prefix: str) -> list[str]:
    result = []
    provenance = fact.get("provenance")
    if isinstance(provenance, list):
        for item in provenance[:MAX_REFS]:
            if not isinstance(item, dict):
                continue
            evidence_id = item.get("evidence_id")
            source_url = item.get("source_url")
            suffix = str(evidence_id) if evidence_id is not None else str(source_url or "source")
            cleaned = _ref(f"legal:{prefix}:{suffix}"[:MAX_REF_LENGTH])
            if cleaned:
                result.append(cleaned)
    source_url = fact.get("source_url")
    if not result and source_url:
        cleaned = _ref(f"legal:{prefix}:{source_url}"[:MAX_REF_LENGTH])
        if cleaned:
            result.append(cleaned)
    return list(dict.fromkeys(result))


def _payment(
    passport: dict[str, object] | None,
    key: str,
) -> tuple[int | None, str, list[str], str | None]:
    fact = _fact(passport, "payments", key)
    status = str(fact.get("status") or "unknown")
    value = fact.get("value")
    structured = value if isinstance(value, dict) else {}
    amount = _exact_kzt(structured.get("amount_kzt"))
    treatment = structured.get("cost_treatment")
    return amount, status, _fact_refs(fact, f"payment:{key}"), str(treatment) if treatment else None


def _explicit_negative(value: object) -> bool:
    if value is False:
        return True
    text = " ".join(str(value or "").casefold().split())
    return any(
        marker in text
        for marker in (
            "не имеются",
            "не имеется",
            "отсутствуют",
            "отсутствует",
            "нет ограничений",
            "жоқ",
        )
    )


def _fact_observed_at(fact: dict[str, object]) -> datetime | None:
    raw = fact.get("observed_at")
    if not isinstance(raw, str):
        return None
    try:
        observed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return observed if observed.tzinfo is not None and observed.utcoffset() is not None else None


def _freshness(
    status: ArtifactStatus,
    observed_at: datetime | None,
    assembled_at: datetime,
    *,
    max_age_days: int = 90,
) -> dict[str, object]:
    if status == "error":
        return {"status": "error", "observed_at": observed_at.isoformat() if observed_at else None}
    if observed_at is None:
        return {"status": "unknown", "observed_at": None}
    observed = _aware(observed_at, label="artifact.observed_at")
    if observed > assembled_at + timedelta(minutes=5):
        raise DecisionInputError("artifact observed_at is in the future")
    return {
        "status": "stale" if assembled_at - observed > timedelta(days=max_age_days) else "fresh",
        "observed_at": observed.isoformat(),
    }


def _artifact_module(
    name: str,
    artifact: EvidenceArtifact | None,
    assembled_at: datetime,
) -> tuple[dict[str, object], dict[str, object], object, list[str], list[str]]:
    if artifact is None:
        return (
            {"status": "unknown", "provenance_refs": []},
            {"status": "unknown", "observed_at": None},
            None,
            [],
            [f"module_incomplete:{name}"],
        )
    if artifact.status not in {"found", "partial", "conflict", "unknown", "error"}:
        raise DecisionInputError(f"invalid artifact status: {name}")
    payload = _mapping(artifact.payload, label=name) or {}
    refs = _refs(artifact.provenance_refs)
    raw_payload_refs = payload.get("provenance_refs")
    payload_refs = raw_payload_refs if isinstance(raw_payload_refs, list) else []
    payload["provenance_refs"] = list(dict.fromkeys([*_refs(payload_refs), *refs]))
    freshness = _freshness(artifact.status, artifact.observed_at, assembled_at)
    payload_complete = True
    if name == "geometry_context":
        payload_complete = payload.get("status") == "ok"
    elif name == "restriction_context":
        payload_complete = (
            payload.get("status") in {"clear", "restricted"}
            and payload.get("coverage_complete") is True
        )
    elif name == "planning_context":
        payload_complete = payload.get("status") == "clear" and payload.get("pdp_complete") is True
    elif name == "site_context":
        payload_complete = all(
            payload.get(key) in {"ready", "attention", "blocked"}
            for key in (
                "physical_access_status",
                "legal_access_status",
                "infrastructure_status",
                "capacity_status",
            )
        )
    incomplete = (
        artifact.status != "found"
        or not artifact.coverage_complete
        or not payload_complete
        or freshness["status"] != "fresh"
    )
    reasons = [f"module_incomplete:{name}"] if incomplete else []
    return payload, freshness, artifact.generation_id, refs, reasons


def _contract_summary(
    extractions: Sequence[object],
    coverage: ContractCoverage | None,
) -> tuple[
    dict[str, object],
    bool,
    list[str],
    list[str],
    list[dict[str, object]],
]:
    if len(extractions) > MAX_EXTRACTIONS:
        raise DecisionInputError("too many contract extraction results")
    total_candidates = 0
    refs: list[str] = []
    statuses = []
    conflicts = []
    risks: list[dict[str, object]] = []
    for index, extraction in enumerate(extractions):
        payload = _mapping(extraction, label=f"contract_extraction[{index}]") or {}
        status = str(payload.get("status") or "unknown")
        statuses.append(status)
        raw_candidates = payload.get("candidates")
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        total_candidates += len(candidates)
        if total_candidates > MAX_CANDIDATES:
            raise DecisionInputError("contract candidate limit exceeded")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            quote_hash = str(candidate.get("quote_hash") or "")[:24]
            document_id = str(candidate.get("document_id") or index)[:64]
            ref = _ref(f"contract:{document_id}:{quote_hash}")
            if ref:
                refs.append(ref)
            field = str(candidate.get("field") or "").strip()
            candidate_status = str(candidate.get("status") or "").strip().lower()
            if field and candidate_status == "conflict":
                conflicts.append(field)
            if field in {
                "development_obligation",
                "termination_ground",
                "responsibility_penalty",
            }:
                risks.append(
                    {
                        "code": f"CONTRACT_{field.upper()}_REVIEW",
                        "evidence_refs": [ref] if ref else [],
                    }
                )
        raw_conflicts = payload.get("conflicts")
        if isinstance(raw_conflicts, list):
            conflicts.extend(
                str(item.get("field")) for item in raw_conflicts if isinstance(item, dict)
            )
    coverage_valid = False
    eligible_ids: tuple[str, ...] = ()
    processed_ids: tuple[str, ...] = ()
    if coverage is not None:
        if (
            len(coverage.eligible_document_ids) > MAX_EXTRACTIONS
            or len(coverage.processed_document_ids) > MAX_EXTRACTIONS
        ):
            raise DecisionInputError("contract coverage document limit exceeded")
        for values in (coverage.eligible_document_ids, coverage.processed_document_ids):
            if any(
                not isinstance(value, str) or not value.strip() or len(value) > 128
                for value in values
            ):
                raise DecisionInputError("invalid contract coverage document id")
            if len(set(values)) != len(values):
                raise DecisionInputError("duplicate contract coverage document id")
        _aware(coverage.observed_at, label="contract_coverage.observed_at")
        if (
            isinstance(coverage.generation_id, bool)
            or not isinstance(coverage.generation_id, (int, str))
            or isinstance(coverage.generation_id, int)
            and coverage.generation_id < 0
            or isinstance(coverage.generation_id, str)
            and (not coverage.generation_id or len(coverage.generation_id) > 128)
        ):
            raise DecisionInputError("invalid contract coverage generation id")
        eligible_ids = coverage.eligible_document_ids
        processed_ids = coverage.processed_document_ids
        coverage_valid = (
            coverage.coverage_complete
            and bool(eligible_ids)
            and set(eligible_ids) == set(processed_ids)
            and len(extractions) == len(processed_ids)
        )
    complete = coverage_valid and all(status == "ok" for status in statuses) and not conflicts
    reasons = [] if complete else ["contract_missing_or_unresolved"]
    return (
        {
            "status": "found" if complete else "conflict" if conflicts else "unknown",
            "documents_processed": len(statuses),
            "eligible_document_count": len(eligible_ids),
            "coverage_complete": coverage_valid,
            "candidate_count": total_candidates,
            "conflict_fields": sorted(set(conflicts)),
            "provenance_refs": list(dict.fromkeys(refs))[:MAX_REFS],
            "candidates_are_unconfirmed": True,
        },
        complete,
        list(dict.fromkeys(refs))[:MAX_REFS],
        reasons,
        risks[:MAX_REFS],
    )


def _legal_parts(
    passport: dict[str, object] | None,
    contract_complete: bool,
) -> tuple[
    dict[str, object],
    dict[str, object],
    list[dict[str, object]],
    list[dict[str, object]],
    list[str],
    bool,
]:
    right = _fact(passport, "facts", "right_type")
    lease = _fact(passport, "facts", "lease_term_years")
    purpose = _fact(passport, "facts", "purpose")
    legal_facts = {
        key: _fact(passport, "facts", key) for key in ("arrests", "restrictions", "encumbrances")
    }
    refs = list(
        dict.fromkeys(
            ref
            for key, fact in (
                ("right", right),
                ("lease", lease),
                ("purpose", purpose),
                *legal_facts.items(),
            )
            for ref in _fact_refs(fact, key)
        )
    )[:MAX_REFS]
    conflicts = any(
        fact.get("status") == "conflict" for fact in (right, lease, purpose, *legal_facts.values())
    )
    critical_known = all(
        fact.get("status") == "found" for fact in (right, purpose, *legal_facts.values())
    )
    blockers = []
    risks = []
    for key, fact in legal_facts.items():
        if fact.get("status") == "found" and not _explicit_negative(fact.get("value")):
            target = blockers if key == "arrests" else risks
            target.append(
                {
                    "code": f"LEGAL_{key.upper()}_FOUND",
                    "evidence_refs": _fact_refs(fact, key),
                }
            )
    right_value = right.get("value") if right.get("status") == "found" else "unknown"
    if right_value not in {"ownership", "lease"}:
        right_value = "unknown"
    lease_value = lease.get("value") if lease.get("status") == "found" else None
    if isinstance(lease_value, bool) or not isinstance(lease_value, (int, float)):
        lease_value = None
    scenario_right = {
        "type": right_value,
        "lease_years": float(lease_value) if lease_value is not None else None,
        "transferable": None,
        "renewable": None,
        "sublease_allowed": None,
        "provenance_refs": refs,
    }
    legal_status = "conflict" if conflicts else "clear" if critical_known else "unknown"
    scenario_legal = {
        "status": legal_status,
        "use_allowed": True if purpose.get("status") == "found" else None,
        "provenance_refs": refs,
    }
    complete = critical_known and not conflicts and contract_complete
    return scenario_right, scenario_legal, blockers, risks, refs, complete


def _market_module(
    value: object | None,
) -> tuple[dict[str, object], list[str], bool, datetime | None]:
    raw = _mapping(value, label="market_result") or {
        "status": "insufficient_data",
        "estimate": None,
        "confidence": "none",
        "high_quality_verified_count": 0,
        "verified_eligible_count": 0,
        "engine_version": STRICT_MARKET_ENGINE_VERSION,
    }
    evaluations = raw.pop("evaluations", [])
    refs = []
    observed_values = []
    if isinstance(evaluations, list):
        for item in evaluations[:MAX_CANDIDATES]:
            if not isinstance(item, dict):
                continue
            if (
                item.get("eligible")
                and item.get("quality_grade") == "A"
                and item.get("price_kind") == "verified_sale"
            ):
                ref = _ref(f"market:{item.get('source_id')}:{item.get('source_record_id')}")
                if ref:
                    refs.append(ref)
                observed_at = item.get("observed_at")
                if isinstance(observed_at, str):
                    try:
                        observed_values.append(datetime.fromisoformat(observed_at))
                    except ValueError:
                        pass
    high_quality = raw.get("high_quality_verified_count")
    estimate = raw.get("estimate")
    used = estimate.get("verified_comparables_used") if isinstance(estimate, dict) else None
    accepted = (
        raw.get("status") == "ok"
        and raw.get("engine_version") == STRICT_MARKET_ENGINE_VERSION
        and isinstance(high_quality, int)
        and not isinstance(high_quality, bool)
        and high_quality >= 3
        and isinstance(estimate, dict)
        and isinstance(used, int)
        and not isinstance(used, bool)
        and used >= 3
        and len(refs) >= 3
    )
    if not accepted:
        raw["status"] = "insufficient_data"
        raw["estimate"] = None
        raw["confidence"] = "none"
    raw["provenance_refs"] = list(dict.fromkeys(refs))[:MAX_REFS]
    observed = min(
        (
            item
            for item in observed_values
            if item.tzinfo is not None and item.utcoffset() is not None
        ),
        default=None,
    )
    return raw, raw["provenance_refs"], accepted, observed


def _history_module(artifact: EvidenceArtifact | None) -> tuple[dict[str, object], list[str]]:
    if artifact is None:
        return {
            "status": "insufficient_data",
            "competition_reference": "No active normalized history reference",
            "provenance_refs": [],
            "audit_only": True,
        }, []
    payload = _mapping(artifact.payload, label="history_reference") or {}
    refs = _refs(artifact.provenance_refs)
    ratio = payload.get("median_sale_to_start_ratio")
    matched = payload.get("matched_count", 0)
    note = (
        f"Normalized auction history: matched={matched}; median sale/start={ratio}"
        if ratio is not None
        else f"Normalized auction history: matched={matched}; sale/start median unavailable"
    )
    return {
        "status": payload.get("status", "insufficient_data"),
        "competition_reference": note[:240],
        "provenance_refs": refs,
        "audit_only": True,
    }, refs


def assemble_decision_input(
    lot: DecisionLotFacts,
    *,
    scenario_key: str,
    legal_passport: object | None,
    contract_extractions: Sequence[object] = (),
    contract_coverage: ContractCoverage | None = None,
    geometry_context: EvidenceArtifact | None = None,
    restriction_context: EvidenceArtifact | None = None,
    site_context: EvidenceArtifact | None = None,
    planning_context: EvidenceArtifact | None = None,
    history_reference: EvidenceArtifact | None = None,
    market_result: object | None = None,
    actual_cost_ranges: Mapping[str, object] | None = None,
    assembled_at: datetime | None = None,
) -> DecisionInputAssembly:
    """Assemble bounded worker inputs without network, writes or account policy."""
    if not isinstance(lot.lot_id, str) or not lot.lot_id or len(lot.lot_id) > 128:
        raise DecisionInputError("invalid lot id")
    if (
        not isinstance(lot.source_lot_id, str)
        or not lot.source_lot_id
        or len(lot.source_lot_id) > 128
        or not isinstance(lot.purpose_text, str)
        or len(lot.purpose_text) > 4_000
    ):
        raise DecisionInputError("invalid lot source or purpose")
    selection = select_decision_scenario(lot.purpose_text)
    expected_scenario = selection.scenario_key or UNCLASSIFIED_SCENARIO
    if scenario_key != expected_scenario:
        raise DecisionInputError("scenario does not match canonical purpose selector")
    if scenario_key != UNCLASSIFIED_SCENARIO and scenario_key not in STANDARD_POLICY:
        raise DecisionInputError("unsupported standard-policy scenario")
    checked = _aware(assembled_at or datetime.now(UTC), label="assembled_at")
    _aware(lot.updated_at, label="lot.updated_at")
    passport = _mapping(legal_passport, label="legal_passport")
    contract, contract_complete, contract_refs, contract_reasons, contract_risks = (
        _contract_summary(contract_extractions, contract_coverage)
    )
    scenario_right, scenario_legal, blockers, legal_risks, legal_refs, legal_complete = (
        _legal_parts(passport, contract_complete)
    )
    profile = classify_scenario(lot.purpose_text)
    if profile not in {
        "retail",
        "roadside",
        "warehouse",
        "hospitality",
        "camping",
        "residential",
        "data_center",
        "agriculture",
        "other",
    }:
        profile = "other"

    module_artifacts = {
        "geometry_context": geometry_context,
        "restriction_context": restriction_context,
        "site_context": site_context,
        "planning_context": planning_context,
    }
    modules: dict[str, object] = {}
    freshness: dict[str, object] = {}
    generations: dict[str, object] = {}
    stale_reasons: list[str] = []
    artifact_refs: list[str] = []
    for name, artifact in module_artifacts.items():
        payload, source_freshness, generation, refs, reasons = _artifact_module(
            name, artifact, checked
        )
        modules[name] = payload
        freshness[name] = source_freshness
        generations[name] = generation
        artifact_refs.extend(refs)
        stale_reasons.extend(reasons)

    history_module, history_refs = _history_module(history_reference)
    modules["history_reference"] = history_module
    history_status = history_reference.status if history_reference else "unknown"
    history_observed = history_reference.observed_at if history_reference else None
    freshness["history_reference"] = _freshness(history_status, history_observed, checked)
    generations["history_reference"] = (
        history_reference.generation_id if history_reference else None
    )
    if (
        history_reference is None
        or history_reference.status != "found"
        or not history_reference.coverage_complete
        or history_module["status"] != "ok"
    ):
        stale_reasons.append("module_incomplete:history_reference")

    market_module, market_refs, market_complete, market_observed = _market_module(market_result)
    modules["market_estimate"] = market_module
    market_freshness = _freshness(
        "found" if market_result is not None else "unknown", market_observed, checked
    )
    freshness["market_estimate"] = market_freshness
    generations["market_estimate"] = market_module.get("engine_version")
    if not market_complete:
        stale_reasons.append("module_incomplete:market_estimate")

    legal_observed = None
    if passport:
        critical_keys = [
            "right_type",
            "purpose",
            "arrests",
            "restrictions",
            "encumbrances",
        ]
        if scenario_right["type"] == "lease":
            critical_keys.append("lease_term_years")
        critical_observations = [
            _fact_observed_at(_fact(passport, "facts", key)) for key in critical_keys
        ]
        if all(item is not None for item in critical_observations):
            legal_observed = min(item for item in critical_observations if item is not None)
    freshness["legal_passport"] = _freshness(
        "found" if passport else "unknown", legal_observed, checked
    )
    generations["legal_passport"] = passport.get("version") if passport else None
    modules["legal_passport"] = {
        "status": scenario_legal["status"],
        "version": passport.get("version") if passport else None,
        "critical_facts_complete": legal_complete,
        "provenance_refs": legal_refs,
    }
    if not legal_complete:
        stale_reasons.append("module_incomplete:legal_passport")
    stale_reasons.extend(contract_reasons)
    contract_status: ArtifactStatus = "found" if contract_complete else "partial"
    contract_observed = contract_coverage.observed_at if contract_coverage else None
    freshness["contract_extraction"] = _freshness(
        contract_status,
        contract_observed,
        checked,
    )
    generations["contract_extraction"] = (
        contract_coverage.generation_id if contract_coverage else None
    )

    guarantee, guarantee_status, guarantee_refs, guarantee_treatment = _payment(
        passport, "guarantee"
    )
    additional, additional_status, additional_refs, additional_treatment = _payment(
        passport, "additional_payment"
    )
    annual, annual_status, annual_refs, annual_treatment = _payment(passport, "annual_rent")
    if guarantee_treatment not in {None, "blocked_capital"}:
        guarantee = None
        stale_reasons.append("guarantee_treatment_conflict")
    one_time = []
    if (
        additional is not None
        and additional_status == "found"
        and additional_treatment == "expense"
    ):
        one_time.append(
            {
                "id": "additional-payment",
                "amount": _exact_range(additional, additional_refs),
            }
        )
    annual_required = scenario_right["type"] == "lease"
    annual_payment = (
        {
            "id": "annual-rent",
            "amount": _exact_range(annual, annual_refs),
        }
        if annual is not None and annual_status == "found" and annual_treatment == "expense"
        else None
    )
    payments_complete = (
        contract_complete
        and additional_status == "found"
        and additional is not None
        and (not annual_required or annual_payment is not None)
    )
    if not payments_complete:
        stale_reasons.append("legal_payments_incomplete")

    supplied_costs = _mapping(actual_cost_ranges, label="actual_cost_ranges") or {}
    if set(supplied_costs) - {*REQUIRED_COST_KEYS, "provenance_refs"}:
        raise DecisionInputError("actual cost ranges contain unsupported keys")
    missing_costs = [key for key in REQUIRED_COST_KEYS if key not in supplied_costs]
    stale_reasons.extend(f"actual_cost_missing:{key}" for key in missing_costs)
    # Unknown purpose has no applicable investment policy. A bounded placeholder is
    # retained only to keep the W11 input schema stable; persisted scenario selection
    # forces requires_check and prevents a ceiling from being calculated.
    policy_scenario = (
        scenario_key if scenario_key in STANDARD_POLICY else "operating_business"
    )
    policy = STANDARD_POLICY[policy_scenario]
    if selection.status != "selected":
        stale_reasons.append("scenario_unclassified")
    policy_assumptions = [
        policy.label,
        f"Стандартный целевой ROI: {policy.target_roi_percent}%",
        f"Стандартная минимальная маржа: {policy.target_margin_percent}%",
        "ROI и маржа применяются одновременно; выбирается более консервативный предел.",
        "Все фактические расходы и risk reserve должны быть подтверждены отдельно.",
        "Политика не заменяет юридические, арендные и сценарные блокировки W10.",
    ]
    price_refs = list(
        dict.fromkeys(
            [
                *legal_refs,
                *contract_refs,
                *market_refs,
                *history_refs,
                *guarantee_refs,
                *additional_refs,
                *annual_refs,
            ]
        )
    )[:MAX_REFS]
    price_input = {
        "policy_version": POLICY_VERSION,
        "policy_assumptions": policy_assumptions,
        "market_estimate": market_module,
        "history_reference": history_module,
        "legal_payments": {
            "payments_complete": payments_complete,
            "one_time": one_time,
            "annual_lease": annual_payment,
            "annual_lease_required": annual_required,
            "refundable_guarantee_kzt": (guarantee if guarantee_status == "found" else None),
            "provenance_refs": list(
                dict.fromkeys([*guarantee_refs, *additional_refs, *annual_refs])
            ),
            "guarantee_treatment": "blocked_capital",
        },
        "acquisition": {
            "start_price_kzt": _exact_kzt(lot.start_price_kzt),
            "provenance_refs": [f"auction_lot:{lot.lot_id}:start_price"],
        },
        "targets": {
            "holding_period_years": policy.holding_period_years,
            "target_roi_percent": policy.target_roi_percent,
            "target_margin_percent": policy.target_margin_percent,
            "policy_version": POLICY_VERSION,
        },
        "cost_ranges": supplied_costs,
        "provenance_refs": price_refs,
    }
    scenario_input = {
        "profile": profile,
        "right": scenario_right,
        "legal_passport": scenario_legal,
        "geometry_context": modules["geometry_context"],
        "restriction_context": modules["restriction_context"],
        "site_context": modules["site_context"],
        "planning_context": modules["planning_context"],
    }
    verdict_refs = list(
        dict.fromkeys([*legal_refs, *contract_refs, *artifact_refs, *market_refs, *history_refs])
    )[:MAX_REFS]
    stale_reasons.extend(
        f"source_{item.get('status', 'unknown')}:{name}"
        for name, item in freshness.items()
        if item.get("status") != "fresh"
    )
    modules.update(
        {
            "contract_extraction": contract,
            "scenario_input": scenario_input,
            "scenario_selection": selection.as_payload(
                provenance_refs=tuple(legal_refs[:MAX_REFS])
            ),
            "price_input": price_input,
            "verdict_evidence": {
                "critical_blockers": blockers,
                "material_risks": [*legal_risks, *contract_risks][:MAX_REFS],
                "provenance_refs": verdict_refs,
            },
            "evidence_generation_ids": generations,
            "source_freshness": freshness,
            "stale_reasons": list(dict.fromkeys(stale_reasons)),
            "standard_policy": {
                "version": POLICY_VERSION,
                "scenario": selection.scenario_key,
                "applicable": selection.status == "selected",
                "calculation_placeholder": (
                    policy_scenario if selection.status != "selected" else None
                ),
                "holding_period_years": policy.holding_period_years,
                "target_roi_percent": policy.target_roi_percent,
                "target_margin_percent": policy.target_margin_percent,
                "assumptions": policy_assumptions,
                "personalized": False,
            },
            "assembler_version": ASSEMBLER_VERSION,
        }
    )
    for required in REQUIRED_MODULES:
        if required not in modules or required not in freshness or required not in generations:
            raise DecisionInputError(f"required module contract missing: {required}")
    output_json = _canonical(modules, maximum=MAX_OUTPUT_BYTES, label="module outputs")
    normalized_modules = json.loads(output_json)
    input_hash = hashlib.sha256(output_json.encode("utf-8")).hexdigest()
    persistence_payloads = {
        f"decision_input:{key}": _canonical(
            value,
            maximum=MAX_ARTIFACT_BYTES,
            label=f"persistence payload {key}",
        )
        for key, value in normalized_modules.items()
    }
    if (
        sum(len(value.encode("utf-8")) for value in persistence_payloads.values())
        > MAX_PERSISTED_TOTAL_BYTES
    ):
        raise DecisionInputError("persistence payload total exceeds byte budget")
    return DecisionInputAssembly(
        module_outputs=normalized_modules,
        input_hash=input_hash,
        persistence_payloads=persistence_payloads,
    )
