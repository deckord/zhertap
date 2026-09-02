from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.auction_decision_input import (
    ASSEMBLER_VERSION,
    POLICY_VERSION,
    STANDARD_POLICY,
    ContractCoverage,
    DecisionInputError,
    DecisionLotFacts,
    EvidenceArtifact,
    assemble_decision_input,
)
from app.auction_decision_snapshot import DecisionLotInput, build_decision_material
from app.auction_price_ceiling import REQUIRED_COST_KEYS, calculate_price_ceiling

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _fact(key: str, value: object, status: str = "found") -> dict[str, object]:
    return {
        "key": key,
        "value": value,
        "status": status,
        "source_url": "https://example.test/jerler",
        "observed_at": NOW.isoformat(),
        "confidence": 0.9,
        "provenance": [
            {
                "source_url": "https://example.test/jerler",
                "observed_at": NOW.isoformat(),
                "confidence": 0.9,
                "evidence_type": "source_object_card",
                "evidence_id": 1,
            }
        ],
        "version": "legal-passport.v1",
    }


def _payment(
    key: str,
    amount: int,
    treatment: str,
    frequency: str,
) -> dict[str, object]:
    return _fact(
        key,
        {
            "amount_kzt": amount,
            "cost_treatment": treatment,
            "frequency": frequency,
        },
    )


def _passport(*, right: str = "ownership", lease_years: int | None = None) -> dict[str, object]:
    return {
        "lot_id": "lot-1",
        "source_lot_id": "452662",
        "generated_at": NOW.isoformat(),
        "version": "legal-passport.v1",
        "facts": {
            "right_type": _fact("right_type", right),
            "lease_term_years": (
                _fact("lease_term_years", lease_years)
                if lease_years is not None
                else _fact("lease_term_years", None, "unknown")
            ),
            "purpose": _fact("purpose", "кемпинг"),
            "arrests": _fact("arrests", "не имеются"),
            "restrictions": _fact("restrictions", "не имеются"),
            "encumbrances": _fact("encumbrances", "не имеются"),
        },
        "payments": {
            "guarantee": _payment("guarantee", 216_250, "blocked_capital", "once_before_auction"),
            "additional_payment": _payment("additional_payment", 0, "expense", "once_after_win"),
            "annual_rent": _fact("annual_rent", None, "unknown"),
        },
        "documents": [],
    }


def _artifact(payload: dict[str, object], generation: str) -> EvidenceArtifact:
    return EvidenceArtifact(
        payload=payload,
        status="found",
        observed_at=NOW,
        generation_id=generation,
        coverage_complete=True,
        provenance_refs=(f"{generation}:source",),
    )


def _contexts() -> dict[str, EvidenceArtifact]:
    return {
        "geometry_context": _artifact(
            {"status": "ok"},
            "geometry-1",
        ),
        "restriction_context": _artifact(
            {
                "status": "clear",
                "coverage_complete": True,
                "usable_area_m2": 9_000,
                "authoritative_blockers": [],
            },
            "restriction-1",
        ),
        "site_context": _artifact(
            {
                "physical_access_status": "ready",
                "legal_access_status": "ready",
                "infrastructure_status": "ready",
                "capacity_status": "ready",
            },
            "site-1",
        ),
        "planning_context": _artifact(
            {
                "status": "clear",
                "current_use_allowed": True,
                "pdp_complete": True,
                "future_adverse": [],
            },
            "planning-1",
        ),
        "history_reference": _artifact(
            {
                "status": "ok",
                "matched_count": 12,
                "median_sale_to_start_ratio": 1.8,
            },
            "history-3",
        ),
    }


def _market() -> dict[str, object]:
    return {
        "status": "ok",
        "estimate": {
            "range_low_kzt": 12_000_000.0,
            "median_kzt": 13_500_000.0,
            "range_high_kzt": 15_000_000.0,
            "verified_comparables_used": 3,
        },
        "confidence": "high",
        "high_quality_verified_count": 3,
        "verified_eligible_count": 3,
        "listing_eligible_count": 0,
        "engine_version": "strict-market-comparables.v2-same-year",
        "evaluations": [
            {
                "source_id": "sale",
                "source_record_id": str(index),
                "price_kind": "verified_sale",
                "quality_grade": "A",
                "eligible": True,
                "observed_at": NOW.isoformat(),
            }
            for index in range(3)
        ],
    }


