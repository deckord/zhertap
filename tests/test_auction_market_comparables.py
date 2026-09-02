from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.auction_market_comparables import (
    ComparableCandidate,
    ComparableConfig,
    ComparableTarget,
    build_strict_market_comparables,
)

VALUATION_AT = datetime(2026, 8, 17, tzinfo=UTC)


def _target(**overrides: object) -> ComparableTarget:
    values: dict[str, object] = {
        "target_id": "target-1",
        "right_type": "ownership",
        "purpose_group": "retail",
        "area_ha": 1.0,
        "valuation_at": VALUATION_AT,
        "locality": "Семей",
        "latitude": 50.4111,
        "longitude": 80.2275,
        "access_readiness": "ready",
        "infrastructure_readiness": "ready",
    }
    values.update(overrides)
    return ComparableTarget(**values)


def _candidate(index: int, **overrides: object) -> ComparableCandidate:
    values: dict[str, object] = {
        "source_id": "registry",
        "source_record_id": f"sale-{index}",
        "source_url": f"https://market.example/sale-{index}",
        "title": f"Продажа участка {index}",
        "object_id": f"land-{index}",
        "right_type": "ownership",
        "purpose_group": "retail",
        "area_ha": 1.0,
        "price_kzt": 10_000_000 + index * 100_000,
        "price_kind": "verified_sale",
        "observed_at": VALUATION_AT - timedelta(days=30 + index),
        "locality": "Семей",
        "latitude": 50.4111 + index * 0.001,
        "longitude": 80.2275,
        "access_readiness": "ready",
        "infrastructure_readiness": "ready",
    }
    values.update(overrides)
    return ComparableCandidate(**values)


def test_two_high_quality_sales_are_insufficient_but_three_produce_estimate() -> None:
    target = _target()
    two = build_strict_market_comparables(target, [_candidate(1), _candidate(2)])
    three = build_strict_market_comparables(
        target, [_candidate(1), _candidate(2), _candidate(3)]
    )

    assert two.status == "insufficient_data"
    assert two.estimate is None
    assert two.high_quality_verified_count == 2
    assert three.status == "ok"
    assert three.estimate is not None
    assert three.estimate.verified_comparables_used == 3
    assert three.confidence == "medium"


def test_hard_mismatches_are_excluded_before_valuation() -> None:
    candidates = [
        _candidate(1, right_type="lease", lease_term_years=5),
        _candidate(2, purpose_group="industrial"),
        _candidate(3, area_ha=1.5),
        _candidate(4, latitude=51.0),
        _candidate(5, access_readiness="none"),
        _candidate(6, infrastructure_readiness="unknown"),
        _candidate(7, price_kzt=float("nan")),
        _candidate(8, latitude=float("nan")),
    ]

    result = build_strict_market_comparables(_target(), candidates)

    assert result.status == "insufficient_data"
    assert result.estimate is None
    reasons = {evaluation.exclusion_reason for evaluation in result.evaluations}
    assert {
        "right_type_mismatch",
        "purpose_mismatch",
        "area_mismatch",
        "outside_radius",
        "access_mismatch",
        "unknown_access_or_infrastructure",
        "invalid_price",
        "invalid_coordinates",
    } <= reasons


def test_listing_only_and_stale_sets_never_become_sale_estimate() -> None:
    listing_candidates = [
        _candidate(index, price_kind="listing") for index in range(1, 5)
    ]
    stale_candidates = [
        _candidate(index, observed_at=VALUATION_AT - timedelta(days=500))
        for index in range(5, 9)
    ]

    listings = build_strict_market_comparables(_target(), listing_candidates)
    stale = build_strict_market_comparables(_target(), stale_candidates)

    assert listings.status == "insufficient_data"
    assert listings.estimate is None
    assert listings.listing_eligible_count == 4
    assert listings.verified_eligible_count == 0
    assert all(item.quality_grade == "L" for item in listings.evaluations)
    assert stale.estimate is None
    assert all(item.exclusion_reason == "stale" for item in stale.evaluations)


def test_prior_year_verified_sales_cannot_produce_strict_estimate() -> None:
    valuation_at = datetime(2026, 1, 15, tzinfo=UTC)
    candidates = [
        _candidate(
            index,
            observed_at=datetime(2025, 12, 20 + index, tzinfo=UTC),
        )
        for index in range(1, 4)
    ]

    result = build_strict_market_comparables(
        _target(valuation_at=valuation_at), candidates
    )

    assert result.status == "insufficient_data"
    assert result.estimate is None
    assert result.verified_eligible_count == 0
    assert all(
        item.exclusion_reason == "different_calendar_year"
        for item in result.evaluations
    )


def test_duplicates_outlier_and_adjustments_are_transparent() -> None:
    normal = [
        _candidate(1, price_kzt=9_000_000),
        _candidate(2, price_kzt=10_000_000),
        _candidate(3, price_kzt=11_000_000),
    ]
    outlier = _candidate(4, price_kzt=100_000_000)
    adjusted = _candidate(
        5,
        price_kzt=10_000_000,
        area_ha=0.9,
        access_readiness="partial",
        infrastructure_readiness="partial",
    )
    duplicate_listing = replace(
        normal[0],
        source_id="listing-site",
        source_record_id="duplicate-listing",
        source_url="https://listing.example/duplicate",
        price_kind="listing",
        observed_at=VALUATION_AT - timedelta(days=1),
    )
    candidates = normal + [outlier, adjusted, duplicate_listing]

    result = build_strict_market_comparables(_target(), candidates)

    assert result.status == "ok"
    assert result.estimate is not None
    assert result.high_quality_verified_count == 3
    assert result.estimate.range_high_kzt < 20_000_000
    excluded = {item.exclusion_reason: item for item in result.evaluations if not item.eligible}
    assert "duplicate" in excluded
    assert excluded["duplicate"].duplicate_of == "registry:sale-1"
    assert "price_outlier" in excluded
    adjusted_result = next(
        item for item in result.evaluations if item.source_record_id == adjusted.source_record_id
    )
    dimensions = {item.dimension for item in adjusted_result.adjustments}
    assert dimensions == {
        "area_normalization",
        "access_readiness",
        "infrastructure_readiness",
    }
    assert adjusted_result.quality_grade == "B"


