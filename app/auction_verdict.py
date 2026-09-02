from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ENGINE_VERSION = "auction-verdict/2026.1"
RULES_VERSION = "five-state-verdict/2026.1"
MAX_KZT = 10**15

VerdictStatus = Literal[
    "participate",
    "participate_up_to",
    "requires_check",
    "high_risk",
    "do_not_participate",
]


@dataclass(frozen=True, slots=True)
class VerdictLimits:
    max_facts: int = 100
    max_references: int = 100
    max_text_length: int = 240


@dataclass(frozen=True, slots=True)
class VerdictFact:
    code: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerdictAnalysis:
    engine_version: str
    rules_version: str
    engine_status: Literal["ok", "error"]
    verdict: VerdictStatus | None = None
    recommended_ceiling_kzt: int | None = None
    current_price_kzt: int | None = None
    reason_codes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    secondary_score: float | None = None
    error_code: str | None = None
    error_message: str | None = None


class VerdictValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: VerdictLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerdictValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_text_length:
        raise VerdictValidationError("string_too_long", f"{field} exceeds text limit")
    return cleaned


def _refs(value: object, field: str, limits: VerdictLimits) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limits.max_references:
        raise VerdictValidationError("invalid_references", f"{field} references are invalid")
    return tuple(_text(item, field, limits) for item in value)


