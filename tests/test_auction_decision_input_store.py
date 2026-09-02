from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auction_actual_cost_writer import (
    STANDARD_INVESTMENT_POLICY_VERSION,
    ActualCostFact,
    canonical_source_identity,
    persist_actual_cost_evidence,
    produce_authoritative_actual_costs,
)
from app.auction_decision_input_store import (
    ASSEMBLER_VERSION,
    POLICY_VERSION,
    SPATIAL_ASSEMBLER_VERSION,
    _read_bundle,
    decision_input_worklist,
    recompute_decision_inputs,
)
from app.auction_decision_snapshot import recompute_decision_snapshot
from app.auction_taxonomy import UNCLASSIFIED_SCENARIO
from app.db import Base
from app.models import (
    AuctionDecisionInputState,
    AuctionDocument,
    AuctionEvidence,
    AuctionLot,
    AuctionMarketComparable,
)

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'decision-input.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _lot(lot_id: str = "lot-452662") -> AuctionLot:
    return AuctionLot(
        id=lot_id,
        source="e-qazyna",
        source_lot_id="452662",
        object_type="land",
        title="Участок для кемпинга",
        purpose="строительство и обслуживание кемпинга",
        land_rights="временное возмездное краткосрочное землепользование",
        lease_term_years=3,
        start_price_kzt=324_000,
        guarantee_kzt=216_250,
        additional_payment_kzt=16_200,
        source_url="https://example.test/452662",
        updated_at=NOW,
    )


def _source_card(lot_id: str = "lot-452662", observed_at: datetime = NOW):
    return AuctionEvidence(
        lot_id=lot_id,
        evidence_type="source_object_card",
        status="found",
        title="Jerler",
        confidence=0.98,
        observed_at=observed_at,
        raw_payload_json=json.dumps(
            {
                "land_rights": "временное возмездное краткосрочное землепользование",
                "lease_term_years": 3,
                "arrests_text": "не имеются",
                "restrictions_text": "не имеются",
            }
        ),
    )


def _actual_connection_cost(value: int):
    fact = ActualCostFact(
        target_lot_id="lot-452662",
        scenario_key="camping",
        investment_policy_version=STANDARD_INVESTMENT_POLICY_VERSION,
        holding_horizon_months=60,
        cost_key="connection",
        low_kzt=value,
        base_kzt=value,
        high_kzt=value,
        status="found",
        source_kind="connection_estimate",
        source_identity=canonical_source_identity(
            "connection_estimate", "Электросети Абай", "quote-452662"
        ),
        source_ref="quote:connection:452662",
        source_url="https://costs.example.test/452662/connection",
        observed_at=NOW,
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        confidence=0.95,
        source_version="utility/2026.1",
        currency="KZT",
        basis="one_time",
    )
    return produce_authoritative_actual_costs(
        [fact],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )


def test_recompute_persists_changed_inputs_then_quiesces(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())

    first = recompute_decision_inputs(factory, "lot-452662", now=NOW)
    second = recompute_decision_inputs(factory, "lot-452662", now=NOW)

    assert first.status == "insufficient"
    assert first.changed is True
    assert first.evidence_ids
    assert second.changed is False
    assert second.evidence_ids == ()
    with factory() as session:
        assert decision_input_worklist(session, now=NOW) == []
        rows = list(
            session.scalars(
                select(AuctionEvidence).where(
                    AuctionEvidence.lot_id == "lot-452662",
                    AuctionEvidence.evidence_type.like("decision_input:%"),
                )
            )
        )
        assert len(rows) == len(first.evidence_ids)
        state = session.get(AuctionDecisionInputState, "lot-452662")
        assert state is not None
        assert state.assembler_version == ASSEMBLER_VERSION
        assert state.spatial_assembler_version == SPATIAL_ASSEMBLER_VERSION


