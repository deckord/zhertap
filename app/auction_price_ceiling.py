from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Literal

from app.auction_market_comparables import ENGINE_VERSION as STRICT_MARKET_ENGINE_VERSION

ENGINE_VERSION = "price-ceiling/2026.1"
MIN_HIGH_QUALITY_VERIFIED = 3
ACCEPTED_MARKET_CONFIDENCE = frozenset({"medium", "high"})
MAX_KZT = 10**15
REQUIRED_COST_KEYS = (
    "connection",
    "development",
    "registration",
    "tax_annual",
    "due_diligence",
    "financing",
    "contingency",
    "risk_reserve",
)


@dataclass(frozen=True, slots=True)
class PriceEngineLimits:
    max_items: int = 100
    max_references: int = 50
    max_text_length: int = 240


@dataclass(frozen=True, slots=True)
class KztRange:
    low: int
    base: int
    high: int


@dataclass(frozen=True, slots=True)
class AuditOperand:
    code: str
    value: str
    provenance_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PriceCeilingAnalysis:
    engine_version: str
    status: Literal["calculated", "insufficient", "blocked", "error"]
    total_cost_kzt: KztRange | None = None
    post_acquisition_cost_kzt: KztRange | None = None
    fair_value_kzt: KztRange | None = None
    recommended_ceiling_kzt: int | None = None
    ceiling_sensitivity_kzt: KztRange | None = None
    refundable_guarantee_kzt: int | None = None
    cash_timing_requirement_kzt: int | None = None
    missing_reasons: tuple[str, ...] = ()
    blocker_reasons: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    audit_trail: tuple[AuditOperand, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class PriceValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: object, field: str, limits: PriceEngineLimits) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriceValidationError("invalid_string", f"{field} must be a non-empty string")
    cleaned = " ".join(value.split())
    if len(cleaned) > limits.max_text_length:
        raise PriceValidationError("string_too_long", f"{field} exceeds text limit")
    return cleaned


def _references(value: object, field: str, limits: PriceEngineLimits) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > limits.max_references:
        raise PriceValidationError("invalid_references", f"{field} references are invalid")
    return tuple(_text(item, field, limits) for item in value)


