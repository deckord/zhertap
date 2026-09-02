from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from app.auction_decision_snapshot import (
    DECISION_ENGINE_VERSION,
    DecisionSnapshotError,
    build_decision_material,
    read_current_decision_snapshot,
    recompute_decision_snapshot,
)
from app.auction_taxonomy import SCENARIO_SELECTOR_VERSION, UNCLASSIFIED_SCENARIO
from app.auction_verdict import RULES_VERSION as VERDICT_RULES_VERSION
from app.db import Base
from app.models import AuctionDecisionSnapshot, AuctionEvidence, AuctionLot

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _lot(source_id: str = "452662", **overrides: object) -> AuctionLot:
    values: dict[str, object] = {
        "source": "e-qazyna",
        "source_lot_id": source_id,
        "source_url": f"https://example.test/{source_id}",
        "title": "Земельный участок",
        "object_type": "land",
        "land_object_id": f"land-{source_id}",
        "start_price_kzt": 1_000_000,
        "updated_at": NOW,
    }
    values.update(overrides)
    return AuctionLot(**values)


def _factory(url: str = "sqlite:///:memory:"):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _complete_outputs() -> dict[str, object]:
    required = (
        "legal_passport",
        "geometry_context",
        "restriction_context",
        "site_context",
        "planning_context",
        "history_reference",
        "market_estimate",
    )
    result: dict[str, object] = {key: {"status": "found"} for key in required}
    def exact(amount: int) -> dict[str, int]:
        return {"low_kzt": amount, "base_kzt": amount, "high_kzt": amount}

    result.update(
        {
            "scenario_selection": {
                "selector_version": SCENARIO_SELECTOR_VERSION,
                "status": "selected",
                "profile": "camping",
                "scenario_key": "camping",
                "reason_codes": [],
                "purpose_confirmation_required": False,
                "provenance_refs": ["legal:1"],
            },
            "scenario_input": {
                "profile": "camping",
                "right": {
                    "type": "ownership",
                    "transferable": True,
                    "renewable": True,
                    "sublease_allowed": True,
                    "provenance_refs": ["right:1"],
                },
                "legal_passport": {
                    "status": "clear",
                    "use_allowed": True,
                    "provenance_refs": ["legal:1"],
                },
                "restriction_context": {
                    "status": "clear",
                    "coverage_complete": True,
                    "usable_area_m2": 9000,
                    "authoritative_blockers": [],
                    "provenance_refs": ["restriction:1"],
                },
                "site_context": {
                    "physical_access_status": "ready",
                    "legal_access_status": "ready",
                    "infrastructure_status": "ready",
                    "capacity_status": "ready",
                    "provenance_refs": ["site:1"],
                },
                "planning_context": {
                    "status": "clear",
                    "current_use_allowed": True,
                    "pdp_complete": True,
                    "future_adverse": [],
                    "provenance_refs": ["planning:1"],
                },
                "geometry_context": {"status": "ok", "provenance_refs": ["geometry:1"]},
            },
            "price_input": {
                "market_estimate": {
                    "status": "ok",
                    "estimate": {
                        "range_low_kzt": 12_000_000.0,
                        "median_kzt": 13_500_000.0,
                        "range_high_kzt": 15_000_000.0,
                        "verified_comparables_used": 5,
                    },
                    "confidence": "high",
                    "high_quality_verified_count": 5,
                    "verified_eligible_count": 5,
                    "engine_version": "strict-market-comparables.v2-same-year",
                    "provenance_refs": ["market:w9"],
                },
                "history_reference": {"competition_reference": "1.8x"},
                "legal_payments": {
                    "payments_complete": True,
                    "one_time": [],
                    "annual_lease": None,
                    "refundable_guarantee_kzt": 216_250,
                },
                "acquisition": {"start_price_kzt": 1_000_000},
                "targets": {"holding_period_years": "1", "target_roi_percent": "25"},
                "cost_ranges": {
                    "connection": exact(500_000),
                    "development": exact(500_000),
                    "registration": exact(100_000),
                    "tax_annual": exact(100_000),
                    "due_diligence": exact(100_000),
                    "financing": exact(200_000),
                    "contingency": exact(200_000),
                    "risk_reserve": exact(300_000),
                },
            },
            "verdict_evidence": {
                "critical_blockers": [],
                "material_risks": [],
                "provenance_refs": ["evidence:complete"],
            },
            "evidence_generation_ids": {"history": 3, "planning": "pdp-2026"},
            "source_freshness": {
                key: {"status": "fresh", "observed_at": NOW.isoformat()}
                for key in required
            },
        }
    )
    return result