def test_previous_assembler_version_requeues_current_decision_input(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())
    recompute_decision_inputs(factory, "lot-452662", now=NOW)
    with factory() as session, session.begin():
        state = session.get(AuctionDecisionInputState, "lot-452662")
        assert state is not None
        state.assembler_version = "decision-input-assembler/previous"
    with factory() as session:
        assert decision_input_worklist(session, now=NOW) == ["lot-452662"]


def test_generic_title_and_missing_purpose_persist_uncertainty_for_w13(tmp_path) -> None:
    factory = _factory(tmp_path)
    lot = _lot("lot-unknown-purpose")
    lot.title = "Земельный участок"
    lot.purpose = None
    lot.use_goal = None
    lot.functional_purpose_level4 = None
    with factory() as session, session.begin():
        session.add(lot)
        session.add(_source_card("lot-unknown-purpose"))

    result = recompute_decision_inputs(factory, lot.id, now=NOW)
    assert result.changed is True
    snapshot = recompute_decision_snapshot(
        factory,
        lot.id,
        scenario_key="resale",  # legacy caller hint must be ignored
        checked_at=NOW,
    )
    assert snapshot.scenario_key == UNCLASSIFIED_SCENARIO
    assert snapshot.verdict == "requires_check"
    assert snapshot.bid_ceiling_kzt is None


def test_decision_output_evidence_never_dirties_its_own_worklist(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
    result = recompute_decision_inputs(factory, "lot-452662", now=NOW)
    assert result.changed
    with factory() as session, session.begin():
        session.add(
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="decision_input:synthetic",
                status="found",
                title="must be ignored",
                raw_payload_json="{}",
                observed_at=NOW + timedelta(minutes=1),
            )
        )
    with factory() as session:
        assert decision_input_worklist(session, now=NOW + timedelta(minutes=1)) == []


def test_stale_market_target_signature_is_rejected_before_decision_assembly(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="strict_market_estimate",
                status="found",
                title="stale W9",
                value_text="a" * 64,
                raw_payload_json=json.dumps(
                    {
                        "status": "ok",
                        "confidence": "high",
                        "estimate": {"median_kzt": 9_000_000},
                        "market_target_signature": "0" * 64,
                    }
                ),
                observed_at=NOW,
            )
        )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.market_result is None


def test_mutated_upstream_row_is_detected_once_by_observed_watermark(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())
    recompute_decision_inputs(factory, "lot-452662", now=NOW)
    with factory() as session, session.begin():
        evidence = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == "source_object_card")
        )
        assert evidence is not None
        evidence.observed_at = NOW + timedelta(hours=1)
    with factory() as session:
        assert decision_input_worklist(session, now=NOW + timedelta(hours=1)) == ["lot-452662"]
    recompute_decision_inputs(factory, "lot-452662", now=NOW + timedelta(hours=1))
    with factory() as session:
        assert decision_input_worklist(session, now=NOW + timedelta(hours=1)) == []


def test_contract_coverage_requires_exact_processed_document_hash(tmp_path) -> None:
    factory = _factory(tmp_path)
    digest = "a" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=7,
                lot_id="lot-452662",
                title="Проект договора",
                source_url="https://example.test/contract.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW,
            )
        )
        session.add(
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="document_extraction",
                status="found",
                title="bounded extraction",
                observed_at=NOW,
                raw_payload_json=json.dumps(
                    {
                        "document_id": "7",
                        "content_sha256": digest,
                        "result": {
                            "status": "ok",
                            "candidates": [],
                            "conflicts": [],
                        },
                    }
                ),
            )
        )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.contract_coverage is not None
    assert bundle.contract_coverage.coverage_complete is True
    assert bundle.contract_coverage.eligible_document_ids == ("7",)
    assert bundle.contract_coverage.processed_document_ids == ("7",)

    with factory() as session, session.begin():
        extraction = session.scalar(
            select(AuctionEvidence).where(AuctionEvidence.evidence_type == "document_extraction")
        )
        assert extraction is not None
        payload = json.loads(extraction.raw_payload_json or "{}")
        payload["content_sha256"] = "b" * 64
        extraction.raw_payload_json = json.dumps(payload)
        extraction.observed_at = NOW + timedelta(minutes=1)
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.contract_coverage is not None
    assert bundle.contract_coverage.coverage_complete is False
    assert bundle.contract_coverage.processed_document_ids == ()


