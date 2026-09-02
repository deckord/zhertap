from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.auction_actual_cost_writer import (
    POLICY_VERSION,
    QUARANTINE_EVIDENCE_TYPE,
    SOURCE_EVIDENCE_TYPE,
    STANDARD_INVESTMENT_POLICY_VERSION,
    ActualCostFact,
    canonical_source_identity,
    persist_actual_cost_evidence,
    produce_authoritative_actual_costs,
)
from app.auction_price_ceiling import REQUIRED_COST_KEYS
from app.db import Base
from app.models import AuctionEvidence, AuctionLot

NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)


def _fact(
    key: str,
    value: int,
    *,
    status: str = "found",
    observed_at: datetime = NOW,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    ref: str | None = None,
    source_kind: str | None = None,
    source_identity: str | None = None,
    target_lot_id: str = "lot-452662",
    scenario_key: str = "camping",
) -> ActualCostFact:
    resolved_source_kind = source_kind or (
        "connection_estimate" if key == "connection" else "contractor_quote"
    )
    return ActualCostFact(
        target_lot_id=target_lot_id,
        scenario_key=scenario_key,
        investment_policy_version=STANDARD_INVESTMENT_POLICY_VERSION,
        holding_horizon_months=60,
        cost_key=key,
        low_kzt=value,
        base_kzt=value,
        high_kzt=value,
        status=status,  # type: ignore[arg-type]
        source_kind=resolved_source_kind,
        source_identity=source_identity
        or canonical_source_identity(resolved_source_kind, "Жертап", f"{key}:primary"),
        source_ref=ref or f"quote:{key}:2026-08",
        source_url=f"https://costs.example.test/{key}",
        observed_at=observed_at,
        issued_at=issued_at or observed_at - timedelta(days=1),
        expires_at=expires_at or observed_at + timedelta(days=30),
        confidence=0.9,
        source_version="provider-feed/2026.1",
        currency="KZT",
        basis={
            "connection": "one_time",
            "development": "one_time",
            "registration": "one_time",
            "tax_annual": "annual",
            "due_diligence": "one_time",
            "financing": "financing_horizon",
            "contingency": "one_time_reserve",
            "risk_reserve": "one_time_reserve",
        }.get(key, "cash_only"),
        horizon_months=60 if key == "financing" else None,
        generation_id="cost-generation-1",
    )


def _complete_facts() -> list[ActualCostFact]:
    source_kinds = {
        "connection": "connection_estimate",
        "development": "contractor_quote",
        "registration": "official_fee",
        "tax_annual": "official_tax",
        "due_diligence": "contractor_quote",
        "financing": "financing_quote",
        "contingency": "cost_plan",
        "risk_reserve": "risk_assessment",
    }
    return [
        _fact(
            key,
            (index + 1) * 100_000,
            source_kind=source_kinds[key],
        )
        for index, key in enumerate(REQUIRED_COST_KEYS)
    ]


def test_complete_actual_costs_require_all_eight_documented_sources() -> None:
    production = produce_authoritative_actual_costs(
        _complete_facts(), target_lot_id="lot-452662", scenario_key="camping", as_of=NOW
    )

    assert production.result.status == "complete"
    assert production.result.missing_keys == ()
    assert set(production.result.payload) == set(REQUIRED_COST_KEYS)
    assert len(production.source_manifest) == 8
    connection = next(
        item for item in production.source_manifest if item["cost_key"] == "connection"
    )
    assert connection["source_url"] == "https://costs.example.test/connection"
    assert connection["confidence"] == 0.9
    assert connection["source_version"] == "provider-feed/2026.1"
    assert connection["freshness_status"] == "fresh"


def test_cyrillic_source_identity_is_canonical_collision_safe_sha256() -> None:
    first = canonical_source_identity("official_tax", "Акимат Абай", "Счёт № 17")
    normalized = canonical_source_identity(
        "official_tax",
        "  АКИМАТ АБАЙ  ",
        "СЧЁТ № 17",
    )
    different = canonical_source_identity("official_tax", "Акимат Абай", "Счёт № 18")
    assert first == normalized
    assert len(first) == 64
    assert first != different


def test_newest_conflict_blocks_older_found_and_newer_resolution_can_restore() -> None:
    older = _fact("connection", 100_000, observed_at=NOW - timedelta(days=2))
    conflict = _fact("connection", 120_000, status="conflict", observed_at=NOW)
    blocked = produce_authoritative_actual_costs(
        [older, conflict], target_lot_id="lot-452662", scenario_key="camping", as_of=NOW
    )
    assert blocked.result.payload == {}
    assert blocked.result.conflict_keys == ("connection",)

    resolved = _fact("connection", 130_000, observed_at=NOW + timedelta(minutes=1))
    restored = produce_authoritative_actual_costs(
        [older, conflict, resolved],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW + timedelta(minutes=1),
    )
    assert restored.result.payload["connection"]["base_kzt"] == 130_000  # type: ignore[index]
    assert restored.result.conflict_keys == ()


