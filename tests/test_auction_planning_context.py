from __future__ import annotations

from app.auction_planning_context import PlanningLimits, analyze_planning_context

STAMP = "2026-08-17T10:00:00+00:00"


def polygon(x1=76.9, y1=43.2, x2=76.901, y2=43.201):
    return {
        "type": "Polygon",
        "coordinates": [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
    }


def source(source_id, document_type, version, coverage, authoritative=True):
    return {
        "id": source_id,
        "document_type": document_type,
        "version": version,
        "provenance": f"official_{source_id}",
        "observed_at": STAMP,
        "authoritative": authoritative,
        "coverage": coverage,
    }


def complete_sources():
    return [
        source("gp", "genplan", "2026", {"current_zoning": True}),
        source(
            "pdp",
            "pdp",
            "2026-07",
            {
                "future_zoning": True,
                "planned_roads": True,
                "red_lines": True,
                "engineering_corridors": True,
                "szz": True,
            },
        ),
    ]


def feature(kind, source_id, geometry, value=None, allowed_use=None):
    result = {"kind": kind, "source_id": source_id, "geometry": geometry}
    if value is not None:
        result["value"] = value
    if allowed_use is not None:
        result["allowed_use"] = allowed_use
    return result


def test_complete_current_attractive_context_is_clear() -> None:
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[
            feature("current_zone", "gp", polygon(), "commercial", True),
            feature("future_zone", "pdp", polygon(), "commercial", True),
        ],
    )
    assert result.status == "clear"
    assert all(item.complete for item in result.coverage)
    assert result.current_relations[0].intersects is True
    assert result.current_relations[0].parcel_percent is not None
    assert not hasattr(result, "score")
    assert not hasattr(result, "verdict")


def test_452662_like_missing_pdp_is_partial_not_clear() -> None:
    result = analyze_planning_context(
        polygon(),
        planning_sources=[source("gp", "genplan", "2026", {"current_zoning": True})],
        planning_features=[feature("current_zone", "gp", polygon(), "recreation", True)],
    )
    assert result.status == "partial"
    assert any("pdp:" in warning for warning in result.warnings)
    assert sum(not item.complete for item in result.coverage) == 5


def test_attractive_current_zone_but_future_road_and_red_line_are_conflict() -> None:
    road = {
        "type": "LineString",
        "coordinates": [[76.9, 43.2005], [76.901, 43.2005]],
    }
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[
            feature("current_zone", "gp", polygon(), "commercial", True),
            feature("planned_road", "pdp", road, "collector"),
            feature("red_line", "pdp", polygon(76.9004, 43.2, 76.9006, 43.201)),
        ],
    )
    assert result.status == "conflict"
    assert result.current_relations[0].allowed_use is True
    assert {item.kind for item in result.future_relations} == {"planned_road", "red_line"}
    assert {item.code for item in result.conflicts} == {
        "planned_road_intersection",
        "red_line_intersection",
    }
    road_relation = next(item for item in result.future_relations if item.kind == "planned_road")
    assert road_relation.intersection_length_m is not None


def test_source_version_zone_disagreement_is_preserved() -> None:
    sources = complete_sources() + [source("gp_old", "genplan", "2024", {"current_zoning": True})]
    result = analyze_planning_context(
        polygon(),
        planning_sources=sources,
        planning_features=[
            feature("current_zone", "gp", polygon(), "commercial", True),
            feature("current_zone", "gp_old", polygon(), "industrial", True),
        ],
    )
    assert result.status == "conflict"
    conflict = next(item for item in result.conflicts if item.code == "source_version_conflict")
    assert set(conflict.source_ids) == {"gp", "gp_old"}
    assert set(conflict.versions) == {"2026", "2024"}


def test_relation_distance_is_computed_for_nonintersecting_feature() -> None:
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[
            feature("current_zone", "gp", polygon(), "commercial", True),
            feature("future_zone", "pdp", polygon(), "commercial", True),
            feature("szz", "pdp", polygon(77.0, 43.3, 77.001, 43.301)),
        ],
    )
    relation = next(item for item in result.future_relations if item.kind == "szz")
    assert result.status == "clear"
    assert relation.intersects is False
    assert relation.distance_m > 10_000
    assert relation.intersection_area_m2 is None


