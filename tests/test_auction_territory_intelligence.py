from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.auction_territory_intelligence import (
    TerritoryIntelligenceError,
    assess_geographic_applicability,
    assess_parcel_geographic_applicability,
    normalize_territory_observation,
    territory_identity_key,
    transition_decision,
)

NOW = datetime(2026, 9, 1, 6, tzinfo=UTC)


def _event(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider_id": "official-project-registry",
        "source_record_id": "project-42",
        "source_revision": 1,
        "record_kind": "event",
        "authority_name": "Акимат области",
        "source_url": "https://gov.kz/project/42",
        "source_published_at": datetime(2026, 8, 31, 9, tzinfo=UTC),
        "observed_at": NOW,
        "territory_code": "KZ-10",
        "geometry_geojson": {"type": "Point", "coordinates": [71.45, 51.13]},
        "event": {
            "event_key": "road-42",
            "event_code": "road_opened",
            "direction": "positive",
            "direction_basis": "official_field",
            "lifecycle_state": "completed",
            "event_date": date(2026, 8, 30),
        },
    }
    payload.update(changes)
    return payload


def test_event_requires_structured_code_and_never_classifies_label() -> None:
    event = normalize_territory_observation(_event())
    assert event.event is not None
    assert event.event.event_code == "road_opened"
    broken = _event()
    broken["event"] = {
        "event_key": "road-42",
        "event_code": "great_new_road",
        "direction": "positive",
        "direction_basis": "official_field",
        "lifecycle_state": "completed",
        "label": "Открыта новая дорога",
    }
    with pytest.raises(TerritoryIntelligenceError, match="unsupported_event_code"):
        normalize_territory_observation(broken)


def test_missing_geometry_is_preserved_unlinked_not_text_matched() -> None:
    event = normalize_territory_observation(_event(geometry_geojson=None))
    assert event.geometry_geojson is None
    assert event.linkage_eligible is False


def test_source_authority_revision_and_timestamp_fail_closed() -> None:
    for changes, reason in (
        ({"source_url": "http://gov.kz/project/42"}, "official_https_source_required"),
        ({"authority_name": ""}, "invalid_authority"),
        ({"source_revision": 0}, "invalid_source_revision"),
        ({"observed_at": NOW.replace(tzinfo=None)}, "aware_observed_at_required"),
    ):
        with pytest.raises(TerritoryIntelligenceError, match=reason):
            normalize_territory_observation(_event(**changes))


def test_geometry_rejects_invalid_bounds_type_and_oversized_payload() -> None:
    for geometry in (
        {"type": "Point", "coordinates": [120, 51]},
        {"type": "GeometryCollection", "geometries": []},
        {"type": "Point", "coordinates": [71.45, 51.13], "padding": "x" * 300_000},
    ):
        with pytest.raises(TerritoryIntelligenceError, match="invalid_geometry"):
            normalize_territory_observation(_event(geometry_geojson=geometry))


def test_demographic_zero_is_preserved_and_missing_is_not_zero() -> None:
    payload = _event(
        record_kind="demographic",
        geometry_geojson=None,
        event=None,
        demographic={
            "indicator_code": "migration_balance",
            "period_start": date(2025, 1, 1),
            "period_end": date(2025, 12, 31),
            "value": 0,
            "unit": "persons",
        },
    )
    observation = normalize_territory_observation(payload)
    assert observation.demographic is not None
    assert observation.demographic.value == 0
    payload["demographic"] = {**payload["demographic"], "value": None}
    with pytest.raises(TerritoryIntelligenceError, match="invalid_demographic_value"):
        normalize_territory_observation(payload)


def test_identity_and_lifecycle_are_revision_safe() -> None:
    first = normalize_territory_observation(_event())
    retry = normalize_territory_observation(_event(source_revision=2))
    assert territory_identity_key(first) == territory_identity_key(retry)
    assert (
        transition_decision("announced", "completed", current_revision=1, next_revision=2)
        == "advance"
    )
    assert (
        transition_decision("completed", "in_progress", current_revision=2, next_revision=3)
        == "conflict"
    )
    assert (
        transition_decision(
            "completed",
            "in_progress",
            current_revision=2,
            next_revision=3,
            correction_of_revision=2,
        )
        == "correction"
    )
    assert (
        transition_decision("approved", "completed", current_revision=3, next_revision=2) == "stale"
    )


def test_official_event_requires_publication_and_event_dates() -> None:
    observation = normalize_territory_observation(_event())
    assert observation.source_published_at == datetime(2026, 8, 31, 9, tzinfo=UTC)
    assert observation.event is not None
    assert observation.event.event_date == date(2026, 8, 30)

    with pytest.raises(TerritoryIntelligenceError, match="aware_source_published_at_required"):
        normalize_territory_observation(_event(source_published_at=None))
    with pytest.raises(TerritoryIntelligenceError, match="source_published_after_observation"):
        normalize_territory_observation(
            _event(source_published_at=datetime(2026, 9, 2, tzinfo=UTC))
        )
    payload = _event()
    payload["event"] = {
        key: value for key, value in payload["event"].items() if key != "event_date"
    }
    with pytest.raises(TerritoryIntelligenceError, match="event_date_required"):
        normalize_territory_observation(payload)


