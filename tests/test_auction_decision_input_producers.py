from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auction_decision_input_producers import (
    MAX_DOCUMENTS,
    ActiveHistoryAudit,
    ContractCoverageInputs,
    DecisionInputProducerError,
    DocumentedMonetaryFact,
    DocumentInventoryRecord,
    ExtractionEvidenceRecord,
    adapt_strict_market_estimate,
    build_authoritative_contract_coverage,
    load_contract_coverage_inputs,
    produce_decision_cost_ranges,
)
from app.auction_decision_input_store import _read_bundle
from app.auction_market_comparables import (
    ComparableEvaluation,
    MarketComparableResult,
    MarketEstimate,
)
from app.auction_price_ceiling import REQUIRED_COST_KEYS
from app.db import Base
from app.models import AuctionDocument, AuctionEvidence, AuctionLot

NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)
DIGEST = "a" * 64


def _document(
    document_id: str = "7",
    *,
    digest: str | None = DIGEST,
    file_type: str = "pdf",
    status: str = "downloaded",
) -> DocumentInventoryRecord:
    return DocumentInventoryRecord(
        document_id,
        file_type,
        status,
        digest,
        NOW,
        "https://example.test/contract.pdf",
    )


def _extraction(
    document_id: str = "7",
    *,
    digest: str = DIGEST,
    result_status: str = "ok",
) -> ExtractionEvidenceRecord:
    return ExtractionEvidenceRecord(
        evidence_id=19,
        status="found",
        observed_at=NOW - timedelta(hours=1),
        payload={
            "document_id": document_id,
            "content_sha256": digest,
            "result": {
                "status": result_status,
                "candidates": [{"field": "lease_term_years", "value": 3}],
                "conflicts": [],
            },
        },
    )


def test_contract_coverage_requires_exact_inventory_hash_and_ok_result() -> None:
    complete = build_authoritative_contract_coverage(
        ContractCoverageInputs((_document(),), (_extraction(),)),
        assembled_at=NOW,
    )
    assert complete.status == "complete"
    assert complete.coverage is not None and complete.coverage.coverage_complete is True
    assert complete.coverage.eligible_document_ids == ("7",)
    assert complete.coverage.processed_document_ids == ("7",)
    assert complete.coverage.observed_at == NOW - timedelta(hours=1)
    assert complete.accepted_extractions[0]["candidates"][0]["document_id"] == "7"

    mismatch = build_authoritative_contract_coverage(
        ContractCoverageInputs((_document(),), (_extraction(digest="b" * 64),)),
        assembled_at=NOW,
    )
    assert mismatch.status == "incomplete"
    assert mismatch.coverage is not None
    assert mismatch.coverage.processed_document_ids == ()

    unknown = build_authoritative_contract_coverage(
        ContractCoverageInputs((_document(),), (_extraction(result_status="unknown"),)),
        assembled_at=NOW,
    )
    assert unknown.status == "incomplete"
    assert unknown.coverage is not None and unknown.coverage.coverage_complete is False


def test_contract_coverage_never_claims_unsupported_or_partial_inventory_complete() -> None:
    result = build_authoritative_contract_coverage(
        ContractCoverageInputs(
            (_document(), _document("8", digest=None, file_type="doc", status="linked")),
            (_extraction(),),
        ),
        assembled_at=NOW,
    )
    assert result.status == "incomplete"
    assert result.coverage is not None
    assert result.coverage.eligible_document_ids == ("7", "8")
    assert result.coverage.processed_document_ids == ("7",)
    assert "document_not_extractable:8" in result.reasons
    assert "document_unprocessed:8" in result.reasons

    empty = build_authoritative_contract_coverage(
        ContractCoverageInputs((), ()), assembled_at=NOW
    )
    assert empty.status == "insufficient_data"
    assert empty.coverage is None


def test_newest_current_hash_conflict_blocks_older_ok_extraction() -> None:
    older_ok = _extraction()
    newer_conflict = ExtractionEvidenceRecord(
        evidence_id=20,
        status="conflict",
        observed_at=NOW,
        payload={
            "document_id": "7",
            "content_sha256": DIGEST,
            "result": {
                "status": "ok",
                "candidates": [],
                "conflicts": [{"field": "annual_rent_kzt"}],
            },
        },
    )
    blocked = build_authoritative_contract_coverage(
        ContractCoverageInputs((_document(),), (older_ok, newer_conflict)),
        assembled_at=NOW,
    )
    assert blocked.status == "incomplete"
    assert blocked.coverage is not None
    assert blocked.coverage.processed_document_ids == ()
    assert "latest_document_extraction_not_usable:7" in blocked.reasons

    ambiguous = ExtractionEvidenceRecord(
        evidence_id=21,
        status="conflict",
        observed_at=NOW + timedelta(minutes=1),
        payload={"result": {"status": "conflict"}},
    )
    ambiguous_result = build_authoritative_contract_coverage(
        ContractCoverageInputs((_document(),), (older_ok, ambiguous)),
        assembled_at=NOW,
    )
    assert ambiguous_result.status == "incomplete"
    assert "ambiguous_document_extraction_conflict" in ambiguous_result.reasons


