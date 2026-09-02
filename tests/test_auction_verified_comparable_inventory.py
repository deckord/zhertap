from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.auction_verified_comparable_inventory import (
    MAX_SCAN_ROWS,
    ComparableInventoryError,
    build_geo_selection_plan,
    normalize_inventory_fact,
    select_nearby_verified_sales,
    source_idempotency_key,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _payload(index: int, **changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "sequence_id": index,
        "source_name": "Official Registry",
        "source_record_id": f"record-{index}",
        "source_sale_id": f"sale-{index}",
        "source_listing_id": None,
        "source_url": f"https://registry.example/sales/{index}",
        "object_id": f"parcel-{index}",
        "fact_status": "found",
        "price_kind": "verified_sale",
        "verification_status": "verified",
        "verification_ref": f"sale-contract:{index}",
        "right_type": "lease",
        "purpose_group": "camping",
        "lease_term_years": 3,
        "area_ha": 1.0,
        "price_kzt": 10_000_000 + index,
        "latitude": 50.4111,
        "longitude": 80.2275,
        "access_readiness": "ready",
        "infrastructure_readiness": "partial",
        "event_at": NOW - timedelta(days=10),
        "observed_at": NOW - timedelta(hours=index),
        "title": f"Verified sale {index}",
        "locality": "Семей",
        "provenance_refs": [f"registry-document:{index}"],
        "conflict_fields": [],
    }
    payload.update(changes)
    return payload


def _select(facts, **changes):
    arguments = {
        "right_type": "lease",
        "purpose_group": "camping",
        "area_ha": 1.0,
        "valuation_at": NOW,
        "lease_term_years": 3.0,
    }
    arguments.update(changes)
    return select_nearby_verified_sales(50.4111, 80.2275, facts, **arguments)


def test_normalized_sale_requires_provider_sale_id_and_verification() -> None:
    with pytest.raises(ComparableInventoryError, match="sale_identity_missing"):
        normalize_inventory_fact(_payload(1, source_sale_id=None))
    with pytest.raises(ComparableInventoryError, match="sale_not_verified"):
        normalize_inventory_fact(_payload(1, verification_status="claimed"))
    fact = normalize_inventory_fact(_payload(1))
    assert source_idempotency_key(fact).startswith("sha256:")


def test_listing_is_retained_as_listing_but_never_selected_as_sale() -> None:
    listing = normalize_inventory_fact(
        _payload(
            1,
            price_kind="listing",
            source_sale_id=None,
            source_listing_id="listing-1",
            verification_status="verified",
        )
    )
    result = _select([listing])
    assert result.status == "insufficient"
    assert result.selected == ()
    assert result.rejected[0].reason == "listing_not_sale"


def test_bbox_prefilter_is_followed_by_exact_five_km_haversine() -> None:
    inside = normalize_inventory_fact(_payload(1, latitude=50.4111, longitude=80.27))
    corner = normalize_inventory_fact(_payload(2, latitude=50.451, longitude=80.29))
    plan = build_geo_selection_plan(
        50.4111,
        80.2275,
        right_type="lease",
        purpose_group="camping",
        area_ha=1,
        valuation_at=NOW,
        lease_term_years=3,
    )
    assert plan.bbox.latitude_min < corner.latitude < plan.bbox.latitude_max
    assert plan.bbox.longitude_min < corner.longitude < plan.bbox.longitude_max
    result = _select([inside, corner])
    assert [item.fact.sequence_id for item in result.selected] == [1]
    assert any(
        item.sequence_id == 2 and item.reason == "outside_radius"
        for item in result.rejected
    )
    assert result.selected[0].distance_km <= 5


def test_newest_conflict_blocks_older_verified_sale_with_same_source_key() -> None:
    older = normalize_inventory_fact(
        _payload(1, source_sale_id="same", observed_at=NOW - timedelta(days=2))
    )
    conflict = normalize_inventory_fact(
        _payload(
            2,
            source_sale_id="same",
            fact_status="conflict",
            observed_at=NOW - timedelta(days=1),
            conflict_fields=["price_kzt"],
        )
    )
    result = _select([older, conflict])
    assert result.selected == ()
    assert result.rejected[0].reason == "newest_conflict"


def test_right_purpose_lease_access_and_infrastructure_survive_conversion() -> None:
    fact = normalize_inventory_fact(_payload(1))
    result = _select([fact])
    candidate = result.selected[0].candidate
    assert candidate.right_type == "lease"
    assert candidate.purpose_group == "camping"
    assert candidate.lease_term_years == 3
    assert candidate.access_readiness == "ready"
    assert candidate.infrastructure_readiness == "partial"
    assert candidate.observed_at == fact.event_at


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"latitude": float("nan")}, "coordinates_outside_kazakhstan"),
        ({"longitude": 90.0}, "coordinates_outside_kazakhstan"),
        ({"observed_at": NOW.replace(tzinfo=None)}, "timestamps_must_be_timezone_aware"),
        ({"title": "x" * 321}, "invalid_or_unbounded_text"),
        ({"lease_term_years": None}, "lease_term_missing"),
    ),
)
def test_malformed_or_unbounded_facts_are_explicit(changes, reason) -> None:
    with pytest.raises(ComparableInventoryError, match=reason):
        normalize_inventory_fact(_payload(1, **changes))


def test_scan_is_bounded_and_keyset_cursor_is_deterministic() -> None:
    facts = [
        normalize_inventory_fact(
            _payload(
                index,
                latitude=50.4111,
                longitude=80.2275,
                observed_at=NOW - timedelta(seconds=index),
            )
        )
        for index in range(1, MAX_SCAN_ROWS + 1)
    ]
    first = _select(facts, result_limit=3)
    second = _select(list(reversed(facts)), result_limit=3)
    assert first.input_generation_hash == second.input_generation_hash
    assert first.next_cursor == second.next_cursor
    assert first.next_cursor is None
    assert len(first.selected) == 3
    with pytest.raises(ComparableInventoryError, match="input_inventory_exceeds_bound"):
        _select([facts[0]] * 5_001)