def _exact(amount: int) -> dict[str, int]:
    return {"low_kzt": amount, "base_kzt": amount, "high_kzt": amount}


def _costs() -> dict[str, object]:
    amounts = {
        "connection": 500_000,
        "development": 500_000,
        "registration": 100_000,
        "tax_annual": 100_000,
        "due_diligence": 100_000,
        "financing": 200_000,
        "contingency": 200_000,
        "risk_reserve": 300_000,
    }
    return {key: _exact(amount) for key, amount in amounts.items()}


def _contract() -> dict[str, object]:
    return {
        "status": "ok",
        "candidates": [],
        "conflicts": [],
        "content_hash": "a" * 64,
        "pages_processed": 1,
        "text_chars_processed": 100,
        "extractor_version": "auction-legal-doc.v1",
    }


def _contract_coverage() -> ContractCoverage:
    return ContractCoverage(
        eligible_document_ids=("contract-1",),
        processed_document_ids=("contract-1",),
        observed_at=NOW,
        generation_id="contract-generation-1",
        coverage_complete=True,
    )


def _lot() -> DecisionLotFacts:
    return DecisionLotFacts(
        lot_id="lot-1",
        source_lot_id="452662",
        updated_at=NOW,
        start_price_kzt=1_000_000,
        purpose_text="земельный участок для кемпинга",
    )


def test_complete_synthetic_input_is_w10_w11_w13_compatible() -> None:
    contexts = _contexts()
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=_market(),
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **contexts,
    )

    outputs = result.module_outputs
    assert outputs["price_input"]["policy_version"] == POLICY_VERSION
    assert outputs["price_input"]["targets"] == {
        "holding_period_years": "5",
        "policy_version": POLICY_VERSION,
        "target_margin_percent": "25",
        "target_roi_percent": "30",
    }
    assert set(outputs["evidence_generation_ids"]) == {
        "legal_passport",
        "geometry_context",
        "restriction_context",
        "site_context",
        "planning_context",
        "history_reference",
        "market_estimate",
        "contract_extraction",
    }
    assert set(outputs["source_freshness"]) == set(outputs["evidence_generation_ids"])
    assert outputs["stale_reasons"] == []
    assert outputs["scenario_input"]["profile"] == "camping"
    assert outputs["history_reference"]["audit_only"] is True
    assert result.assembler_version == ASSEMBLER_VERSION
    assert all(key.startswith("decision_input:") for key in result.persistence_payloads)
    json.dumps(result.as_dict(), allow_nan=False)

    material = build_decision_material(
        DecisionLotInput("lot-1", NOW, 1_000_000, None),
        repeat_attempt_count=0,
        scenario_key="camping",
        module_outputs=outputs,
        checked_at=NOW,
    )
    assert material.verdict == "participate_up_to"
    assert material.bid_ceiling_kzt is not None


def test_452662_keeps_guarantee_out_of_cost_and_has_null_ceiling() -> None:
    passport = _passport(right="lease", lease_years=3)
    passport["payments"]["additional_payment"] = _payment(
        "additional_payment", 16_200, "expense", "once_after_win"
    )
    passport["payments"]["annual_rent"] = _payment(
        "annual_rent", 17_970, "expense", "annual_during_lease"
    )
    lot = DecisionLotFacts("lot-452662", "452662", NOW, 17_970, "кемпинг")
    result = assemble_decision_input(
        lot,
        scenario_key="camping",
        legal_passport=passport,
        contract_extractions=(),
        market_result={
            "status": "insufficient_data",
            "estimate": None,
            "confidence": "none",
            "high_quality_verified_count": 0,
            "verified_eligible_count": 0,
            "engine_version": "strict-market-comparables.v2-same-year",
        },
        actual_cost_ranges={},
        assembled_at=NOW,
    )
    price = result.module_outputs["price_input"]
    legal = price["legal_payments"]
    assert legal["refundable_guarantee_kzt"] == 216_250
    assert legal["guarantee_treatment"] == "blocked_capital"
    assert legal["one_time"] == [
        {
            "id": "additional-payment",
            "amount": _exact(16_200) | {"provenance_refs": ["legal:payment:additional_payment:1"]},
        }
    ]
    assert legal["annual_lease"]["amount"]["base_kzt"] == 17_970
    assert legal["payments_complete"] is False
    assert price["market_estimate"]["estimate"] is None
    assert set(price["cost_ranges"]) == set()

    price_for_engine = dict(price)
    price_for_engine["scenario"] = {
        "status": "requires_check",
        "critical_checks_resolved": False,
        "provenance_refs": [],
    }
    analysis = calculate_price_ceiling(price_for_engine)
    assert analysis.status == "insufficient"
    assert analysis.recommended_ceiling_kzt is None
    assert analysis.cash_timing_requirement_kzt == 216_250
    assert analysis.post_acquisition_cost_kzt is None


