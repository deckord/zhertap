from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auction_restriction_context import REQUIRED_RESTRICTION_LAYERS
from app.auction_scenario_rules import evaluate_scenario_rules
from app.auction_spatial_decision_input import (
    MAX_ITEM_BYTES,
    SpatialEvidenceInput,
    assemble_spatial_decision_inputs,
    load_spatial_evidence,
)
from app.db import Base
from app.models import AuctionEvidence, AuctionLot

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
STAMP = "2026-08-17T10:00:00+00:00"


def polygon(x1=76.9, y1=43.2, x2=76.901, y2=43.201):
    return {
        "type": "Polygon",
        "coordinates": [[[x1, y1], [x2, y1], [x2, y2], [x1, y2], [x1, y1]]],
    }


def record(key: str, payload: dict[str, object], evidence_id: int) -> SpatialEvidenceInput:
    return SpatialEvidenceInput(
        key=key,
        evidence_id=evidence_id,
        payload=payload,
        observed_at=NOW,
        source_url=f"https://example.test/{key}",
        status="found",
    )


def metadata(provenance: str):
    return {
        "provenance": provenance,
        "observed_at": STAMP,
        "coverage_complete": True,
        "confidence": 0.9,
    }


def complete_evidence() -> dict[str, SpatialEvidenceInput]:
    parcel = polygon()
    restriction_source = {
        "id": "all",
        "version": "2026",
        "provenance": "official_restrictions",
        "observed_at": STAMP,
        "authoritative": True,
        "coverage": {layer: True for layer in REQUIRED_RESTRICTION_LAYERS},
    }
    planning_sources = [
        {
            "id": "gp",
            "document_type": "genplan",
            "version": "2026",
            "provenance": "official_gp",
            "observed_at": STAMP,
            "authoritative": True,
            "coverage": {"current_zoning": True},
        },
        {
            "id": "pdp",
            "document_type": "pdp",
            "version": "2026",
            "provenance": "official_pdp",
            "observed_at": STAMP,
            "authoritative": True,
            "coverage": {
                "future_zoning": True,
                "planned_roads": True,
                "red_lines": True,
                "engineering_corridors": True,
                "szz": True,
            },
        },
    ]
    services = {
        code: {
            "distance_m": 25,
            "connection_status": "confirmed",
            "capacity_status": "sufficient",
            "cost_min_kzt": 100_000,
            "cost_max_kzt": 200_000,
            "evidence": metadata(f"utility:{code}"),
        }
        for code in ("electricity", "water", "sewer")
    }
    return {
        "parcel": record(
            "parcel",
            {
                "parcel_geojson": parcel,
                "generation_id": "egkn-1",
                "source": {
                    "authoritative": True,
                    "coverage_complete": True,
                    "version": "egkn-2026",
                    "provenance": "official-egkn",
                },
            },
            1,
        ),
        "restrictions": record(
            "restrictions",
            {
                "restriction_sources": [restriction_source],
                "restriction_features": [],
                "expected_layers": list(REQUIRED_RESTRICTION_LAYERS),
                "generation_id": "restrictions-1",
            },
            2,
        ),
        "site": record(
            "site",
            {
                "physical_access": {
                    "connected": True,
                    "road_distance_m": 10,
                    "evidence": metadata("road"),
                },
                "legal_access": {
                    "public_road_access": True,
                    "easement_confirmed": False,
                    "servitude_required": False,
                    "evidence": metadata("legal-access"),
                },
                "infrastructure": {
                    "services": services,
                    "evidence": metadata("utility-inventory"),
                },
                "environment": {
                    "features": [],
                    "coverage": {
                        str(radius): metadata(f"environment:{radius}")
                        for radius in (500, 1000, 3000, 5000)
                    },
                },
                "generation_id": "site-1",
            },
            3,
        ),
        "planning": record(
            "planning",
            {
                "planning_sources": planning_sources,
                "planning_features": [
                    {
                        "kind": "current_zone",
                        "source_id": "gp",
                        "geometry": parcel,
                        "value": "recreation",
                        "allowed_use": True,
                    },
                    {
                        "kind": "future_zone",
                        "source_id": "pdp",
                        "geometry": parcel,
                        "value": "recreation",
                        "allowed_use": True,
                    },
                ],
                "generation_id": "planning-1",
            },
            4,
        ),
        "legal": record(
            "legal",
            {
                "status": "clear",
                "use_allowed": True,
                "right": {
                    "type": "ownership",
                    "transferable": True,
                    "renewable": True,
                    "sublease_allowed": True,
                },
                "provenance_refs": ["legal:passport"],
                "generation_id": "legal-1",
            },
            5,
        ),
    }