def test_nonauthoritative_adverse_layer_is_warning_not_authoritative_conflict() -> None:
    sources = complete_sources() + [
        source("draft", "pdp", "draft-1", {"red_lines": True}, authoritative=False)
    ]
    result = analyze_planning_context(
        polygon(),
        planning_sources=sources,
        planning_features=[feature("red_line", "draft", polygon())],
    )
    assert result.status == "partial"
    assert not result.conflicts
    assert "non-authoritative" in result.warnings[0]


def test_missing_inputs_and_malformed_or_oversized_payloads_are_explicit() -> None:
    missing = analyze_planning_context(None, planning_sources=None, planning_features=None)
    malformed = analyze_planning_context(
        polygon(),
        planning_sources=[source("bad", "map", "1", {})],
        planning_features=[],
    )
    oversized = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[feature("current_zone", "gp", polygon()) for _ in range(3)],
        limits=PlanningLimits(max_features=2),
    )
    outside = analyze_planning_context(
        polygon(10, 43, 11, 44),
        planning_sources=complete_sources(),
        planning_features=[],
    )
    assert missing.status == "unknown"
    assert malformed.status == "error" and malformed.error_code == "invalid_document_type"
    assert oversized.status == "error" and oversized.error_code == "invalid_features"
    assert outside.status == "error" and outside.error_code == "outside_kazakhstan"


def test_vertex_and_text_bounds_are_enforced_deterministically() -> None:
    too_many_vertices = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[feature("current_zone", "gp", polygon())],
        limits=PlanningLimits(max_vertices=4),
    )
    long_value = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[feature("current_zone", "gp", polygon(), "x" * 241)],
    )
    assert too_many_vertices.status == "error"
    assert too_many_vertices.error_code == "too_many_vertices"
    assert long_value.status == "error" and long_value.error_code == "string_too_long"


def test_complete_inventory_without_zone_relations_is_partial_not_clear() -> None:
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[],
    )
    assert result.status == "partial"
    warnings = " ".join(result.warnings)
    assert "current_zone_relation" in warnings
    assert "future_zone_relation_or_not_applicable" in warnings


def test_explicit_future_zone_not_applicable_satisfies_relation_contract() -> None:
    sources = complete_sources()
    sources[1]["not_applicable"] = ["future_zoning"]
    result = analyze_planning_context(
        polygon(),
        planning_sources=sources,
        planning_features=[feature("current_zone", "gp", polygon(), "commercial", True)],
    )
    assert result.status == "clear"


def test_feature_kind_must_match_source_document_and_layer() -> None:
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[feature("red_line", "gp", polygon())],
    )
    assert result.status == "error"
    assert result.error_code == "feature_source_layer_mismatch"


def test_boundary_touch_is_not_adverse_intersection_conflict() -> None:
    touching = polygon(76.901, 43.2, 76.902, 43.201)
    result = analyze_planning_context(
        polygon(),
        planning_sources=complete_sources(),
        planning_features=[
            feature("current_zone", "gp", polygon(), "commercial", True),
            feature("future_zone", "pdp", polygon(), "commercial", True),
            feature("red_line", "pdp", touching),
        ],
    )
    red_line = next(item for item in result.future_relations if item.kind == "red_line")
    assert result.status == "clear"
    assert red_line.intersects is False
    assert red_line.touches_only is True
    assert not result.conflicts


def test_future_not_applicable_must_come_from_source_owning_future_zoning() -> None:
    sources = complete_sources() + [
        {
            **source("pdp_other", "pdp", "2026-other", {"red_lines": True}),
            "not_applicable": ["future_zoning"],
        }
    ]
    result = analyze_planning_context(
        polygon(),
        planning_sources=sources,
        planning_features=[feature("current_zone", "gp", polygon(), "commercial", True)],
    )
    assert result.status == "partial"
    assert "future_zone_relation_or_not_applicable" in " ".join(result.warnings)


def test_source_observed_at_requires_timezone() -> None:
    sources = complete_sources()
    sources[0]["observed_at"] = "2026-08-17T10:00:00"
    result = analyze_planning_context(
        polygon(),
        planning_sources=sources,
        planning_features=[],
    )
    assert result.status == "error"
    assert result.error_code == "timezone_required"