def _seed(factory, *lots: AuctionLot) -> None:
    with factory() as session, session.begin():
        session.add_all(lots)


def test_idempotent_recompute_returns_one_current_snapshot() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    first = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=_complete_outputs(), checked_at=NOW
    )
    second = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=_complete_outputs(), checked_at=NOW
    )
    assert first.id == second.id
    assert first.verdict == "participate_up_to"
    assert first.bid_ceiling_kzt == 7_600_000
    assert first.fair_value_low_kzt == 12_000_000
    assert first.data_readiness == "complete"
    with factory() as session:
        assert len(list(session.scalars(select(AuctionDecisionSnapshot)))) == 1
        assert read_current_decision_snapshot(session, lot.id).id == first.id


def test_changed_input_atomically_supersedes_but_retains_prior_snapshot() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    first = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=_complete_outputs(), checked_at=NOW
    )
    changed = _complete_outputs()
    changed["history_reference"] = {"status": "found", "matched_count": 47}
    second = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=changed, checked_at=NOW
    )
    assert first.id != second.id
    with factory() as session:
        rows = list(
            session.scalars(
                select(AuctionDecisionSnapshot).order_by(AuctionDecisionSnapshot.id)
            )
        )
        assert len(rows) == 2
        assert rows[0].is_current is False and rows[0].stale is True
        assert rows[1].is_current is True


def test_a_b_a_reactivates_matching_snapshot_and_never_returns_noncurrent() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    original = _complete_outputs()
    first = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=original, checked_at=NOW
    )
    changed = _complete_outputs()
    changed["history_reference"] = {"status": "found", "matched_count": 47}
    second = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=changed, checked_at=NOW
    )
    reactivated = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=original, checked_at=NOW
    )
    assert reactivated.id == first.id
    with factory() as session:
        assert session.get(AuctionDecisionSnapshot, first.id).is_current is True
        assert session.get(AuctionDecisionSnapshot, second.id).is_current is False


def test_452662_missing_contract_geo_and_comparables_persists_requires_check() -> None:
    factory, _ = _factory()
    lot = _lot(start_price_kzt=17_970)
    _seed(factory, lot)
    snapshot = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs={}, checked_at=NOW
    )
    assert snapshot.verdict == "requires_check"
    assert snapshot.data_readiness == "insufficient"
    assert snapshot.bid_ceiling_kzt is None
    assert snapshot.fair_value_low_kzt is None
    payload = json.loads(snapshot.payload_json)
    assert "legal_passport" in payload["missing_modules"]
    assert payload["verdict_analysis"]["recommended_ceiling_kzt"] is None


def test_snapshot_payload_exposes_explicit_unknown_risk_and_action_contract() -> None:
    outputs = _complete_outputs()
    outputs["stale_reasons"] = ["module_incomplete:planning_context"]
    outputs["verdict_evidence"] = {
        "critical_blockers": [],
        "material_risks": [
            {"code": "LEGAL_RESTRICTIONS_FOUND", "evidence_refs": ["official:restriction:1"]}
        ],
        "provenance_refs": ["official:restriction:1"],
    }
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key="camping",
        module_outputs=outputs,
        validated_evidence_id=7,
        checked_at=NOW,
    )
    payload = json.loads(material.payload_json)
    contract = payload["decision_evidence_contract"]

    assert contract["status"] == "manual_required"
    assert contract["action"] == {
        "code": "manual_review",
        "reason_codes": [
            "CRITICAL_EVIDENCE_INCOMPLETE",
            "UNRESOLVED_CRITICAL:STALE_MODULE_INCOMPLETE:PLANNING_CONTEXT",
        ],
        "evidence_refs": [],
        "recommended_ceiling_kzt": None,
    }
    assert contract["unknowns"] == [
        {"code": "STALE_MODULE_INCOMPLETE:PLANNING_CONTEXT", "evidence_refs": []}
    ]
    assert contract["risks"] == [
        {"code": "LEGAL_RESTRICTIONS_FOUND", "evidence_refs": ["official:restriction:1"]}
    ]
    assert contract["blockers"] == []


