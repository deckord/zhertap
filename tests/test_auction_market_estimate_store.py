from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auction_market_comparables import ComparableTarget
from app.auction_market_estimate_store import (
    EVIDENCE_TYPE,
    AuthoritativeTargetFacts,
    build_authoritative_market_target,
    build_candidate_set,
    calculate_market_evidence,
    load_authoritative_target_facts,
    load_persisted_comparable_facts,
    recompute_market_evidence,
)
from app.db import Base
from app.models import AuctionEvidence, AuctionLandObject, AuctionLot, AuctionMarketComparable

NOW = datetime(2026, 8, 17, 6, tzinfo=UTC)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'market.sqlite3'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _lot() -> AuctionLot:
    return AuctionLot(
        id="lot-452662",
        source="e-qazyna",
        source_lot_id="452662",
        source_url="https://example.test/452662",
        title="Кемпинг",
        purpose="строительство кемпинга",
        object_type="land",
        area_ha=1,
        land_rights="временное землепользование",
        lease_term_years=3,
        region="Абай",
        district="Жаңасемей",
        locality="Семей",
        updated_at=NOW,
    )


def _seed_authoritative_target(session) -> None:
    session.add(_lot())
    session.add_all(
        (
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="decision_input:site_context",
                status="found",
                title="worker-owned W6",
                value_text="a" * 64,
                raw_payload_json=json.dumps(
                    {
                        "physical_access": {"readiness": "ready"},
                        "legal_access": {"readiness": "ready"},
                        "infrastructure": {"readiness": "ready"},
                    }
                ),
                observed_at=NOW,
            ),
            AuctionEvidence(
                lot_id="lot-452662",
                evidence_type="cadastre_boundary",
                status="found",
                title="ЕГКН",
                raw_payload_json=json.dumps(
                    {
                        "geometry_geojson": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [80.227, 50.4106],
                                    [80.228, 50.4106],
                                    [80.228, 50.4116],
                                    [80.227, 50.4116],
                                    [80.227, 50.4106],
                                ]
                            ],
                        }
                    }
                ),
                observed_at=NOW,
            ),
        )
    )


def _target() -> ComparableTarget:
    return ComparableTarget(
        target_id="lot-452662",
        right_type="lease",
        purpose_group="camping",
        area_ha=1,
        valuation_at=NOW,
        locality="Семей",
        latitude=50.4111,
        longitude=80.2275,
        lease_term_years=3,
        access_readiness="ready",
        infrastructure_readiness="ready",
    )


def _row(
    index: int,
    *,
    record_id: str | None = None,
    object_id: str | None = None,
    observed_at: datetime | None = None,
    price_kind: str = "verified_sale",
    listing_status: str = "sold",
    payload_status: str = "found",
) -> AuctionMarketComparable:
    raw = {
        "source_record_id": record_id or f"sale-{index}",
        "object_id": object_id or f"parcel-{index}",
        "status": payload_status,
        "verification_status": "verified",
        "verification_source_ref": f"registry-document:{index}",
        "price_kind": price_kind,
        "right_type": "lease",
        "purpose_group": "camping",
        "area_ha": 1,
        "price_kzt": 10_000_000 + index * 100_000,
        "locality": "Семей",
        "latitude": 50.4111 + (index % 3) * 0.001,
        "longitude": 80.2275,
        "lease_term_years": 3,
        "access_readiness": "ready",
        "infrastructure_readiness": "ready",
    }
    return AuctionMarketComparable(
        lot_id="lot-452662",
        source_name="official-sales",
        source_url=f"https://market.example/{record_id or index}/{payload_status}",
        title=f"Продажа {index}",
        region="Абай",
        district="Жаңасемей",
        locality="Семей",
        area_ha=1,
        price_kzt=10_000_000 + index * 100_000,
        listing_status=listing_status,
        raw_payload_json=json.dumps(raw),
        observed_at=observed_at or NOW - timedelta(days=30 + index),
    )


def _evidence_payload(factory) -> dict[str, object]:
    with factory() as session:
        evidence = session.scalar(
            select(AuctionEvidence)
            .where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
            .order_by(AuctionEvidence.id.desc())
        )
        assert evidence is not None
        return json.loads(evidence.raw_payload_json or "{}")


def test_452662_with_two_verified_sales_persists_explicit_insufficient(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all((_row(1), _row(2)))
    result = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)
    assert result.changed is True
    assert result.status == "insufficient_data"
    assert payload["status"] == "insufficient_data"
    assert payload["estimate"] is None
    assert payload["high_quality_verified_count"] == 2
    assert payload["history_reference"]["audit_only"] is True


