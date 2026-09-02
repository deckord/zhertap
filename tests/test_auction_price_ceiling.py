from __future__ import annotations

import math

from app.auction_market_comparables import ENGINE_VERSION as STRICT_MARKET_ENGINE_VERSION
from app.auction_price_ceiling import ENGINE_VERSION, calculate_price_ceiling


def exact(amount):
    return {"low_kzt": amount, "base_kzt": amount, "high_kzt": amount}


def complete_input():
    return {
        "market_estimate": {
            "status": "ok",
            "estimate": {
                "range_low_kzt": 12_000_000.0,
                "median_kzt": 13_500_000.0,
                "range_high_kzt": 15_000_000.0,
                "verified_comparables_used": 5,
            },
            "confidence": "high",
            "high_quality_verified_count": 5,
            "verified_eligible_count": 5,
            "engine_version": STRICT_MARKET_ENGINE_VERSION,
            "provenance_refs": ["market:w9"],
        },
        "history_reference": {
            "competition_reference": "district sale/start median 1.8x",
            "provenance_refs": ["history:w8"],
        },
        "scenario": {"status": "eligible", "provenance_refs": ["scenario:w10"]},
        "legal_payments": {
            "payments_complete": True,
            "one_time": [],
            "annual_lease": None,
            "refundable_guarantee_kzt": 216_250,
            "provenance_refs": ["jerler:payments"],
        },
        "acquisition": {
            "start_price_kzt": 1_000_000,
            "provenance_refs": ["eqazyna:start"],
        },
        "targets": {
            "holding_period_years": "1",
            "target_roi_percent": "25",
        },
        "cost_ranges": {
            "connection": exact(500_000),
            "development": exact(500_000),
            "registration": exact(100_000),
            "tax_annual": exact(100_000),
            "due_diligence": exact(100_000),
            "financing": exact(200_000),
            "contingency": exact(200_000),
            "risk_reserve": exact(300_000),
            "provenance_refs": ["cost:model"],
        },
    }


def test_target_example_12m_value_2m_costs_25_percent_roi_ceiling_7_6m() -> None:
    result = calculate_price_ceiling(complete_input())
    assert result.status == "calculated"
    assert result.post_acquisition_cost_kzt == exact_range(2_000_000)
    assert result.recommended_ceiling_kzt == 7_600_000
    assert result.fair_value_kzt == exact_range(12_000_000, 13_500_000, 15_000_000)


def exact_range(low, base=None, high=None):
    from app.auction_price_ceiling import KztRange

    return KztRange(low, low if base is None else base, low if high is None else high)


def test_guarantee_excluded_from_cost_but_retained_for_cash_timing() -> None:
    data = complete_input()
    without_guarantee = complete_input()
    without_guarantee["legal_payments"]["refundable_guarantee_kzt"] = 0
    result = calculate_price_ceiling(data)
    comparison = calculate_price_ceiling(without_guarantee)
    assert result.recommended_ceiling_kzt == comparison.recommended_ceiling_kzt
    assert result.total_cost_kzt == comparison.total_cost_kzt
    assert result.cash_timing_requirement_kzt == 216_250
    assert "refundable_guarantee_excluded" in {item.code for item in result.audit_trail}


def test_annual_lease_and_tax_are_applied_over_horizon_once() -> None:
    data = complete_input()
    data["targets"]["holding_period_years"] = "3"
    data["legal_payments"]["annual_lease"] = {
        "id": "annual-rent",
        "amount": exact(50_000),
    }
    result = calculate_price_ceiling(data)
    assert result.post_acquisition_cost_kzt == exact_range(2_350_000)
    assert result.recommended_ceiling_kzt == 7_250_000


def test_452662_insufficient_market_has_no_fair_value_or_ceiling() -> None:
    data = complete_input()
    data["market_estimate"] = {
        "status": "insufficient_data",
        "estimate": None,
        "confidence": "none",
        "high_quality_verified_count": 0,
        "verified_eligible_count": 0,
        "engine_version": STRICT_MARKET_ENGINE_VERSION,
        "provenance_refs": ["market:no-comparables"],
    }
    data["acquisition"]["start_price_kzt"] = 17_970
    data["legal_payments"]["one_time"] = [
        {"id": "agricultural-loss", "amount": exact(16_200)}
    ]
    data["legal_payments"]["annual_lease_required"] = True
    data["legal_payments"]["annual_lease"] = {
        "id": "annual-rent",
        "amount": exact(17_970),
    }
    result = calculate_price_ceiling(data)
    assert result.status == "insufficient"
    assert result.fair_value_kzt is None
    assert result.recommended_ceiling_kzt is None
    assert "market_estimate_not_found" in result.missing_reasons
    assert result.post_acquisition_cost_kzt == exact_range(2_034_170)


def test_blocked_scenario_and_missing_network_cost_suppress_value_and_ceiling() -> None:
    blocked = complete_input()
    blocked["scenario"]["status"] = "blocked"
    missing = complete_input()
    del missing["cost_ranges"]["connection"]
    blocked_result = calculate_price_ceiling(blocked)
    missing_result = calculate_price_ceiling(missing)
    assert blocked_result.status == "blocked"
    assert blocked_result.fair_value_kzt is None
    assert blocked_result.recommended_ceiling_kzt is None
    assert missing_result.status == "insufficient"
    assert "missing_cost:connection" in missing_result.missing_reasons
    assert missing_result.fair_value_kzt is None
    assert missing_result.post_acquisition_cost_kzt is None
    assert missing_result.total_cost_kzt is None