def test_document_conflict_array_reaches_fail_closed_contract_summary(tmp_path) -> None:
    factory = _factory(tmp_path)
    digest = "c" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=8,
                lot_id="lot-452662",
                title="Проект договора",
                source_url="https://example.test/conflicting-contract.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW,
            )
        )
        session.add(
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="document_extraction",
                status="found",
                title="bounded extraction with conflict",
                observed_at=NOW,
                raw_payload_json=json.dumps(
                    {
                        "document_id": "8",
                        "content_sha256": digest,
                        "result": {
                            "status": "ok",
                            "candidates": [],
                            "conflicts": [
                                {
                                    "field": "lease_term",
                                    "values": ["3 года", "5 лет"],
                                }
                            ],
                        },
                    }
                ),
            )
        )

    result = recompute_decision_inputs(factory, "lot-452662", now=NOW)
    assert result.changed is True
    with factory() as session:
        contract = session.scalar(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == "lot-452662",
                AuctionEvidence.evidence_type == "decision_input:contract_extraction",
            )
            .order_by(AuctionEvidence.id.desc())
        )
    assert contract is not None
    payload = json.loads(contract.raw_payload_json or "{}")
    assert payload["status"] == "conflict"
    assert payload["conflict_fields"] == ["lease_term"]
    assert payload["coverage_complete"] is True


def test_452662_missing_geo_pdp_contract_and_market_remains_requires_check(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())
    result = recompute_decision_inputs(factory, "lot-452662", now=NOW)
    assert result.status == "insufficient"
    with factory() as session:
        scenario = session.scalar(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == "lot-452662",
                AuctionEvidence.evidence_type == "decision_input:scenario_input",
            )
            .order_by(AuctionEvidence.id.desc())
        )
        price = session.scalar(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == "lot-452662",
                AuctionEvidence.evidence_type == "decision_input:price_input",
            )
            .order_by(AuctionEvidence.id.desc())
        )
    assert scenario is not None and price is not None
    scenario_payload = json.loads(scenario.raw_payload_json or "{}")
    price_payload = json.loads(price.raw_payload_json or "{}")
    assert scenario_payload["planning_context"]["status"] in {"unknown", "partial"}
    assert price_payload["market_estimate"]["status"] == "insufficient_data"
    assert price_payload["legal_payments"]["refundable_guarantee_kzt"] == 216_250


def test_oversized_upstream_is_error_with_backoff_and_no_assembled_evidence(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        evidence = _source_card()
        evidence.raw_payload_json = json.dumps({"blob": "x" * 70_000})
        session.add(evidence)
    result = recompute_decision_inputs(factory, "lot-452662", now=NOW)
    assert result.status == "error"
    with factory() as session:
        state = session.get(AuctionDecisionInputState, "lot-452662")
        assert state is not None
        assert state.status == "error"
        assert state.next_attempt_at is not None
        assert not list(
            session.scalars(
                select(AuctionEvidence).where(
                    AuctionEvidence.evidence_type.like("decision_input:%")
                )
            )
        )


def test_large_valid_upstream_history_is_bounded_without_losing_source_card(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())
        for index in range(30):
            session.add(
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="official_document_summary",
                    status="found",
                    title=f"document revision {index}",
                    confidence=0.8,
                    observed_at=NOW,
                    raw_payload_json=json.dumps({"revision": index, "blob": "x" * 50_000}),
                )
            )

    result = recompute_decision_inputs(factory, "lot-452662", now=NOW)

    assert result.status == "insufficient"
    with factory() as session:
        state = session.get(AuctionDecisionInputState, "lot-452662")
        bundle = _read_bundle(session, "lot-452662")
        price = session.scalar(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == "lot-452662",
                AuctionEvidence.evidence_type == "decision_input:price_input",
            )
            .order_by(AuctionEvidence.id.desc())
        )
    assert state is not None
    assert state.last_error_code is None
    assert price is not None
    assert bundle.legal_passport["facts"]["right_type"]["value"] == "lease"