def test_three_grade_a_verified_sales_produce_provenanced_estimate_and_oldest_freshness(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all((_row(1), _row(2), _row(3)))
    result = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)
    assert result.status == "ok"
    assert payload["estimate"]["verified_comparables_used"] == 3
    assert payload["confidence"] == "medium"
    assert payload["oldest_used_at"] == (NOW - timedelta(days=33)).isoformat()
    assert len(payload["provenance_refs"]) >= 6
    assert payload["history_reference"]["audit_only"] is True


def test_listings_stale_and_unverified_sales_never_green(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all(
            (
                *(_row(index, price_kind="listing") for index in range(1, 4)),
                *(_row(index, observed_at=NOW - timedelta(days=500)) for index in range(4, 7)),
            )
        )
        unverified = _row(8)
        raw = json.loads(unverified.raw_payload_json or "{}")
        raw["verification_status"] = "claimed"
        unverified.raw_payload_json = json.dumps(raw)
        session.add(unverified)
    recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)
    assert payload["status"] == "insufficient_data"
    assert payload["estimate"] is None
    assert any(item["reason"] == "sale_not_verified" for item in payload["rejected_source_rows"])


def test_newest_conflict_blocks_older_verified_record(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all(
            (
                _row(
                    1,
                    record_id="same",
                    object_id="same-object",
                    observed_at=NOW - timedelta(days=10),
                ),
                _row(
                    1,
                    record_id="same",
                    object_id="same-object",
                    observed_at=NOW - timedelta(days=1),
                    listing_status="conflict",
                    payload_status="conflict",
                ),
                _row(2),
                _row(3),
            )
        )
    with factory() as session:
        facts = load_persisted_comparable_facts(session, "lot-452662")
    candidate_set = build_candidate_set(facts)
    material = calculate_market_evidence(_target(), candidate_set)
    assert len(candidate_set.candidates) == 2
    assert material.status == "insufficient_data"
    assert material.payload["estimate"] is None
    assert any(
        item["reason"] == "newest_record_conflict"
        for item in material.payload["rejected_source_rows"]
    )


def test_writer_is_idempotent_but_a_b_a_reactivation_creates_new_current_observation(
    tmp_path,
) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all((_row(1), _row(2), _row(3)))
    first = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    same = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    assert first.changed is True
    assert same.changed is False
    with factory() as session, session.begin():
        session.add(
            _row(
                1,
                record_id="sale-1",
                object_id="parcel-1",
                observed_at=NOW - timedelta(hours=1),
                listing_status="conflict",
                payload_status="conflict",
            )
        )
    middle = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    assert middle.changed is True
    assert middle.status == "insufficient_data"
    with factory() as session, session.begin():
        session.add(
            _row(
                1,
                record_id="sale-1",
                object_id="parcel-1",
                observed_at=NOW,
            )
        )
    restored = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    assert restored.changed is True
    assert restored.status == "ok"
    with factory() as session:
        rows = list(
            session.scalars(
                select(AuctionEvidence).where(AuctionEvidence.evidence_type == EVIDENCE_TYPE)
            )
        )
    assert len(rows) == 3


def test_history_reference_cannot_turn_insufficient_market_into_estimate(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all((_row(1), _row(2)))
    with factory() as session:
        facts = load_persisted_comparable_facts(session, "lot-452662")
    candidate_set = build_candidate_set(facts)
    material = calculate_market_evidence(_target(), candidate_set, history=None)
    assert material.payload["history_reference"]["audit_only"] is True
    assert material.status == "insufficient_data"
    assert material.payload["estimate"] is None


def test_more_than_100_historical_rows_uses_newest_window_and_quiesces(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all(tuple(_row(index) for index in range(1, 102)))
    first = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    second = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)
    assert first.status == "ok"
    assert first.changed is True
    assert second.changed is False
    assert payload["source_history_truncated"] is True
    assert payload["high_quality_verified_count"] >= 3
    assert payload["estimate"] is not None


def test_missing_w6_readiness_forces_authoritative_target_insufficient(tmp_path) -> None:
    factory = _factory(tmp_path)
    with factory() as session, session.begin():
        session.add(_lot())
        session.add_all((_row(1), _row(2), _row(3)))
    result = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)
    assert result.status == "insufficient_data"
    assert payload["estimate"] is None
    assert payload["target_status"] == "insufficient"
    assert "access_readiness_unknown" in payload["target_missing_reasons"]
    assert "infrastructure_readiness_unknown" in payload["target_missing_reasons"]
    assert payload["target_generation_signature"]


def test_authoritative_target_uses_canonical_boundary_when_cadastre_evidence_is_missing() -> None:
    facts = AuthoritativeTargetFacts(
        lot_id="lot-canonical-boundary",
        updated_at=NOW,
        land_rights="частная собственность",
        lease_term_years=None,
        purpose="ИЖС",
        area_ha=0.1,
        locality="Астана",
        site_evidence_id=None,
        site_payload_json=None,
        cadastre_evidence_id=None,
        cadastre_status=None,
        cadastre_payload_json=None,
        legal_evidence_id=None,
        legal_status=None,
        legal_payload_json=None,
        canonical_object_id="canonical-42",
        canonical_boundary_geojson=json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [
                        [71.40, 51.10],
                        [71.42, 51.10],
                        [71.42, 51.12],
                        [71.40, 51.12],
                        [71.40, 51.10],
                    ]
                ],
            }
        ),
        canonical_boundary_source="jerler:source_object",
    )

    target = build_authoritative_market_target(facts, valuation_at=NOW)

    assert math.isclose(target.target.latitude or 0, 51.11)
    assert math.isclose(target.target.longitude or 0, 71.41)
    assert "auction_land_object:canonical-42" in target.provenance_refs
    assert "location_unknown" not in target.missing_reasons