def test_ownership_and_lease_terms_are_never_mixed() -> None:
    target = _target(
        right_type="lease",
        lease_term_years=3,
    )
    candidates = [
        _candidate(1, right_type="ownership"),
        _candidate(2, right_type="lease", lease_term_years=5),
        _candidate(3, right_type="lease", lease_term_years=None),
        _candidate(4, right_type="lease", lease_term_years=2),
    ]

    result = build_strict_market_comparables(target, candidates)

    reasons = {item.source_record_id: item.exclusion_reason for item in result.evaluations}
    assert reasons["sale-1"] == "right_type_mismatch"
    assert reasons["sale-2"] == "lease_term_band_mismatch"
    assert reasons["sale-3"] == "unknown_lease_term"
    assert reasons["sale-4"] is None
    assert result.estimate is None


def test_unknown_target_readiness_returns_invalid_target_without_estimate() -> None:
    result = build_strict_market_comparables(
        _target(access_readiness="unknown"),
        [_candidate(1), _candidate(2), _candidate(3)],
    )

    assert result.status == "invalid_target"
    assert result.detail == "unknown_target_readiness"
    assert result.estimate is None


def test_locality_fallback_is_allowed_but_different_unknown_geography_is_not() -> None:
    target = _target(latitude=None, longitude=None)
    candidates = [
        _candidate(1, latitude=None, longitude=None, locality="Семей"),
        _candidate(2, latitude=None, longitude=None, locality="Курчатов"),
    ]

    result = build_strict_market_comparables(target, candidates)

    evaluations = {item.source_record_id: item for item in result.evaluations}
    assert evaluations["sale-1"].eligible is True
    assert evaluations["sale-1"].quality_grade == "B"
    assert evaluations["sale-1"].distance_km is None
    assert evaluations["sale-2"].exclusion_reason == "geography_unknown_or_mismatch"


def test_three_locality_only_records_cannot_satisfy_high_quality_geo_gate() -> None:
    target = _target(latitude=None, longitude=None)
    candidates = [
        _candidate(index, latitude=None, longitude=None, locality="Семей")
        for index in range(1, 4)
    ]

    result = build_strict_market_comparables(target, candidates)

    assert result.status == "insufficient_data"
    assert result.estimate is None
    assert result.high_quality_verified_count == 0
    assert result.verified_eligible_count == 3
    assert all(item.quality_grade == "B" for item in result.evaluations)


def test_oversized_candidate_input_is_rejected_not_silently_truncated() -> None:
    candidates = [_candidate(index) for index in range(1, 5)]

    result = build_strict_market_comparables(
        _target(),
        candidates,
        config=ComparableConfig(max_candidates=3),
    )

    assert result.status == "invalid_input"
    assert result.estimate is None
    assert result.detail == "candidate_count_exceeds_limit:3"
    assert result.evaluations == ()


def test_invalid_timestamp_is_sanitized_and_api_serialization_remains_safe() -> None:
    candidate = _candidate(1, observed_at="not-a-datetime")

    result = build_strict_market_comparables(_target(), [candidate])
    payload = result.as_dict()

    assert result.evaluations[0].exclusion_reason == "observed_at_not_timezone_aware"
    assert result.evaluations[0].observed_at is None
    assert payload["evaluations"][0]["observed_at"] is None


def test_nonfinite_config_is_rejected_without_estimate() -> None:
    result = build_strict_market_comparables(
        _target(),
        [_candidate(1), _candidate(2), _candidate(3)],
        config=ComparableConfig(radius_km=float("nan")),
    )

    assert result.status == "invalid_input"
    assert result.detail == "invalid_nonfinite_config"
    assert result.estimate is None


def test_malformed_numeric_payloads_are_rejected_without_runtime_error() -> None:
    malformed_candidate = _candidate(1, price_kzt="bad", area_ha=True)
    candidate_result = build_strict_market_comparables(
        _target(),
        [malformed_candidate],
    )
    malformed_target = build_strict_market_comparables(
        _target(area_ha="bad"),
        [_candidate(1)],
    )

    assert candidate_result.status == "insufficient_data"
    assert candidate_result.evaluations[0].exclusion_reason == "invalid_area"
    assert candidate_result.as_dict()["estimate"] is None
    assert malformed_target.status == "invalid_target"
    assert malformed_target.detail == "invalid_area"
    assert malformed_target.estimate is None


def test_output_is_deterministic_for_reordered_input() -> None:
    candidates = [
        _candidate(3, price_kzt=11_000_000),
        _candidate(1, price_kzt=9_000_000),
        _candidate(2, price_kzt=10_000_000),
    ]

    first = build_strict_market_comparables(_target(), candidates)
    second = build_strict_market_comparables(_target(), list(reversed(candidates)))

    assert first.as_dict() == second.as_dict()