def test_contract_inventory_loader_is_bounded_and_matches_exact_evidence(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'producer.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-1",
                source="e-qazyna",
                source_lot_id="452662",
                title="Кемпинг",
                source_url="https://example.test/452662",
                last_seen_at=NOW,
            )
        )
        session.add(
            AuctionDocument(
                id=7,
                lot_id="lot-1",
                title="Договор",
                source_url="https://example.test/contract.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256=DIGEST,
                downloaded_at=NOW,
            )
        )
        session.add(
            AuctionEvidence(
                lot_id="lot-1",
                evidence_type="document_extraction",
                status="found",
                observed_at=NOW,
                raw_payload_json=json.dumps(_extraction().payload),
            )
        )
    with Session(engine) as session:
        inputs = load_contract_coverage_inputs(session, "lot-1")
    result = build_authoritative_contract_coverage(inputs, assembled_at=NOW)
    assert result.status == "complete"

    with Session(engine) as session, session.begin():
        for index in range(49):
            session.add(
                AuctionEvidence(
                    lot_id="lot-1",
                    evidence_type="document_extraction",
                    status="found",
                    observed_at=NOW + timedelta(minutes=index + 1),
                    raw_payload_json=json.dumps(_extraction().payload),
                )
            )
    with Session(engine) as session:
        truncated_inputs = load_contract_coverage_inputs(session, "lot-1")
    truncated = build_authoritative_contract_coverage(truncated_inputs, assembled_at=NOW)
    assert truncated.status == "complete"
    assert "extraction_history_truncated" in truncated.reasons

    with Session(engine) as session, session.begin():
        session.add(
            AuctionDocument(
                id=8,
                lot_id="lot-1",
                title="Второй договор",
                source_url="https://example.test/contract-2.pdf",
                file_type="pdf",
                storage_status="downloaded",
                content_sha256="b" * 64,
                downloaded_at=NOW,
            )
        )
    with Session(engine) as session:
        missing_current_inputs = load_contract_coverage_inputs(session, "lot-1")
    missing_current = build_authoritative_contract_coverage(
        missing_current_inputs, assembled_at=NOW
    )
    assert missing_current.status == "incomplete"
    assert missing_current.coverage is not None
    assert missing_current.coverage.processed_document_ids == ("7",)
    assert "document_unprocessed:8" in missing_current.reasons

    oversized = ContractCoverageInputs(
        tuple(_document(str(index + 1)) for index in range(MAX_DOCUMENTS + 1)), ()
    )
    with pytest.raises(DecisionInputProducerError, match="count exceeds"):
        build_authoritative_contract_coverage(oversized, assembled_at=NOW)

    huge_extraction = ExtractionEvidenceRecord(
        1,
        "found",
        NOW,
        {"document_id": "1", "padding": "x" * 70_000},
    )
    with pytest.raises(DecisionInputProducerError, match="byte budget"):
        build_authoritative_contract_coverage(
            ContractCoverageInputs((_document("1"),), (huge_extraction,)),
            assembled_at=NOW,
        )


def _fact(
    key: str,
    value: int,
    *,
    ref: str | None = None,
    status: str = "found",
) -> DocumentedMonetaryFact:
    return DocumentedMonetaryFact(
        key,
        value,
        value,
        value,
        status,  # type: ignore[arg-type]
        ref or f"auction_evidence:cost:{key}",
        f"https://example.test/costs/{key}",
        NOW,
        "cost-dataset-1",
    )


def test_cost_ranges_require_all_explicit_documented_costs_without_defaults() -> None:
    facts = [_fact(key, (index + 1) * 1_000) for index, key in enumerate(REQUIRED_COST_KEYS)]
    result = produce_decision_cost_ranges(facts)
    assert result.status == "complete"
    assert result.missing_keys == ()
    assert set(result.payload) == set(REQUIRED_COST_KEYS)
    assert result.payload["connection"]["base_kzt"] == 1_000  # type: ignore[index]
    assert result.observed_at == NOW
    evidence = result.evidence_payload()
    assert set(evidence) == {*REQUIRED_COST_KEYS, "provenance_refs"}
    assert result.persistence_metadata()["producer_status"] == "complete"
    assert result.persistence_metadata()["generation_id"] == result.generation_id


