from __future__ import annotations

import math

from app.auction_scenario_rules import (
    RULES_VERSION,
    ScenarioRuleLimits,
    evaluate_scenario_rules,
)


def complete_input(profile="retail", right_type="ownership"):
    return {
        "profile": profile,
        "right": {
            "type": right_type,
            "transferable": True,
            "renewable": True,
            "sublease_allowed": True,
            "provenance_refs": ["jerler:right"],
        },
        "legal_passport": {
            "status": "clear",
            "use_allowed": True,
            "provenance_refs": ["contract:1"],
        },
        "restriction_context": {
            "status": "clear",
            "coverage_complete": True,
            "usable_area_m2": 9000,
            "authoritative_blockers": [],
            "provenance_refs": ["restrictions:2026"],
        },
        "site_context": {
            "physical_access_status": "ready",
            "legal_access_status": "ready",
            "infrastructure_status": "ready",
            "capacity_status": "ready",
            "provenance_refs": ["site:2026"],
        },
        "planning_context": {
            "status": "clear",
            "current_use_allowed": True,
            "pdp_complete": True,
            "future_adverse": [],
            "provenance_refs": ["pdp:2026"],
        },
        "geometry_context": {"status": "ok", "provenance_refs": ["geometry:egkn"]},
    }


def result_for(analysis, scenario):
    return next(item for item in analysis.results if item.scenario == scenario)


def rule_ids(findings):
    return {item.rule_id for item in findings}


def test_ownership_retail_scenarios_are_eligible_with_complete_evidence() -> None:
    analysis = evaluate_scenario_rules(complete_input())
    assert analysis.status == "ok"
    assert all(item.status == "eligible" for item in analysis.results)
    assert analysis.rules_version == RULES_VERSION
    assert not hasattr(analysis, "score")
    assert not hasattr(analysis, "verdict")
    assert not hasattr(analysis, "recommended_bid")


def test_452662_camping_three_year_lease_is_never_green() -> None:
    data = complete_input("camping", "lease")
    data["right"].update(
        {
            "lease_years": 3,
            "transferable": None,
            "renewable": None,
            "sublease_allowed": None,
        }
    )
    analysis = evaluate_scenario_rules(data)
    camping = result_for(analysis, "camping")
    resale = result_for(analysis, "resale")
    development = result_for(analysis, "development")
    assert camping.status == "requires_check"
    assert {
        "CAMPING_LEGAL_TERMS_CHECK",
        "CAMPING_RENEWAL_CHECK",
        "CAMPING_PAYBACK_CHECK",
    }.issubset(rule_ids(camping.checks))
    assert resale.status == "blocked"
    assert "RESALE_REQUIRES_OWNERSHIP" in rule_ids(resale.blockers)
    assert development.status == "blocked"
    assert "SHORT_LEASE_LONG_CAPEX_BLOCK" in rule_ids(development.blockers)


def test_legal_access_unknown_requires_check_not_absence() -> None:
    data = complete_input()
    data["site_context"]["legal_access_status"] = "unknown"
    analysis = evaluate_scenario_rules(data, scenarios=("operating_business",))
    result = analysis.results[0]
    assert result.status == "requires_check"
    assert "LEGAL_ACCESS_CHECK" in rule_ids(result.checks)
    assert result.checks[0].provenance_refs or any(
        finding.provenance_refs for finding in result.checks
    )


def test_future_road_blocks_development_and_checks_resale() -> None:
    data = complete_input()
    data["planning_context"].update(
        {"status": "conflict", "future_adverse": ["planned_road", "red_line"]}
    )
    analysis = evaluate_scenario_rules(data, scenarios=("development", "resale"))
    development, resale = analysis.results
    assert development.status == "blocked"
    assert "FUTURE_PLANNING_ADVERSE" in rule_ids(development.blockers)
    assert resale.status == "requires_check"
    assert "FUTURE_PLANNING_CHECK" in rule_ids(resale.checks)


def test_sublease_forbidden_unknown_and_allowed_are_distinct() -> None:
    statuses = {}
    rules = {}
    for permission in (False, None, True):
        data = complete_input(right_type="lease")
        data["right"].update({"lease_years": 10, "sublease_allowed": permission})
        result = evaluate_scenario_rules(data, scenarios=("sublease",)).results[0]
        statuses[permission] = result.status
        rules[permission] = rule_ids(result.blockers + result.checks)
    assert statuses[False] == "blocked"
    assert "SUBLEASE_FORBIDDEN" in rules[False]
    assert statuses[None] == "requires_check"
    assert "SUBLEASE_PERMISSION_CHECK" in rules[None]
    assert statuses[True] == "eligible"