def _clean_state(lot_id: str, updated_at: datetime) -> AuctionDecisionInputState:
    return AuctionDecisionInputState(
        lot_id=lot_id,
        status="insufficient",
        source_watermark_id=0,
        lot_updated_at=updated_at,
        history_generation=None,
        market_signature="unused",
        market_watermark_id=0,
        market_row_count=0,
        document_signature="unused",
        document_watermark_id=0,
        document_row_count=0,
        input_hash="a" * 64,
        assembler_version=ASSEMBLER_VERSION,
        spatial_assembler_version=SPATIAL_ASSEMBLER_VERSION,
        policy_version=POLICY_VERSION,
        validated_at=NOW,
        updated_at=NOW,
    )


def test_db_filter_prevents_clean_recent_lots_starving_old_dirty(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        for index in range(6):
            lot = _lot(f"clean-{index}")
            lot.source_lot_id = f"clean-{index}"
            lot.updated_at = NOW + timedelta(hours=index + 1)
            session.add(lot)
            session.add(_clean_state(lot.id, lot.updated_at))
        dirty = _lot("old-dirty")
        dirty.source_lot_id = "old-dirty"
        dirty.updated_at = NOW - timedelta(days=1)
        session.add(dirty)
    with factory() as session:
        assert decision_input_worklist(session, limit=1, now=NOW + timedelta(hours=8)) == [
            "old-dirty"
        ]


def test_due_error_and_expired_processing_are_retried(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        error_lot = _lot("due-error")
        error_lot.source_lot_id = "due-error"
        processing_lot = _lot("expired-processing")
        processing_lot.source_lot_id = "expired-processing"
        session.add_all((error_lot, processing_lot))
        error_state = _clean_state(error_lot.id, NOW)
        error_state.status = "error"
        error_state.next_attempt_at = NOW - timedelta(minutes=1)
        processing_state = _clean_state(processing_lot.id, NOW)
        processing_state.status = "processing"
        processing_state.claim_token = "old"
        processing_state.claim_expires_at = NOW - timedelta(minutes=1)
        session.add_all((error_state, processing_state))
    with factory() as session:
        assert set(decision_input_worklist(session, limit=5, now=NOW)) == {
            "due-error",
            "expired-processing",
        }


def test_market_and_document_generations_dirty_existing_state(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
    recompute_decision_inputs(factory, "lot-452662", now=NOW)
    with factory() as session, session.begin():
        session.add(
            AuctionMarketComparable(
                lot_id="lot-452662",
                source_name="verified",
                source_url="https://example.test/comparable",
                title="Comparable",
                listing_status="sold",
                observed_at=NOW + timedelta(minutes=1),
            )
        )
    with factory() as session:
        assert decision_input_worklist(session, now=NOW + timedelta(minutes=1)) == ["lot-452662"]
    recompute_decision_inputs(factory, "lot-452662", now=NOW + timedelta(minutes=1))
    with factory() as session, session.begin():
        session.add(
            AuctionDocument(
                lot_id="lot-452662",
                title="new contract",
                source_url="https://example.test/new.pdf",
                file_type="pdf",
                storage_status="linked",
                created_at=NOW + timedelta(minutes=2),
            )
        )
    with factory() as session:
        assert decision_input_worklist(session, now=NOW + timedelta(minutes=2)) == ["lot-452662"]


def test_source_conflict_is_preserved_in_worker_legal_passport(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        evidence = _source_card()
        evidence.status = "conflict"
        payload = json.loads(evidence.raw_payload_json or "{}")
        payload["conflicts"] = [{"field": "land_rights"}]
        evidence.raw_payload_json = json.dumps(payload)
        session.add(evidence)
    with factory() as session:
        passport = _read_bundle(session, "lot-452662").legal_passport
    assert passport["facts"]["right_type"]["status"] == "conflict"
    assert passport["facts"]["arrests"]["status"] == "found"


def test_newest_exact_extraction_wins_and_freshness_uses_oldest_doc(tmp_path) -> None:
    factory = _factory(tmp_path)
    digest = "c" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=10,
                lot_id="lot-452662",
                title="contract",
                source_url="https://example.test/c.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW - timedelta(days=5),
            )
        )
        for stamp, marker in (
            (NOW - timedelta(days=4), "older"),
            (NOW - timedelta(days=1), "newer"),
        ):
            session.add(
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="found",
                    title=marker,
                    observed_at=stamp,
                    raw_payload_json=json.dumps(
                        {
                            "document_id": "10",
                            "content_sha256": digest,
                            "result": {
                                "status": "ok",
                                "candidates": [{"field": marker}],
                                "conflicts": [],
                            },
                        }
                    ),
                )
            )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.contract_extractions[0]["candidates"][0]["field"] == "newer"
    assert bundle.contract_coverage is not None
    assert bundle.contract_coverage.observed_at == NOW - timedelta(days=5)


def test_unrelated_conflict_does_not_poison_legal_and_module_conflicts_are_not_applied(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    digest = "d" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(_source_card())
        session.add(
            AuctionDocument(
                id=11,
                lot_id="lot-452662",
                title="contract",
                source_url="https://example.test/d.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW,
            )
        )
        for evidence_type, payload in (
            ("cadastre_boundary", {"geometry_geojson": {"type": "bad"}}),
            (
                "strict_market_estimate",
                {"status": "ok", "confidence": "high", "estimate": {"median_kzt": 9}},
            ),
            ("decision_cost_ranges", {"connection": {"low_kzt": 1}}),
            (
                "document_extraction",
                {
                    "document_id": "11",
                    "content_sha256": digest,
                    "result": {"status": "ok", "candidates": [], "conflicts": []},
                },
            ),
        ):
            session.add(
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type=evidence_type,
                    status="conflict",
                    title="conflicted module evidence",
                    observed_at=NOW,
                    raw_payload_json=json.dumps(payload),
                )
            )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.legal_passport["facts"]["right_type"]["status"] == "found"
    assert bundle.market_result is None
    assert bundle.actual_cost_ranges is None
    assert bundle.contract_coverage is not None
    assert bundle.contract_coverage.coverage_complete is False


def test_actual_costs_require_latest_exact_source_manifest_pair(tmp_path) -> None:
    factory = _factory(tmp_path)
    production = _actual_connection_cost(500_000)
    with factory() as session, session.begin():
        session.add(_lot())
    with factory() as session, session.begin():
        persisted = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=production,
            written_at=NOW,
        )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.actual_cost_ranges is not None
    assert bundle.actual_cost_ranges["connection"]["base_kzt"] == 500_000  # type: ignore[index]
    assert {
        f"auction_evidence:{persisted.evidence_id}",
        f"auction_evidence:{persisted.source_evidence_id}",
    }.issubset(bundle.actual_cost_ranges["provenance_refs"])  # type: ignore[arg-type]