def test_complete_authoritative_inputs_produce_persistable_eligible_scenario() -> None:
    result = assemble_spatial_decision_inputs(complete_evidence(), profile="camping", now=NOW)
    assert result.status == "ready"
    assert result.scenario_key == "camping"
    payload = result.as_persistable_dict()
    assert payload["scenario_input"]["planning_context"]["pdp_complete"] is True
    assert payload["scenario_input"]["restriction_context"]["coverage_complete"] is True
    assert payload["geometry_context"]["status"] == "ok"
    analysis = evaluate_scenario_rules(payload["scenario_input"], scenarios=("camping",))
    assert analysis.results[0].status == "eligible"
    assert payload["evidence_generation_ids"]["planning_context"] == "planning-1"


def test_452662_missing_geo_pdp_access_and_contract_remains_requires_check() -> None:
    legal_passport = {
        "version": "legal-passport.v1",
        "generated_at": NOW.isoformat(),
        "facts": {
            "right_type": {"status": "found", "value": "lease"},
            "lease_term_years": {"status": "found", "value": 3},
            "purpose": {"status": "found", "value": "кемпинг"},
            "restrictions": {"status": "unknown", "value": None},
            "arrests": {"status": "unknown", "value": None},
            "encumbrances": {"status": "unknown", "value": None},
        },
    }
    result = assemble_spatial_decision_inputs(
        {}, profile="camping", legal_passport=legal_passport, now=NOW
    )
    scenario = result.decision_inputs["scenario_input"]
    analysis = evaluate_scenario_rules(scenario, scenarios=("camping",))
    assert result.status == "requires_check"
    assert analysis.results[0].status == "requires_check"
    assert scenario["geometry_context"]["status"] == "unknown"
    assert scenario["planning_context"]["pdp_complete"] is False
    assert scenario["site_context"]["legal_access_status"] == "unknown"
    assert "source_unknown:planning_context" in result.stale_reasons


def test_missing_pdp_cannot_be_promoted_to_complete_or_clear() -> None:
    evidence = complete_evidence()
    evidence["planning"] = record(
        "planning",
        {
            "planning_sources": [
                {
                    "id": "gp",
                    "document_type": "genplan",
                    "version": "2026",
                    "provenance": "official_gp",
                    "observed_at": STAMP,
                    "authoritative": True,
                    "coverage": {"current_zoning": True},
                }
            ],
            "planning_features": [
                {
                    "kind": "current_zone",
                    "source_id": "gp",
                    "geometry": polygon(),
                    "allowed_use": True,
                }
            ],
        },
        8,
    )
    result = assemble_spatial_decision_inputs(evidence, profile="camping", now=NOW)
    planning = result.decision_inputs["scenario_input"]["planning_context"]
    assert result.status == "requires_check"
    assert planning["status"] == "partial"
    assert planning["pdp_complete"] is False


def test_parcel_without_authoritative_source_checklist_stays_unknown() -> None:
    evidence = complete_evidence()
    evidence["parcel"] = record(
        "parcel",
        {"parcel_geojson": polygon(), "generation_id": "unverified-map"},
        10,
    )
    result = assemble_spatial_decision_inputs(evidence, profile="camping", now=NOW)
    scenario = result.decision_inputs["scenario_input"]
    assert result.status == "requires_check"
    assert scenario["geometry_context"]["status"] == "unknown"
    assert result.source_freshness["geometry_context"]["status"] == "error"


def test_complete_whole_parcel_restriction_is_critical_but_partial_is_not() -> None:
    complete = complete_evidence()
    complete["restrictions"].payload["restriction_features"] = [
        {
            "layer": "red_lines",
            "source_id": "all",
            "geometry": polygon(),
            "geometry_mode": "area",
            "impact": "blocker",
            "reduces_usable_area": True,
        }
    ]
    complete_result = assemble_spatial_decision_inputs(complete, profile="camping", now=NOW)
    restriction = complete_result.decision_inputs["scenario_input"]["restriction_context"]
    assert restriction["whole_parcel_prohibited"] is True
    assert restriction["critical_blockers"] == ["WHOLE_PARCEL_RESTRICTION"]
    analysis = evaluate_scenario_rules(
        complete_result.decision_inputs["scenario_input"], scenarios=("camping",)
    )
    assert analysis.results[0].status == "blocked"

    partial = complete_evidence()
    partial["restrictions"].payload["restriction_sources"][0]["coverage"] = {"red_lines": True}
    partial["restrictions"].payload["restriction_features"] = complete["restrictions"].payload[
        "restriction_features"
    ]
    partial_result = assemble_spatial_decision_inputs(partial, profile="camping", now=NOW)
    partial_restriction = partial_result.decision_inputs["scenario_input"]["restriction_context"]
    assert partial_restriction["coverage_complete"] is False
    assert partial_restriction["whole_parcel_prohibited"] is None
    assert partial_restriction["critical_blockers"] == []


