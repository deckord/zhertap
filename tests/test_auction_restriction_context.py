from __future__ import annotations

import math

from app.auction_restriction_context import (
    REQUIRED_RESTRICTION_LAYERS,
    RestrictionLimits,
    analyze_restriction_context,
)

STAMP = "2026-08-17T10:00:00+00:00"


def polygon(x1=76.9, y1=43.2, x2=76.901, y2=43.201):
    return {
        "type": "Polygon",
        "coordinates": [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
    }


def source(source_id="all", version="2026", layers=REQUIRED_RESTRICTION_LAYERS):
    return {
        "id": source_id,
        "version": version,
        "provenance": f"official_{source_id}",
        "observed_at": STAMP,
        "authoritative": True,
        "coverage": {layer: True for layer in layers},
    }


def feature(
    layer,
    geometry,
    source_id="all",
    value=None,
    mode="area",
    impact=None,
    restriction_id=None,
    reduces_usable_area=None,
):
    result = {
        "layer": layer,
        "source_id": source_id,
        "geometry": geometry,
        "geometry_mode": mode,
    }
    if value is not None:
        result["value"] = value
    if impact is not None:
        result["impact"] = impact
    if restriction_id is not None:
        result["restriction_id"] = restriction_id
    if reduces_usable_area is not None:
        result["reduces_usable_area"] = reduces_usable_area
    return result


def test_complete_authoritative_coverage_with_no_restrictions_is_clear() -> None:
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[],
    )
    assert result.status == "clear"
    assert result.observed_restricted_area_m2 == 0
    assert result.restricted_area_m2 == 0
    assert result.usable_area_m2 == result.parcel_area_m2
    assert all(item.status == "clear" for item in result.layers)
    assert not hasattr(result, "score")
    assert not hasattr(result, "verdict")


def test_452662_missing_layers_is_partial_and_usable_unknown() -> None:
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source(layers=("red_lines", "szz"))],
        restriction_features=[],
    )
    assert result.status == "partial"
    assert result.restricted_area_m2 is None
    assert result.usable_area_m2 is None
    assert result.observed_restricted_area_m2 is None
    assert sum(not layer.coverage_complete for layer in result.layers) == 6


def test_overlapping_layers_union_does_not_double_count_restricted_area() -> None:
    half = polygon(76.9005, 43.2, 76.901, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[
            feature("red_lines", half, value="RL-1"),
            feature("szz", half, value="SZZ-1"),
        ],
    )
    assert result.status == "restricted"
    assert result.parcel_area_m2 and result.restricted_area_m2 and result.usable_area_m2
    assert math.isclose(result.restricted_area_m2 / result.parcel_area_m2, 0.5, rel_tol=0.03)
    layer_sum = sum(layer.observed_area_m2 or 0 for layer in result.layers)
    assert layer_sum > result.restricted_area_m2
    assert math.isclose(
        result.restricted_area_m2 + result.usable_area_m2,
        result.parcel_area_m2,
        rel_tol=0.001,
    )


def test_touch_is_not_positive_restriction_and_remains_partial() -> None:
    touching = polygon(76.901, 43.2, 76.902, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[feature("red_lines", touching)],
    )
    fact = result.facts[0]
    assert result.status == "partial"
    assert fact.intersects is False
    assert fact.touches_only is True
    assert result.restricted_area_m2 is None
    assert result.usable_area_m2 is None


def test_line_fact_reports_length_but_does_not_invent_usable_area() -> None:
    line = {
        "type": "LineString",
        "coordinates": [[76.9, 43.2005], [76.901, 43.2005]],
    }
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[feature("power_protection", line, mode="line_fact")],
    )
    fact = result.facts[0]
    assert result.status == "restricted"
    assert fact.intersection_length_m and fact.intersection_length_m > 70
    assert result.restricted_area_m2 is None
    assert result.usable_area_m2 is None


