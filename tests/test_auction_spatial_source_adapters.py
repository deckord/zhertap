from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime

import pytest

from app.auction_planning_context import analyze_planning_context
from app.auction_restriction_context import analyze_restriction_context
from app.auction_site_context import analyze_site_context
from app.auction_spatial_decision_input import (
    SpatialEvidenceInput,
    assemble_spatial_decision_inputs,
)
from app.auction_spatial_source_adapters import (
    SCHEMA_VERSION,
    SpatialSourceAdapterError,
    SpatialTrustedProvider,
    SpatialTrustedReceipt,
    adapt_planning_feed,
    adapt_restriction_feed,
    adapt_site_feed,
    canonical_spatial_authority_hash,
    canonical_spatial_feed_hash,
    parse_spatial_feed_json,
    spatial_provider_registry,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
LOT = "452662"
AUTHORITY = "ГУ Архитектуры и градостроительства области Абай"
DOC_HASH = "a" * 64
RECEIPT_HASH = "b" * 64
BBOX = [75.0, 49.0, 76.0, 50.0]
PARCEL = {
    "type": "Polygon",
    "coordinates": [
        [[75.1, 49.1], [75.2, 49.1], [75.2, 49.2], [75.1, 49.2], [75.1, 49.1]]
    ],
}


def registry():
    return spatial_provider_registry(
        [
            SpatialTrustedProvider(
                provider_id="abay-gis",
                registry_version="abay-gis/2026.1",
                allowed_feed_kinds=("restrictions", "site", "planning"),
                allowed_https_hosts=("gis.gov.kz",),
                authority_or_license_sha256=canonical_spatial_authority_hash(AUTHORITY),
                authority_bbox=(74.0, 48.0, 77.0, 51.0),
                allowed_restriction_layers=(
                    "red_lines",
                    "szz",
                    "power_protection",
                    "water_protection",
                    "flood",
                    "engineering_corridors",
                    "servitudes",
                    "cadastral_restrictions",
                ),
                allowed_planning_layers=(
                    "genplan:current_zoning",
                    "genplan:current_roads",
                    "pdp:future_zoning",
                    "pdp:planned_roads",
                    "pdp:red_lines",
                    "pdp:engineering_corridors",
                    "pdp:szz",
                ),
                allowed_site_coverage=(
                    "physical_access",
                    "legal_access",
                    "utilities",
                    "hazards",
                ),
            )
        ]
    )


def feed(kind: str, payload: dict[str, object], *, status: str = "found") -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "feed_kind": kind,
        "feed_id": f"{kind}-452662-v1",
        "provider_id": "abay-gis",
        "authority_or_license": AUTHORITY,
        "document_sha256": DOC_HASH,
        "receipt_sha256": RECEIPT_HASH,
        "source_url": f"https://gis.gov.kz/feeds/{kind}/452662",
        "target_lot_id": LOT,
        "crs": "EPSG:4326",
        "bbox": BBOX,
        "observed_at": "2026-08-17T10:00:00Z",
        "valid_from": "2026-08-01T00:00:00Z",
        "valid_until": "2026-09-01T00:00:00Z",
        "status": status,
        "payload": payload,
    }


def receipts(value: dict[str, object]):
    key = f"abay-gis:{value['feed_id']}"
    return {
        key: SpatialTrustedReceipt(
            provider_id="abay-gis",
            feed_id=str(value["feed_id"]),
            receipt_sha256=RECEIPT_HASH,
            canonical_feed_sha256=canonical_spatial_feed_hash(value),
            provenance_kind="signed_feed",
        )
    }


def polygon(minx: float, miny: float, maxx: float, maxy: float):
    return {
        "type": "Polygon",
        "coordinates": [
            [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]
        ],
    }


def point(x: float, y: float):
    return {"type": "Point", "coordinates": [x, y]}


def planning_payload(*, include_pdp: bool = False):
    sources: list[dict[str, object]] = [
        {
            "source_id": "genplan-2026",
            "document_type": "genplan",
            "version": "2026-01",
            "coverage": {"current_zoning": True},
        }
    ]
    if include_pdp:
        sources.append(
            {
                "source_id": "pdp-2026",
                "document_type": "pdp",
                "version": "2026-05",
                "coverage": {
                    "future_zoning": True,
                    "planned_roads": True,
                    "red_lines": True,
                    "engineering_corridors": True,
                    "szz": True,
                },
            }
        )
    return {
        "sources": sources,
        "features": [
            {
                "feature_id": "zone-1",
                "kind": "current_zone",
                "source_id": "genplan-2026",
                "geometry": polygon(75.05, 49.05, 75.25, 49.25),
                "value": "Рекреационная зона",
                "allowed_use": True,
            }
        ],
    }