def test_single_target_loader_reads_linked_canonical_boundary(tmp_path) -> None:
    factory = _factory(tmp_path)
    boundary = json.dumps(
        {
            "type": "Polygon",
            "coordinates": [
                [[71.40, 51.10], [71.42, 51.10], [71.42, 51.12], [71.40, 51.10]]
            ],
        }
    )
    with factory() as session, session.begin():
        land_object = AuctionLandObject(
            id="canonical-loader-42",
            canonical_key="jerler:42",
            jerler_object_id="42",
            boundary_geojson=boundary,
            boundary_source="jerler:source_object",
        )
        lot = _lot()
        lot.land_object_ref_id = land_object.id
        session.add_all((land_object, lot))
    with factory() as session:
        facts = load_authoritative_target_facts(session, "lot-452662")

    assert facts.canonical_object_id == "canonical-loader-42"
    assert facts.canonical_boundary_geojson == boundary
    assert facts.canonical_boundary_source == "jerler:source_object"


def test_oversized_and_malformed_newest_rows_are_quarantined_and_quiesce(tmp_path) -> None:
    factory = _factory(tmp_path)
    oversized = _row(90, observed_at=NOW - timedelta(minutes=1))
    oversized.raw_payload_json = json.dumps({"blob": "x" * 70_000})
    malformed = _row(91, observed_at=NOW - timedelta(minutes=2))
    malformed.raw_payload_json = "{not-json"
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all(
            (
                oversized,
                malformed,
                _row(1, observed_at=NOW - timedelta(days=31)),
                _row(2, observed_at=NOW - timedelta(days=32)),
                _row(3, observed_at=NOW - timedelta(days=33)),
            )
        )

    first = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    second = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)

    assert first.status == "ok"
    assert first.changed is True
    assert second.changed is False
    reasons = {item["reason"] for item in payload["rejected_source_rows"]}
    assert "raw_payload_too_large" in reasons
    assert "malformed_json" in reasons
    assert payload["inventory_scope"] == {
        "kind": "lot_scoped_candidate_inventory",
        "provider_ingest_performed": False,
        "global_geo_selection_performed": False,
    }


def test_aggregate_overflow_quarantines_tail_but_keeps_valid_newest_rows(tmp_path) -> None:
    factory = _factory(tmp_path)
    bulky_rows = []
    for index in range(20, 30):
        row = _row(index, observed_at=NOW - timedelta(days=100 + index))
        row.raw_payload_json = json.dumps({"blob": "x" * 59_000})
        bulky_rows.append(row)
    with factory() as session, session.begin():
        _seed_authoritative_target(session)
        session.add_all(
            (
                _row(1, observed_at=NOW - timedelta(days=31)),
                _row(2, observed_at=NOW - timedelta(days=32)),
                _row(3, observed_at=NOW - timedelta(days=33)),
                *bulky_rows,
            )
        )

    first = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    second = recompute_market_evidence(factory, "lot-452662", observed_at=NOW)
    payload = _evidence_payload(factory)

    assert first.status == "ok"
    assert first.changed is True
    assert second.changed is False
    assert any(
        item["reason"] == "aggregate_budget_exceeded"
        for item in payload["rejected_source_rows"]
    )
    assert payload["estimate"] is not None
