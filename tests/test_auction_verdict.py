from __future__ import annotations

import math

from app.auction_verdict import ENGINE_VERSION, RULES_VERSION, evaluate_auction_verdict


def complete_input(*, mode="auction"):
    return {
        "scenario": {
            "status": "eligible",
            "provenance_refs": ["scenario:w10"],
        },
        "price_analysis": {
            "status": "calculated",
            "recommended_ceiling_kzt": 8_500_000,
            "provenance_refs": ["ceiling:w11"],
        },
        "evidence": {
            "critical_facts_complete": True,
            "critical_blockers": [],
            "unresolved_critical": [],
            "material_risks": [],
            "provenance_refs": ["passport:complete"],
        },
        "pricing": {
            "transaction_mode": mode,
            "current_price_kzt": 1_000_000,
            "provenance_refs": ["eqazyna:current"],
        },
        "secondary_score": 76,
    }


def test_auction_is_participate_up_to_only_with_calculated_ceiling() -> None:
    result = evaluate_auction_verdict(complete_input())
    assert result.engine_status == "ok"
    assert result.verdict == "participate_up_to"
    assert result.recommended_ceiling_kzt == 8_500_000
    assert "ceiling:w11" in result.evidence_refs


def test_complete_fixed_price_case_is_participate() -> None:
    result = evaluate_auction_verdict(complete_input(mode="fixed_price"))
    assert result.verdict == "participate"
    assert result.recommended_ceiling_kzt is None


def test_material_noncritical_risk_is_high_risk() -> None:
    data = complete_input()
    data["evidence"]["material_risks"] = [
        {"code": "PARTIAL_SZZ_OVERLAP", "evidence_refs": ["szz:pdp"]}
    ]
    result = evaluate_auction_verdict(data)
    assert result.verdict == "high_risk"
    assert result.recommended_ceiling_kzt is None
    assert "MATERIAL_RISK:PARTIAL_SZZ_OVERLAP" in result.reason_codes


def test_critical_and_whole_parcel_blockers_are_do_not_participate() -> None:
    data = complete_input()
    data["evidence"]["critical_blockers"] = [
        {"code": "WHOLE_PARCEL_PROHIBITION", "evidence_refs": ["restriction:1"]}
    ]
    result = evaluate_auction_verdict(data)
    assert result.verdict == "do_not_participate"
    assert result.recommended_ceiling_kzt is None


def test_452662_incomplete_contract_geo_and_comparables_requires_check() -> None:
    data = complete_input()
    data["scenario"]["status"] = "requires_check"
    data["price_analysis"].update(
        {"status": "insufficient", "recommended_ceiling_kzt": None}
    )
    data["evidence"].update(
        {
            "critical_facts_complete": False,
            "unresolved_critical": [
                {"code": "CONTRACT_UNKNOWN", "evidence_refs": ["lot:452662"]},
                {"code": "GEOMETRY_UNKNOWN", "evidence_refs": ["lot:452662"]},
                {"code": "COMPARABLES_INSUFFICIENT", "evidence_refs": ["market:w9"]},
            ],
        }
    )
    result = evaluate_auction_verdict(data)
    assert result.verdict == "requires_check"
    assert result.recommended_ceiling_kzt is None
    assert "PRICE_INPUTS_INSUFFICIENT" in result.reason_codes


def test_unknown_price_or_missing_ceiling_requires_check_never_invents_bid() -> None:
    unknown_price = complete_input()
    unknown_price["pricing"]["current_price_kzt"] = None
    no_ceiling = complete_input()
    no_ceiling["price_analysis"]["recommended_ceiling_kzt"] = None
    first = evaluate_auction_verdict(unknown_price)
    second = evaluate_auction_verdict(no_ceiling)
    assert first.verdict == second.verdict == "requires_check"
    assert first.recommended_ceiling_kzt is second.recommended_ceiling_kzt is None


def test_current_price_equal_or_above_ceiling_is_do_not_participate() -> None:
    equal = complete_input()
    above = complete_input()
    equal["pricing"]["current_price_kzt"] = 8_500_000
    above["pricing"]["current_price_kzt"] = 9_000_000
    assert evaluate_auction_verdict(equal).verdict == "do_not_participate"
    assert evaluate_auction_verdict(above).verdict == "do_not_participate"


def test_score_is_secondary_and_never_changes_verdict() -> None:
    low = complete_input()
    high = complete_input()
    low["secondary_score"] = 1
    high["secondary_score"] = 100
    assert evaluate_auction_verdict(low).verdict == evaluate_auction_verdict(high).verdict


def test_malformed_nonfinite_and_oversized_inputs_are_explicit_errors() -> None:
    nonfinite = complete_input()
    nonfinite["secondary_score"] = math.inf
    malformed = complete_input()
    malformed["evidence"]["critical_facts_complete"] = "yes"
    malformed_status = complete_input()
    malformed_status["scenario"]["status"] = []
    oversized = complete_input()
    oversized["evidence"]["material_risks"] = [
        {"code": f"risk-{index}"} for index in range(101)
    ]
    assert evaluate_auction_verdict(nonfinite).error_code == "invalid_score"
    assert evaluate_auction_verdict(malformed).error_code == "invalid_completeness"
    assert evaluate_auction_verdict(malformed_status).error_code == "invalid_scenario_status"
    assert evaluate_auction_verdict(oversized).error_code == "invalid_facts"


def test_combined_reference_limit_counts_unique_evidence_not_repeated_provenance() -> None:
    data = complete_input()
    shared = [f"evidence:{index}" for index in range(94)]
    data["scenario"]["provenance_refs"] = shared[:6]
    data["evidence"]["provenance_refs"] = shared
    data["pricing"]["provenance_refs"] = ["auction_lot:one"]

    accepted = evaluate_auction_verdict(data)

    assert accepted.engine_status == "ok"
    assert len(accepted.evidence_refs) == 96

    data["price_analysis"]["provenance_refs"] = [f"market:{index}" for index in range(6)]
    rejected = evaluate_auction_verdict(data)
    assert rejected.engine_status == "error"
    assert rejected.error_code == "too_many_references"


def test_version_and_determinism() -> None:
    first = evaluate_auction_verdict(complete_input())
    second = evaluate_auction_verdict(complete_input())
    unsupported = evaluate_auction_verdict({}, engine_version="old")
    assert first == second
    assert first.engine_version == ENGINE_VERSION
    assert first.rules_version == RULES_VERSION
    assert unsupported.error_code == "unsupported_version"