def test_sources_govern_independently_then_disagreement_becomes_conflict() -> None:
    first = _fact(
        "connection",
        100_000,
        source_identity=canonical_source_identity("connection_estimate", "Альфа", "1"),
    )
    second = _fact(
        "connection",
        120_000,
        source_identity=canonical_source_identity("connection_estimate", "Бета", "1"),
    )
    disagreement = produce_authoritative_actual_costs(
        [first, second], target_lot_id="lot-452662", scenario_key="camping", as_of=NOW
    )
    assert disagreement.result.conflict_keys == ("connection",)

    second_conflicted = replace(second, status="conflict")
    isolated = produce_authoritative_actual_costs(
        [first, second_conflicted],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert isolated.result.payload["connection"]["base_kzt"] == 100_000  # type: ignore[index]
    assert isolated.result.conflict_keys == ()


def test_lot_currency_basis_and_financing_horizon_are_trusted_boundaries() -> None:
    wrong_lot = replace(_fact("connection", 1), target_lot_id="lot-other")
    usd = replace(_fact("development", 2), currency="USD")
    annual_connection = replace(_fact("connection", 3), basis="annual")
    financing_without_horizon = replace(
        _fact("financing", 4, source_kind="financing_quote"),
        horizon_months=None,
    )
    production = produce_authoritative_actual_costs(
        [wrong_lot, usd, annual_connection, financing_without_horizon],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.payload == {}
    assert [item.reason for item in production.quarantined] == [
        "target_lot_mismatch",
        "currency_must_be_kzt",
        "invalid_cost_basis",
        "financing_horizon_required",
    ]


def test_malformed_newest_conflict_is_quarantined_and_still_blocks_older_found() -> None:
    older = _fact("connection", 100_000, observed_at=NOW - timedelta(days=2))
    malformed_conflict = ActualCostFact(
        target_lot_id="lot-452662",
        scenario_key="camping",
        investment_policy_version=STANDARD_INVESTMENT_POLICY_VERSION,
        holding_horizon_months=60,
        cost_key="connection",
        low_kzt=120_000,
        base_kzt=120_000,
        high_kzt=120_000,
        status="conflict",
        source_kind="connection_estimate",
        source_identity=canonical_source_identity(
            "connection_estimate", "Жертап", "connection:primary"
        ),
        source_ref="bad ref with spaces",
        source_url="https://costs.example.test/conflict",
        observed_at=NOW,
        issued_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        confidence=0.9,
        source_version="provider-feed/2026.1",
        currency="KZT",
        basis="one_time",
    )
    production = produce_authoritative_actual_costs(
        [older, malformed_conflict],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.payload == {}
    assert production.result.conflict_keys == ("connection",)
    assert production.quarantined[0].reason == "invalid_source_ref"


def test_stale_expired_and_unknown_sources_never_fall_back_to_older_values() -> None:
    fresh_old = _fact(
        "registration",
        50_000,
        observed_at=NOW - timedelta(days=10),
        source_kind="official_fee",
    )
    expired_new = _fact(
        "registration",
        60_000,
        observed_at=NOW,
        issued_at=NOW - timedelta(days=3),
        expires_at=NOW - timedelta(days=1),
        source_kind="official_fee",
    )
    production = produce_authoritative_actual_costs(
        [fresh_old, expired_new],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert "registration" in production.result.missing_keys
    assert production.stale_keys == ("registration",)
    assert production.source_manifest[-1]["freshness_status"] == "stale"


def test_452662_guarantee_is_excluded_and_never_completes_actual_costs() -> None:
    production = produce_authoritative_actual_costs(
        [
            _fact("guarantee", 216_250, source_kind="official_fee"),
            _fact("additional_payment", 16_200, source_kind="official_fee"),
            _fact("annual_rent", 17_970, source_kind="official_fee"),
        ],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.status == "insufficient_data"
    assert production.result.payload == {}
    assert set(production.excluded_keys) == {
        "guarantee",
        "additional_payment",
        "annual_rent",
    }
    assert set(production.result.missing_keys) == set(REQUIRED_COST_KEYS)


def test_malformed_items_are_bounded_and_quarantined_without_poisoning_valid_fact() -> None:
    malformed = _fact("development", 100_000)
    malformed = ActualCostFact(
        **{
            field: getattr(malformed, field)
            for field in malformed.__dataclass_fields__
            if field != "source_url"
        },
        source_url="file:///unsafe",
    )
    production = produce_authoritative_actual_costs(
        [malformed, {"cost_key": "connection"}, _fact("connection", 500_000)],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert production.result.payload["connection"]["base_kzt"] == 500_000  # type: ignore[index]
    assert [item.reason for item in production.quarantined] == [
        "missing_source_url",
        "invalid_fact_type",
    ]
    assert all(len(item.fingerprint) == 64 for item in production.quarantined)


def test_quarantine_is_capped_and_reports_truncation() -> None:
    production = produce_authoritative_actual_costs(
        [object()] * 64,
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert len(production.quarantined) == 32
    assert production.quarantine_truncated is True


def test_immutable_persistence_is_idempotent_and_keeps_w11_payload_shape(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'actual-costs.db'}")
    Base.metadata.create_all(engine)
    production = produce_authoritative_actual_costs(
        [_fact("connection", 500_000), object()],
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-452662",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.test/452662",
                title="Кемпинг",
                last_seen_at=NOW,
            )
        )
    with Session(engine) as session, session.begin():
        first = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=production,
            written_at=NOW,
        )
    with Session(engine) as session, session.begin():
        second = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=production,
            written_at=NOW,
        )
    assert first.status == "written"
    assert second.status == "already_current"
    assert second.evidence_id == first.evidence_id
    with Session(engine) as session:
        rows = list(
            session.scalars(
                select(AuctionEvidence).where(AuctionEvidence.lot_id == "lot-452662")
            )
        )
    assert len(rows) == 3
    costs = next(row for row in rows if row.evidence_type == "decision_cost_ranges")
    sources = next(row for row in rows if row.evidence_type == SOURCE_EVIDENCE_TYPE)
    quarantine = next(row for row in rows if row.evidence_type == QUARANTINE_EVIDENCE_TYPE)
    assert set(json.loads(costs.raw_payload_json)) == {"connection", "provenance_refs"}
    source_payload = json.loads(sources.raw_payload_json)
    assert source_payload["policy_version"] == POLICY_VERSION
    assert source_payload["sources"][0]["source_url"].startswith("https://")
    assert json.loads(quarantine.raw_payload_json)["items"][0]["reason"] == "invalid_fact_type"


def test_persistence_latest_governs_a_b_a_reactivation_then_noop(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'actual-cost-reactivation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-452662",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.test/452662",
                title="Кемпинг",
                last_seen_at=NOW,
            )
        )
    fact_a = _fact("connection", 100_000)
    fact_b = replace(fact_a, low_kzt=200_000, base_kzt=200_000, high_kzt=200_000)
    production_a = produce_authoritative_actual_costs(
        [fact_a], target_lot_id="lot-452662", scenario_key="camping", as_of=NOW
    )
    production_b = produce_authoritative_actual_costs(
        [fact_b], target_lot_id="lot-452662", scenario_key="camping", as_of=NOW
    )
    statuses = []
    for production in (production_a, production_b, production_a, production_a):
        with Session(engine) as session, session.begin():
            statuses.append(
                persist_actual_cost_evidence(
                    session,
                    lot_id="lot-452662",
                    production=production,
                    written_at=NOW,
                ).status
            )
    assert statuses == ["written", "written", "written", "already_current"]
    with Session(engine) as session:
        decision_rows = list(
            session.scalars(
                select(AuctionEvidence).where(
                    AuctionEvidence.lot_id == "lot-452662",
                    AuctionEvidence.evidence_type == "decision_cost_ranges",
                )
            )
        )
    assert len(decision_rows) == 3


def test_input_reordering_has_deterministic_marker_and_is_persistence_noop(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'actual-cost-order.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(
            AuctionLot(
                id="lot-452662",
                source="e-qazyna",
                source_lot_id="452662",
                source_url="https://example.test/452662",
                title="Кемпинг",
                last_seen_at=NOW,
            )
        )
    facts = _complete_facts()
    forward = produce_authoritative_actual_costs(
        facts,
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    reversed_input = produce_authoritative_actual_costs(
        list(reversed(facts)),
        target_lot_id="lot-452662",
        scenario_key="camping",
        as_of=NOW,
    )
    assert forward.result.generation_id == reversed_input.result.generation_id
    assert forward.source_manifest == reversed_input.source_manifest
    with Session(engine) as session, session.begin():
        first = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=forward,
            written_at=NOW,
        )
    with Session(engine) as session, session.begin():
        second = persist_actual_cost_evidence(
            session,
            lot_id="lot-452662",
            production=reversed_input,
            written_at=NOW,
        )
    assert first.status == "written"
    assert second.status == "already_current"