def evidence_meta(coverage_key: str):
    return {"coverage_key": coverage_key}


def site_payload():
    return {
        "coverage": {
            "physical_access": True,
            "legal_access": False,
            "utilities": False,
            "hazards": False,
        },
        "features": [
            {
                "feature_id": "road-1",
                "kind": "road",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[75.1, 49.05], [75.1, 49.25]],
                },
            },
            {"feature_id": "power-1", "kind": "utility", "geometry": point(75.15, 49.15)},
            {"feature_id": "landfill-1", "kind": "hazard", "geometry": point(75.16, 49.16)},
        ],
        "physical_access": {
            "feature_id": "road-1",
            "connected": True,
            "road_distance_m": 0,
            "surface": "unpaved",
            "evidence": evidence_meta("physical_access"),
        },
        "legal_access": {
            "feature_ids": ["road-1"],
            "public_road_access": None,
            "easement_confirmed": None,
            "servitude_required": None,
            "evidence": evidence_meta("legal_access"),
        },
        "infrastructure": {
            "services": {
                "electricity": {
                    "feature_id": "power-1",
                    "distance_m": 20,
                    "connection_status": "unknown",
                    "capacity_status": "unknown",
                    "cost_min_kzt": None,
                    "cost_max_kzt": None,
                    "evidence": evidence_meta("utilities"),
                }
            },
            "evidence": evidence_meta("utilities"),
        },
        "environment": {
            "features": [
                {
                    "feature_id": "landfill-1",
                    "category": "landfill",
                    "name": "Полигон",
                    "distance_m": 800,
                }
            ],
            "coverage": {
                str(radius): evidence_meta("hazards") for radius in (500, 1000, 3000, 5000)
            },
        },
    }


def test_receipt_binds_all_decision_fields_and_registry_blocks_host_forgery() -> None:
    value = feed("planning", planning_payload())
    trusted = receipts(value)
    mutated = copy.deepcopy(value)
    mutated["bbox"] = [74.0, 48.0, 76.0, 50.0]
    result = adapt_planning_feed(
        mutated,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=trusted,
        now=NOW,
    )
    assert result.envelope is None
    assert result.issues[0].code == "trusted_receipt_mismatch"

    hostile = copy.deepcopy(value)
    hostile["source_url"] = "https://evil.example/official.json"
    result = adapt_planning_feed(
        hostile,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(hostile),
        now=NOW,
    )
    assert result.issues[0].code == "provider_registry_mismatch"


def test_duplicate_json_keys_and_wrong_crs_bbox_or_expiry_fail_closed() -> None:
    with pytest.raises(SpatialSourceAdapterError, match="duplicate"):
        parse_spatial_feed_json('{"payload":{"x":1,"x":2}}')

    for key, invalid, code in (
        ("crs", "EPSG:3857", "applicability_mismatch"),
        ("bbox", [0, 0, 1, 1], "invalid_validity_or_bbox"),
        ("valid_until", "2026-08-16T00:00:00Z", "expired_feed"),
    ):
        value = feed("planning", planning_payload())
        value[key] = invalid
        if key == "valid_until":
            value["observed_at"] = "2026-08-15T00:00:00Z"
        result = adapt_planning_feed(
            value,
            expected_lot_id=LOT,
            parcel_geojson=PARCEL,
            registry=registry(),
            receipts=receipts(value),
            now=NOW,
        )
        assert result.envelope is None
        assert result.issues[0].code == code