def _kzt(value: object, field: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PriceValidationError("invalid_kzt", f"{field} must be integer KZT")
    if value < 0 or value > MAX_KZT:
        raise PriceValidationError("invalid_kzt", f"{field} is outside KZT bounds")
    return value


def _market_kzt(value: object, field: str) -> int:
    """Accept W9's JSON numeric output, but retain exact integral-KZT semantics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceValidationError("invalid_market_kzt", f"{field} must be numeric KZT")
    if not math.isfinite(value) or value < 0 or value > MAX_KZT or not float(value).is_integer():
        raise PriceValidationError("invalid_market_kzt", f"{field} must be finite integral KZT")
    return int(value)


def _count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000:
        raise PriceValidationError("invalid_market_count", f"{field} is invalid")
    return value


def _strict_market_range(market: dict[object, object]) -> KztRange:
    estimate = market.get("estimate")
    if not isinstance(estimate, dict):
        raise PriceValidationError("missing_market_estimate", "W9 estimate is required")
    low = _market_kzt(estimate.get("range_low_kzt"), "market.estimate.range_low_kzt")
    base = _market_kzt(estimate.get("median_kzt"), "market.estimate.median_kzt")
    high = _market_kzt(estimate.get("range_high_kzt"), "market.estimate.range_high_kzt")
    if not low <= base <= high:
        raise PriceValidationError(
            "inverted_range", "W9 market range must be low <= median <= high"
        )
    return KztRange(low, base, high)


def _decimal(value: object, field: str, *, maximum: Decimal) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise PriceValidationError("invalid_decimal", f"{field} must be an exact decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PriceValidationError("invalid_decimal", f"{field} is invalid") from exc
    if not number.is_finite() or number < 0 or number > maximum:
        raise PriceValidationError("invalid_decimal", f"{field} is outside bounds")
    return number


def _range(value: object, field: str) -> KztRange:
    if not isinstance(value, dict):
        raise PriceValidationError("missing_range", f"{field} range is required")
    low = _kzt(value.get("low_kzt"), f"{field}.low")
    base = _kzt(value.get("base_kzt"), f"{field}.base")
    high = _kzt(value.get("high_kzt"), f"{field}.high")
    assert low is not None and base is not None and high is not None
    if not low <= base <= high:
        raise PriceValidationError("inverted_range", f"{field} range must be low <= base <= high")
    return KztRange(low=low, base=base, high=high)


def _add_ranges(*ranges: KztRange) -> KztRange:
    return KztRange(
        low=sum(item.low for item in ranges),
        base=sum(item.base for item in ranges),
        high=sum(item.high for item in ranges),
    )


def _multiply_range(value: KztRange, multiplier: Decimal) -> KztRange:
    def amount(raw: int) -> int:
        return int((Decimal(raw) * multiplier).to_integral_value(rounding=ROUND_CEILING))

    return KztRange(low=amount(value.low), base=amount(value.base), high=amount(value.high))


def _subtract_constraints(
    value_kzt: int,
    post_cost_kzt: int,
    *,
    roi: Decimal | None,
    margin: Decimal | None,
    target_profit_kzt: int | None,
) -> int:
    value = Decimal(value_kzt)
    cost = Decimal(post_cost_kzt)
    candidates = [value - cost, value]
    if roi is not None:
        candidates.append(value / (Decimal(1) + roi) - cost)
    if margin is not None:
        candidates.append(value * (Decimal(1) - margin) - cost)
    if target_profit_kzt is not None:
        candidates.append(value - cost - Decimal(target_profit_kzt))
    result = min(candidates)
    return max(0, int(result.to_integral_value(rounding=ROUND_FLOOR)))


def calculate_price_ceiling(
    normalized_input: object,
    *,
    engine_version: str = ENGINE_VERSION,
    limits: PriceEngineLimits = PriceEngineLimits(),
) -> PriceCeilingAnalysis:
    """Calculate a conditional price ceiling from explicit, exact-KZT operands."""
    if engine_version != ENGINE_VERSION:
        return PriceCeilingAnalysis(
            engine_version=engine_version,
            status="error",
            error_code="unsupported_engine_version",
            error_message=f"Supported engine version is {ENGINE_VERSION}",
        )
    try:
        if not isinstance(normalized_input, dict):
            raise PriceValidationError("invalid_input", "Price input must be an object")
        market = normalized_input.get("market_estimate")
        history = normalized_input.get("history_reference")
        scenario = normalized_input.get("scenario")
        legal = normalized_input.get("legal_payments")
        acquisition = normalized_input.get("acquisition")
        targets = normalized_input.get("targets")
        costs = normalized_input.get("cost_ranges")
        for name, value in (
            ("market_estimate", market),
            ("history_reference", history),
            ("scenario", scenario),
            ("legal_payments", legal),
            ("acquisition", acquisition),
            ("targets", targets),
            ("cost_ranges", costs),
        ):
            if not isinstance(value, dict):
                raise PriceValidationError("missing_section", f"{name} section is required")

        scenario_status = scenario.get("status")
        if scenario_status not in {"eligible", "requires_check", "blocked"}:
            raise PriceValidationError("invalid_scenario_status", "Scenario status is invalid")
        scenario_refs = _references(scenario.get("provenance_refs"), "scenario", limits)
        scenario_checks_resolved = scenario.get("critical_checks_resolved", False)
        if not isinstance(scenario_checks_resolved, bool):
            raise PriceValidationError(
                "invalid_scenario_resolution", "critical_checks_resolved must be boolean"
            )

        guarantee = _kzt(
            legal.get("refundable_guarantee_kzt"),
            "refundable_guarantee_kzt",
            allow_none=True,
        )
        legal_refs = _references(legal.get("provenance_refs"), "legal_payments", limits)
        payments_complete = legal.get("payments_complete")
        if not isinstance(payments_complete, bool):
            raise PriceValidationError(
                "invalid_payment_completeness",
                "payments_complete must be explicit boolean",
            )
        one_time_raw = legal.get("one_time", [])
        if not isinstance(one_time_raw, list) or len(one_time_raw) > limits.max_items:
            raise PriceValidationError("invalid_payments", "One-time legal payments are invalid")
        payment_ids = set()
        one_time_ranges = []
        audit = []
        for payment in one_time_raw:
            if not isinstance(payment, dict):
                raise PriceValidationError("invalid_payment", "Legal payment must be an object")
            payment_id = _text(payment.get("id"), "payment.id", limits)
            if payment_id in payment_ids:
                raise PriceValidationError("duplicate_payment", "Legal payment IDs repeat")
            payment_ids.add(payment_id)
            payment_range = _range(payment.get("amount"), f"payment.{payment_id}")
            one_time_ranges.append(payment_range)
            audit.append(
                AuditOperand(
                    code=f"legal_one_time:{payment_id}",
                    value=str(payment_range),
                    provenance_refs=legal_refs,
                )
            )
        annual_lease_raw = legal.get("annual_lease")
        annual_lease_required = legal.get("annual_lease_required", False)
        if not isinstance(annual_lease_required, bool):
            raise PriceValidationError(
                "invalid_annual_lease_requirement",
                "annual_lease_required must be boolean",
            )
        annual_lease_range = KztRange(0, 0, 0)
        if annual_lease_raw is not None:
            if not isinstance(annual_lease_raw, dict):
                raise PriceValidationError("invalid_payment", "annual_lease must be an object")
            annual_id = _text(annual_lease_raw.get("id"), "annual_lease.id", limits)
            if annual_id in payment_ids:
                raise PriceValidationError(
                    "duplicate_payment", "Annual payment duplicates one-time ID"
                )
            payment_ids.add(annual_id)
            annual_lease_range = _range(annual_lease_raw.get("amount"), "annual_lease")

        holding_years = _decimal(
            targets.get("holding_period_years"), "holding_period_years", maximum=Decimal(100)
        )
        if holding_years <= 0:
            raise PriceValidationError(
                "invalid_holding_period", "holding_period_years must be greater than zero"
            )
        target_roi_percent = targets.get("target_roi_percent")
        target_margin_percent = targets.get("target_margin_percent")
        target_profit = _kzt(targets.get("target_profit_kzt"), "target_profit_kzt", allow_none=True)
        roi = (
            _decimal(target_roi_percent, "target_roi_percent", maximum=Decimal(1000)) / 100
            if target_roi_percent is not None
            else None
        )
        margin = (
            _decimal(target_margin_percent, "target_margin_percent", maximum=Decimal(100)) / 100
            if target_margin_percent is not None
            else None
        )
        if roi is None and margin is None and target_profit is None:
            raise PriceValidationError("target_missing", "ROI, margin or target profit is required")

        cost_ranges = {}
        cost_refs = _references(costs.get("provenance_refs"), "costs", limits)
        missing_costs = []
        for key in REQUIRED_COST_KEYS:
            raw = costs.get(key)
            if raw is None:
                missing_costs.append(key)
                continue
            cost_range = _range(raw, f"cost.{key}")
            cost_ranges[key] = cost_range
            item_refs = (
                _references(raw.get("provenance_refs"), f"cost.{key}", limits)
                if isinstance(raw, dict) and raw.get("provenance_refs") is not None
                else cost_refs
            )
            audit.append(
                AuditOperand(
                    code=f"cost_range:{key}",
                    value=str(cost_range),
                    provenance_refs=item_refs,
                )
            )

        market_status = market.get("status")
        if market_status not in {"ok", "insufficient_data", "invalid_target", "invalid_input"}:
            raise PriceValidationError("invalid_market_status", "Market status is invalid")
        market_refs = _references(market.get("provenance_refs"), "market", limits)
        market_version = market.get("engine_version")
        market_confidence = market.get("confidence")
        high_quality_count = _count(
            market.get("high_quality_verified_count", 0),
            "market.high_quality_verified_count",
        )
        market_range = _strict_market_range(market) if market_status == "ok" else None
        estimate = market.get("estimate")
        comparables_used = (
            _count(estimate.get("verified_comparables_used"), "market.verified_comparables_used")
            if isinstance(estimate, dict)
            else 0
        )
        strict_market_accepted = (
            market_status == "ok"
            and market_version == STRICT_MARKET_ENGINE_VERSION
            and market_confidence in ACCEPTED_MARKET_CONFIDENCE
            and high_quality_count >= MIN_HIGH_QUALITY_VERIFIED
            and comparables_used >= MIN_HIGH_QUALITY_VERIFIED
        )
        audit.append(
            AuditOperand(
                code="strict_market_policy",
                value=(
                    f"version={market_version}; confidence={market_confidence}; "
                    f"high_quality={high_quality_count}; used={comparables_used}; "
                    f"accepted={strict_market_accepted}"
                ),
                provenance_refs=market_refs,
            )
        )

        start_price = _kzt(acquisition.get("start_price_kzt"), "start_price_kzt", allow_none=True)
        acquisition_price = _kzt(
            acquisition.get("acquisition_price_kzt"),
            "acquisition_price_kzt",
            allow_none=True,
        )
        illustrative_price = acquisition_price if acquisition_price is not None else start_price
        acquisition_refs = _references(acquisition.get("provenance_refs"), "acquisition", limits)

        history_refs = _references(history.get("provenance_refs"), "history", limits)
        history_note = _text(
            history.get("competition_reference", "No competition reference supplied"),
            "history.competition_reference",
            limits,
        )
        audit.append(
            AuditOperand(
                code="history_reference_only",
                value=history_note,
                provenance_refs=history_refs,
            )
        )
        audit.append(
            AuditOperand(
                code="refundable_guarantee_excluded",
                value=str(guarantee or 0),
                provenance_refs=legal_refs,
            )
        )
        audit.append(
            AuditOperand(
                code="selected_scenario_status",
                value=str(scenario_status),
                provenance_refs=scenario_refs,
            )
        )

        blockers = []
        missing = []
        if scenario_status == "blocked":
            blockers.append("selected_scenario_blocked")
        elif scenario_status == "requires_check" and not scenario_checks_resolved:
            missing.append("selected_scenario_requires_check")
        if market_status != "ok":
            missing.append("market_estimate_not_found")
        elif not strict_market_accepted:
            missing.append("market_strict_policy_not_met")
        missing.extend(f"missing_cost:{key}" for key in missing_costs)
        if not payments_complete:
            missing.append("legal_payments_incomplete")
        if annual_lease_required and annual_lease_raw is None:
            missing.append("annual_lease_payment_missing")
        annual_lease_total = _multiply_range(annual_lease_range, holding_years)
        if "tax_annual" in cost_ranges:
            audit.append(
                AuditOperand(
                    "cost_over_horizon:tax_annual",
                    str(_multiply_range(cost_ranges["tax_annual"], holding_years)),
                    next(
                        (
                            item.provenance_refs
                            for item in audit
                            if item.code == "cost_range:tax_annual"
                        ),
                        cost_refs,
                    ),
                )
            )
            cost_ranges["tax_annual"] = _multiply_range(cost_ranges["tax_annual"], holding_years)
        post_ranges = one_time_ranges + [annual_lease_total] + list(cost_ranges.values())
        post_cost = _add_ranges(*post_ranges) if post_ranges else KztRange(0, 0, 0)
        reported_post_cost = post_cost if not missing_costs else None
        total_cost = (
            _add_ranges(
                KztRange(illustrative_price, illustrative_price, illustrative_price), post_cost
            )
            if illustrative_price is not None and reported_post_cost is not None
            else None
        )
        audit.extend(
            (
                AuditOperand("holding_period_years", str(holding_years)),
                AuditOperand("annual_lease_over_horizon", str(annual_lease_total), legal_refs),
                AuditOperand("post_acquisition_cost", str(post_cost), cost_refs + legal_refs),
                AuditOperand(
                    "illustrative_acquisition_price", str(illustrative_price), acquisition_refs
                ),
                AuditOperand("target_roi", str(roi) if roi is not None else "none"),
                AuditOperand(
                    "roi_definition",
                    "(realizable_value - acquisition - post_cost) / "
                    "(acquisition + post_cost)",
                ),
                AuditOperand("target_margin", str(margin) if margin is not None else "none"),
                AuditOperand(
                    "target_profit_kzt", str(target_profit) if target_profit is not None else "none"
                ),
            )
        )
        assumptions = (
            "History competition is context only and never changes fair value.",
            "Start price affects illustrative total cost only, never fair value or ceiling.",
            "Refundable guarantee is excluded from cost and retained as cash timing.",
            "Annual lease and annual tax are prorated over the exact holding period.",
            "ROI means (realizable value - acquisition - post-acquisition cost) divided "
            "by (acquisition + post-acquisition cost).",
            "Recommended ceiling uses low market value and high post-acquisition cost.",
        )
        if blockers:
            return PriceCeilingAnalysis(
                engine_version=ENGINE_VERSION,
                status="blocked",
                total_cost_kzt=total_cost,
                post_acquisition_cost_kzt=reported_post_cost,
                refundable_guarantee_kzt=guarantee,
                cash_timing_requirement_kzt=guarantee,
                blocker_reasons=tuple(blockers),
                assumptions=assumptions,
                audit_trail=tuple(audit),
            )
        if missing:
            return PriceCeilingAnalysis(
                engine_version=ENGINE_VERSION,
                status="insufficient",
                total_cost_kzt=total_cost,
                post_acquisition_cost_kzt=reported_post_cost,
                refundable_guarantee_kzt=guarantee,
                cash_timing_requirement_kzt=guarantee,
                missing_reasons=tuple(missing),
                assumptions=assumptions,
                audit_trail=tuple(audit),
            )
        assert market_range is not None
        sensitivity = KztRange(
            low=_subtract_constraints(
                market_range.low,
                post_cost.high,
                roi=roi,
                margin=margin,
                target_profit_kzt=target_profit,
            ),
            base=_subtract_constraints(
                market_range.base,
                post_cost.base,
                roi=roi,
                margin=margin,
                target_profit_kzt=target_profit,
            ),
            high=_subtract_constraints(
                market_range.high,
                post_cost.low,
                roi=roi,
                margin=margin,
                target_profit_kzt=target_profit,
            ),
        )
        audit.append(AuditOperand("fair_value_range", str(market_range), market_refs))
        audit.append(AuditOperand("ceiling_sensitivity", str(sensitivity), market_refs + cost_refs))
        return PriceCeilingAnalysis(
            engine_version=ENGINE_VERSION,
            status="calculated",
            total_cost_kzt=total_cost,
            post_acquisition_cost_kzt=post_cost,
            fair_value_kzt=market_range,
            recommended_ceiling_kzt=min(sensitivity.low, market_range.low),
            ceiling_sensitivity_kzt=sensitivity,
            refundable_guarantee_kzt=guarantee,
            cash_timing_requirement_kzt=guarantee,
            assumptions=assumptions,
            audit_trail=tuple(audit),
        )
    except PriceValidationError as exc:
        return PriceCeilingAnalysis(
            engine_version=ENGINE_VERSION,
            status="error",
            error_code=exc.code,
            error_message=str(exc),
        )