def test_radius_and_query_coordinates_are_kazakhstan_bounded() -> None:
    with pytest.raises(ComparableInventoryError, match="invalid_geo_query"):
        build_geo_selection_plan(
            50.0,
            80.0,
            right_type="lease",
            purpose_group="camping",
            area_ha=1,
            valuation_at=NOW,
            lease_term_years=3,
            radius_km=5.1,
        )
    with pytest.raises(ComparableInventoryError, match="invalid_geo_query"):
        build_geo_selection_plan(
            39.9,
            80.0,
            right_type="lease",
            purpose_group="camping",
            area_ha=1,
            valuation_at=NOW,
            lease_term_years=3,
        )


def test_unicode_identity_is_collision_safe() -> None:
    kazakh = normalize_inventory_fact(_payload(1, source_name="Қазына", source_sale_id="Сату-1"))
    russian = normalize_inventory_fact(_payload(2, source_name="Казына", source_sale_id="Сату-1"))
    assert source_idempotency_key(kazakh) != source_idempotency_key(russian)


def test_verified_sale_requires_event_date_and_old_sale_is_filtered_by_event() -> None:
    with pytest.raises(ComparableInventoryError, match="sale_event_at_required"):
        normalize_inventory_fact(_payload(1, event_at=None))
    old_sale = normalize_inventory_fact(
        _payload(2, event_at=NOW - timedelta(days=500), observed_at=NOW)
    )
    result = _select([old_sale])
    assert result.selected == ()
    assert result.rejected[0].reason == "target_prefilter_mismatch"


def test_geo_selection_excludes_previous_calendar_year_within_rolling_window() -> None:
    valuation_at = datetime(2026, 1, 15, 12, tzinfo=UTC)
    previous_year = normalize_inventory_fact(
        _payload(
            1,
            event_at=datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
            observed_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
        )
    )
    current_year = normalize_inventory_fact(
        _payload(
            2,
            event_at=datetime(2026, 1, 2, 12, tzinfo=UTC),
            observed_at=datetime(2026, 1, 3, 12, tzinfo=UTC),
        )
    )

    result = _select([previous_year, current_year], valuation_at=valuation_at)

    assert result.plan.event_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert [item.fact.sequence_id for item in result.selected] == [2]
    assert any(
        item.sequence_id == 1 and item.reason == "target_prefilter_mismatch"
        for item in result.rejected
    )


def test_found_with_conflicts_is_not_selected() -> None:
    fact = normalize_inventory_fact(_payload(1, conflict_fields=["price_kzt"]))
    result = _select([fact])
    assert result.selected == ()
    assert result.rejected[0].reason == "found_has_conflicts"


def test_minimal_conflict_tombstone_blocks_older_complete_sale() -> None:
    older = normalize_inventory_fact(
        _payload(1, source_sale_id="same", observed_at=NOW - timedelta(days=2))
    )
    tombstone = normalize_inventory_fact(
        {
            "sequence_id": 2,
            "source_name": "Official Registry",
            "source_record_id": "conflict-2",
            "source_sale_id": "same",
            "source_listing_id": None,
            "fact_status": "conflict",
            "price_kind": "verified_sale",
            "observed_at": NOW - timedelta(days=1),
            "provenance_refs": ["provider-response:conflict-2"],
            "conflict_fields": ["price_kzt"],
        }
    )
    result = _select([older, tombstone])
    assert result.selected == ()
    assert result.rejected[0].reason == "newest_conflict"


def test_generation_hash_changes_for_decision_material_changes() -> None:
    original = normalize_inventory_fact(_payload(1))
    changed = normalize_inventory_fact(_payload(1, price_kzt=11_000_000))
    first = _select([original])
    second = _select([changed])
    assert first.input_generation_hash != second.input_generation_hash


def test_target_prefilters_prevent_dense_irrelevant_inventory_from_starving_matches() -> None:
    irrelevant = [
        normalize_inventory_fact(
            _payload(
                index,
                purpose_group="warehouse",
                observed_at=NOW - timedelta(seconds=index),
            )
        )
        for index in range(1, 602)
    ]
    relevant = [
        normalize_inventory_fact(
            _payload(
                700 + index,
                observed_at=NOW - timedelta(days=2, seconds=index),
            )
        )
        for index in range(3)
    ]
    result = _select([*irrelevant, *relevant])
    assert len(result.selected) == 3
    assert {item.fact.sequence_id for item in result.selected} == {700, 701, 702}
    assert "latest-per-source-key first" in result.plan.sql_contract
    assert "verification_status=verified" in result.plan.sql_contract


@pytest.mark.parametrize(
    ("target_years", "valid_years", "invalid_years"),
    (
        (3.0, 1.0, 3.01),
        (10.0, 4.0, 3.0),
        (11.0, 20.0, 10.0),
    ),
)
def test_lease_prefilter_exactly_matches_w9_categorical_bands(
    target_years: float, valid_years: float, invalid_years: float
) -> None:
    valid = normalize_inventory_fact(_payload(1, lease_term_years=valid_years))
    invalid = normalize_inventory_fact(_payload(2, lease_term_years=invalid_years))
    result = _select([valid, invalid], lease_term_years=target_years)
    assert [item.fact.sequence_id for item in result.selected] == [1]
    assert any(
        item.sequence_id == 2 and item.reason == "target_prefilter_mismatch"
        for item in result.rejected
    )
