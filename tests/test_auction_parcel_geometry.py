from __future__ import annotations

import math

from app.auction_parcel_geometry import GeometryLimits, analyze_parcel_geometry


def polygon(x1=76.9, y1=43.2, x2=76.901, y2=43.201):
    return {
        "type": "Polygon",
        "coordinates": [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
    }


def feature(layer: str, geometry: dict):
    return {"type": "Feature", "properties": {"layer": layer}, "geometry": geometry}


def test_polygon_metrics_and_explicit_road_facade() -> None:
    parcel = polygon()
    road = {
        "type": "LineString",
        "coordinates": [[76.9, 43.2], [76.901, 43.2]],
    }
    result = analyze_parcel_geometry(
        parcel,
        road_edge_geojson=road,
        road_edge_confidence=0.92,
        road_edge_provenance="surveyed_access_edge",
    )

    assert result.status == "ok"
    assert result.area_m2 is not None and 8_000 < result.area_m2 < 10_000
    assert result.perimeter_m is not None and 350 < result.perimeter_m < 410
    assert result.compactness is not None and 0.7 < result.compactness < 0.9
    assert result.bbox_width_m is not None and 75 < result.bbox_width_m < 90
    assert result.bbox_height_m is not None and 105 < result.bbox_height_m < 120
    assert result.facade_status == "ok"
    assert result.facade_m is not None and 75 < result.facade_m < 90
    assert result.depth_m is not None and 100 < result.depth_m < 120
    assert result.facade_confidence == 0.92
    assert result.facade_provenance == "surveyed_access_edge"


def test_facade_is_unknown_without_explicit_road_edge() -> None:
    result = analyze_parcel_geometry(polygon())
    assert result.status == "ok"
    assert result.facade_m is None
    assert result.facade_status == "unknown"
    assert result.depth_m is None
    assert result.facade_confidence is None
    assert result.facade_provenance is None


def test_multipolygon_and_hole_area_are_supported() -> None:
    outer = polygon()["coordinates"][0]
    hole = polygon(76.9002, 43.2002, 76.9004, 43.2004)["coordinates"][0]
    with_hole = {"type": "Polygon", "coordinates": [outer, hole]}
    second = polygon(77.0, 43.3, 77.0005, 43.3005)["coordinates"]
    multi = {"type": "MultiPolygon", "coordinates": [[outer, hole], second]}

    plain = analyze_parcel_geometry(polygon())
    holed = analyze_parcel_geometry(with_hole)
    combined = analyze_parcel_geometry(multi)

    assert plain.area_m2 and holed.area_m2 and combined.area_m2
    assert holed.area_m2 < plain.area_m2
    assert combined.area_m2 > plain.area_m2


def test_invalid_outside_open_and_oversized_geometry_return_explicit_error() -> None:
    malformed = analyze_parcel_geometry({"type": "Polygon", "coordinates": [[1, 2]]})
    outside = analyze_parcel_geometry(polygon(10, 43, 11, 44))
    open_ring = polygon()
    open_ring["coordinates"][0].pop()
    open_result = analyze_parcel_geometry(open_ring)
    oversized = analyze_parcel_geometry(polygon(), limits=GeometryLimits(max_vertices=4))

    assert malformed.status == "error" and malformed.error_code == "invalid_ring"
    assert outside.status == "error" and outside.error_code == "outside_kazakhstan"
    assert open_result.status == "error" and open_result.error_code == "open_ring"
    assert oversized.status == "error" and oversized.error_code == "too_many_vertices"


def test_missing_geometry_and_restrictions_remain_unknown() -> None:
    missing = analyze_parcel_geometry(None)
    parcel_only = analyze_parcel_geometry(polygon())

    assert missing.status == "unknown"
    assert missing.area_m2 is None
    assert parcel_only.restrictions_status == "unknown"
    assert parcel_only.restricted_area_m2 is None
    assert parcel_only.remaining_usable_area_m2 is None


def test_partial_full_and_non_intersecting_restrictions() -> None:
    parcel = polygon()
    partial = polygon(76.9005, 43.2, 76.9015, 43.201)
    full = polygon(76.899, 43.199, 76.902, 43.202)
    away = polygon(77.1, 43.4, 77.101, 43.401)

    partial_result = analyze_parcel_geometry(
        parcel,
        restriction_features=[feature("red_line", partial)],
        restrictions_complete=True,
    )
    full_result = analyze_parcel_geometry(
        parcel,
        restriction_features=[feature("sanitary", full)],
        restrictions_complete=True,
    )
    none_result = analyze_parcel_geometry(
        parcel,
        restriction_features=[feature("water", away)],
        restrictions_complete=True,
    )

    assert partial_result.restrictions_status == "intersecting"
    assert partial_result.restricted_area_m2 and partial_result.area_m2
    assert math.isclose(
        partial_result.restricted_area_m2 / partial_result.area_m2,
        0.5,
        rel_tol=0.03,
    )
    assert full_result.remaining_usable_area_m2 is not None
    assert full_result.remaining_usable_area_m2 < 1
    assert none_result.restrictions_status == "clear"
    assert none_result.restricted_area_m2 == 0
    assert none_result.remaining_usable_area_m2 == none_result.area_m2


def test_per_layer_intersections_do_not_double_count_remaining_area() -> None:
    parcel = polygon()
    same_half = polygon(76.9005, 43.2, 76.901, 43.201)
    result = analyze_parcel_geometry(
        parcel,
        restriction_features=[
            feature("red_line", same_half),
            feature("power", same_half),
        ],
        restrictions_complete=True,
    )

    assert len(result.restriction_intersections) == 2
    assert result.area_m2 and result.restricted_area_m2 and result.remaining_usable_area_m2
    assert math.isclose(result.restricted_area_m2 / result.area_m2, 0.5, rel_tol=0.03)
    assert math.isclose(
        result.remaining_usable_area_m2 + result.restricted_area_m2,
        result.area_m2,
        rel_tol=0.001,
    )


def test_invalid_or_oversized_restrictions_are_error_not_clear() -> None:
    invalid = analyze_parcel_geometry(
        polygon(),
        restriction_features=[{"type": "Feature", "properties": {}, "geometry": None}],
    )
    oversized = analyze_parcel_geometry(
        polygon(),
        restriction_features=[feature("a", polygon()), feature("b", polygon())],
        limits=GeometryLimits(max_features=1),
    )

    assert invalid.status == "ok" and invalid.restrictions_status == "error"
    assert invalid.restriction_error_code == "invalid_restrictions"
    assert invalid.restriction_error_message
    assert invalid.restricted_area_m2 is None
    assert oversized.restrictions_status == "error"


def test_invalid_road_edge_has_explicit_error_status_and_never_crashes() -> None:
    result = analyze_parcel_geometry(
        polygon(),
        road_edge_geojson={"type": "Point", "coordinates": [76.9, 43.2]},
    )

    assert result.status == "ok"
    assert result.facade_status == "error"
    assert result.facade_error_code == "invalid_road_edge"
    assert result.facade_m is None


def test_nonintersecting_road_is_unknown_not_checked_facade() -> None:
    result = analyze_parcel_geometry(
        polygon(),
        road_edge_geojson={
            "type": "LineString",
            "coordinates": [[77.1, 43.4], [77.101, 43.4]],
        },
    )

    assert result.facade_status == "unknown"
    assert result.facade_error_code == "road_edge_not_intersecting"
    assert result.facade_m is None
    assert result.depth_m is None


def test_nonfinite_or_out_of_range_road_confidence_is_explicit_error() -> None:
    road = {"type": "LineString", "coordinates": [[76.9, 43.2], [76.901, 43.2]]}
    for confidence in (math.nan, math.inf, -0.1, 1.1):
        result = analyze_parcel_geometry(
            polygon(),
            road_edge_geojson=road,
            road_edge_confidence=confidence,
        )
        assert result.facade_status == "error"
        assert result.facade_error_code == "invalid_road_confidence"
        assert result.facade_m is None


def test_incomplete_restrictions_never_claim_clear_or_usable_area() -> None:
    incomplete_empty = analyze_parcel_geometry(polygon(), restriction_features=[])
    incomplete_away = analyze_parcel_geometry(
        polygon(),
        restriction_features=[feature("known_layer", polygon(77.1, 43.4, 77.101, 43.401))],
    )
    complete_clear = analyze_parcel_geometry(
        polygon(),
        restriction_features=[],
        restrictions_complete=True,
    )

    for result in (incomplete_empty, incomplete_away):
        assert result.restrictions_status == "partial"
        assert result.restrictions_complete is False
        assert result.restricted_area_m2 is None
        assert result.remaining_usable_area_m2 is None
    assert complete_clear.restrictions_status == "clear"
    assert complete_clear.restrictions_complete is True
    assert complete_clear.restricted_area_m2 == 0
    assert complete_clear.remaining_usable_area_m2 == complete_clear.area_m2