def test_conflicting_authoritative_versions_are_preserved() -> None:
    half = polygon(76.9005, 43.2, 76.901, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[
            source("new", "2026", ("cadastral_restrictions",)),
            source("old", "2024", ("cadastral_restrictions",)),
        ],
        restriction_features=[
            feature(
                "cadastral_restrictions",
                half,
                "new",
                "lease-only",
                impact="warning",
                restriction_id="restriction-77",
            ),
            feature(
                "cadastral_restrictions",
                half,
                "old",
                "no-burden",
                impact="warning",
                restriction_id="restriction-77",
            ),
        ],
        expected_layers=("cadastral_restrictions",),
    )
    assert result.status == "conflict"
    assert result.conflicts[0].code == "source_version_conflict"
    assert set(result.conflicts[0].versions) == {"2026", "2024"}
    assert result.usable_area_m2 is None


def test_per_layer_geometry_error_is_preserved_without_crashing_other_layers() -> None:
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[
            feature("red_lines", {"type": "Polygon", "coordinates": []}),
            feature("szz", polygon(77.0, 43.3, 77.001, 43.301)),
        ],
    )
    red_lines = next(layer for layer in result.layers if layer.layer == "red_lines")
    assert result.status == "partial"
    assert red_lines.status == "error"
    assert "invalid_nesting" in red_lines.errors[0]
    assert result.usable_area_m2 is None


def test_malformed_oversized_and_out_of_kz_inputs_are_explicit() -> None:
    malformed = analyze_restriction_context(
        polygon(),
        restriction_sources=[{**source(), "observed_at": "2026-08-17T10:00:00"}],
        restriction_features=[],
    )
    oversized = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[feature("red_lines", polygon()) for _ in range(3)],
        limits=RestrictionLimits(max_features=2),
    )
    outside = analyze_restriction_context(
        polygon(10, 43, 11, 44),
        restriction_sources=[source()],
        restriction_features=[],
    )
    assert malformed.status == "error" and malformed.error_code == "timezone_required"
    assert oversized.status == "error" and oversized.error_code == "invalid_features"
    assert outside.status == "error" and outside.error_code == "outside_kazakhstan"


def test_distinct_same_source_objects_are_not_version_conflict() -> None:
    half_a = polygon(76.9, 43.2, 76.9004, 43.201)
    half_b = polygon(76.9006, 43.2, 76.901, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[
            feature(
                "servitudes",
                half_a,
                value="access",
                impact="warning",
                restriction_id="servitude-a",
            ),
            feature(
                "servitudes",
                half_b,
                value="utility",
                impact="warning",
                restriction_id="servitude-b",
            ),
        ],
    )
    assert not result.conflicts


def test_warning_area_is_observed_but_does_not_reduce_usable_area() -> None:
    half = polygon(76.9005, 43.2, 76.901, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source()],
        restriction_features=[
            feature(
                "servitudes",
                half,
                value="notice-only",
                impact="warning",
                restriction_id="servitude-notice",
            )
        ],
    )
    assert result.status == "restricted"
    assert result.observed_restricted_area_m2 and result.observed_restricted_area_m2 > 0
    assert result.restricted_area_m2 == 0
    assert result.usable_area_m2 == result.parcel_area_m2
    assert result.facts[0].reduces_usable_area is False


def test_nonauthoritative_touch_is_retained_but_does_not_degrade_aggregate() -> None:
    draft = {**source("draft", "draft", ("red_lines",)), "authoritative": False}
    touching = polygon(76.901, 43.2, 76.902, 43.201)
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source(), draft],
        restriction_features=[feature("red_lines", touching, source_id="draft")],
    )
    assert result.status == "clear"
    assert result.facts[0].touches_only is True
    assert result.warnings
    assert result.restricted_area_m2 == 0
    assert result.usable_area_m2 == result.parcel_area_m2


def test_nonauthoritative_positive_blocker_claim_is_never_effective_area() -> None:
    draft = {**source("draft", "draft", ("red_lines",)), "authoritative": False}
    result = analyze_restriction_context(
        polygon(),
        restriction_sources=[source(), draft],
        restriction_features=[
            feature(
                "red_lines",
                polygon(76.9005, 43.2, 76.901, 43.201),
                source_id="draft",
            )
        ],
    )
    fact = result.facts[0]
    assert result.status == "clear"
    assert fact.claimed_reduces_usable_area is True
    assert fact.reduces_usable_area is False
    assert result.observed_restricted_area_m2 == 0
    assert result.restricted_area_m2 == 0
    assert result.usable_area_m2 == result.parcel_area_m2