def test_less_than_three_grade_a_never_emits_market_estimate() -> None:
    market = _market()
    market["high_quality_verified_count"] = 2
    market["estimate"]["verified_comparables_used"] = 2
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=market,
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )
    assert result.module_outputs["market_estimate"]["status"] == "insufficient_data"
    assert result.module_outputs["market_estimate"]["estimate"] is None
    assert "module_incomplete:market_estimate" in result.module_outputs["stale_reasons"]


def test_partial_stale_conflict_and_missing_costs_remain_explicit() -> None:
    passport = _passport()
    passport["facts"]["encumbrances"] = _fact("encumbrances", ["conflicting records"], "conflict")
    contexts = _contexts()
    contexts["geometry_context"] = EvidenceArtifact(
        payload={"status": "ok"},
        status="partial",
        observed_at=NOW - timedelta(days=200),
        generation_id="geometry-old",
        coverage_complete=False,
    )
    contexts["site_context"] = _artifact(
        {
            "physical_access_status": "ready",
            "legal_access_status": "ready",
            "infrastructure_status": "ready",
            "capacity_status": "unknown",
        },
        "site-incomplete",
    )
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=passport,
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=_market(),
        actual_cost_ranges={"connection": _exact(1)},
        assembled_at=NOW,
        **contexts,
    )
    stale = result.module_outputs["stale_reasons"]
    assert "module_incomplete:geometry_context" in stale
    assert "module_incomplete:site_context" in stale
    assert "module_incomplete:legal_passport" in stale
    assert result.module_outputs["source_freshness"]["geometry_context"]["status"] == "stale"
    for key in REQUIRED_COST_KEYS:
        if key != "connection":
            assert f"actual_cost_missing:{key}" in stale
    assert result.module_outputs["scenario_input"]["legal_passport"]["status"] == "conflict"


def test_policy_constants_are_exact_global_non_personalized_contract() -> None:
    expected = {
        "resale": ("1", "20", "15"),
        "operating_business": ("3", "25", "20"),
        "land_rent": ("5", "20", "15"),
        "sublease": ("3", "25", "20"),
        "development": ("5", "30", "25"),
        "camping": ("5", "30", "25"),
        "hospitality": ("7", "30", "25"),
    }
    assert {
        key: (
            value.holding_period_years,
            value.target_roi_percent,
            value.target_margin_percent,
        )
        for key, value in STANDARD_POLICY.items()
    } == expected


def test_single_ok_extraction_does_not_imply_complete_contract_coverage() -> None:
    incomplete_coverage = ContractCoverage(
        eligible_document_ids=("contract", "project", "rules", "notice", "appendix"),
        processed_document_ids=("contract",),
        observed_at=NOW,
        generation_id="contract-partial",
        coverage_complete=False,
    )
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(_contract(),),
        contract_coverage=incomplete_coverage,
        market_result=_market(),
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )
    assert result.module_outputs["contract_extraction"]["coverage_complete"] is False
    assert result.module_outputs["price_input"]["legal_payments"]["payments_complete"] is False
    assert "contract_missing_or_unresolved" in result.module_outputs["stale_reasons"]