def test_actual_cost_pair_mismatch_missing_or_latest_conflict_fails_closed(tmp_path) -> None:
    factory = _factory(tmp_path)
    production = _actual_connection_cost(500_000)
    with factory() as session, session.begin():
        session.add(_lot())
    with factory() as session, session.begin():
        persisted = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=production,
            written_at=NOW,
        )
        source = session.get(AuctionEvidence, persisted.source_evidence_id)
        assert source is not None
        manifest = json.loads(source.raw_payload_json)
        manifest["scenario_key"] = "development"
        source.raw_payload_json = json.dumps(manifest)
    with factory() as session:
        assert _read_bundle(session, "lot-452662").actual_cost_ranges is None

    with factory() as session, session.begin():
        source = session.get(AuctionEvidence, persisted.source_evidence_id)
        assert source is not None
        manifest = json.loads(source.raw_payload_json)
        manifest["scenario_key"] = "camping"
        source.raw_payload_json = json.dumps(manifest)
        session.add(
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="decision_cost_ranges",
                status="conflict",
                title="newest conflict",
                value_text="conflict",
                raw_payload_json="{}",
                observed_at=NOW + timedelta(minutes=1),
            )
        )
    with factory() as session:
        assert _read_bundle(session, "lot-452662").actual_cost_ranges is None