def test_unresolved_requires_check_scenario_suppresses_ceiling() -> None:
    data = complete_input()
    data["scenario"]["status"] = "requires_check"
    result = calculate_price_ceiling(data)
    assert result.status == "insufficient"
    assert result.fair_value_kzt is None
    assert result.recommended_ceiling_kzt is None
    assert "selected_scenario_requires_check" in result.missing_reasons


def test_risk_reserve_reduces_ceiling_exactly() -> None:
    with_risk = complete_input()
    no_risk = complete_input()
    no_risk["cost_ranges"]["risk_reserve"] = exact(0)
    risky = calculate_price_ceiling(with_risk)
    safe = calculate_price_ceiling(no_risk)
    assert safe.recommended_ceiling_kzt - risky.recommended_ceiling_kzt == 300_000


def test_start_price_never_changes_fair_value_or_recommended_ceiling() -> None:
    low_start = complete_input()
    high_start = complete_input()
    low_start["acquisition"]["start_price_kzt"] = 1
    high_start["acquisition"]["start_price_kzt"] = 10_000_000
    low_result = calculate_price_ceiling(low_start)
    high_result = calculate_price_ceiling(high_start)
    assert low_result.fair_value_kzt == high_result.fair_value_kzt
    assert low_result.recommended_ceiling_kzt == high_result.recommended_ceiling_kzt
    assert low_result.total_cost_kzt != high_result.total_cost_kzt


def test_missing_start_price_does_not_suppress_fair_value_or_ceiling() -> None:
    data = complete_input()
    del data["acquisition"]["start_price_kzt"]
    result = calculate_price_ceiling(data)
    assert result.status == "calculated"
    assert result.total_cost_kzt is None
    assert result.fair_value_kzt == exact_range(12_000_000, 13_500_000, 15_000_000)
    assert result.recommended_ceiling_kzt == 7_600_000


def test_fixed_w9_policy_accepts_medium_and_high_but_rejects_insufficient() -> None:
    medium = complete_input()
    medium["market_estimate"]["confidence"] = "medium"
    medium["market_estimate"]["high_quality_verified_count"] = 3
    medium["market_estimate"]["estimate"]["verified_comparables_used"] = 3
    insufficient = complete_input()
    insufficient["market_estimate"]["confidence"] = "medium"
    insufficient["market_estimate"]["high_quality_verified_count"] = 2
    insufficient["market_estimate"]["estimate"]["verified_comparables_used"] = 2
    assert calculate_price_ceiling(complete_input()).status == "calculated"
    assert calculate_price_ceiling(medium).status == "calculated"
    rejected = calculate_price_ceiling(insufficient)
    assert rejected.status == "insufficient"
    assert "market_strict_policy_not_met" in rejected.missing_reasons


def test_pre_same_year_market_engine_is_rejected_fail_closed() -> None:
    legacy = complete_input()
    legacy["market_estimate"]["engine_version"] = "strict-market-comparables.v1"

    result = calculate_price_ceiling(legacy)

    assert result.status == "insufficient"
    assert result.fair_value_kzt is None
    assert result.recommended_ceiling_kzt is None
    assert "market_strict_policy_not_met" in result.missing_reasons


def test_zero_holding_period_is_invalid_and_each_cost_is_audited() -> None:
    invalid = complete_input()
    invalid["targets"]["holding_period_years"] = "0"
    assert calculate_price_ceiling(invalid).error_code == "invalid_holding_period"
    result = calculate_price_ceiling(complete_input())
    codes = {item.code for item in result.audit_trail}
    expected_cost_codes = {
        f"cost_range:{key}"
        for key in complete_input()["cost_ranges"]
        if key != "provenance_refs"
    }
    assert expected_cost_codes <= codes
    assert "roi_definition" in codes


def test_inverted_range_noninteger_kzt_nonfinite_decimal_and_duplicate_payment_error() -> None:
    inverted = complete_input()
    inverted["cost_ranges"]["connection"] = {
        "low_kzt": 10,
        "base_kzt": 5,
        "high_kzt": 20,
    }
    noninteger = complete_input()
    noninteger["cost_ranges"]["connection"] = exact(1.5)
    nonfinite = complete_input()
    nonfinite["targets"]["target_roi_percent"] = math.inf
    duplicate = complete_input()
    duplicate["legal_payments"].update(
        {
            "one_time": [{"id": "rent", "amount": exact(10)}],
            "annual_lease": {"id": "rent", "amount": exact(10)},
        }
    )
    assert calculate_price_ceiling(inverted).error_code == "inverted_range"
    assert calculate_price_ceiling(noninteger).error_code == "invalid_kzt"
    assert calculate_price_ceiling(nonfinite).error_code == "invalid_decimal"
    assert calculate_price_ceiling(duplicate).error_code == "duplicate_payment"


def test_version_and_determinism() -> None:
    unsupported = calculate_price_ceiling({}, engine_version="price-ceiling/2025")
    first = calculate_price_ceiling(complete_input())
    second = calculate_price_ceiling(complete_input())
    assert unsupported.error_code == "unsupported_engine_version"
    assert first == second
    assert first.engine_version == ENGINE_VERSION
    assert not hasattr(first, "participate_verdict")