def test_non_ready_decision_contract_never_has_an_empty_explanation() -> None:
    outputs = _complete_outputs()
    outputs["scenario_selection"] = {
        "selector_version": SCENARIO_SELECTOR_VERSION,
        "status": "requires_check",
        "profile": "unknown",
        "scenario_key": None,
        "reason_codes": ["PURPOSE_MISSING"],
        "purpose_confirmation_required": True,
        "provenance_refs": ["auction_lot:lot-1"],
    }
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key=UNCLASSIFIED_SCENARIO,
        module_outputs=outputs,
        checked_at=NOW,
    )
    contract = json.loads(material.payload_json)["decision_evidence_contract"]

    assert contract["status"] == "manual_required"
    assert contract["unknowns"] == [
        {
            "code": "SCENARIO_REQUIRES_CHECK",
            "evidence_refs": ["auction_lot:lot-1"],
        },
        {"code": "PRICE_INPUTS_INSUFFICIENT", "evidence_refs": []},
    ]
    assert contract["action"] == {
        "code": "manual_review",
        "reason_codes": ["SCENARIO_REQUIRES_CHECK", "PRICE_INPUTS_INSUFFICIENT"],
        "evidence_refs": ["auction_lot:lot-1"],
        "recommended_ceiling_kzt": None,
    }


def test_blocked_decision_contract_exposes_engine_reason_and_provenance() -> None:
    outputs = _complete_outputs()
    evidence = outputs["verdict_evidence"]
    assert isinstance(evidence, dict)
    evidence["critical_blockers"] = [
        {"code": "WHOLE_PARCEL_RESTRICTION", "evidence_refs": ["restriction:whole"]}
    ]
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key="camping",
        module_outputs=outputs,
        checked_at=NOW,
    )
    contract = json.loads(material.payload_json)["decision_evidence_contract"]

    assert contract["status"] == "blocked"
    assert contract["action"] == {
        "code": "stop",
        "reason_codes": ["CRITICAL_BLOCKER:WHOLE_PARCEL_RESTRICTION"],
        "evidence_refs": ["restriction:whole"],
        "recommended_ceiling_kzt": None,
    }


def test_fresh_source_without_observed_at_fails_closed_into_explicit_unknown_action() -> None:
    outputs = _complete_outputs()
    outputs["source_freshness"]["planning_context"]["observed_at"] = None

    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key="camping",
        module_outputs=outputs,
        checked_at=NOW,
    )
    payload = json.loads(material.payload_json)
    freshness = json.loads(material.source_freshness_json)
    contract = payload["decision_evidence_contract"]

    assert material.verdict == "requires_check"
    assert material.data_readiness == "partial"
    assert material.stale is True
    assert freshness["planning_context"]["status"] == "unknown"
    assert "source_unknown:planning_context" in json.loads(material.stale_reasons_json)
    assert contract["status"] == "manual_required"
    assert {item["code"] for item in contract["unknowns"]} >= {
        "STALE_SOURCE_UNKNOWN:PLANNING_CONTEXT"
    }
    assert contract["action"]["code"] == "manual_review"
    assert contract["action"]["recommended_ceiling_kzt"] is None


def test_stale_sources_and_wrong_module_versions_never_leak_green_decision() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    stale = _complete_outputs()
    stale["source_freshness"]["planning_context"]["status"] = "stale"
    stale_snapshot = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=stale, checked_at=NOW
    )
    invalid = _complete_outputs()
    invalid["price_input"]["market_estimate"]["engine_version"] = "caller-market-engine"
    invalid_snapshot = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=invalid, checked_at=NOW
    )
    assert stale_snapshot.verdict == "requires_check"
    assert stale_snapshot.stale is True
    assert "source_stale:planning_context" in json.loads(stale_snapshot.stale_reasons_json)
    assert invalid_snapshot.verdict == "requires_check"
    assert invalid_snapshot.bid_ceiling_kzt is None


def test_caller_supplied_result_status_and_ceiling_are_never_trusted() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    invented_scenario = _complete_outputs()
    del invented_scenario["scenario_input"]
    invented_scenario["scenario_analysis"] = {
        "rules_version": "scenario-rules/2026.1",
        "results": [{"scenario": "camping", "status": "eligible"}],
    }
    scenario_snapshot = recompute_decision_snapshot(
        factory,
        lot.id,
        scenario_key="camping",
        module_outputs=invented_scenario,
        checked_at=NOW,
    )
    invented_price = _complete_outputs()
    del invented_price["price_input"]
    invented_price["price_analysis"] = {
        "engine_version": "price-ceiling/2026.1",
        "status": "calculated",
        "recommended_ceiling_kzt": 999_999_999,
    }
    price_snapshot = recompute_decision_snapshot(
        factory,
        lot.id,
        scenario_key="camping",
        module_outputs=invented_price,
        checked_at=NOW,
    )
    assert scenario_snapshot.verdict == "requires_check"
    assert scenario_snapshot.bid_ceiling_kzt is None
    assert price_snapshot.verdict == "requires_check"
    assert price_snapshot.bid_ceiling_kzt is None