def test_actual_cost_latest_pair_a_b_a_and_mixed_generation_never_falls_back(tmp_path) -> None:
    factory = _factory(tmp_path)
    production_a = _actual_connection_cost(500_000)
    production_b = _actual_connection_cost(700_000)
    with factory() as session, session.begin():
        session.add(_lot())
    for production, expected in (
        (production_a, 500_000),
        (production_b, 700_000),
        (production_a, 500_000),
    ):
        with factory() as session, session.begin():
            persist_actual_cost_evidence(
                session,
                lot_id="lot-452662",
                production=production,
                written_at=NOW,
            )
        with factory() as session:
            bundle = _read_bundle(session, "lot-452662")
        assert bundle.actual_cost_ranges is not None
        assert bundle.actual_cost_ranges["connection"]["base_kzt"] == expected  # type: ignore[index]

    with factory() as session, session.begin():
        latest_cost = session.scalar(
            select(AuctionEvidence)
            .where(
                AuctionEvidence.lot_id == "lot-452662",
                AuctionEvidence.evidence_type == "decision_cost_ranges",
            )
            .order_by(AuctionEvidence.id.desc())
            .limit(1)
        )
        assert latest_cost is not None
        latest_cost.value_text = latest_cost.value_text.replace(
            production_a.result.generation_id,
            production_b.result.generation_id,
        )
    with factory() as session:
        assert _read_bundle(session, "lot-452662").actual_cost_ranges is None


def test_new_linked_document_invalidates_previous_complete_contract_coverage(tmp_path) -> None:
    factory = _factory(tmp_path)
    digest = "e" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add_all(
            (
                AuctionDocument(
                    id=20,
                    lot_id="lot-452662",
                    title="processed contract",
                    source_url="https://example.test/processed.pdf",
                    file_type="pdf",
                    storage_status="downloaded",
                    content_sha256=digest,
                    downloaded_at=NOW,
                ),
                AuctionDocument(
                    id=21,
                    lot_id="lot-452662",
                    title="new pending contract",
                    source_url="https://example.test/pending.pdf",
                    file_type="pdf",
                    storage_status="linked",
                    created_at=NOW + timedelta(minutes=1),
                ),
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="found",
                    title="processed",
                    observed_at=NOW,
                    raw_payload_json=json.dumps(
                        {
                            "document_id": "20",
                            "content_sha256": digest,
                            "result": {"status": "ok", "candidates": [], "conflicts": []},
                        }
                    ),
                ),
            )
        )
    with factory() as session:
        coverage = _read_bundle(session, "lot-452662").contract_coverage
    assert coverage is not None
    assert coverage.eligible_document_ids == ("20", "21")
    assert coverage.processed_document_ids == ("20",)
    assert coverage.coverage_complete is False


def test_unsupported_document_is_eligible_but_cannot_satisfy_coverage(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=30,
                lot_id="lot-452662",
                title="unknown project attachment",
                source_url="https://example.test/attachment",
                file_type=None,
                storage_status="linked",
                created_at=NOW,
            )
        )
    with factory() as session:
        coverage = _read_bundle(session, "lot-452662").contract_coverage
    assert coverage is not None
    assert coverage.eligible_document_ids == ("30",)
    assert coverage.processed_document_ids == ()
    assert coverage.coverage_complete is False


