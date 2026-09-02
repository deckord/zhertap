from __future__ import annotations

import json

from app.auction_land_identity import set_land_object_boundary
from app.models import AuctionLandObject


def _object() -> AuctionLandObject:
    return AuctionLandObject.from_identifiers(jerler_object_id="17830")


def test_boundary_accepts_closed_polygon_and_persists_canonical_json() -> None:
    land_object = _object()
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[76.0, 50.0], [76.1, 50.0], [76.1, 50.1], [76.0, 50.0]]
        ],
    }

    assert set_land_object_boundary(
        land_object, geometry, source="jerler:source_object"
    ) is True
    assert json.loads(land_object.boundary_geojson or "null") == geometry
    assert land_object.boundary_source == "jerler:source_object"
    assert land_object.boundary_observed_at is not None


def test_boundary_rejects_open_degenerate_or_out_of_world_rings_without_mutation() -> None:
    invalid_geometries = (
        {
            "type": "Polygon",
            "coordinates": [[[76.0, 50.0], [76.1, 50.0], [76.1, 50.1]]],
        },
        {
            "type": "Polygon",
            "coordinates": [[[76.0, 50.0], [76.1, 50.0], [76.0, 50.0]]],
        },
        {
            "type": "Polygon",
            "coordinates": [
                [[181.0, 50.0], [76.1, 50.0], [76.1, 50.1], [181.0, 50.0]]
            ],
        },
    )

    for geometry in invalid_geometries:
        land_object = _object()
        assert set_land_object_boundary(
            land_object, geometry, source="jerler:source_object"
        ) is False
        assert land_object.boundary_geojson is None
        assert land_object.boundary_source is None
        assert land_object.boundary_observed_at is None


def test_boundary_rejects_non_finite_boolean_and_oversized_coordinate_payloads() -> None:
    invalid_geometries = (
        {
            "type": "Polygon",
            "coordinates": [
                [[76.0, 50.0], [float("nan"), 50.0], [76.1, 50.1], [76.0, 50.0]]
            ],
        },
        {
            "type": "Polygon",
            "coordinates": [[[True, 50.0], [76.1, 50.0], [76.1, 50.1], [True, 50.0]]],
        },
        {
            "type": "Polygon",
            "coordinates": [
                [[76.0 + index / 100_000, 50.0] for index in range(10_001)]
                + [[76.0, 50.0]]
            ],
        },
    )

    for geometry in invalid_geometries:
        land_object = _object()
        assert set_land_object_boundary(
            land_object, geometry, source="jerler:source_object"
        ) is False
        assert land_object.boundary_geojson is None


def test_boundary_accepts_valid_multipolygon() -> None:
    land_object = _object()
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[[76.0, 50.0], [76.1, 50.0], [76.1, 50.1], [76.0, 50.0]]][0]],
            [[[[77.0, 51.0], [77.1, 51.0], [77.1, 51.1], [77.0, 51.0]]][0]],
        ],
    }

    assert set_land_object_boundary(
        land_object, geometry, source="jerler:source_object"
    ) is True


def test_boundary_rejects_self_intersection_without_mutating_existing_boundary() -> None:
    land_object = _object()
    valid_geometry = {
        "type": "Polygon",
        "coordinates": [
            [[76.0, 50.0], [76.1, 50.0], [76.1, 50.1], [76.0, 50.0]]
        ],
    }
    assert set_land_object_boundary(
        land_object, valid_geometry, source="egkn:cadastre_boundary"
    ) is True
    previous = (
        land_object.boundary_geojson,
        land_object.boundary_source,
        land_object.boundary_observed_at,
    )
    self_intersecting = {
        "type": "Polygon",
        "coordinates": [
            [
                [76.0, 50.0],
                [76.02, 50.02],
                [76.0, 50.02],
                [76.03, 50.0],
                [76.0, 50.0],
            ]
        ],
    }

    assert set_land_object_boundary(
        land_object, self_intersecting, source="jerler:source_object"
    ) is False
    assert (
        land_object.boundary_geojson,
        land_object.boundary_source,
        land_object.boundary_observed_at,
    ) == previous


def test_boundary_rejects_world_valid_polygon_outside_kazakhstan() -> None:
    land_object = _object()
    geometry = {
        "type": "Polygon",
        "coordinates": [
            [[10.0, 50.0], [10.1, 50.0], [10.1, 50.1], [10.0, 50.0]]
        ],
    }

    assert set_land_object_boundary(
        land_object, geometry, source="jerler:source_object"
    ) is False
    assert land_object.boundary_geojson is None