def test_explicit_document_candidate_conflict_fails_contract_closed() -> None:
    contract = _contract()
    contract["candidates"] = [
        {
            "field": "transfer_right",
            "value": "Передача права требует согласия арендодателя",
            "status": "conflict",
            "document_id": "contract-1",
            "quote_hash": "quote-1",
        }
    ]
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(contract,),
        contract_coverage=_contract_coverage(),
        market_result=_market(),
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )

    outputs = result.module_outputs
    assert outputs["contract_extraction"]["status"] == "conflict"
    assert outputs["contract_extraction"]["conflict_fields"] == ["transfer_right"]
    assert "contract_missing_or_unresolved" in outputs["stale_reasons"]
    assert outputs["price_input"]["legal_payments"]["payments_complete"] is False


def test_distinct_additive_document_conditions_remain_complete_without_explicit_conflict() -> None:
    contract = _contract()
    contract["candidates"] = [
        {
            "field": "termination_ground",
            "value": "Расторжение при неосвоении участка",
            "status": "preliminary",
            "document_id": "contract-1",
            "quote_hash": "quote-1",
        },
        {
            "field": "termination_ground",
            "value": "Расторжение при просрочке платежа",
            "status": "preliminary",
            "document_id": "contract-1",
            "quote_hash": "quote-2",
        },
    ]
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(contract,),
        contract_coverage=_contract_coverage(),
        market_result=_market(),
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )

    outputs = result.module_outputs
    assert outputs["contract_extraction"]["status"] == "found"
    assert outputs["contract_extraction"]["conflict_fields"] == []
    assert outputs["price_input"]["legal_payments"]["payments_complete"] is True


def test_legal_freshness_uses_oldest_required_fact_not_fresh_payment() -> None:
    passport = _passport()
    old = NOW - timedelta(days=200)
    passport["facts"]["right_type"]["observed_at"] = old.isoformat()
    passport["payments"]["guarantee"]["observed_at"] = NOW.isoformat()
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=passport,
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=_market(),
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )
    assert result.module_outputs["source_freshness"]["legal_passport"] == {
        "status": "stale",
        "observed_at": old.isoformat(),
    }
    assert "source_stale:legal_passport" in result.module_outputs["stale_reasons"]


def test_market_freshness_uses_oldest_grade_a_and_malformed_count_is_insufficient() -> None:
    market = _market()
    old = NOW - timedelta(days=200)
    market["evaluations"][0]["observed_at"] = old.isoformat()
    result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=market,
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )
    assert result.module_outputs["source_freshness"]["market_estimate"] == {
        "status": "stale",
        "observed_at": old.isoformat(),
    }

    malformed = _market()
    malformed["estimate"]["verified_comparables_used"] = "3"
    malformed_result = assemble_decision_input(
        _lot(),
        scenario_key="camping",
        legal_passport=_passport(),
        contract_extractions=(_contract(),),
        contract_coverage=_contract_coverage(),
        market_result=malformed,
        actual_cost_ranges=_costs(),
        assembled_at=NOW,
        **_contexts(),
    )
    assert malformed_result.module_outputs["market_estimate"]["status"] == "insufficient_data"
    assert malformed_result.module_outputs["market_estimate"]["estimate"] is None


def test_deterministic_hash_and_malformed_oversized_nonfinite_rejected() -> None:
    kwargs = {
        "scenario_key": "camping",
        "legal_passport": _passport(),
        "contract_extractions": (_contract(),),
        "contract_coverage": _contract_coverage(),
        "market_result": _market(),
        "actual_cost_ranges": _costs(),
        "assembled_at": NOW,
        **_contexts(),
    }
    first = assemble_decision_input(_lot(), **kwargs)
    second = assemble_decision_input(_lot(), **kwargs)
    assert first.input_hash == second.input_hash
    assert first.persistence_payloads == second.persistence_payloads

    with pytest.raises(DecisionInputError):
        assemble_decision_input(
            _lot(),
            **(kwargs | {"actual_cost_ranges": {"connection": _exact(float("nan"))}}),
        )
    with pytest.raises(DecisionInputError):
        assemble_decision_input(
            _lot(),
            **(
                kwargs
                | {
                    "geometry_context": EvidenceArtifact(
                        payload={"huge": "x" * 70_000},
                        status="found",
                        observed_at=NOW,
                        coverage_complete=True,
                    )
                }
            ),
        )
    with pytest.raises(DecisionInputError):
        assemble_decision_input(
            _lot(),
            **(kwargs | {"scenario_key": "personalized_roi"}),
        )