def test_repeat_attempts_are_materialized_from_object_identity() -> None:
    factory, _ = _factory()
    current = _lot("one", land_object_id="shared-land")
    prior = _lot("two", land_object_id="shared-land")
    _seed(factory, current, prior)
    snapshot = recompute_decision_snapshot(
        factory,
        current.id,
        scenario_key="camping",
        module_outputs=_complete_outputs(),
        checked_at=NOW,
    )
    assert snapshot.repeat_attempt_count == 1
    assert snapshot.has_repeat is True


def test_repeat_attempts_use_official_source_object_identity() -> None:
    factory, _ = _factory()
    source_object_url = (
        "https://jerler.e-qazyna.kz/ru/guest/reestr/objects/list/934/view"
    )
    current = _lot(
        "one",
        land_object_id=None,
        cadastre_number=None,
        source_object_url=source_object_url,
    )
    prior = _lot(
        "two",
        land_object_id=None,
        cadastre_number="04:061:003:1326",
        source_object_url=source_object_url,
    )
    with factory() as session:
        session.add_all((current, prior))
        session.commit()

    snapshot = recompute_decision_snapshot(
        factory,
        current.id,
        scenario_key="camping",
        module_outputs=_complete_outputs(),
        checked_at=NOW,
    )

    assert snapshot.repeat_attempt_count == 1
    assert snapshot.has_repeat is True


def test_oversized_or_non_json_input_and_naive_timestamp_are_rejected() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    oversized = {"legal_passport": {"blob": "x" * 513_000}}
    for outputs, checked_at in (
        (oversized, NOW),
        ({"legal_passport": {"bad": float("nan")}}, NOW),
        ({}, datetime(2026, 8, 17, 12)),
    ):
        try:
            recompute_decision_snapshot(
                factory,
                lot.id,
                scenario_key="camping",
                module_outputs=outputs,
                checked_at=checked_at,
            )
        except DecisionSnapshotError:
            pass
        else:
            raise AssertionError("invalid snapshot input was accepted")
    try:
        build_decision_material(
            lot,
            repeat_attempt_count=-1,
            scenario_key="camping",
            module_outputs={},
            checked_at=NOW,
        )
    except DecisionSnapshotError:
        pass
    else:
        raise AssertionError("negative repeat count was accepted")


def test_oversized_newest_persisted_evidence_uses_bounded_projection_and_older_valid() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    with factory() as session, session.begin():
        session.add_all(
            [
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:legal_passport",
                    status="found",
                    title="older-valid",
                    raw_payload_json='{"status":"clear"}',
                    observed_at=datetime(2026, 8, 16, tzinfo=UTC),
                ),
                AuctionEvidence(
                    lot_id=lot.id,
                    evidence_type="decision_input:legal_passport",
                    status="found",
                    title="oversized-newest",
                    raw_payload_json=json.dumps({"blob": "x" * 300_000}),
                    observed_at=NOW,
                ),
            ]
        )
    snapshot = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs={}, checked_at=NOW
    )
    assert snapshot.verdict == "requires_check"
    assert "legal_passport" not in json.loads(snapshot.payload_json)["missing_modules"]


def test_concurrent_identical_recompute_keeps_one_snapshot(tmp_path: Path) -> None:
    factory, _ = _factory(f"sqlite:///{tmp_path / 'decision.db'}")
    lot = _lot()
    _seed(factory, lot)

    def work() -> int:
        return recompute_decision_snapshot(
            factory,
            lot.id,
            scenario_key="camping",
            module_outputs=_complete_outputs(),
            checked_at=NOW,
        ).id

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: work(), range(2)))
    assert ids[0] == ids[1]
    with factory() as session:
        assert len(list(session.scalars(select(AuctionDecisionSnapshot)))) == 1


def test_schema_and_migration_bind_current_to_exact_policy_versions() -> None:
    factory, engine = _factory()
    inspector = inspect(engine)
    indexes = {item["name"] for item in inspector.get_indexes("auction_decision_snapshots")}
    assert "uq_auction_decision_snapshot_current" in indexes
    assert "ix_auction_decision_snapshot_verdict_current" in indexes
    assert AuctionDecisionSnapshot.__table__.c.bid_ceiling_kzt.type.python_type is int

    migration = ROOT / "migrations" / "versions" / "a6d4e8f1c2b7_auction_decision_snapshots.py"
    source = migration.read_text(encoding="utf-8")
    assert 'down_revision: str | Sequence[str] | None = "f8c1d2e3a4b5"' in source
    assert "f3a2b6c9d8e1" not in source
    assert "postgresql_where=sa.text(\"is_current = true\")" in source
    assert "sa.BigInteger()" in source
    assert "fair_value_low_kzt <= fair_value_high_kzt" in source
    assert "bid_ceiling_kzt <= 1000000000000000" in source
    assert DECISION_ENGINE_VERSION == "decision-snapshot/2026.3"