def test_sublease_also_requires_explicit_lease_transferability() -> None:
    unknown = complete_input(right_type="lease")
    unknown["right"].update({"lease_years": 10, "transferable": None, "sublease_allowed": True})
    forbidden = complete_input(right_type="lease")
    forbidden["right"].update({"lease_years": 10, "transferable": False, "sublease_allowed": True})
    unknown_result = evaluate_scenario_rules(unknown, scenarios=("sublease",)).results[0]
    forbidden_result = evaluate_scenario_rules(forbidden, scenarios=("sublease",)).results[0]
    assert unknown_result.status == "requires_check"
    assert "LEASE_TRANSFER_PERMISSION_CHECK" in rule_ids(unknown_result.checks)
    assert forbidden_result.status == "blocked"
    assert "LEASE_TRANSFER_FORBIDDEN" in rule_ids(forbidden_result.blockers)


def test_authoritative_unusable_restriction_and_current_use_conflict_block() -> None:
    data = complete_input()
    data["restriction_context"].update(
        {
            "status": "restricted",
            "usable_area_m2": 0,
            "critical_blockers": ["whole-parcel SZZ"],
        }
    )
    data["legal_passport"]["use_allowed"] = False
    result = evaluate_scenario_rules(data, scenarios=("operating_business",)).results[0]
    assert result.status == "blocked"
    assert "AUTHORITATIVE_UNUSABLE_RESTRICTION" in rule_ids(result.blockers)
    assert "CURRENT_USE_FORBIDDEN" in rule_ids(result.blockers)


def test_unknown_capacity_and_missing_pdp_require_checks() -> None:
    data = complete_input()
    data["site_context"]["capacity_status"] = "unknown"
    data["planning_context"].update({"status": "partial", "pdp_complete": False})
    result = evaluate_scenario_rules(data, scenarios=("development",)).results[0]
    assert result.status == "requires_check"
    assert {"CAPACITY_CHECK", "PDP_COVERAGE_CHECK"}.issubset(rule_ids(result.checks))


def test_version_malformed_nonfinite_and_bounded_inputs_are_explicit() -> None:
    unsupported = evaluate_scenario_rules({}, rules_version="scenario-rules/2025.1")
    malformed = complete_input()
    malformed["right"]["lease_years"] = math.inf
    invalid_number = evaluate_scenario_rules(malformed)
    oversized = evaluate_scenario_rules(
        complete_input(),
        scenarios=tuple("resale" for _ in range(11)),
        limits=ScenarioRuleLimits(max_scenarios=10),
    )
    deterministic_a = evaluate_scenario_rules(complete_input())
    deterministic_b = evaluate_scenario_rules(complete_input())
    assert unsupported.error_code == "unsupported_rules_version"
    assert invalid_number.error_code == "invalid_number"
    assert oversized.error_code == "invalid_scenarios"
    assert deterministic_a == deterministic_b


def test_partial_authoritative_overlap_is_check_not_universal_block() -> None:
    data = complete_input()
    data["restriction_context"].update(
        {
            "status": "restricted",
            "usable_area_m2": 8100,
            "authoritative_blockers": ["red line affects 10%"],
            "critical_blockers": [],
        }
    )
    result = evaluate_scenario_rules(data, scenarios=("operating_business",)).results[0]
    assert result.status == "requires_check"
    assert "AUTHORITATIVE_UNUSABLE_RESTRICTION" not in rule_ids(result.blockers)
    assert "AUTHORITATIVE_RESTRICTION_FACT_CHECK" in rule_ids(result.checks)


def test_critical_whole_parcel_or_scenario_minimum_remains_blocking() -> None:
    critical = complete_input()
    critical["restriction_context"].update(
        {"critical_blockers": ["whole parcel prohibition"], "usable_area_m2": 9000}
    )
    minimum = complete_input()
    minimum["restriction_context"].update(
        {
            "usable_area_m2": 4000,
            "scenario_minimum_usable_area_m2": {"development": 5000},
        }
    )
    critical_result = evaluate_scenario_rules(critical, scenarios=("operating_business",)).results[
        0
    ]
    minimum_result = evaluate_scenario_rules(minimum, scenarios=("development",)).results[0]
    assert critical_result.status == "blocked"
    assert minimum_result.status == "blocked"
    assert "AUTHORITATIVE_UNUSABLE_RESTRICTION" in rule_ids(minimum_result.blockers)


def test_blocked_physical_access_checks_resale_and_rent_but_blocks_operation() -> None:
    data = complete_input()
    data["site_context"]["physical_access_status"] = "blocked"
    analysis = evaluate_scenario_rules(
        data,
        scenarios=("resale", "land_rent", "operating_business"),
    )
    resale, land_rent, operation = analysis.results
    assert resale.status == "requires_check"
    assert land_rent.status == "requires_check"
    assert "PHYSICAL_ACCESS_MARKETABILITY_CHECK" in rule_ids(resale.checks)
    assert "PHYSICAL_ACCESS_MARKETABILITY_CHECK" in rule_ids(land_rent.checks)
    assert operation.status == "blocked"
    assert "PHYSICAL_ACCESS_BLOCKED" in rule_ids(operation.blockers)