def test_backdated_high_id_outside_payload_window_quiesces_after_one_recompute(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        for index in range(49):
            session.add(
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="official_lot",
                    status="found",
                    title=f"evidence-{index}",
                    observed_at=NOW + timedelta(minutes=index),
                    raw_payload_json="{}",
                )
            )
    # Make the highest id older than the 48-row payload selection window.
    with factory() as session, session.begin():
        highest = session.scalar(
            select(AuctionEvidence).order_by(AuctionEvidence.id.desc()).limit(1)
        )
        assert highest is not None
        highest.observed_at = NOW - timedelta(days=10)
        highest_id = highest.id
    result = recompute_decision_inputs(factory, "lot-452662", now=NOW + timedelta(hours=1))
    assert result.status == "insufficient"
    with factory() as session:
        state = session.get(AuctionDecisionInputState, "lot-452662")
        assert state is not None and state.source_watermark_id == highest_id
        assert decision_input_worklist(session, now=NOW + timedelta(hours=1)) == []


def test_newest_current_hash_conflict_blocks_older_successful_extraction(tmp_path) -> None:
    factory = _factory(tmp_path)
    digest = "f" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=40,
                lot_id="lot-452662",
                title="contract",
                source_url="https://example.test/f.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW - timedelta(days=3),
            )
        )
        session.add_all(
            (
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="found",
                    title="older ok",
                    observed_at=NOW - timedelta(days=2),
                    raw_payload_json=json.dumps(
                        {
                            "document_id": "40",
                            "content_sha256": digest,
                            "result": {"status": "ok", "candidates": [], "conflicts": []},
                        }
                    ),
                ),
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="conflict",
                    title="new conflict",
                    observed_at=NOW - timedelta(days=1),
                    raw_payload_json=json.dumps(
                        {
                            "document_id": "40",
                            "content_sha256": digest,
                            "result": {
                                "status": "conflict",
                                "candidates": [],
                                "conflicts": [{"field": "lease_term_years"}],
                            },
                        }
                    ),
                ),
            )
        )
    with factory() as session:
        bundle = _read_bundle(session, "lot-452662")
    assert bundle.contract_extractions == ()
    assert bundle.contract_coverage is not None
    assert bundle.contract_coverage.processed_document_ids == ()
    assert bundle.contract_coverage.coverage_complete is False


def test_unidentifiable_extraction_conflict_invalidates_otherwise_complete_coverage(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    digest = "1" * 64
    with factory() as session, session.begin():
        session.add(_lot())
        session.add(
            AuctionDocument(
                id=41,
                lot_id="lot-452662",
                title="contract",
                source_url="https://example.test/1.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=digest,
                downloaded_at=NOW,
            )
        )
        session.add_all(
            (
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="found",
                    title="valid",
                    observed_at=NOW,
                    raw_payload_json=json.dumps(
                        {
                            "document_id": "41",
                            "content_sha256": digest,
                            "result": {"status": "ok", "candidates": [], "conflicts": []},
                        }
                    ),
                ),
                AuctionEvidence(
                    lot_id="lot-452662",
                    evidence_type="document_extraction",
                    status="conflict",
                    title="unidentified conflict",
                    observed_at=NOW + timedelta(minutes=1),
                    raw_payload_json=json.dumps(
                        {
                            "result": {
                                "status": "conflict",
                                "candidates": [],
                                "conflicts": [{"field": "unknown"}],
                            }
                        }
                    ),
                ),
            )
        )
    with factory() as session:
        coverage = _read_bundle(session, "lot-452662").contract_coverage
    assert coverage is not None
    assert coverage.processed_document_ids == ("41",)
    assert coverage.coverage_complete is False