def test_unknown_profile_selection_is_versioned_and_cannot_turn_green_without_purpose() -> None:
    evidence = complete_evidence()
    evidence["legal"].payload["use_allowed"] = None
    result = assemble_spatial_decision_inputs(evidence, profile="other", now=NOW)
    selection = result.decision_inputs["scenario_selection"]
    assert result.scenario_key == "unclassified"
    assert result.status == "requires_check"
    assert selection["policy_version"] == "spatial-decision-input/2026.2"
    assert selection["selector_version"] == "scenario-selector/2026.2"
    assert selection["status"] == "requires_check"
    assert selection["scenario_key"] is None
    assert selection["purpose_confirmation_required"] is True


def test_stale_or_conflicting_source_is_explicit_not_fresh() -> None:
    evidence = complete_evidence()
    old = evidence["site"]
    evidence["site"] = replace(old, observed_at=NOW - timedelta(days=60))
    conflict = evidence["planning"]
    evidence["planning"] = SpatialEvidenceInput(
        key=conflict.key,
        evidence_id=conflict.evidence_id,
        payload=conflict.payload,
        observed_at=conflict.observed_at,
        source_url=conflict.source_url,
        status="conflict",
    )
    result = assemble_spatial_decision_inputs(evidence, profile="camping", now=NOW)
    assert result.status == "requires_check"
    assert result.source_freshness["site_context"]["status"] == "stale"
    assert result.source_freshness["planning_context"]["status"] == "error"


def test_loader_uses_bounded_projection_and_older_valid_after_oversized() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="452662",
            source_url="https://example.test/452662",
            title="Кемпинг",
        )
        session.add(lot)
        session.flush()
        session.add_all(
            [
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:parcel_geometry_source",
                    status="found",
                    title="older-valid",
                    raw_payload_json=json.dumps({"parcel_geojson": polygon()}),
                    observed_at=NOW - timedelta(minutes=1),
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:parcel_geometry_source",
                    status="found",
                    title="oversized-url",
                    source_url="https://example.test/" + "u" * 1100,
                    raw_payload_json=json.dumps({"parcel_geojson": polygon()}),
                    observed_at=NOW - timedelta(seconds=30),
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:parcel_geometry_source",
                    status="found",
                    title="oversized",
                    raw_payload_json=json.dumps({"blob": "x" * (MAX_ITEM_BYTES + 1000)}),
                    observed_at=NOW,
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:site_source",
                    status="error",
                    title="ignored",
                    raw_payload_json='{"physical_access":{}}',
                    observed_at=NOW,
                ),
            ]
        )
        session.commit()
        loaded = load_spatial_evidence(session, lot.id)
    assert set(loaded) == {"parcel"}
    assert loaded["parcel"].payload["parcel_geojson"]["type"] == "Polygon"


def test_loader_adapts_existing_cadastre_boundary_evidence() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        lot = AuctionLot(
            source="e-qazyna",
            source_lot_id="actual-egkn",
            source_url="https://example.test/actual-egkn",
            title="Участок",
        )
        session.add(lot)
        session.flush()
        session.add(
            AuctionEvidence(
                lot_id=lot.id,
                evidence_type="cadastre_boundary",
                status="found",
                title="ЕГКН",
                source_url="https://map.gov4c.kz/egkn/",
                raw_payload_json=json.dumps(
                    {
                        "source_layer": "egkn:lands",
                        "geometry_geojson": polygon(),
                    }
                ),
                observed_at=NOW,
            )
        )
        session.commit()
        loaded = load_spatial_evidence(session, lot.id)
    assert loaded["parcel"].payload["source"]["authoritative"] is True
    result = assemble_spatial_decision_inputs(loaded, profile="camping", now=NOW)
    assert result.decision_inputs["scenario_input"]["geometry_context"]["status"] == "ok"
    assert result.status == "requires_check"


def test_naive_now_and_oversized_output_are_explicit_errors() -> None:
    naive = assemble_spatial_decision_inputs({}, profile="camping", now=datetime(2026, 8, 17, 12))
    assert naive.status == "error"
    evidence = complete_evidence()
    evidence["parcel"].payload["huge"] = "x" * 600_000
    oversized = assemble_spatial_decision_inputs(evidence, profile="camping", now=NOW)
    assert oversized.status == "error"
    bad_age = assemble_spatial_decision_inputs({}, profile="camping", now=NOW, max_age_days=1.5)
    assert bad_age.status == "error"
    future = complete_evidence()
    future["site"] = replace(future["site"], observed_at=NOW + timedelta(hours=1))
    future_result = assemble_spatial_decision_inputs(future, profile="camping", now=NOW)
    assert future_result.status == "requires_check"
    assert future_result.source_freshness["site_context"]["status"] == "error"
