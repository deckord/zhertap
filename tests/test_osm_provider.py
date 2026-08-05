import pytest

from app.providers.osm import (
    OsmProvider,
    OsmProviderError,
    build_point_query,
    feature_distance_m,
    surroundings_from_payload,
)


def test_feature_distance_uses_full_road_geometry() -> None:
    distance = feature_distance_m(
        52.0,
        70.0,
        {
            "geometry": [
                {"lat": 52.0001, "lon": 69.999},
                {"lat": 52.0001, "lon": 70.001},
            ]
        },
    )

    assert distance == pytest.approx(11.13, abs=0.2)


def test_feature_distance_is_zero_inside_mapped_object() -> None:
    distance = feature_distance_m(
        52.0,
        70.0,
        {
            "geometry": [
                {"lat": 51.9999, "lon": 69.9999},
                {"lat": 51.9999, "lon": 70.0001},
                {"lat": 52.0001, "lon": 70.0001},
                {"lat": 52.0001, "lon": 69.9999},
                {"lat": 51.9999, "lon": 69.9999},
            ]
        },
    )

    assert distance == 0


def test_point_query_limits_heavy_objects_to_candidate_neighborhood() -> None:
    query = build_point_query([(52.8, 70.7)], radius_m=2000)

    assert "around:2000,52.8000000,70.7000000" in query
    assert "around:300,52.8000000,70.7000000)[highway]" in query
    assert "around:500,52.8000000,70.7000000)[waterway]" in query
    assert "around:500,52.8000000,70.7000000)[natural=water]" in query
    assert "around:100,52.8000000,70.7000000);" in query
    assert "bbox" not in query


def test_surroundings_marks_river_as_open_water() -> None:
    rows = surroundings_from_payload(
        [(52.0, 70.0)],
        {
            "elements": [
                {
                    "type": "way",
                    "id": 1,
                    "tags": {"waterway": "river"},
                    "geometry": [
                        {"lat": 52.0001, "lon": 69.999},
                        {"lat": 52.0001, "lon": 70.001},
                    ],
                }
            ]
        },
        radius_m=500,
    )

    assert rows[0].open_water_distance_m == pytest.approx(11.13, abs=0.2)


def test_analyze_points_marks_each_successful_batch_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OsmProvider()
    calls = 0

    def empty_response(query: str, *, deadline: float | None = None) -> dict:
        nonlocal calls
        calls += 1
        return {"elements": []}

    monkeypatch.setattr(provider, "_request", empty_response)
    rows = provider.analyze_points([(52.8, 70.7)] * 9)

    assert calls == 2
    assert all(row.checked for row in rows)


def test_analyze_points_continues_unchecked_when_osm_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OsmProvider()

    def unavailable(query: str, *, deadline: float | None = None) -> dict:
        raise OsmProviderError("temporary failure")

    monkeypatch.setattr(provider, "_request", unavailable)
    rows = provider.analyze_points([(52.8, 70.7)])

    assert len(rows) == 1
    assert rows[0].checked is False
    return

    with pytest.raises(OsmProviderError, match="координаты не выдаются"):
        provider.analyze_points([(52.8, 70.7)])