def test_polygon_can_prove_parcel_applicability_but_point_cannot() -> None:
    polygon = normalize_territory_observation(
        _event(
            geometry_geojson={
                "type": "Polygon",
                "coordinates": [
                    [[71.4, 51.1], [71.5, 51.1], [71.5, 51.2], [71.4, 51.2], [71.4, 51.1]]
                ],
            }
        )
    )
    inside = assess_geographic_applicability(
        polygon, parcel_longitude=71.45, parcel_latitude=51.13, parcel_territory_code="KZ-10"
    )
    outside = assess_geographic_applicability(
        polygon, parcel_longitude=72.0, parcel_latitude=51.13, parcel_territory_code="KZ-10"
    )
    point = assess_geographic_applicability(
        normalize_territory_observation(_event()),
        parcel_longitude=71.45,
        parcel_latitude=51.13,
        parcel_territory_code="KZ-10",
    )
    assert (inside.status, inside.scope, inside.basis) == (
        "applicable", "parcel", "polygon_contains_parcel"
    )
    assert (outside.status, outside.scope, outside.basis) == (
        "not_applicable", "parcel", "polygon_excludes_parcel"
    )
    assert (point.status, point.scope, point.basis) == (
        "manual_required", "territory", "territory_code_match"
    )


def test_geographic_applicability_never_uses_locality_prose() -> None:
    observation = normalize_territory_observation(_event(geometry_geojson=None))
    mismatch = assess_geographic_applicability(
        observation,
        parcel_longitude=71.45,
        parcel_latitude=51.13,
        parcel_territory_code="KZ-11",
    )
    unknown = assess_geographic_applicability(
        observation,
        parcel_longitude=71.45,
        parcel_latitude=51.13,
        parcel_territory_code=None,
    )
    assert (mismatch.status, mismatch.basis) == ("not_applicable", "territory_code_mismatch")
    assert (unknown.status, unknown.basis) == ("manual_required", "insufficient_official_scope")


def test_whole_parcel_applicability_distinguishes_cover_overlap_and_exclusion() -> None:
    observation = normalize_territory_observation(
        _event(
            geometry_geojson={
                "type": "Polygon",
                "coordinates": [
                    [[71.4, 51.1], [71.5, 51.1], [71.5, 51.2], [71.4, 51.2], [71.4, 51.1]]
                ],
            }
        )
    )
    covered = {
        "type": "Polygon",
        "coordinates": [
            [[71.42, 51.12], [71.44, 51.12], [71.44, 51.14], [71.42, 51.14], [71.42, 51.12]]
        ],
    }
    overlap = {
        "type": "Polygon",
        "coordinates": [
            [[71.49, 51.12], [71.51, 51.12], [71.51, 51.14], [71.49, 51.14], [71.49, 51.12]]
        ],
    }
    excluded = {
        "type": "Polygon",
        "coordinates": [
            [[71.6, 51.12], [71.61, 51.12], [71.61, 51.14], [71.6, 51.14], [71.6, 51.12]]
        ],
    }
    inside = assess_parcel_geographic_applicability(
        observation, parcel_geojson=covered, parcel_territory_code="KZ-10"
    )
    assert (inside.status, inside.scope, inside.basis, inside.overlap_ratio) == (
        "applicable", "parcel", "scope_polygon_covers_parcel", 1.0
    )
    partial = assess_parcel_geographic_applicability(
        observation, parcel_geojson=overlap, parcel_territory_code="KZ-10"
    )
    assert (partial.status, partial.basis) == (
        "manual_required", "scope_polygon_intersects_parcel"
    )
    assert partial.overlap_ratio == pytest.approx(0.5)
    outside = assess_parcel_geographic_applicability(
        observation, parcel_geojson=excluded, parcel_territory_code="KZ-10"
    )
    assert (outside.status, outside.basis, outside.overlap_ratio) == (
        "not_applicable", "scope_polygon_excludes_parcel", 0.0
    )


def test_point_or_missing_parcel_never_becomes_applicable_from_centroid() -> None:
    point = assess_parcel_geographic_applicability(
        normalize_territory_observation(_event()),
        parcel_geojson=None,
        parcel_territory_code="KZ-10",
    )
    polygon_without_parcel = normalize_territory_observation(
        _event(
            geometry_geojson={
                "type": "Polygon",
                "coordinates": [
                    [[71.4, 51.1], [71.5, 51.1], [71.5, 51.2], [71.4, 51.2], [71.4, 51.1]]
                ],
            }
        )
    )
    missing = assess_parcel_geographic_applicability(
        polygon_without_parcel, parcel_geojson=None, parcel_territory_code="KZ-10"
    )
    assert (point.status, point.basis) == ("manual_required", "territory_code_match")
    assert (missing.status, missing.basis) == (
        "manual_required", "insufficient_official_scope"
    )