def test_cost_conflict_is_not_collapsed_and_guarantee_is_never_a_cost() -> None:
    result = produce_decision_cost_ranges(
        [
            _fact("connection", 100_000, ref="auction_evidence:1"),
            _fact(
                "connection",
                100_000,
                ref="auction_evidence:2",
                status="conflict",
            ),
            _fact("guarantee", 216_250),
            _fact("additional_payment", 16_200),
            _fact("annual_rent", 17_970),
        ]
    )
    assert result.status == "insufficient_data"
    assert result.payload == {}
    assert result.conflict_keys == ("connection",)
    assert result.excluded_keys == ("additional_payment", "annual_rent", "guarantee")
    assert set(result.missing_keys) == set(REQUIRED_COST_KEYS)


def test_unpaired_legacy_cost_payload_is_rejected_by_decision_store(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'cost-store.db'}")
    Base.metadata.create_all(engine)
    result = produce_decision_cost_ranges([_fact("connection", 100_000)])
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-cost",
                source="e-qazyna",
                source_lot_id="cost-1",
                title="Коммерческий участок",
                source_url="https://example.test/cost-1",
                last_seen_at=NOW,
            )
        )
        session.add(
            AuctionEvidence(
                lot_id="lot-cost",
                evidence_type="decision_cost_ranges",
                status="found",
                observed_at=NOW,
                raw_payload_json=json.dumps(result.evidence_payload()),
            )
        )
    with Session(engine) as session:
        bundle = _read_bundle(session, "lot-cost")
    assert bundle.actual_cost_ranges is None


def _evaluation(index: int, *, source_url: str | None = None) -> ComparableEvaluation:
    return ComparableEvaluation(
        source_id="market-provider",
        source_record_id=str(index),
        source_url=source_url or f"https://market.test/sale/{index}",
        object_id=f"object-{index}",
        price_kind="verified_sale",
        observed_at=NOW - timedelta(days=index),
        age_days=index,
        distance_km=1.0,
        eligible=True,
        exclusion_reason=None,
        duplicate_of=None,
        quality_grade="A",
        price_kzt=10_000_000 + index,
        price_per_ha_kzt=10_000_000 + index,
        adjusted_price_per_ha_kzt=10_000_000 + index,
        adjusted_target_value_kzt=10_000_000 + index,
    )


def _market(count: int = 3) -> MarketComparableResult:
    evaluations = tuple(_evaluation(index) for index in range(1, count + 1))
    estimate = (
        MarketEstimate(10_000_002, 9_000_000, 11_000_000, 10_000_002, 9_000_000, 11_000_000, count)
        if count >= 3
        else None
    )
    return MarketComparableResult(
        "ok" if estimate else "insufficient_data",
        estimate,
        "medium" if estimate else "none",
        count,
        count,
        0,
        evaluations,
        "strict verified sales",
    )


def test_market_adapter_accepts_only_three_real_grade_a_verified_sources() -> None:
    result = adapt_strict_market_estimate(
        _market(3),
        input_generation_id="w9-market-42",
        history_audit=ActiveHistoryAudit(
            42,
            "active",
            NOW,
            ("auction_history_generation:42",),
        ),
    )
    assert result.status == "ok"
    assert result.payload["estimate"] is not None
    assert result.observed_at == NOW - timedelta(days=3)
    assert result.payload["history_audit"]["audit_only"] is True  # type: ignore[index]
    assert len([ref for ref in result.provenance_refs if ref.startswith("market:")]) == 3

    insufficient = adapt_strict_market_estimate(
        _market(2), input_generation_id="w9-market-43"
    )
    assert insufficient.status == "insufficient_data"
    assert insufficient.payload["estimate"] is None


def test_history_alone_never_becomes_market_value_and_452662_stays_null() -> None:
    result = adapt_strict_market_estimate(
        None,
        input_generation_id="452662-no-market",
        history_audit=ActiveHistoryAudit(
            9,
            "active",
            NOW,
            ("auction_history_generation:9",),
        ),
    )
    assert result.status == "insufficient_data"
    assert result.payload["estimate"] is None
    assert result.payload["history_audit"]["status"] == "active"  # type: ignore[index]

    coverage = build_authoritative_contract_coverage(
        ContractCoverageInputs((), ()), assembled_at=NOW
    )
    costs = produce_decision_cost_ranges(
        [
            _fact("guarantee", 216_250),
            _fact("additional_payment", 16_200),
            _fact("annual_rent", 17_970),
        ]
    )
    assert coverage.status == "insufficient_data"
    assert costs.status == "insufficient_data"
    assert result.evidence_payload()["estimate"] is None