def test_missing_restriction_coverage_is_unknown_not_false_clear() -> None:
    value = feed(
        "restrictions",
        {"coverage": {"red_lines": True}, "features": [], "source_version": "2026.1"},
    )
    result = adapt_restriction_feed(
        value,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert result.envelope is not None
    analysis = analyze_restriction_context(
        PARCEL,
        restriction_sources=result.envelope.payload["restriction_sources"],
        restriction_features=result.envelope.payload["restriction_features"],
    )
    assert analysis.status == "partial"
    assert analysis.usable_area_m2 is None


def test_restriction_overlap_reduces_usable_but_boundary_touch_does_not() -> None:
    coverage = {
        layer: True
        for layer in (
            "red_lines",
            "szz",
            "power_protection",
            "water_protection",
            "flood",
            "engineering_corridors",
            "servitudes",
            "cadastral_restrictions",
        )
    }
    features = [
        {
            "feature_id": "lep-overlap",
            "layer": "power_protection",
            "geometry": polygon(75.1, 49.1, 75.15, 49.2),
            "geometry_mode": "area",
            "impact": "warning",
            "reduces_usable_area": True,
            "value": "Охранная зона ЛЭП",
        },
        {
            "feature_id": "red-touch",
            "layer": "red_lines",
            "geometry": {
                "type": "LineString",
                "coordinates": [[75.0, 49.1], [75.1, 49.1]],
            },
            "geometry_mode": "line_fact",
            "impact": "warning",
            "reduces_usable_area": False,
            "value": "Красная линия",
        },
    ]
    value = feed(
        "restrictions",
        {"coverage": coverage, "features": features, "source_version": "2026.1"},
    )
    result = adapt_restriction_feed(
        value,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert result.envelope is not None
    analysis = analyze_restriction_context(
        PARCEL,
        restriction_sources=result.envelope.payload["restriction_sources"],
        restriction_features=result.envelope.payload["restriction_features"],
    )
    relations = {item.restriction_id: item for item in analysis.facts}
    assert relations["lep-overlap"].intersects is True
    assert relations["lep-overlap"].reduces_usable_area is True
    assert relations["red-touch"].touches_only is True
    assert relations["red-touch"].reduces_usable_area is False


def test_452662_missing_pdp_remains_partial_and_requires_check() -> None:
    value = feed("planning", planning_payload(include_pdp=False))
    result = adapt_planning_feed(
        value,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert result.envelope is not None
    analysis = analyze_planning_context(
        PARCEL,
        planning_sources=result.envelope.payload["planning_sources"],
        planning_features=result.envelope.payload["planning_features"],
    )
    assert analysis.status == "partial"
    assert any(item.document_type == "pdp" and not item.complete for item in analysis.coverage)

    parcel = SpatialEvidenceInput(
        key="parcel",
        evidence_id=1,
        payload={
            "parcel_geojson": PARCEL,
            "generation_id": "parcel-v1",
            "source": {
                "authoritative": True,
                "coverage_complete": True,
                "version": "egkn-v1",
                "provenance": "EGKN signed boundary",
            },
        },
        observed_at=NOW,
        source_url="https://map.gov.kz/parcel/452662",
        status="found",
    )
    planning = SpatialEvidenceInput(
        key="planning",
        evidence_id=2,
        payload=result.envelope.payload,
        observed_at=result.envelope.observed_at,
        source_url=result.envelope.source_url,
        status="found",
    )
    assembled = assemble_spatial_decision_inputs(
        {"parcel": parcel, "planning": planning},
        profile="camping",
        now=NOW,
    )
    assert assembled.status == "requires_check"
    scenario = assembled.decision_inputs["scenario_input"]
    assert scenario["planning_context"]["pdp_complete"] is False


def test_planning_current_and_future_conflict_is_preserved() -> None:
    payload = planning_payload(include_pdp=True)
    payload["features"].append(
        {
            "feature_id": "road-future",
            "kind": "planned_road",
            "source_id": "pdp-2026",
            "geometry": polygon(75.12, 49.12, 75.18, 49.18),
            "value": "Перспективная дорога",
            "allowed_use": None,
        }
    )
    value = feed("planning", payload)
    result = adapt_planning_feed(
        value,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert result.envelope is not None
    analysis = analyze_planning_context(
        PARCEL,
        planning_sources=result.envelope.payload["planning_sources"],
        planning_features=result.envelope.payload["planning_features"],
    )
    assert analysis.status == "conflict"
    assert any(
        item.kind == "planned_road" and item.intersects for item in analysis.future_relations
    )


def test_provider_cannot_self_authorize_pdp_and_statutory_planning_can_have_no_expiry() -> None:
    limited = spatial_provider_registry(
        [
            SpatialTrustedProvider(
                provider_id="abay-gis",
                registry_version="abay-gis/2026.1",
                allowed_feed_kinds=("planning",),
                allowed_https_hosts=("gis.gov.kz",),
                authority_or_license_sha256=canonical_spatial_authority_hash(AUTHORITY),
                authority_bbox=(74.0, 48.0, 77.0, 51.0),
                allowed_planning_layers=("genplan:current_zoning",),
            )
        ]
    )
    unauthorized = feed("planning", planning_payload(include_pdp=True))
    rejected = adapt_planning_feed(
        unauthorized,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=limited,
        receipts=receipts(unauthorized),
        now=NOW,
    )
    assert rejected.envelope is None
    assert rejected.issues[0].code == "coverage_invalid"

    statutory = feed("planning", planning_payload())
    statutory["valid_until"] = None
    accepted = adapt_planning_feed(
        statutory,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(statutory),
        now=NOW,
    )
    assert accepted.envelope is not None

    malformed_expiry = feed("planning", planning_payload())
    malformed_expiry["valid_until"] = "not-a-date"
    malformed = adapt_planning_feed(
        malformed_expiry,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(malformed_expiry),
        now=NOW,
    )
    assert malformed.envelope is None
    assert malformed.issues[0].code == "invalid_validity_or_bbox"


def test_site_access_utilities_and_hazards_stay_independent() -> None:
    value = feed("site", site_payload())
    result = adapt_site_feed(
        value,
        expected_lot_id=LOT,
        profile="camping",
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert result.envelope is not None
    payload = result.envelope.payload
    analysis = analyze_site_context(
        "camping",
        physical_access=payload["physical_access"],
        legal_access=payload["legal_access"],
        infrastructure=payload["infrastructure"],
        environment=payload["environment"],
    )
    assert analysis.physical_access.status == "ready"
    assert analysis.legal_access.status == "unknown"
    assert analysis.infrastructure.status == "attention"
    assert analysis.environment.status == "blocked"
    assert any("distance alone proves nothing" in item for item in analysis.infrastructure.warnings)
    assert payload["infrastructure"]["services"]["electricity"]["distance_m"] == 0
    assert payload["environment"]["features"][0]["distance_m"] == 0


def test_site_connected_claim_must_touch_parcel_and_conflict_status_is_preserved() -> None:
    payload = site_payload()
    payload["features"][0]["geometry"] = {
        "type": "LineString",
        "coordinates": [[75.5, 49.5], [75.6, 49.6]],
    }
    value = feed("site", payload)
    rejected = adapt_site_feed(
        value,
        expected_lot_id=LOT,
        profile="camping",
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(value),
        now=NOW,
    )
    assert rejected.issues[0].code == "connection_geometry_conflict"

    legal_payload = site_payload()
    legal_payload["physical_access"]["connected"] = False
    legal_payload["legal_access"]["public_road_access"] = True
    legal_payload["features"][0]["geometry"] = {
        "type": "LineString",
        "coordinates": [[75.5, 49.5], [75.6, 49.6]],
    }
    legal_value = feed("site", legal_payload)
    legal_rejected = adapt_site_feed(
        legal_value,
        expected_lot_id=LOT,
        profile="camping",
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(legal_value),
        now=NOW,
    )
    assert legal_rejected.issues[0].code == "legal_access_geometry_conflict"

    conflict_value = feed("planning", planning_payload(), status="conflict")
    conflict = adapt_planning_feed(
        conflict_value,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(conflict_value),
        now=NOW,
    )
    assert conflict.envelope is not None
    assert conflict.envelope.status == "conflict"


def test_generation_changes_when_signed_feed_changes_with_same_document_hash() -> None:
    first = feed("planning", planning_payload())
    second = copy.deepcopy(first)
    second["payload"]["sources"][0]["version"] = "2026-02"
    first_result = adapt_planning_feed(
        first,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(first),
        now=NOW,
    )
    second_result = adapt_planning_feed(
        second,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts=receipts(second),
        now=NOW,
    )
    assert first_result.envelope is not None
    assert second_result.envelope is not None
    assert first_result.envelope.generation_id != second_result.envelope.generation_id


def test_geometry_and_payload_bounds_reject_nonfinite_or_oversized() -> None:
    payload = planning_payload()
    payload["features"][0]["geometry"] = point(float("nan"), 49.1)
    value = feed("planning", payload)
    with pytest.raises(SpatialSourceAdapterError, match="canonical JSON"):
        canonical_spatial_feed_hash(value)

    digest = hashlib.sha256(b"x").hexdigest()
    assert len(digest) == 64

    malformed = feed("planning", planning_payload())
    malformed["authority_or_license"] = "x" * 10_000
    result = adapt_planning_feed(
        malformed,
        expected_lot_id=LOT,
        parcel_geojson=PARCEL,
        registry=registry(),
        receipts={},
        now=NOW,
    )
    assert result.envelope is None
    assert result.issues[0].code == "provider_registry_mismatch"