def _facts(value: object, field: str, limits: VerdictLimits) -> tuple[VerdictFact, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limits.max_facts:
        raise VerdictValidationError("invalid_facts", f"{field} facts are invalid")
    result = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise VerdictValidationError("invalid_fact", f"{field}[{index}] must be an object")
        result.append(
            VerdictFact(
                code=_text(raw.get("code"), f"{field}[{index}].code", limits),
                evidence_refs=_refs(
                    raw.get("evidence_refs"), f"{field}[{index}].evidence_refs", limits
                ),
            )
        )
    return tuple(result)


def _kzt(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_KZT:
        raise VerdictValidationError("invalid_kzt", f"{field} must be bounded integer KZT")
    return value


def _score(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerdictValidationError("invalid_score", "secondary_score must be numeric")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise VerdictValidationError("invalid_score", "secondary_score is outside 0..100")
    return result


def _unique_refs(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for group in groups for ref in group))


def evaluate_auction_verdict(
    normalized_input: object,
    *,
    engine_version: str = ENGINE_VERSION,
    rules_version: str = RULES_VERSION,
    limits: VerdictLimits = VerdictLimits(),
) -> VerdictAnalysis:
    """Combine W10/W11 and evidence gates without inventing value, score, or bid data."""
    if engine_version != ENGINE_VERSION or rules_version != RULES_VERSION:
        return VerdictAnalysis(
            engine_version=engine_version,
            rules_version=rules_version,
            engine_status="error",
            error_code="unsupported_version",
            error_message=f"Supported versions are {ENGINE_VERSION} and {RULES_VERSION}",
        )
    try:
        if not isinstance(normalized_input, dict):
            raise VerdictValidationError("invalid_input", "Verdict input must be an object")
        scenario = normalized_input.get("scenario")
        price = normalized_input.get("price_analysis")
        evidence = normalized_input.get("evidence")
        pricing = normalized_input.get("pricing")
        if not all(isinstance(item, dict) for item in (scenario, price, evidence, pricing)):
            raise VerdictValidationError("missing_section", "All verdict sections are required")

        scenario_status = scenario.get("status")
        if not isinstance(scenario_status, str) or scenario_status not in {
            "eligible",
            "requires_check",
            "blocked",
        }:
            raise VerdictValidationError("invalid_scenario_status", "Scenario status is invalid")
        price_status = price.get("status")
        if not isinstance(price_status, str) or price_status not in {
            "calculated",
            "insufficient",
            "blocked",
            "error",
        }:
            raise VerdictValidationError("invalid_price_status", "W11 status is invalid")
        completeness = evidence.get("critical_facts_complete")
        if not isinstance(completeness, bool):
            raise VerdictValidationError(
                "invalid_completeness", "critical_facts_complete must be explicit boolean"
            )
        transaction_mode = pricing.get("transaction_mode")
        if not isinstance(transaction_mode, str) or transaction_mode not in {
            "auction",
            "fixed_price",
        }:
            raise VerdictValidationError(
                "invalid_transaction_mode", "transaction_mode must be auction or fixed_price"
            )

        critical_blockers = _facts(
            evidence.get("critical_blockers"), "critical_blockers", limits
        )
        unresolved = _facts(
            evidence.get("unresolved_critical"), "unresolved_critical", limits
        )
        material_risks = _facts(evidence.get("material_risks"), "material_risks", limits)
        scenario_refs = _refs(scenario.get("provenance_refs"), "scenario", limits)
        price_refs = _refs(price.get("provenance_refs"), "price_analysis", limits)
        pricing_refs = _refs(pricing.get("provenance_refs"), "pricing", limits)
        common_refs = _refs(evidence.get("provenance_refs"), "evidence", limits)
        fact_refs = tuple(
            ref
            for fact in critical_blockers + unresolved + material_risks
            for ref in fact.evidence_refs
        )
        all_refs = _unique_refs(
            scenario_refs, price_refs, pricing_refs, common_refs, fact_refs
        )
        if len(all_refs) > limits.max_references:
            raise VerdictValidationError(
                "too_many_references", "Combined unique evidence references exceed limit"
            )
        score = _score(normalized_input.get("secondary_score"))
        current_price = _kzt(pricing.get("current_price_kzt"), "current_price", optional=True)
        ceiling = _kzt(
            price.get("recommended_ceiling_kzt"), "recommended_ceiling", optional=True
        )

        def result(verdict: VerdictStatus, reasons: tuple[str, ...]) -> VerdictAnalysis:
            return VerdictAnalysis(
                engine_version=ENGINE_VERSION,
                rules_version=RULES_VERSION,
                engine_status="ok",
                verdict=verdict,
                recommended_ceiling_kzt=(
                    ceiling if verdict == "participate_up_to" else None
                ),
                current_price_kzt=current_price,
                reason_codes=reasons,
                evidence_refs=all_refs,
                secondary_score=score,
            )

        if scenario_status == "blocked" or price_status == "blocked" or critical_blockers:
            reasons = []
            if scenario_status == "blocked":
                reasons.append("SCENARIO_BLOCKED")
            if price_status == "blocked":
                reasons.append("PRICE_ANALYSIS_BLOCKED")
            reasons.extend(f"CRITICAL_BLOCKER:{fact.code}" for fact in critical_blockers)
            return result("do_not_participate", tuple(reasons))

        if (
            scenario_status == "requires_check"
            or price_status in {"insufficient", "error"}
            or not completeness
            or unresolved
        ):
            reasons = []
            if scenario_status == "requires_check":
                reasons.append("SCENARIO_REQUIRES_CHECK")
            if price_status in {"insufficient", "error"}:
                reasons.append("PRICE_INPUTS_INSUFFICIENT")
            if not completeness:
                reasons.append("CRITICAL_EVIDENCE_INCOMPLETE")
            reasons.extend(f"UNRESOLVED_CRITICAL:{fact.code}" for fact in unresolved)
            return result("requires_check", tuple(reasons))

        if price_status != "calculated" or ceiling is None:
            return result("requires_check", ("RECOMMENDED_CEILING_UNAVAILABLE",))
        if current_price is None:
            return result("requires_check", ("CURRENT_PRICE_UNKNOWN",))
        if current_price >= ceiling:
            return result("do_not_participate", ("CURRENT_PRICE_AT_OR_ABOVE_CEILING",))
        if material_risks:
            return result(
                "high_risk",
                tuple(f"MATERIAL_RISK:{fact.code}" for fact in material_risks),
            )
        if transaction_mode == "fixed_price":
            return result("participate", ("FIXED_PRICE_BELOW_CEILING",))
        return result("participate_up_to", ("AUCTION_PRICE_BELOW_CEILING",))
    except VerdictValidationError as exc:
        return VerdictAnalysis(
            engine_version=ENGINE_VERSION,
            rules_version=RULES_VERSION,
            engine_status="error",
            error_code=exc.code,
            error_message=str(exc),
        )