def test_unclassified_persisted_selection_cannot_be_turned_into_resale_blocker() -> None:
    outputs = _complete_outputs()
    outputs["scenario_selection"] = {
        "selector_version": SCENARIO_SELECTOR_VERSION,
        "status": "requires_check",
        "profile": "unknown",
        "scenario_key": None,
        "reason_codes": ["PURPOSE_MISSING"],
        "purpose_confirmation_required": True,
        "provenance_refs": ["legal:purpose"],
    }
    scenario_input = outputs["scenario_input"]
    assert isinstance(scenario_input, dict)
    scenario_input["right"] = {"type": "lease", "lease_years": 3}
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key="resale",
        module_outputs=outputs,
        checked_at=NOW,
    )
    assert material.scenario_key == UNCLASSIFIED_SCENARIO
    assert material.verdict == "requires_check"
    assert material.bid_ceiling_kzt is None


def test_inconsistent_selected_payload_fails_closed() -> None:
    outputs = _complete_outputs()
    outputs["scenario_selection"] = {
        "selector_version": SCENARIO_SELECTOR_VERSION,
        "status": "selected",
        "profile": "unknown",
        "scenario_key": "operating_business",
        "reason_codes": [],
        "purpose_confirmation_required": False,
        "provenance_refs": [],
    }
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key="operating_business",
        module_outputs=outputs,
        checked_at=NOW,
    )
    assert material.scenario_key == UNCLASSIFIED_SCENARIO
    assert material.verdict == "requires_check"
    assert material.bid_ceiling_kzt is None


def test_independent_whole_parcel_blocker_remains_do_not_participate() -> None:
    outputs = _complete_outputs()
    outputs["scenario_selection"] = {
        "selector_version": SCENARIO_SELECTOR_VERSION,
        "status": "requires_check",
        "profile": "unknown",
        "scenario_key": None,
        "reason_codes": ["PURPOSE_MISSING"],
        "purpose_confirmation_required": True,
        "provenance_refs": [],
    }
    evidence = outputs["verdict_evidence"]
    assert isinstance(evidence, dict)
    evidence["critical_blockers"] = [
        {
            "code": "WHOLE_PARCEL_RESTRICTION",
            "message": "Authoritative whole-parcel prohibition",
            "evidence_refs": ["restriction:whole"],
        }
    ]
    material = build_decision_material(
        _lot(),
        repeat_attempt_count=0,
        scenario_key=UNCLASSIFIED_SCENARIO,
        module_outputs=outputs,
        checked_at=NOW,
    )
    assert material.verdict == "do_not_participate"
    assert material.bid_ceiling_kzt is None
    assert VERDICT_RULES_VERSION == "five-state-verdict/2026.1"


def test_read_path_never_selects_legacy_current_policy_row() -> None:
    factory, _ = _factory()
    lot = _lot()
    _seed(factory, lot)
    current = recompute_decision_snapshot(
        factory, lot.id, scenario_key="camping", module_outputs=_complete_outputs(), checked_at=NOW
    )
    with factory() as session, session.begin():
        session.add(
            AuctionDecisionSnapshot(
                lot_id=lot.id,
                engine_version="decision-snapshot/2025",
                rules_version="five-state-verdict/2025",
                verdict_engine_version="auction-verdict/2025",
                scenario_engine_version=None,
                price_engine_version=None,
                formula_version=None,
                input_hash="f" * 64,
                is_current=True,
                stale=False,
                verdict="requires_check",
                data_readiness="insufficient",
                scenario_key="camping",
                repeat_attempt_count=0,
                has_repeat=False,
                bid_ceiling_kzt=None,
                fair_value_low_kzt=None,
                fair_value_high_kzt=None,
                evidence_generation_ids_json="{}",
                source_freshness_json="{}",
                stale_reasons_json="[]",
                payload_json="{}",
                computed_at=NOW,
                checked_at=NOW,
                created_at=NOW,
            )
        )
    with factory() as session:
        selected = read_current_decision_snapshot(session, lot.id)
        assert selected is not None
        assert selected.id == current.id
        assert selected.engine_version == DECISION_ENGINE_VERSION
